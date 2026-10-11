import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import server


class VercelTests(unittest.TestCase):
    def setUp(self):
        self.previous_store, self.previous_secure = server.CLOUD_STORE, server.COOKIE_SECURE

    def tearDown(self):
        server.CLOUD_STORE, server.COOKIE_SECURE = self.previous_store, self.previous_secure

    def entrypoint(self, env):
        path = Path(__file__).resolve().parents[1] / 'api' / 'index.py'
        spec = importlib.util.spec_from_file_location('vercel_entrypoint_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, env, clear=True):
            spec.loader.exec_module(module)
        return module

    def request(self, module, path, body=None, headers=None):
        handler = object.__new__(module.handler)
        handler.path = path
        handler.headers = {'Host': 'poker.example', **(headers or {})}
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        if body is not None:
            encoded = json.dumps(body).encode()
            handler.rfile = io.BytesIO(encoded)
            handler.headers.update({'Content-Type': 'application/json', 'Content-Length': str(len(encoded)),
                                    'X-Request-Id': 'vercel-request-123456'})
        with patch.object(server, 'database', side_effect=AssertionError('Hosted request touched SQLite')):
            (handler.do_GET if body is None else handler.do_POST)()
        return handler, json.loads(handler.wfile.getvalue())

    def test_missing_credentials_fail_closed_for_reads_and_writes(self):
        module = self.entrypoint({})
        for path, body in [('/api/sessions', None), ('/api/sessions', {'name': 'Game'})]:
            handler, data = self.request(module, path, body)
            handler.send_response.assert_called_once_with(503)
            self.assertIn('environment variables', data['error'])
        self.assertIsNone(server.CLOUD_STORE)

    def test_cloud_routes_and_secure_cookie_without_local_disk(self):
        module = self.entrypoint({'SUPABASE_URL': 'https://example.com', 'SUPABASE_SECRET_KEY': 'sb_secret_test'})
        store = server.CLOUD_STORE = Mock()
        store.listing.return_value = {'sessions': [], 'next_offset': None}
        store.leaderboard.return_value = {'players': [], 'session_count': 0, 'total_buyin': 0}
        store.get.return_value = {'code': 'example', 'players': []}
        for path in ('/api/sessions?offset=50', '/api/leaderboard', '/api/sessions/example'):
            handler, _ = self.request(module, path)
            handler.send_response.assert_called_once_with(200)
            cookies = [call.args[1] for call in handler.send_header.call_args_list if call.args[0] == 'Set-Cookie']
            self.assertIn('; Secure', cookies[0])
            self.assertIn('HttpOnly', cookies[0])
        self.assertEqual(store.listing.call_args.args[1], 50)
        handler, _ = self.request(module, '/api/not/a/route')
        handler.send_response.assert_called_once_with(404)
        store.mutate.return_value = {'code': 'new'}
        handler, data = self.request(module, '/api/sessions', {'name': 'Game'}, {'Origin': 'https://poker.example'})
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(data['code'], 'new')
        self.assertEqual(store.mutate.call_args.args[0], '/api/sessions')
        self.assertEqual(store.mutate.call_args.args[3], 'vercel-request-123456')

    def test_cross_origin_write_is_still_rejected(self):
        module = self.entrypoint({'SUPABASE_URL': 'https://example.com', 'SUPABASE_SECRET_KEY': 'sb_secret_test'})
        store = server.CLOUD_STORE = Mock()
        handler, _ = self.request(module, '/api/sessions', {'name': 'Game'}, {'Origin': 'https://another.example'})
        handler.send_response.assert_called_once_with(403)
        store.mutate.assert_not_called()
