#!/usr/bin/env python3
"""A small, persistent poker ledger. Python 3.9+, no third-party packages."""
import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from config import load_env
from supabase_store import CloudError, SupabaseAPI, SupabaseStore

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('POKER_DB', str(ROOT / 'data' / 'ledger.sqlite3')))
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '0') == '1'
CLOUD_STORE = None
HISTORY_PAGE_SIZE = 50
STARTER_PLAYERS = ('Aloysius', 'Arden', 'Brendon', 'Byron', 'Febie',
                   'Isaiah', 'Min', 'Rayner', 'Vernon', 'ZS')


class UserError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def now():
    return datetime.now(timezone.utc).isoformat()


def amount(value, zero=False):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,6}(?:\.\d{1,2})?', value.strip()):
        raise UserError('Enter an amount from $0.00 to $999,999.99, with up to two decimal places.')
    whole, _, fraction = value.strip().partition('.')
    cents = int(whole) * 100 + int(fraction.ljust(2, '0') or '0')
    if cents == 0 and not zero:
        raise UserError('A buy-in must be more than $0.00.')
    return cents


def clean_name(value, maximum=40):
    if not isinstance(value, str):
        raise UserError('Please enter a name.')
    value = ' '.join(value.split())
    if not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise UserError('Please enter a name with 1 to %d characters.' % maximum)
    return value


def validate_balances(players):
    if any(p['cashout'] is None for p in players):
        raise UserError('Enter a final amount for every player, including $0 for anyone who busted.')
    if not players or sum(p['buyin'] for p in players) == 0:
        raise UserError('Record some buy-ins before settling the session.')
    if sum(p['cashout'] - p['buyin'] for p in players) != 0:
        raise UserError('Final amounts must equal total buy-ins before you can settle.')


def match_group(players):
    debtors = sorted([[p['id'], p['buyin'] - p['cashout']] for p in players if p['buyin'] > p['cashout']], key=lambda x: (-x[1], x[0]))
    creditors = sorted([[p['id'], p['cashout'] - p['buyin']] for p in players if p['cashout'] > p['buyin']], key=lambda x: (-x[1], x[0]))
    result, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        paid = min(debtors[i][1], creditors[j][1])
        result.append({'from_id': debtors[i][0], 'to_id': creditors[j][0], 'amount': paid})
        debtors[i][1] -= paid
        creditors[j][1] -= paid
        i += debtors[i][1] == 0
        j += creditors[j][1] == 0
    return result


def solve_transfers(players):
    """Maximise zero-sum components, giving the fewest transfers.

    A component with k nonzero balances needs at least k-1 transfers. Subset
    dynamic programming finds the maximum number of disjoint zero-sum groups.
    Exact up to 16 nonzero balances. Larger tables use deterministic matching
    and claim a minimum only if the debtor/creditor count lower bound is met.
    """
    validate_balances(players)
    active = [p for p in players if p['cashout'] != p['buyin']]
    if len(active) > 16:
        result = match_group(active)
        lower_bound = max(sum(p['cashout'] > p['buyin'] for p in active), sum(p['cashout'] < p['buyin'] for p in active))
        return result, len(result) == lower_bound
    size = 1 << len(active)
    sums, groups, last = [0] * size, [0] * size, [0] * size
    balances = [p['cashout'] - p['buyin'] for p in active]
    for mask in range(1, size):
        bit = mask & -mask
        sums[mask] = sums[mask ^ bit] + balances[bit.bit_length() - 1]
        remaining, best, chosen = mask, -1, bit
        while remaining:
            bit = remaining & -remaining
            count = groups[mask ^ bit]
            if count > best:
                best, chosen = count, bit
            remaining ^= bit
        groups[mask] = best + (sums[mask] == 0)
        last[mask] = chosen
    mask, partition, group = size - 1, [], []
    while mask:
        bit = last[mask]
        group.append(active[bit.bit_length() - 1])
        mask ^= bit
        if sums[mask] == 0:
            partition.append(group)
            group = []
    result = [t for group in partition for t in match_group(group)]
    debts = {p['id']: p['buyin'] - p['cashout'] for p in active}
    result.sort(key=lambda t: (-debts[t['from_id']], t['from_id'], -t['amount'], t['to_id']))
    return result, True


