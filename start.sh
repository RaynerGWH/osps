#!/bin/sh
set -eu

# Hosted deployments must use durable cloud storage.
: "${SUPABASE_URL:?Set SUPABASE_URL in the hosting environment}"
: "${SUPABASE_SECRET_KEY:?Set SUPABASE_SECRET_KEY in the hosting environment}"
export COOKIE_SECURE="${COOKIE_SECURE:-1}"

exec python3 -B server.py --host 0.0.0.0 --port "${PORT:-10000}"
