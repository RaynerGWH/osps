"""Vercel Python entrypoint. All saved data stays in Supabase."""
import os

import server
from supabase_store import CloudError, SupabaseAPI, SupabaseStore


server.COOKIE_SECURE = True
server.CLOUD_STORE = None
configuration_error = None
try:
    url, key = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_SECRET_KEY')
    if not url or not key:
        raise CloudError('Set SUPABASE_URL and SUPABASE_SECRET_KEY in Vercel environment variables.', 503)
    server.CLOUD_STORE = SupabaseStore(
        SupabaseAPI(url, key, os.environ.get('SUPABASE_SCHEMA', 'public')), server)
except CloudError as error:
    configuration_error = error.message


class handler(server.Handler):
    def ready(self):
        if configuration_error is None:
            return True
        self.identity()
        self.respond(503, {'error': configuration_error})
        return False

    def do_GET(self):
        if self.ready():
            super().do_GET()

    def do_POST(self):
        if self.ready():
            super().do_POST()
