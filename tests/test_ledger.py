import functools
import http.cookiejar
import json
import random
import secrets
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def players_from_net(nets):
    return [{'id': i + 1, 'buyin': 100000, 'cashout': 100000 + n} for i, n in enumerate(nets)]


def independent_minimum(nets):
    """Exhaustively choose disjoint zero-sum subsets, independent of the DP."""
    nets = tuple(n for n in nets if n)

    @functools.lru_cache(None)
    def groups(mask):
        if not mask:
            return 0
        first = mask & -mask
        best = 1
        sub = mask
        while sub:
            if sub & first and sum(n for i, n in enumerate(nets) if sub >> i & 1) == 0:
                best = max(best, 1 + groups(mask ^ sub))
            sub = (sub - 1) & mask
        return best
    return len(nets) - groups((1 << len(nets)) - 1)


class SettlementTests(unittest.TestCase):
    def verify_conservation(self, nets, plan):
        actual = [0] * len(nets)
        for transfer in plan:
            a, b, value = transfer['from_id'] - 1, transfer['to_id'] - 1, transfer['amount']
            self.assertGreater(value, 0)
            self.assertLess(nets[a], 0)
            self.assertGreater(nets[b], 0)
            actual[a] -= value
            actual[b] += value
        self.assertEqual(actual, nets)

    def test_user_example(self):
        nets = [44500, 40800, 23500, 19850, 2600, -10000, -52250, -69000]
        plan, proven = server.solve_transfers(players_from_net(nets))
        self.assertEqual(len(plan), 7)
        self.assertTrue(proven)
        self.verify_conservation(nets, plan)

    def test_finds_better_than_greedy(self):
        nets = [-800, -500, 500, 400, 400]
        players = players_from_net(nets)
        self.assertEqual(len(server.match_group(players)), 4)
        plan, proven = server.solve_transfers(players)
        self.assertEqual(len(plan), 3)
        self.assertTrue(proven)
        self.verify_conservation(nets, plan)

    def test_randomised_conservation_and_optimality(self):
        rng = random.Random(821)
        for _ in range(250):
            nets = [rng.randint(-50, 50) for _ in range(rng.randint(1, 7))]
            nets.append(-sum(nets))
            with self.subTest(nets=nets):
                plan, proven = server.solve_transfers(players_from_net(nets))
                self.assertTrue(proven)
                self.assertEqual(len(plan), independent_minimum(nets))
                self.verify_conservation(nets, plan)

    def test_large_group_never_claims_unproven_minimum(self):
        nets = [-123, -67, -54, -44, -33, -29, -23, -18, -11, 55, 51, 49, 47, 43, 41, 39, 35, 25, 17]
        nets.append(-sum(nets))
        plan, proven = server.solve_transfers(players_from_net(nets))
        self.verify_conservation(nets, plan)
        lower = max(sum(n < 0 for n in nets), sum(n > 0 for n in nets))
        self.assertEqual(proven, len(plan) == lower)

    def test_missing_and_mismatch_rejected(self):
        for p in [[{'id': 1, 'buyin': 100, 'cashout': None}], [{'id': 1, 'buyin': 100, 'cashout': 99}]]:
            with self.assertRaises(server.UserError):
                server.solve_transfers(p)

    def test_integer_cent_parsing(self):
        for value, result in [('100', 10000), ('198.50', 19850), ('0.01', 1), ('0', 0)]:
            self.assertEqual(server.amount(value, zero=True), result)
        for value in ['NaN', '1e2', '-1', '1.001', '1,000', '1000000', 1, True, None, '0']:
            with self.assertRaises(server.UserError):
                server.amount(value)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / 'test.sqlite3'
        server.initialise()
        self.host, self.friend = 'host-token-hash', 'friend-token-hash'
        self.code = self.call('/api/sessions', {'name': 'Friday'})['code']
        self.path = '/api/sessions/' + self.code + '/'

    def tearDown(self):
        server.DB_PATH = self.previous
        self.temp.cleanup()

    def call(self, path, body, device=None):
        with server.database() as db:
            db.execute('BEGIN IMMEDIATE')
            return server.mutate(db, path, body, device or self.host)

    def get(self, code=None, device=None):
        with server.database() as db:
            return server.view(db, code or self.code, device or self.host)

    def populated(self):
        self.call(self.path + 'players', {'name': 'A', 'amount': '100'})
        s = self.call(self.path + 'players', {'name': 'B', 'amount': '100'})
        return [p['id'] for p in s['players']]

    def test_new_session_does_not_add_fake_player(self):
        self.assertEqual(self.get()['players'], [])

    def test_saved_players_remember_names_without_carrying_over_amounts(self):
        self.call(self.path + 'players', {'name': 'Zoe', 'amount': '100'})
        self.call(self.path + 'players', {'name': 'rayner', 'amount': '50'})
        self.call('/api/demo', {})
        private = self.call('/api/sessions', {'name': 'Private'}, self.friend)['code']
        self.call('/api/sessions/' + private + '/players', {'name': 'Private person'}, self.friend)
        joined = self.call('/api/sessions', {'name': 'Joined'}, self.friend)['code']
        self.call('/api/sessions/' + joined + '/players', {'name': 'Amy'}, self.friend)
        self.call('/api/sessions/' + joined + '/join', {'name': 'Host'}, self.host)
        fresh = self.call('/api/sessions', {'name': 'Next session'})['code']
        self.assertEqual(self.get(fresh)['players'], [])
        self.assertEqual(self.get(fresh)['total_buyin'], 0)
        with server.database() as db:
            names = server.saved_players(db, self.host)['players']
            self.assertEqual(names, sorted(set(server.STARTER_PLAYERS) | {'Zoe', 'Amy', 'Host'}, key=str.casefold))
            self.assertNotIn('Sarah', names)
            self.assertNotIn('Private person', names)
            self.assertEqual(server.saved_players(db, 'outsider')['players'], list(server.STARTER_PLAYERS))

    def test_history_has_no_lifetime_cap_and_pages_without_duplicates(self):
        a, b = self.populated()
        entry = self.call(self.path + 'buyin', {'player_id': a, 'amount': '20'})['buyins'][0]
        self.call(self.path + 'undo', {'entry_id': entry['id']})
        with server.database() as db:
            for i in range(205):
                server.mutate(db, '/api/sessions', {'name': 'Game %d' % i}, self.host)
            other = server.mutate(db, '/api/sessions', {'name': 'Private'}, self.friend)['code']
        codes, offset = [], 0
        with server.database() as db:
            while offset is not None:
                page = server.listing(db, self.host, offset)
                self.assertLessEqual(len(page['sessions']), 50)
                codes.extend(s['code'] for s in page['sessions'])
                offset = page['next_offset']
            original = next(s for s in page['sessions'] if s['code'] == self.code)
            self.assertEqual(original['total_buyin'], 20000)
            self.assertEqual(server.listing(db, 'outsider')['sessions'], [])
        self.assertEqual(len(codes), 206)
        self.assertEqual(len(set(codes)), 206)
        self.assertNotIn(other, codes)
        self.assertEqual(self.get()['buyins'][0], dict(entry, voided=1))

    def test_rebuy_and_removal_keep_audit(self):
        a, b = self.populated()
        self.call(self.path + 'cashouts', {'values': [{'player_id': a, 'amount': '90'}]})
        s = self.call(self.path + 'buyin', {'player_id': a, 'amount': '50.25'})
        self.assertEqual(s['players'][0]['buyin'], 15025)
        self.assertIsNone(s['players'][0]['cashout'])
        s = self.call(self.path + 'undo', {'entry_id': s['buyins'][0]['id']})
        self.assertEqual(s['players'][0]['buyin'], 10000)
        self.assertEqual(s['buyins'][0]['voided'], 1)

    def test_join_permissions_and_isolation(self):
        a, b = self.populated()
        s = self.call(self.path + 'join', {'name': 'Friend'}, self.friend)
        own = s['my_player_id']
        self.assertEqual(s['role'], 'player')
        self.call(self.path + 'buyin', {'player_id': own, 'amount': '50'}, self.friend)
        for action, body in [('buyin', {'player_id': a, 'amount': '50'}), ('players', {'name': 'X'}), ('settle', {})]:
            with self.assertRaises(server.UserError):
                self.call(self.path + action, body, self.friend)
        second = self.call('/api/sessions', {'name': 'Saturday'})['code']
        with self.assertRaises(server.UserError):
            self.call('/api/sessions/' + second + '/buyin', {'player_id': a, 'amount': '50'})

    def test_atomic_settlement_and_past_sessions(self):
        a, b = self.populated()
        values = [{'player_id': a, 'amount': '150'}, {'player_id': b, 'amount': '50'}]
        s = self.call(self.path + 'settle', {'values': values})
        self.assertEqual(s['status'], 'settled')
        self.assertEqual(s['minimum_proven'], 1)
        self.assertEqual(s['transfers'][0]['amount'], 5000)
        second = self.call('/api/sessions', {'name': 'Next game'})['code']
        self.assertEqual(self.get()['status'], 'settled')
        self.assertEqual(self.get(second)['players'], [])
        with self.assertRaises(server.UserError):
            self.call(self.path + 'buyin', {'player_id': a, 'amount': '25'})

    def test_failed_settlement_rolls_back_all_cashouts(self):
        a, b = self.populated()
        with self.assertRaises(server.UserError):
            self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '150'}, {'player_id': b, 'amount': '49.99'}]})
        s = self.get()
        self.assertEqual(s['status'], 'active')
        self.assertTrue(all(p['cashout'] is None for p in s['players']))
        self.assertEqual(s['transfers'], [])

    def test_paid_status_is_persistent_reversible_and_authorised(self):
        self.call(self.path + 'players', {'name': 'A', 'amount': '100'})
        state = self.call(self.path + 'join', {'name': 'B'}, self.friend)
        a, b = state['players'][0]['id'], state['my_player_id']
        self.call(self.path + 'buyin', {'player_id': b, 'amount': '100'}, self.friend)
        state = self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '0'}, {'player_id': b, 'amount': '200'}]})
        transfer = state['transfers'][0]
        with self.assertRaises(server.UserError):
            self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': True}, 'outsider')
        self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': True}, self.friend)
        self.assertTrue(self.get()['transfers'][0]['paid'])
        self.assertEqual(self.get()['players'], state['players'])
        with server.database() as db:
            self.assertEqual(server.listing(db, self.host)['sessions'][0]['unpaid_count'], 0)
        self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': False})
        self.assertEqual(self.get()['transfers'][0], transfer)
        with self.assertRaises(server.UserError):
            self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': 'false'})

    def test_duplicate_names_and_partial_cashout_list(self):
        a, b = self.populated()
        with self.assertRaises(server.UserError):
            self.call(self.path + 'players', {'name': 'a', 'amount': '20'})
        with self.assertRaises(server.UserError):
            self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '200'}]})

    def test_rename_permissions_validation_and_settled_money_are_preserved(self):
        self.call(self.path + 'players', {'name': 'Tyop', 'amount': '100'})
        state = self.call(self.path + 'join', {'name': 'Frend'}, self.friend)
        first, second = state['players'][0]['id'], state['my_player_id']
        state = self.call(self.path + 'rename', {'player_id': second, 'name': '  Friend  '}, self.friend)
        self.assertEqual(state['players'][1]['name'], 'Friend')
        self.assertEqual(state['my_player_id'], second)
        for device, player_id, name in [('outsider', first, 'No'), (self.friend, first, 'No'),
                                        (self.host, first, ''), (self.host, first, 'x' * 41),
                                        (self.host, first, 'fRIEnd'), (self.host, 9999, 'No')]:
            with self.assertRaises(server.UserError):
                self.call(self.path + 'rename', {'player_id': player_id, 'name': name}, device)
        self.call(self.path + 'buyin', {'player_id': second, 'amount': '100'}, self.friend)
        settled = self.call(self.path + 'settle', {'values': [{'player_id': first, 'amount': '50'}, {'player_id': second, 'amount': '150'}]})
        before = self.call(self.path + 'paid', {'transfer_id': settled['transfers'][0]['id'], 'paid': True})
        after = self.call(self.path + 'rename', {'player_id': first, 'name': 'Taylor'})
        self.assertEqual(after['status'], 'settled')
        self.assertEqual(after['settled_at'], before['settled_at'])
        self.assertEqual(after['transfers'], before['transfers'])
        self.assertEqual(after['players'][0], dict(before['players'][0], name='Taylor'))
        self.assertEqual(after['players'][1], before['players'][1])
        self.assertEqual(after['buyins'], [dict(b, name='Taylor') if b['player'] == first else b for b in before['buyins']])
        with server.database() as db:
            names = server.saved_players(db, self.host)['players']
        self.assertIn('Taylor', names)
        self.assertNotIn('Tyop', names)
        self.assertNotIn('Frend', names)


