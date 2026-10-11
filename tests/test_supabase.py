import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from config import load_env
from supabase_store import CloudError, SupabaseAPI, SupabaseStore, metadata


class MemoryAPI:
    def __init__(self):
        self.rows = {}
        self.conflict = None
        self.lose_reply = False

    def get(self, code):
        return copy.deepcopy(self.rows.get(code))

    def list(self, device, offset=0, limit=51):
        rows = [row for row in self.rows.values() if row['owner_hash'] == device or device in row['member_hashes']]
        rows.sort(key=lambda row: (row['created_at'], row['session_code']), reverse=True)
        return copy.deepcopy(rows[offset:offset + limit])

    def roster_rows(self, device, offset=0, limit=200):
        # Simulate a database row limit smaller than the requested page size.
        return [{'players': row['players']} for row in self.list(device, offset, min(limit, 2))]

    def insert(self, row):
        if row['session_code'] in self.rows:
            raise CloudError('Conflict', 409)
        self.rows[row['session_code']] = copy.deepcopy(row)
        return copy.deepcopy(row)

    def compare_and_swap(self, previous, row):
        if self.conflict:
            callback, self.conflict = self.conflict, None
            callback(self.rows[row['session_code']])
            return None
        if row['session_code'] not in self.rows or self.rows[row['session_code']]['revision'] != previous['revision']:
            return None
        self.rows[row['session_code']] = copy.deepcopy(row)
        if self.lose_reply:
            self.lose_reply = False
            raise CloudError('Reply lost after save')
        return copy.deepcopy(row)

    def delete(self, previous):
        code = previous['session_code']
        if self.conflict:
            callback, self.conflict = self.conflict, None
            callback(self.rows[code])
            return False
        current = self.rows.get(code)
        if not current or current['revision'] != previous['revision'] or current['owner_hash'] != previous['owner_hash']:
            return False
        del self.rows[code]
        if self.lose_reply:
            self.lose_reply = False
            raise CloudError('Reply lost after delete')
        return True