def transfers_for(players):
    return solve_transfers(players)[0]


@contextmanager
def database():
    con = sqlite3.connect(str(DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def initialise():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
          code TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
          created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          demo INTEGER NOT NULL DEFAULT 0, settled_at TEXT
        );
        CREATE TABLE IF NOT EXISTS players (
          id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(code),
          name TEXT NOT NULL, device TEXT, cashout INTEGER, joined TEXT NOT NULL,
          UNIQUE(session, name COLLATE NOCASE), UNIQUE(session, device)
        );
        CREATE TABLE IF NOT EXISTS buyins (
          id INTEGER PRIMARY KEY, player INTEGER NOT NULL REFERENCES players(id),
          amount INTEGER NOT NULL CHECK(amount > 0), created TEXT NOT NULL,
          voided INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS activity (
          id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(code),
          text TEXT NOT NULL, created TEXT NOT NULL, kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transfers (
          id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(code),
          from_id INTEGER NOT NULL REFERENCES players(id), to_id INTEGER NOT NULL REFERENCES players(id),
          amount INTEGER NOT NULL CHECK(amount > 0), paid INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS requests (
          device TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
          response TEXT NOT NULL, PRIMARY KEY(device, request_id)
        );
        CREATE INDEX IF NOT EXISTS player_session ON players(session);
        CREATE INDEX IF NOT EXISTS buyin_player ON buyins(player);
        ''')
        if 'minimum_proven' not in [r['name'] for r in db.execute('PRAGMA table_info(sessions)')]:
            db.execute('ALTER TABLE sessions ADD COLUMN minimum_proven INTEGER NOT NULL DEFAULT 0')
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def audit(db, code, text, kind='info'):
    db.execute('INSERT INTO activity(session,text,created,kind) VALUES(?,?,?,?)', (code, text, now(), kind))


def money(cents):
    return '$%s.%02d' % (format(cents // 100, ','), cents % 100)


def get_session(db, code):
    session = db.execute('SELECT * FROM sessions WHERE code=?', (code,)).fetchone()
    if not session:
        raise UserError('We could not find that session. Check the invitation link.', 404)
    return dict(session)


def player_rows(db, code):
    return [dict(r) for r in db.execute('''SELECT p.id,p.name,p.cashout,p.joined,
      COALESCE(SUM(CASE WHEN b.voided=0 THEN b.amount ELSE 0 END),0) AS buyin,
      SUM(CASE WHEN b.voided=0 THEN 1 ELSE 0 END) AS entries
      FROM players p LEFT JOIN buyins b ON p.id=b.player WHERE p.session=? GROUP BY p.id ORDER BY p.id''', (code,))]


def listing(db, device, offset=0):
    # One shared ledger for everyone who opens the site. Device identity is
    # still checked separately for edits, settlement and deletion.
    rows = [dict(r) for r in db.execute('''SELECT s.code,s.name,s.created,s.status,s.demo,
      COALESCE((SELECT SUM(b.amount) FROM buyins b JOIN players p ON b.player=p.id
        WHERE p.session=s.code AND b.voided=0),0) AS total_buyin,
      (SELECT COUNT(*) FROM transfers t WHERE t.session=s.code AND t.paid=0) AS unpaid_count
      FROM sessions s
      ORDER BY s.created DESC,s.code DESC LIMIT ? OFFSET ?''',
      (HISTORY_PAGE_SIZE + 1, offset))]
    return {'sessions': rows[:HISTORY_PAGE_SIZE],
            'next_offset': offset + HISTORY_PAGE_SIZE if len(rows) > HISTORY_PAGE_SIZE else None}


def player_directory(names):
    # Starter spelling wins; recent history supplies other remembered names.
    unique = {name.casefold(): name for name in STARTER_PLAYERS}
    for name in names:
        name = clean_name(name)
        unique.setdefault(name.casefold(), name)
    return {'players': sorted(unique.values(), key=str.casefold)}


def saved_players(db, device):
    rows = db.execute('''SELECT p.name FROM players p JOIN sessions s ON s.code=p.session
      WHERE s.demo=0
      ORDER BY s.created DESC,s.code DESC,p.id''')
    return player_directory(row['name'] for row in rows)


def player_statistics(sessions):
    """Combine settled games by normalised display name, keeping money in cents."""
    players = {}
    starter_names = {name.casefold(): name for name in STARTER_PLAYERS}
    session_count, total_buyin = 0, 0
    for session in sessions:
        if session['status'] != 'settled' or session.get('demo'):
            continue
        session_count += 1
        for p in session['players']:
            if not p['buyin'] and not p['cashout']:
                continue
            name = clean_name(p['name'])
            key = name.casefold()
            net = p['cashout'] - p['buyin']
            if key not in players:
                players[key] = {'key': key, 'name': starter_names.get(key, name),
                                'sessions': 0, 'wins': 0, 'losses': 0, 'draws': 0,
                                'buyin': 0, 'cashout': 0, 'net': 0,
                                'best': net, 'worst': net, 'history': []}
            stats = players[key]
            stats['sessions'] += 1
            stats['wins'] += net > 0
            stats['losses'] += net < 0
            stats['draws'] += net == 0
            for field in ('buyin', 'cashout'):
                stats[field] += p[field]
            stats['net'] += net
            stats['best'] = max(stats['best'], net)
            stats['worst'] = min(stats['worst'], net)
            stats['history'].append({field: session[field] for field in ('code', 'name', 'created')} |
                                    {'buyin': p['buyin'], 'cashout': p['cashout'], 'net': net})
            total_buyin += p['buyin']
    ranked = sorted(players.values(), key=lambda p: (-p['net'], p['key']))
    for index, p in enumerate(ranked):
        p['rank'] = ranked[index - 1]['rank'] if index and p['net'] == ranked[index - 1]['net'] else index + 1
        p['history'].sort(key=lambda s: (s['created'], s['code']), reverse=True)
    return {'players': ranked, 'session_count': session_count, 'total_buyin': total_buyin}


def leaderboard(db, device):
    rows = db.execute('''SELECT s.code,s.name AS session_name,s.created,p.name,p.cashout,
      COALESCE(SUM(CASE WHEN b.voided=0 THEN b.amount ELSE 0 END),0) AS buyin
      FROM sessions s JOIN players p ON p.session=s.code LEFT JOIN buyins b ON b.player=p.id
      WHERE s.status='settled' AND s.demo=0
      GROUP BY s.code,p.id ORDER BY s.created DESC,s.code DESC,p.id''')
    sessions = {}
    for row in rows:
        session = sessions.setdefault(row['code'], {'code': row['code'], 'name': row['session_name'],
                                      'created': row['created'], 'status': 'settled', 'players': []})
        session['players'].append({field: row[field] for field in ('name', 'buyin', 'cashout')})
    return player_statistics(sessions.values())


def view(db, code, device):
    session = get_session(db, code)
    host = session.pop('owner') == device
    mine = db.execute('SELECT id FROM players WHERE session=? AND device=?', (code, device)).fetchone()
    session['role'] = 'host' if host else ('player' if mine else 'guest')
    session['my_player_id'] = mine['id'] if mine else None
    session['players'] = player_rows(db, code)
    session['buyins'] = [dict(r) for r in db.execute('''SELECT b.*,p.name FROM buyins b JOIN players p ON b.player=p.id WHERE p.session=? ORDER BY b.id DESC''', (code,))]
    session['activity'] = [dict(r) for r in db.execute('SELECT id,text,created,kind FROM activity WHERE session=? ORDER BY id DESC LIMIT 30', (code,))]
    session['transfers'] = [dict(r) for r in db.execute('SELECT * FROM transfers WHERE session=? ORDER BY id', (code,))]
    session['total_buyin'] = sum(p['buyin'] for p in session['players'])
    session['total_cashout'] = sum(p['cashout'] or 0 for p in session['players'])
    session['missing'] = sum(p['cashout'] is None for p in session['players'])
    return session


def require_host(session, device):
    if session['owner'] != device:
        raise UserError('Only the session host can do that.', 403)


def require_active(session):
    if session['status'] != 'active':
        raise UserError('This session is settled. Its amounts are now locked.')


def get_player(db, session, player_id, device):
    row = db.execute('SELECT * FROM players WHERE id=? AND session=?', (player_id, session['code'])).fetchone()
    if not row:
        raise UserError('Choose a player from this session.')
    if session['owner'] != device and row['device'] != device:
        raise UserError('You can only edit your own entries.', 403)
    return dict(row)


def add_player(db, code, name, device=None):
    if db.execute('SELECT COUNT(*) FROM players WHERE session=?', (code,)).fetchone()[0] >= 30:
        raise UserError('This table already has 30 players.')
    try:
        result = db.execute('INSERT INTO players(session,name,device,joined) VALUES(?,?,?,?)', (code, clean_name(name), device, now()))
    except sqlite3.IntegrityError:
        raise UserError('That name is already at the table. Use a different name, or ask the host to log your entries.')
    audit(db, code, '%s joined the table' % name, 'join')
    return result.lastrowid


def add_buyin(db, code, player, cents):
    db.execute('INSERT INTO buyins(player,amount,created) VALUES(?,?,?)', (player['id'], cents, now()))
    db.execute('UPDATE players SET cashout=NULL WHERE id=?', (player['id'],))
    audit(db, code, '%s bought in for %s' % (player['name'], money(cents)), 'buyin')


def mutate(db, path, body, device):
    if path in ('/api/sessions', '/api/demo'):
        demo = path == '/api/demo'
        code = secrets.token_urlsafe(9)
        name = 'Saturday night poker' if demo else clean_name(body.get('name'), 70)
        db.execute('INSERT INTO sessions(code,name,owner,created,demo) VALUES(?,?,?,?,?)', (code, name, device, now(), int(demo)))
        if demo:
            data = [('Rayner', [10000, 10000], 34500), ('Marcus', [10000, 5000], 6500), ('Sarah', [10000], 22500), ('Daniel', [10000, 10000], 0), ('Chloe', [10000], 17000), ('Ethan', [10000, 5000], 14500)]
            for index, (pname, ins, final) in enumerate(data):
                pid = add_player(db, code, pname, device if index == 0 else None)
                for cents in ins:
                    add_buyin(db, code, {'id': pid, 'name': pname}, cents)
                # Final values are left blank until the settlement screen is opened.
            audit(db, code, 'Sample session ready. Explore it, or start your own.', 'info')
        else:
            if body.get('host_name'):
                pname = clean_name(body['host_name'])
                add_player(db, code, pname, device)
        return {'code': code}
    match = re.fullmatch(r'/api/sessions/([A-Za-z0-9_-]+)/([a-z]+)', path)
    if not match:
        raise UserError('Unknown action.', 404)
    code, action = match.groups()
    if action == 'delete':
        session = db.execute('SELECT * FROM sessions WHERE code=?', (code,)).fetchone()
        if session is not None:
            require_host(session, device)
            db.execute('DELETE FROM transfers WHERE session=?', (code,))
            db.execute('DELETE FROM buyins WHERE player IN (SELECT id FROM players WHERE session=?)', (code,))
            db.execute('DELETE FROM activity WHERE session=?', (code,))
            db.execute('DELETE FROM players WHERE session=?', (code,))
            db.execute('DELETE FROM sessions WHERE code=?', (code,))
            # Old retry receipts must not return a deleted ledger's contents.
            db.execute("DELETE FROM requests WHERE json_extract(response, '$.code')=?", (code,))
        return {'code': code, 'deleted': True}
    session = get_session(db, code)
    if action == 'rename':
        player = get_player(db, session, body.get('player_id'), device)
        name = clean_name(body.get('name'))
        if any(p['id'] != player['id'] and p['name'].casefold() == name.casefold()
               for p in player_rows(db, code)):
            raise UserError('That name is already at the table. Choose another name.')
        if name != player['name']:
            db.execute('UPDATE players SET name=? WHERE id=?', (name, player['id']))
            audit(db, code, '%s renamed to %s' % (player['name'], name), 'rename')
        return view(db, code, device)
    if action == 'paid':
        if session['status'] != 'settled':
            raise UserError('Settle the session first.')
        transfer = db.execute('SELECT * FROM transfers WHERE id=? AND session=?', (body.get('transfer_id'), code)).fetchone()
        if not transfer:
            raise UserError('That transfer could not be found.')
        receiver = db.execute('SELECT device FROM players WHERE id=?', (transfer['to_id'],)).fetchone()
        if session['owner'] != device and receiver['device'] != device:
            raise UserError('The recipient or host can confirm a payment.', 403)
        paid = body.get('paid', True)
        if type(paid) is not bool:
            raise UserError('Choose paid or unpaid.')
        if bool(transfer['paid']) != paid:
            db.execute('UPDATE transfers SET paid=? WHERE id=?', (int(paid), transfer['id']))
            names = {p['id']: p['name'] for p in player_rows(db, code)}
            audit(db, code, '%s to %s: %s marked %s' % (names[transfer['from_id']], names[transfer['to_id']], money(transfer['amount']), 'paid' if paid else 'unpaid'), 'paid')
        return view(db, code, device)
    require_active(session)
    if action == 'join':
        if session['owner'] == device or db.execute('SELECT 1 FROM players WHERE session=? AND device=?', (code, device)).fetchone():
            raise UserError('You have already joined this session.')
        add_player(db, code, body.get('name'), device)
    elif action == 'players':
        require_host(session, device)
        pid = add_player(db, code, body.get('name'))
        if body.get('amount'):
            add_buyin(db, code, {'id': pid, 'name': clean_name(body.get('name'))}, amount(body['amount']))
    elif action == 'buyin':
        player = get_player(db, session, body.get('player_id'), device)
        add_buyin(db, code, player, amount(body.get('amount')))
    elif action == 'undo':
        entry = db.execute('SELECT b.* FROM buyins b JOIN players p ON b.player=p.id WHERE b.id=? AND p.session=? AND b.voided=0', (body.get('entry_id'), code)).fetchone()
        if not entry:
            raise UserError('That entry has already been removed or could not be found.')
        player = get_player(db, session, entry['player'], device)
        db.execute('UPDATE buyins SET voided=1 WHERE id=?', (entry['id'],))
        db.execute('UPDATE players SET cashout=NULL WHERE id=?', (player['id'],))
        audit(db, code, '%s: removed a %s buy-in' % (player['name'], money(entry['amount'])), 'undo')
    elif action == 'cashouts':
        values = body.get('values')
        if not isinstance(values, list) or not values or len(values) > 30:
            raise UserError('Enter at least one final amount.')
        for entry in values:
            if not isinstance(entry, dict):
                raise UserError('Check your final amounts.')
            player = get_player(db, session, entry.get('player_id'), device)
            cents = None if entry.get('amount') == '' else amount(entry.get('amount'), zero=True)
            db.execute('UPDATE players SET cashout=? WHERE id=?', (cents, player['id']))
            if cents != player['cashout']:
                audit(db, code, '%s: final amount %s' % (player['name'], 'cleared' if cents is None else money(cents)), 'cashout')
    elif action == 'settle':
        require_host(session, device)
        if 'values' in body:
            values = body['values']
            if not isinstance(values, list) or len(values) > 30:
                raise UserError('Check the final amounts.')
            supplied = []
            for entry in values:
                if not isinstance(entry, dict):
                    raise UserError('Check the final amounts.')
                player = get_player(db, session, entry.get('player_id'), device)
                supplied.append(player['id'])
                cents = amount(entry.get('amount'), zero=True)
                db.execute('UPDATE players SET cashout=? WHERE id=?', (cents, player['id']))
            expected = [p['id'] for p in player_rows(db, code)]
            if sorted(supplied) != sorted(expected):
                raise UserError('Enter a final amount for every player exactly once.')
        players = player_rows(db, code)
        if len(players) < 2:
            raise UserError('Add at least two players before settling.')
        transfers, proven = solve_transfers(players)
        for transfer in transfers:
            db.execute('INSERT INTO transfers(session,from_id,to_id,amount) VALUES(?,?,?,?)', (code, transfer['from_id'], transfer['to_id'], transfer['amount']))
        db.execute("UPDATE sessions SET status='settled',settled_at=?,minimum_proven=? WHERE code=?", (now(), int(proven), code))
        audit(db, code, 'Session settled. The final leaderboard is locked.', 'settled')
    else:
        raise UserError('Unknown action.', 404)
    return view(db, code, device)


class Handler(BaseHTTPRequestHandler):
    server_version = 'PokerLedger'

    def log_message(self, fmt, *args):
        # Keep capability URLs and invitation codes out of access logs.
        pass

    def identity(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            pass
        value = cookies.get('poker_device')
        token = value.value if value and re.fullmatch(r'[A-Za-z0-9_-]{43}', value.value) else secrets.token_urlsafe(32)
        self.device_token = token
        return hashlib.sha256(token.encode()).hexdigest()

    def respond(self, status, data, mime='application/json; charset=utf-8'):
        content = json.dumps(data).encode() if mime.startswith('application/json') else data
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header('Set-Cookie', 'poker_device=%s; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000%s' % (self.device_token, '; Secure' if COOKIE_SECURE else ''))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        device = self.identity()
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            offset = 0
            if path == '/api/sessions':
                value = parse_qs(parsed.query).get('offset', ['0'])[0]
                if not re.fullmatch(r'[0-9]{1,10}', value):
                    raise UserError('Invalid session history page.')
                offset = int(value)
            if CLOUD_STORE is not None:
                if path == '/api/leaderboard':
                    return self.respond(200, CLOUD_STORE.leaderboard(device))
                if path == '/api/players':
                    return self.respond(200, CLOUD_STORE.saved_players(device))
                if path == '/api/sessions':
                    return self.respond(200, CLOUD_STORE.listing(device, offset))
                match = re.fullmatch(r'/api/sessions/([A-Za-z0-9_-]+)', path)
                if match:
                    return self.respond(200, CLOUD_STORE.get(match.group(1), device))
            if path.startswith('/api/'):
                if CLOUD_STORE is not None:
                    raise UserError('Not found.', 404)
                with database() as db:
                    if path == '/api/leaderboard':
                        return self.respond(200, leaderboard(db, device))
                    if path == '/api/players':
                        return self.respond(200, saved_players(db, device))
                    if path == '/api/sessions':
                        return self.respond(200, listing(db, device, offset))
                    match = re.fullmatch(r'/api/sessions/([A-Za-z0-9_-]+)', path)
                    if match:
                        return self.respond(200, view(db, match.group(1), device))
            files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8'), '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
            files.update({
                '/fonts/source-serif-4-latin.woff2': ('fonts/source-serif-4-latin.woff2', 'font/woff2'),
                '/fonts/open-sans-latin.woff2': ('fonts/open-sans-latin.woff2', 'font/woff2'),
            })
            if path not in files:
                raise UserError('Not found.', 404)
            filename, mime = files[path]
            self.respond(200, (ROOT / 'static' / filename).read_bytes(), mime)
        except UserError as err:
            self.respond(err.status, {'error': err.message})
        except CloudError as err:
            self.respond(err.status, {'error': err.message})
        except Exception as err:
            print('Read failed: %s' % type(err).__name__, flush=True)
            self.respond(500, {'error': 'We could not load the session. Please try again.'})

    def do_POST(self):
        device = self.identity()
        try:
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                raise UserError('This request came from a different website.', 403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise UserError('Expected a JSON request.', 415)
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 20000:
                raise UserError('This request is too large or empty.', 413)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise UserError('Invalid request.')
            path = urlparse(self.path).path
            request_id = self.headers.get('X-Request-Id', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
                raise UserError('This request needs a unique identifier.')
            fingerprint = hashlib.sha256((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
            if CLOUD_STORE is not None:
                result = CLOUD_STORE.mutate(path, body, device, request_id, fingerprint)
                return self.respond(200, result)
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                prior = db.execute('SELECT response,fingerprint FROM requests WHERE device=? AND request_id=?', (device, request_id)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint:
                        raise UserError('This request identifier was already used for another action.', 409)
                    result = json.loads(prior['response'])
                else:
                    result = mutate(db, path, body, device)
                    db.execute('INSERT INTO requests VALUES(?,?,?,?)', (device, request_id, fingerprint, json.dumps(result)))
            self.respond(200, result)
        except UserError as err:
            self.respond(err.status, {'error': err.message})
        except CloudError as err:
            self.respond(err.status, {'error': err.message})
        except (ValueError, TypeError, KeyError):
            self.respond(400, {'error': 'Please check the details and try again.'})
        except Exception as err:
            print('Request failed: %s' % type(err).__name__, flush=True)
            self.respond(500, {'error': 'We could not save that change. Please try again.'})


def configure_storage(local=False):
    global CLOUD_STORE, COOKIE_SECURE, DB_PATH
    load_env(ROOT / '.env')
    COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '0') == '1'
    DB_PATH = Path(os.environ.get('POKER_DB', str(ROOT / 'data' / 'ledger.sqlite3')))
    url, key = os.environ.get('SUPABASE_URL', ''), os.environ.get('SUPABASE_SECRET_KEY', '')
    CLOUD_STORE = None
    if not local and (url or key):
        if not url or not key:
            raise CloudError('Fill in both SUPABASE_URL and SUPABASE_SECRET_KEY in .env.', 500)
        api = SupabaseAPI(url, key, os.environ.get('SUPABASE_SCHEMA', 'public'))
        api.check()
        CLOUD_STORE = SupabaseStore(api, sys.modules[__name__])
    return 'Supabase' if CLOUD_STORE is not None else 'local SQLite'


def main():
    parser = argparse.ArgumentParser(description='Run the poker session website.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8765, type=int)
    parser.add_argument('--local', action='store_true', help='Explicitly use local SQLite instead of configured Supabase.')
    parser.add_argument('--check-storage', action='store_true', help='Check storage configuration without starting a server.')
    parser.add_argument('--import-local', action='store_true', help='Copy missing local sessions to Supabase, then exit.')
    args = parser.parse_args()
    try:
        storage = configure_storage(args.local)
    except (CloudError, ValueError) as err:
        parser.exit(1, str(err) + '\n')
    if args.check_storage:
        print('Storage ready: %s.' % storage)
        return
    initialise()
    if args.import_local:
        if CLOUD_STORE is None:
            parser.exit(1, 'Configure Supabase before importing local sessions.\n')
        backup = DB_PATH.with_name('ledger-before-supabase.sqlite3')
        try:
            with database() as db:
                if not backup.exists():
                    destination = sqlite3.connect(str(backup))
                    try:
                        db.backup(destination)
                    finally:
                        destination.close()
                    os.chmod(backup, 0o600)
                imported, skipped = CLOUD_STORE.import_local(db)
        except CloudError as err:
            parser.exit(1, str(err) + '\n')
        print('Imported %d sessions; kept %d existing cloud sessions unchanged.' % (imported, skipped))
        return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print('Poker Ledger is running at http://%s:%s (storage: %s)' % (args.host, args.port, storage), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == '__main__':
    main()