class HttpTests(unittest.TestCase):
    call = LedgerTests.call

    def setUp(self):
        LedgerTests.setUp(self)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.http.server_port
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.client.open(self.url + '/api/sessions').read()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        LedgerTests.tearDown(self)

    def post(self, path, body, request_id=None, origin=None):
        headers = {'Content-Type': 'application/json', 'X-Request-Id': request_id or secrets.token_hex(16)}
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), headers)
        return json.load(self.client.open(req))

    def test_retried_http_request_does_not_duplicate_buyin(self):
        code = self.post('/api/sessions', {'name': 'HTTP game'})['code']
        path = '/api/sessions/' + code + '/'
        s = self.post(path + 'players', {'name': 'P', 'amount': '100'})
        pid = s['players'][0]['id']
        key = secrets.token_hex(16)
        first = self.post(path + 'buyin', {'player_id': pid, 'amount': '50'}, key)
        second = self.post(path + 'buyin', {'player_id': pid, 'amount': '50'}, key)
        self.assertEqual(first, second)
        self.assertEqual(second['players'][0]['buyin'], 15000)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(path + 'buyin', {'player_id': pid, 'amount': '75'}, key)
        self.assertEqual(error.exception.code, 409)

    def test_cross_origin_write_denied(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/sessions', {'name': 'No'}, origin='https://other.example')
        self.assertEqual(error.exception.code, 403)

    def test_history_page_validation_and_totals(self):
        code = self.post('/api/sessions', {'name': 'Saved game'})['code']
        self.post('/api/sessions/' + code + '/players', {'name': 'A', 'amount': '100'})
        page = json.load(self.client.open(self.url + '/api/sessions?offset=0'))
        self.assertEqual(page['sessions'][0]['total_buyin'], 10000)
        self.assertIsNone(page['next_offset'])
        self.assertEqual(json.load(self.client.open(self.url + '/api/sessions?offset=50'))['sessions'], [])
        for offset in ('-1', 'NaN', '1.5', '999999999999999999999'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.client.open(self.url + '/api/sessions?offset=' + offset)
            self.assertEqual(error.exception.code, 400)

    def test_saved_player_api_uses_requesting_browser(self):
        code = self.post('/api/sessions', {'name': 'Names'})['code']
        self.post('/api/sessions/' + code + '/players', {'name': 'Zoe', 'amount': '100'})
        response = json.load(self.client.open(self.url + '/api/players'))
        self.assertEqual(response['players'], sorted([*server.STARTER_PLAYERS, 'Zoe'], key=str.casefold))
        self.assertEqual(json.load(urllib.request.urlopen(self.url + '/api/players'))['players'], list(server.STARTER_PLAYERS))


if __name__ == '__main__':
    unittest.main()