class SupabaseTests(unittest.TestCase):
    def setUp(self):
        self.api = MemoryAPI()
        self.store = SupabaseStore(self.api, server)
        self.host = hashlib.sha256(b'host').hexdigest()
        self.friend = hashlib.sha256(b'friend').hexdigest()
        self.counter = 0
        self.code = self.call('/api/sessions', {'name': 'Poker Night'})['code']
        self.path = '/api/sessions/' + self.code + '/'

    def call(self, path, body, device=None, key=None):
        self.counter += 1
        key = key or 'request-%016d' % self.counter
        fingerprint = hashlib.sha256((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        return self.store.mutate(path, body, device or self.host, key, fingerprint)

    def populated(self):
        self.call(self.path + 'players', {'name': 'A', 'amount': '100'})
        state = self.call(self.path + 'players', {'name': 'B', 'amount': '100'})
        return [p['id'] for p in state['players']]

    def test_create_replay_and_mismatched_body(self):
        key = 'create-request-id-123'
        first = self.call('/api/sessions', {'name': 'Repeat'}, key=key)
        self.assertEqual(first, self.call('/api/sessions', {'name': 'Repeat'}, key=key))
        with self.assertRaises(server.UserError):
            self.call('/api/sessions', {'name': 'Changed'}, key=key)

    def test_delete_requires_host_and_removes_only_selected_session(self):
        self.populated()
        self.call(self.path + 'join', {'name': 'Friend'}, self.friend)
        other = self.call('/api/sessions', {'name': 'Keep me'})['code']
        for device in (self.friend, 'outsider'):
            with self.assertRaises(server.UserError) as failure:
                self.call(self.path + 'delete', {}, device)
            self.assertEqual(failure.exception.status, 403)
        self.assertEqual(self.call(self.path + 'delete', {}), {'code': self.code, 'deleted': True})
        self.assertIsNone(self.api.get(self.code))
        self.assertEqual([s['code'] for s in self.store.listing(self.host)['sessions']], [other])
        self.assertEqual(self.store.listing(self.friend)['sessions'], [])
        for action, body in [('players', {'name': 'Late player'}), ('paid', {'transfer_id': 1, 'paid': True})]:
            with self.assertRaises(server.UserError) as failure:
                self.call(self.path + action, body)
            self.assertEqual(failure.exception.status, 404)

    def test_delete_retries_after_lost_reply_and_revision_conflict(self):
        self.populated()
        self.api.conflict = lambda row: row.update(revision=row['revision'] + 1)
        self.api.lose_reply = True
        key = 'delete-request-lost-response'
        with self.assertRaises(CloudError):
            self.call(self.path + 'delete', {}, key=key)
        self.store = SupabaseStore(self.api, server)
        self.assertEqual(self.call(self.path + 'delete', {}, key=key), {'code': self.code, 'deleted': True})
        self.assertEqual(self.api.rows, {})

    def test_stale_cloud_write_cannot_restore_a_deleted_session(self):
        self.populated()
        def delete_during_write(row):
            self.call(self.path + 'delete', {})
        self.api.conflict = delete_during_write
        with self.assertRaises(server.UserError) as failure:
            self.call(self.path + 'players', {'name': 'Late'})
        self.assertEqual(failure.exception.status, 404)
        self.assertIsNone(self.api.get(self.code))

    def test_cloud_delete_is_filtered_by_session_owner_and_revision(self):
        api = SupabaseAPI('https://example.com', 'sb_secret_test')
        row = self.api.get(self.code)
        with patch.object(api, 'request', return_value=[{'session_code': self.code}]) as request:
            self.assertTrue(api.delete(row))
        self.assertEqual(request.call_args.args, ('DELETE', {
            'session_code': 'eq.' + self.code, 'owner_hash': 'eq.' + self.host,
            'revision': 'eq.0', 'select': 'session_code'}))

    def test_delete_rejects_reusing_another_mutations_request_id(self):
        key = 'request-already-used-for-buyin'
        self.call(self.path + 'players', {'name': 'A'}, key=key)
        with self.assertRaises(server.UserError) as failure:
            self.call(self.path + 'delete', {}, key=key)
        self.assertEqual(failure.exception.status, 409)
        self.assertIsNotNone(self.api.get(self.code))

    def test_history_beyond_1000_sessions_keeps_every_game_and_access_scope(self):
        self.call(self.path + 'players', {'name': 'Old regular', 'amount': '100'})
        original = self.store.get(self.code, self.host)
        with patch.object(server, 'now', return_value='2026-10-11T01:00:00+00:00'):
            for i in range(1005):
                self.call('/api/sessions', {'name': 'Game %d' % i})
            member_code = self.call('/api/sessions', {'name': 'Joined game'}, self.friend)['code']
            self.call('/api/sessions/' + member_code + '/join', {'name': 'Host as player'}, self.host)
            self.call('/api/sessions', {'name': 'Private game'}, self.friend)
        codes, offset = [], 0
        while offset is not None:
            page = self.store.listing(self.host, offset)
            self.assertLessEqual(len(page['sessions']), 50)
            codes.extend(s['code'] for s in page['sessions'])
            offset = page['next_offset']
        self.assertEqual(len(codes), 1007)
        self.assertEqual(len(set(codes)), 1007)
        self.assertIn(member_code, codes)
        self.assertEqual(self.store.get(self.code, self.host), original)
        self.assertEqual(self.store.listing('outsider')['sessions'], [])
        self.assertIn('Old regular', self.store.saved_players(self.host)['players'])

    def test_saved_players_persist_across_sessions_and_exclude_private_and_demo_names(self):
        self.call(self.path + 'players', {'name': 'Zoe', 'amount': '100'})
        self.call(self.path + 'players', {'name': 'rayner', 'amount': '50'})
        sample = self.call('/api/sessions', {'name': 'Sample'})['code']
        self.call('/api/sessions/' + sample + '/players', {'name': 'Sarah'})
        metadata(self.api.rows[sample])['demo'] = True
        private = self.call('/api/sessions', {'name': 'Private'}, self.friend)['code']
        self.call('/api/sessions/' + private + '/players', {'name': 'Private person'}, self.friend)
        joined = self.call('/api/sessions', {'name': 'Joined'}, self.friend)['code']
        self.call('/api/sessions/' + joined + '/players', {'name': 'Amy'}, self.friend)
        self.call('/api/sessions/' + joined + '/join', {'name': 'Host'}, self.host)
        fresh = self.call('/api/sessions', {'name': 'Next session'})['code']
        self.store = SupabaseStore(self.api, server)
        names = self.store.saved_players(self.host)['players']
        self.assertEqual(names, sorted(set(server.STARTER_PLAYERS) | {'Zoe', 'Amy', 'Host'}, key=str.casefold))
        self.assertEqual(self.store.saved_players('outsider')['players'], list(server.STARTER_PLAYERS))
        self.assertEqual(self.store.get(fresh, self.host)['players'], [])
        self.assertEqual(self.store.get(fresh, self.host)['total_buyin'], 0)

    def test_cloud_roster_reads_only_accessible_sessions_and_player_data(self):
        api = SupabaseAPI('https://example.com', 'sb_secret_test')
        with patch.object(api, 'request', return_value=[]) as request:
            api.roster_rows(self.host, 1000)
        params = request.call_args.kwargs['params']
        self.assertEqual(params['offset'], '1000')
        self.assertEqual(params['select'], 'players')
        self.assertIn('owner_hash.eq.' + self.host, params['or'])
        self.assertIn('member_hashes.cs.{%s}' % self.host, params['or'])

    def test_cloud_history_requests_a_bounded_page_with_stable_order(self):
        api = SupabaseAPI('https://example.com', 'sb_secret_test')
        with patch.object(api, 'request', return_value=[]) as request:
            api.list(self.host, 1000, 51)
        params = request.call_args.kwargs['params']
        self.assertEqual(params['offset'], '1000')
        self.assertEqual(params['limit'], '51')
        self.assertEqual(params['order'], 'created_at.desc,session_code.desc')
        self.assertIn('owner_hash.eq.' + self.host, params['or'])
        self.assertNotIn('*', params['select'])

    def test_receipt_survives_new_store_instance(self):
        a, b = self.populated()
        key = 'buyin-request-id-123'
        body = {'player_id': a, 'amount': '50'}
        self.call(self.path + 'buyin', body, key=key)
        self.store = SupabaseStore(self.api, server)
        state = self.call(self.path + 'buyin', body, key=key)
        self.assertEqual(state['players'][0]['buyin'], 15000)
        with self.assertRaises(server.UserError):
            self.call(self.path + 'buyin', {'player_id': a, 'amount': '75'}, key=key)

    def test_rename_preserves_payments_permissions_and_replays_safely(self):
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
        self.api.lose_reply = True
        body, key = {'player_id': first, 'name': 'Taylor'}, 'rename-lost-response-123'
        with self.assertRaises(CloudError):
            self.call(self.path + 'rename', body, key=key)
        self.store = SupabaseStore(self.api, server)
        after = self.call(self.path + 'rename', body, key=key)
        self.assertEqual(after['status'], 'settled')
        self.assertEqual(after['settled_at'], before['settled_at'])
        self.assertEqual(after['transfers'], before['transfers'])
        self.assertEqual(after['players'][0], dict(before['players'][0], name='Taylor'))
        self.assertEqual(after['players'][1], before['players'][1])
        self.assertEqual(after['buyins'], [dict(b, name='Taylor') if b['player'] == first else b for b in before['buyins']])
        names = self.store.saved_players(self.host)['players']
        self.assertIn('Taylor', names)
        self.assertNotIn('Tyop', names)
        self.assertNotIn('Frend', names)

    def test_response_lost_after_commit_is_safe_to_retry(self):
        a, b = self.populated()
        self.api.lose_reply = True
        key = 'lost-response-id-123'
        with self.assertRaises(CloudError):
            self.call(self.path + 'buyin', {'player_id': a, 'amount': '50'}, key=key)
        result = self.call(self.path + 'buyin', {'player_id': a, 'amount': '50'}, key=key)
        self.assertEqual(result['players'][0]['buyin'], 15000)

    def test_concurrent_buyin_is_rebased_without_losing_either(self):
        a, b = self.populated()

        def competing_write(row):
            self.store.add_buyin(row, row['players'][0], '25')
            row['revision'] += 1

        self.api.conflict = competing_write
        state = self.call(self.path + 'buyin', {'player_id': a, 'amount': '50'})
        self.assertEqual(state['players'][0]['buyin'], 17500)
        self.assertEqual(state['total_buyin'], 27500)

    def test_cashouts_and_settlement_are_atomic(self):
        a, b = self.populated()
        before = self.api.get(self.code)
        with self.assertRaises(server.UserError):
            self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '150'}, {'player_id': b, 'amount': '49.99'}]})
        self.assertEqual(before, self.api.get(self.code))
        result = self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '150'}, {'player_id': b, 'amount': '50'}]})
        self.assertEqual(result['status'], 'settled')
        self.assertEqual(result['transfers'][0]['amount'], 5000)
        self.assertEqual(self.api.get(self.code)['total_cash_out_cents'], 20000)
        with self.assertRaises(server.UserError):
            self.call(self.path + 'buyin', {'player_id': a, 'amount': '10'})

    def test_paid_status_persists_can_be_corrected_and_requires_host_or_recipient(self):
        self.call(self.path + 'players', {'name': 'A', 'amount': '100'})
        state = self.call(self.path + 'join', {'name': 'B'}, self.friend)
        b = state['my_player_id']
        a = state['players'][0]['id']
        self.call(self.path + 'buyin', {'player_id': b, 'amount': '100'}, self.friend)
        with self.assertRaises(server.UserError):
            self.call(self.path + 'paid', {'transfer_id': 1, 'paid': True})
        state = self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '0'}, {'player_id': b, 'amount': '200'}]})
        transfer = state['transfers'][0]
        with self.assertRaises(server.UserError):
            self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': True}, 'outsider')
        self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': True}, self.friend)
        self.store = SupabaseStore(self.api, server)
        reopened = self.store.get(self.code, self.host)
        self.assertTrue(reopened['transfers'][0]['paid'])
        self.assertEqual(reopened['players'], state['players'])
        self.assertEqual(reopened['total_buyin'], 20000)
        self.assertEqual(self.store.listing(self.host)['sessions'][0]['unpaid_count'], 0)
        corrected = self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': False})
        self.assertEqual(corrected['transfers'][0], transfer)
        self.assertEqual(self.store.listing(self.host)['sessions'][0]['unpaid_count'], 1)
        with self.assertRaises(server.UserError):
            self.call(self.path + 'paid', {'transfer_id': transfer['id'], 'paid': 'false'})

    def test_concurrent_buyin_invalidates_stale_settlement(self):
        a, b = self.populated()

        def competing_write(row):
            self.store.add_buyin(row, row['players'][0], '25')
            row['revision'] += 1

        self.api.conflict = competing_write
        with self.assertRaises(server.UserError):
            self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '150'}, {'player_id': b, 'amount': '50'}]})
        result = self.store.get(self.code, self.host)
        self.assertEqual(result['status'], 'active')
        self.assertEqual(result['total_buyin'], 22500)
        self.assertTrue(all(p['cashout'] is None for p in result['players']))

    def test_player_authority_and_private_metadata(self):
        a, b = self.populated()
        state = self.call(self.path + 'join', {'name': 'Friend'}, self.friend)
        mine = state['my_player_id']
        self.call(self.path + 'buyin', {'player_id': mine, 'amount': '50'}, self.friend)
        for action, body in [('buyin', {'player_id': a, 'amount': '5'}), ('players', {'name': 'Other'}), ('settle', {})]:
            with self.assertRaises(server.UserError):
                self.call(self.path + action, body, self.friend)
        guest = self.store.get(self.code, 'guest')
        serialized = json.dumps(guest)
        for secret in (self.host, self.friend, '_ledger', 'device_hash', 'requests'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.store.listing('guest')['sessions'], [])
        self.assertEqual(len(self.store.listing(self.friend)['sessions']), 1)

    def test_rebuy_removal_and_zero_cashout(self):
        a, b = self.populated()
        self.call(self.path + 'cashouts', {'values': [{'player_id': a, 'amount': '0'}]})
        result = self.call(self.path + 'buyin', {'player_id': a, 'amount': '20.25'})
        self.assertIsNone(result['players'][0]['cashout'])
        original_entry = dict(result['buyins'][0])
        entry = result['buyins'][0]['id']
        result = self.call(self.path + 'undo', {'entry_id': entry})
        self.assertEqual(result['players'][0]['buyin'], 10000)
        self.assertTrue(result['buyins'][0]['voided'])
        self.assertEqual(result['buyins'][0], dict(original_entry, voided=1))
        result = self.call(self.path + 'settle', {'values': [{'player_id': a, 'amount': '0'}, {'player_id': b, 'amount': '200'}]})
        self.assertEqual(result['transfers'][0]['amount'], 10000)
        self.store = SupabaseStore(self.api, server)
        reopened = self.store.get(self.code, self.host)
        self.assertEqual(reopened['buyins'][0], dict(original_entry, voided=1))
        self.assertEqual(self.store.listing(self.host)['sessions'][0]['total_buyin'], 20000)

    def test_import_preserves_ids_ownership_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            old = server.DB_PATH
            try:
                server.DB_PATH = Path(folder) / 'ledger.sqlite3'
                server.initialise()
                with server.database() as db:
                    code = server.mutate(db, '/api/sessions', {'name': 'Local'}, self.host)['code']
                    path = '/api/sessions/' + code + '/'
                    server.mutate(db, path + 'players', {'name': 'Imported', 'amount': '99.50'}, self.host)
                with server.database() as db:
                    self.assertEqual(self.store.import_local(db), (1, 0))
                self.assertEqual(self.store.get(code, self.host)['players'][0]['buyin'], 9950)
                self.call(path + 'buyin', {'player_id': 1, 'amount': '50'})
                with server.database() as db:
                    self.assertEqual(self.store.import_local(db), (0, 1))
                self.assertEqual(self.store.get(code, self.host)['players'][0]['buyin'], 14950)
            finally:
                server.DB_PATH = old


class ConfigTests(unittest.TestCase):
    def test_env_is_data_and_does_not_override_exported_variables(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'EXISTING': 'kept'}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('EXISTING=changed\nNEW="literal $(no-shell)"\nSCHEMA=public # comment\n')
            load_env(path)
            self.assertEqual(os.environ['EXISTING'], 'kept')
            self.assertEqual(os.environ['NEW'], 'literal $(no-shell)')
            self.assertEqual(os.environ['SCHEMA'], 'public')

    def test_refuses_insecure_url_and_public_key(self):
        for url, key in [('http://example.com', 'sb_secret_placeholder'), ('https://example.com/path', 'sb_secret_placeholder'), ('https://example.com', 'sb_publishable_placeholder')]:
            with self.assertRaises(CloudError):
                SupabaseAPI(url, key)


if __name__ == '__main__':
    unittest.main()
