import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from supabase_store import SupabaseAPI, SupabaseStore, metadata
from test_supabase import MemoryAPI


class StatisticsAPI(MemoryAPI):
    def stats_rows(self, device, offset=0, limit=200):
        rows = [row for row in self.rows.values()
                if row['status'] == 'settled' and
                (row['owner_hash'] == device or device in row['member_hashes'])]
        rows.sort(key=lambda row: (row['created_at'], row['session_code']), reverse=True)
        # A smaller service-side limit must not truncate lifetime statistics.
        return copy.deepcopy(rows[offset:offset + min(limit, 2)])


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / 'stats.sqlite3'
        server.initialise()
        self.api = StatisticsAPI()
        self.cloud = SupabaseStore(self.api, server)
        self.counter = 0
        self.host, self.friend = 'host-stats', 'friend-stats'

    def tearDown(self):
        server.DB_PATH = self.previous
        self.temp.cleanup()

    def call(self, path, body, device=None):
        self.counter += 1
        device = device or self.host
        if self.backend == 'cloud':
            return self.cloud.mutate(path, body, device, 'stats-request-%016d' % self.counter,
                                     hashlib.sha256(json.dumps(body).encode()).hexdigest())
        with server.database() as db:
            return server.mutate(db, path, body, device)

    def board(self, device=None):
        device = device or self.host
        if self.backend == 'cloud':
            return self.cloud.leaderboard(device)
        with server.database() as db:
            return server.leaderboard(db, device)

    def game(self, names, cashouts, host=None, joined=False, rebuy=False, sample=False):
        host = host or self.host
        code = self.call('/api/sessions', {'name': 'Game'}, host)['code']
        path = '/api/sessions/' + code + '/'
        for index, name in enumerate(names):
            if joined and index == 0:
                state = self.call(path + 'join', {'name': name}, self.host)
                self.call(path + 'buyin', {'player_id': state['my_player_id'], 'amount': '100'}, self.host)
            else:
                self.call(path + 'players', {'name': name, 'amount': '100'}, host)
        state = self.call(path + 'players', {'name': 'Spectator'}, host)
        first = state['players'][0]['id']
        if rebuy:
            self.call(path + 'buyin', {'player_id': first, 'amount': '50'}, host)
            state = self.call(path + 'buyin', {'player_id': first, 'amount': '20'}, host)
            self.call(path + 'undo', {'entry_id': state['buyins'][0]['id']}, host)
        if sample:
            if self.backend == 'cloud':
                metadata(self.api.rows[code])['demo'] = True
            else:
                with server.database() as db:
                    db.execute('UPDATE sessions SET demo=1 WHERE code=?', (code,))
        if cashouts is not None:
            state = self.call(path + 'settle', {'values': [
                {'player_id': p['id'], 'amount': amount}
                for p, amount in zip(state['players'], cashouts + ['0'])]}, host)
        return code, state

    def test_deleted_session_disappears_from_stats_without_changing_other_games(self):
        for self.backend in ('sqlite', 'cloud'):
            with self.subTest(backend=self.backend):
                deleted, _ = self.game(['A', 'B'], ['200', '0'])
                kept, _ = self.game(['A', 'B'], ['50', '150'])
                self.assertEqual(self.board()['session_count'], 2)
                self.call('/api/sessions/' + deleted + '/delete', {})
                board = self.board()
                self.assertEqual(board['session_count'], 1)
                self.assertEqual(board['total_buyin'], 20000)
                self.assertEqual([(p['name'], p['net']) for p in board['players']], [('B', 5000), ('A', -5000)])
                self.assertTrue(all([s['code'] for s in p['history']] == [kept] for p in board['players']))

    def test_both_stores_aggregate_only_accessible_finished_games(self):
        for self.backend in ('sqlite', 'cloud'):
            with self.subTest(backend=self.backend):
                self.game(['Arden', 'Rayner'], ['175', '75'], rebuy=True)
                self.game(['arden', 'RAYNER'], ['100', '100'])
                code, latest = self.game(['ARDEN', 'rayner'], ['70', '130'], self.friend, joined=True)
                self.game(['Arden', 'Rayner'], None)
                self.game(['Secret', 'Private'], ['200', '0'], 'private-owner')
                self.game(['Sample', 'Demo'], ['200', '0'], sample=True)
                board = self.board()
                self.assertEqual(board['session_count'], 3)
                self.assertEqual(board['total_buyin'], 65000)
                self.assertEqual([p['name'] for p in board['players']], ['Rayner', 'Arden'])
                rayner, arden = board['players']
                self.assertEqual(sum(p['net'] for p in board['players']), 0)
                self.assertEqual((arden['sessions'], arden['wins'], arden['losses'], arden['draws']), (3, 1, 1, 1))
                self.assertEqual((arden['buyin'], arden['cashout'], arden['net'], arden['best'], arden['worst']),
                                 (35000, 34500, -500, 2500, -3000))
                self.assertEqual((rayner['net'], rayner['best'], rayner['worst']), (500, 3000, -2500))
                self.assertEqual(len(arden['history']), 3)
                self.assertEqual(arden['history'][0]['code'], code)
                self.assertEqual(self.board('outsider'), {'players': [], 'session_count': 0, 'total_buyin': 0})
                text = json.dumps(board)
                for private in ('host-stats', 'friend-stats', 'device_hash', '_ledger', 'Spectator', 'Secret', 'Demo'):
                    self.assertNotIn(private, text)
                transfer = latest['transfers'][0]
                self.call('/api/sessions/' + code + '/paid', {'transfer_id': transfer['id'], 'paid': True}, self.friend)
                self.assertEqual(self.board(), board)
                self.call('/api/sessions/' + code + '/rename',
                          {'player_id': latest['players'][0]['id'], 'name': 'Arden corrected'}, self.friend)
                names = {p['name']: p['sessions'] for p in self.board()['players']}
                self.assertEqual(names, {'Rayner': 3, 'Arden': 2, 'Arden corrected': 1})

    def test_ties_and_cent_values(self):
        result = server.player_statistics([{'code': 'game', 'name': 'Game', 'created': server.now(),
            'status': 'settled', 'players': [
                {'name': 'ZS', 'buyin': 100, 'cashout': 101},
                {'name': '  New   Friend ', 'buyin': 100, 'cashout': 101},
                {'name': 'Min', 'buyin': 100, 'cashout': 98}]}])
        self.assertEqual([p['rank'] for p in result['players']], [1, 1, 3])
        self.assertEqual([p['name'] for p in result['players']], ['New Friend', 'ZS', 'Min'])
        self.assertEqual([p['net'] for p in result['players']], [1, 1, -2])

    def test_cloud_statistics_beyond_1000_and_after_restart(self):
        self.backend = 'cloud'
        code, _ = self.game(['Arden', 'Rayner'], ['150', '50'])
        base = self.api.rows.pop(code)
        for index in range(1005):
            row = copy.deepcopy(base)
            row['session_code'] = 'game%04d' % index
            self.api.rows[row['session_code']] = row
        self.cloud = SupabaseStore(self.api, server)
        board = self.board()
        self.assertEqual(board['session_count'], 1005)
        self.assertEqual(board['total_buyin'], 20100000)
        self.assertEqual(board['players'][0]['net'], 5025000)
        self.assertEqual(len(board['players'][0]['history']), 1005)

    def test_cloud_query_filters_access_and_requests_only_statistics_fields(self):
        api = SupabaseAPI('https://example.com', 'sb_secret_test')
        with patch.object(api, 'request', return_value=[]) as request:
            api.stats_rows(self.host, 1000)
        params = request.call_args.kwargs['params']
        self.assertEqual(params['status'], 'eq.settled')
        self.assertEqual(params['offset'], '1000')
        self.assertEqual(params['limit'], '200')
        self.assertEqual(params['order'], 'created_at.desc,session_code.desc')
        self.assertIn('owner_hash.eq.' + self.host, params['or'])
        self.assertIn('member_hashes.cs.{%s}' % self.host, params['or'])
        self.assertNotIn('transfers', params['select'])
        self.assertNotIn('*', params['select'])
