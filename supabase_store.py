"""Supabase persistence using one poker_sessions row per session.

The server is authoritative. Every write compares the previous revision, and
durable request receipts make retries safe across restarts and worker processes.
"""
import base64
import copy
import hashlib
import json
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CloudError(Exception):
    def __init__(self, message, status=503):
        self.message, self.status = message, status
        super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SupabaseAPI:
    def __init__(self, url, key, schema='public'):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise CloudError('SUPABASE_URL must be the HTTPS project URL, without a path.', 500)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schema):
            raise CloudError('Check SUPABASE_SCHEMA in .env.', 500)
        if not key or '\n' in key or '\r' in key:
            raise CloudError('Set SUPABASE_SECRET_KEY in .env.', 500)
        if not key.startswith('sb_secret_'):
            try:
                payload = key.split('.')[1]
                role = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['role']
            except (ValueError, IndexError, KeyError):
                raise CloudError('Use a Supabase secret key or a legacy service_role key.', 500) from None
            if role != 'service_role':
                raise CloudError('Use the server secret/service_role key, not the public or anon key.', 500)
        self.url = url.rstrip('/') + '/rest/v1/poker_sessions'
        self.headers = {'apikey': key, 'Accept-Profile': schema, 'Content-Profile': schema, 'Content-Type': 'application/json', 'User-Agent': 'PokerLedgerServer/1.0'}
        if not key.startswith('sb_secret_'):
            self.headers['Authorization'] = 'Bearer ' + key
        self.opener = build_opener(NoRedirect())

    def request(self, method='GET', params=None, body=None, prefer=None):
        url = self.url + ('?' + urlencode(params) if params else '')
        headers = dict(self.headers)
        if prefer:
            headers['Prefer'] = prefer
        req = Request(url, None if body is None else json.dumps(body).encode(), headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as response:
                data = response.read()
                return json.loads(data) if data else []
        except HTTPError as error:
            try:
                code = json.loads(error.read()).get('code', '')
            except (ValueError, AttributeError):
                code = ''
            if error.code == 409:
                raise CloudError('A session with that identifier already exists.', 409) from None
            if error.code in (401, 403):
                raise CloudError('Supabase denied access. Check the server key and table permissions.', 503) from None
            if error.code == 404 or code in ('PGRST106', 'PGRST205', '42P01'):
                raise CloudError('The poker_sessions table is not available. Check the migration and exposed schema.', 503) from None
            raise CloudError('Supabase could not complete the request (HTTP %d).' % error.code) from None
        except (URLError, TimeoutError, OSError):
            raise CloudError('Could not reach Supabase. Your change has not been confirmed; please retry.') from None
        except ValueError:
            raise CloudError('Supabase returned an unreadable response.') from None

    def check(self):
        self.request(params={'select': 'id,session_code,revision,players,buy_ins,transfers,owner_hash,member_hashes', 'limit': '1'})

    def get(self, code):
        rows = self.request(params={'session_code': 'eq.' + code, 'select': '*', 'limit': '1'})
        return rows[0] if rows else None

    def list(self, device, offset=0, limit=51):
        return self.request(params={'select': 'session_code,name,created_at,status,total_buy_in_cents,transfers', 'order': 'created_at.desc,session_code.desc', 'offset': str(offset), 'limit': str(limit)})

    def roster_rows(self, device, offset=0, limit=200):
        return self.request(params={'select': 'players', 'order': 'created_at.desc,session_code.desc', 'offset': str(offset), 'limit': str(limit)})

    def stats_rows(self, device, offset=0, limit=200):
        return self.request(params={'status': 'eq.settled', 'select': 'session_code,name,created_at,status,players,buy_ins', 'order': 'created_at.desc,session_code.desc', 'offset': str(offset), 'limit': str(limit)})

    def insert(self, row):
        return self.request('POST', body=row, prefer='return=representation')[0]

    def compare_and_swap(self, previous, row):
        payload = {k: v for k, v in row.items() if k not in ('id', 'session_code')}
        rows = self.request('PATCH', {'session_code': 'eq.' + previous['session_code'], 'revision': 'eq.' + str(previous['revision'])}, payload, 'return=representation')
        return rows[0] if rows else None

    def delete(self, previous):
        rows = self.request('DELETE', {
            'session_code': 'eq.' + previous['session_code'],
            'owner_hash': 'eq.' + previous['owner_hash'],
            'revision': 'eq.' + str(previous['revision']),
            'select': 'session_code',
        }, prefer='return=representation')
        return bool(rows)


def metadata(row):
    # Private server metadata stays inside the first player's JSON object. It is
    # never exposed in a browser response. No extra database column is required.
    return row['players'][0].setdefault('_ledger', {}) if row['players'] else {}


def receipt_key(device, request_id):
    return hashlib.sha256((device + ':' + request_id).encode()).hexdigest()


class SupabaseStore:
    def __init__(self, api, domain):
        self.api, self.domain = api, domain

    def get_row(self, code):
        row = self.api.get(code)
        if row is None:
            raise self.domain.UserError('We could not find that session. Check the invitation link.', 404)
        return row

    def listing(self, device, offset=0):
        size = self.domain.HISTORY_PAGE_SIZE
        rows = self.api.list(device, offset, size + 1)
        return {'sessions': [{'code': r['session_code'], 'name': r['name'], 'created': r['created_at'], 'status': r['status'], 'total_buyin': r['total_buy_in_cents'], 'unpaid_count': sum(not t['paid'] for t in r['transfers']), 'demo': 0} for r in rows[:size]],
                'next_offset': offset + size if len(rows) > size else None}

    def saved_players(self, device):
        def names():
            offset = 0
            while True:
                rows = self.api.roster_rows(device, offset)
                if not rows:
                    return
                for row in rows:
                    if not metadata(row).get('demo', False):
                        yield from (p['name'] for p in row['players'])
                offset += len(rows)
        return self.domain.player_directory(names())

    def leaderboard(self, device):
        def sessions():
            offset = 0
            while True:
                rows = self.api.stats_rows(device, offset)
                if not rows:
                    return
                for row in rows:
                    if row['status'] != 'settled' or metadata(row).get('demo', False):
                        continue
                    totals = {}
                    for entry in row['buy_ins']:
                        if not entry.get('voided'):
                            pid = entry['player_id']
                            totals[pid] = totals.get(pid, 0) + entry['amount']
                    yield {'code': row['session_code'], 'name': row['name'], 'created': row['created_at'],
                           'status': row['status'], 'players': [{'name': p['name'], 'cashout': p['cashout'],
                           'buyin': totals.get(p['id'], 0)} for p in row['players']]}
                offset += len(rows)
        return self.domain.player_statistics(sessions())

    def view(self, row, device):
        players = []
        for p in row['players']:
            ins = [b for b in row['buy_ins'] if b['player_id'] == p['id'] and not b.get('voided')]
            players.append({'id': p['id'], 'name': p['name'], 'cashout': p.get('cashout'), 'joined': p['joined'], 'buyin': sum(b['amount'] for b in ins), 'entries': len(ins)})
        names = {p['id']: p['name'] for p in players}
        mine = next((p['id'] for p in row['players'] if p.get('device_hash') == device), None)
        return {'code': row['session_code'], 'name': row['name'], 'created': row['created_at'], 'status': row['status'], 'settled_at': row.get('settled_at'), 'minimum_proven': int(row['minimum_proven']), 'demo': int(metadata(row).get('demo', False)), 'role': 'host' if row['owner_hash'] == device else 'player' if mine is not None else 'guest', 'my_player_id': mine, 'players': players,
                'buyins': [{'id': b['id'], 'player': b['player_id'], 'name': names[b['player_id']], 'amount': b['amount'], 'created': b['created_at'], 'voided': int(b.get('voided', False))} for b in reversed(row['buy_ins'])],
                'activity': [], 'transfers': [{k: t[k] for k in ('id', 'from_id', 'to_id', 'amount', 'paid')} for t in row['transfers']],
                'total_buyin': sum(p['buyin'] for p in players), 'total_cashout': sum(p['cashout'] or 0 for p in players), 'missing': sum(p['cashout'] is None for p in players)}

    def get(self, code, device):
        return self.view(self.get_row(code), device)

    def require_host(self, row, device):
        if row['owner_hash'] != device:
            raise self.domain.UserError('Only the session host can do that.', 403)

    def player(self, row, player_id, device):
        p = next((p for p in row['players'] if p['id'] == player_id), None)
        if p is None:
            raise self.domain.UserError('Choose a player from this session.')
        if row['owner_hash'] != device and p.get('device_hash') != device:
            raise self.domain.UserError('You can only edit your own entries.', 403)
        return p

    def add_player(self, row, name, device=None):
        name = self.domain.clean_name(name)
        if len(row['players']) >= 30:
            raise self.domain.UserError('This table already has 30 players.')
        if any(p['name'].casefold() == name.casefold() for p in row['players']):
            raise self.domain.UserError('That name is already at the table.')
        if device and any(p.get('device_hash') == device for p in row['players']):
            raise self.domain.UserError('You have already joined this session.')
        p = {'id': max((p['id'] for p in row['players']), default=0) + 1, 'name': name, 'device_hash': device, 'cashout': None, 'joined': self.domain.now()}
        row['players'].append(p)
        if device and device not in row['member_hashes']:
            row['member_hashes'].append(device)
        return p

    def add_buyin(self, row, player, amount):
        value = self.domain.amount(amount)
        row['buy_ins'].append({'id': max((b['id'] for b in row['buy_ins']), default=0) + 1, 'player_id': player['id'], 'amount': value, 'created_at': self.domain.now(), 'voided': False})
        player['cashout'] = None

    def set_cashouts(self, row, values, device, require_all=False):
        if not isinstance(values, list) or not values or len(values) > 30:
            raise self.domain.UserError('Enter the final amounts.')
        supplied = []
        for entry in values:
            if not isinstance(entry, dict):
                raise self.domain.UserError('Check the final amounts.')
            p = self.player(row, entry.get('player_id'), device)
            supplied.append(p['id'])
            p['cashout'] = None if not require_all and entry.get('amount') == '' else self.domain.amount(entry.get('amount'), zero=True)
        if len(set(supplied)) != len(supplied) or require_all and sorted(supplied) != sorted(p['id'] for p in row['players']):
            raise self.domain.UserError('Enter a final amount for every player exactly once.')

    def apply(self, row, action, body, device):
        if action == 'reopen':
            if row['status'] == 'active':
                return
            if body.get('settled_at') != row['settled_at']:
                raise self.domain.UserError('This settlement has changed. Refresh the session and try again.', 409)
            row['transfers'] = []
            row['status'], row['settled_at'], row['minimum_proven'] = 'active', None, False
            return
        if action == 'rename':
            player = self.player(row, body.get('player_id'), device)
            name = self.domain.clean_name(body.get('name'))
            if any(p['id'] != player['id'] and p['name'].casefold() == name.casefold()
                   for p in row['players']):
                raise self.domain.UserError('That name is already at the table. Choose another name.')
            player['name'] = name
            return
        if action == 'paid':
            if row['status'] != 'settled':
                raise self.domain.UserError('Settle the session first.')
            if body.get('settled_at') != row['settled_at']:
                raise self.domain.UserError('This settlement has changed. Refresh the session and try again.', 409)
            t = next((t for t in row['transfers'] if t['id'] == body.get('transfer_id')), None)
            if t is None:
                raise self.domain.UserError('That transfer could not be found.')
            self.player(row, t['to_id'], device)
            paid = body.get('paid', True)
            if type(paid) is not bool:
                raise self.domain.UserError('Choose paid or unpaid.')
            t['paid'] = paid
            return
        if row['status'] != 'active':
            raise self.domain.UserError('This session is settled. Its amounts are now locked.')
        if action == 'join':
            if row['owner_hash'] == device:
                raise self.domain.UserError('You are already hosting this session.')
            self.add_player(row, body.get('name'), device)
        elif action == 'players':
            self.require_host(row, device)
            p = self.add_player(row, body.get('name'))
            if body.get('amount'):
                self.add_buyin(row, p, body['amount'])
        elif action == 'buyin':
            self.add_buyin(row, self.player(row, body.get('player_id'), device), body.get('amount'))
        elif action == 'undo':
            entry = next((b for b in row['buy_ins'] if b['id'] == body.get('entry_id') and not b['voided']), None)
            if entry is None:
                raise self.domain.UserError('That entry was already removed or could not be found.')
            p = self.player(row, entry['player_id'], device)
            entry['voided'], p['cashout'] = True, None
        elif action == 'cashouts':
            self.set_cashouts(row, body.get('values'), device)
        elif action == 'settle':
            self.require_host(row, device)
            if 'values' in body:
                self.set_cashouts(row, body['values'], device, require_all=True)
            players = self.view(row, device)['players']
            if len(players) < 2:
                raise self.domain.UserError('Add at least two players before settling.')
            plan, proven = self.domain.solve_transfers(players)
            row['transfers'] = [dict(t, id=i + 1, paid=False) for i, t in enumerate(plan)]
            row['minimum_proven'], row['status'], row['settled_at'] = proven, 'settled', self.domain.now()
        else:
            raise self.domain.UserError('Unknown action.', 404)

    def create(self, body, device, key, fingerprint):
        code = key[:32]
        creation_id = str(uuid.UUID(hashlib.sha256((key + fingerprint).encode()).hexdigest()[:32]))
        prior = self.api.get(code)
        if prior:
            if prior['owner_hash'] != device or prior['id'] != creation_id:
                raise self.domain.UserError('This request identifier was already used for another action.', 409)
            return {'code': code}
        row = {'id': creation_id, 'session_code': code, 'name': self.domain.clean_name(body.get('name'), 70), 'currency': 'SGD', 'status': 'active', 'created_at': self.domain.now(), 'updated_at': self.domain.now(), 'settled_at': None, 'owner_hash': device, 'member_hashes': [], 'players': [], 'buy_ins': [], 'transfers': [], 'total_buy_in_cents': 0, 'total_cash_out_cents': None, 'minimum_proven': False, 'revision': 0}
        if body.get('host_name'):
            self.add_player(row, body['host_name'], device)
        try:
            self.api.insert(row)
        except CloudError as err:
            if err.status != 409:
                raise
            prior = self.api.get(code)
            if prior is None or prior['id'] != creation_id or prior['owner_hash'] != device:
                raise self.domain.UserError('This request identifier was already used for another action.', 409) from None
        return {'code': code}

    def mutate(self, path, body, device, request_id, fingerprint):
        key = receipt_key(device, request_id)
        if path == '/api/sessions':
            return self.create(body, device, key, fingerprint)
        match = re.fullmatch(r'/api/sessions/([A-Za-z0-9_-]+)/([a-z]+)', path)
        if not match:
            raise self.domain.UserError('Unknown action.', 404)
        code, action = match.groups()
        for _ in range(6):
            if action == 'delete':
                previous = self.api.get(code)
                # A missing row is a successful retry after a lost delete reply.
                if previous is None:
                    return {'code': code, 'deleted': True}
                self.require_host(previous, device)
                prior_fingerprint = metadata(previous).get('requests', {}).get(key)
                if prior_fingerprint is not None and prior_fingerprint != fingerprint:
                    raise self.domain.UserError('This request identifier was already used for another action.', 409)
                if self.api.delete(previous):
                    return {'code': code, 'deleted': True}
                continue
            previous = self.get_row(code)
            row = copy.deepcopy(previous)
            receipts = metadata(row).get('requests', {})
            if key in receipts:
                if receipts[key] != fingerprint:
                    raise self.domain.UserError('This request identifier was already used for another action.', 409)
                return self.view(row, device)
            self.apply(row, action, body, device)
            metadata(row).setdefault('requests', {})[key] = fingerprint
            totals = self.view(row, device)
            row['total_buy_in_cents'] = totals['total_buyin']
            row['total_cash_out_cents'] = totals['total_cashout'] if not totals['missing'] else None
            row['revision'] = previous['revision'] + 1
            row['updated_at'] = self.domain.now()
            saved = self.api.compare_and_swap(previous, row)
            if saved is not None:
                return self.view(saved, device)
        raise self.domain.UserError('Someone else is updating the table. Please try again.', 409)

    def import_local(self, db):
        """Copy missing local sessions only. Never overwrite cloud rows."""
        imported, skipped = 0, 0
        for s in [dict(r) for r in db.execute('SELECT * FROM sessions')]:
            if self.api.get(s['code']) is not None:
                skipped += 1
                continue
            players = [dict(r) for r in db.execute('SELECT * FROM players WHERE session=? ORDER BY id', (s['code'],))]
            row = {'id': str(uuid.uuid4()), 'session_code': s['code'], 'name': s['name'], 'currency': 'SGD', 'status': s['status'], 'created_at': s['created'], 'updated_at': self.domain.now(), 'settled_at': s['settled_at'], 'owner_hash': s['owner'], 'member_hashes': [p['device'] for p in players if p['device']],
                   'players': [{'id': p['id'], 'name': p['name'], 'cashout': p['cashout'], 'joined': p['joined'], 'device_hash': p['device']} for p in players],
                   'buy_ins': [{'id': b['id'], 'player_id': b['player'], 'amount': b['amount'], 'created_at': b['created'], 'voided': bool(b['voided'])} for b in db.execute('SELECT b.* FROM buyins b JOIN players p ON p.id=b.player WHERE p.session=? ORDER BY b.id', (s['code'],))],
                   'transfers': [{'id': t['id'], 'from_id': t['from_id'], 'to_id': t['to_id'], 'amount': t['amount'], 'paid': bool(t['paid'])} for t in db.execute('SELECT * FROM transfers WHERE session=? ORDER BY id', (s['code'],))],
                   'total_buy_in_cents': 0, 'total_cash_out_cents': None, 'minimum_proven': bool(s['minimum_proven']), 'revision': 0}
            if players:
                meta = metadata(row)
                meta['demo'] = bool(s['demo'])
                meta['requests'] = {}
                for r in db.execute('SELECT device,request_id,fingerprint,response FROM requests'):
                    response = json.loads(r['response'])
                    if response.get('code') == s['code']:
                        meta['requests'][receipt_key(r['device'], r['request_id'])] = r['fingerprint']
            totals = self.view(row, s['owner'])
            row['total_buy_in_cents'] = totals['total_buyin']
            row['total_cash_out_cents'] = totals['total_cashout'] if not totals['missing'] else None
            try:
                self.api.insert(row)
                imported += 1
            except CloudError as err:
                if err.status != 409:
                    raise
                skipped += 1
        return imported, skipped
