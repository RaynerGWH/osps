-- Additive migration: creates one new table and its indexes.
-- Set target_schema to your existing Supabase schema before running.
-- This file is prepared only. It has not been applied to your database.
-- Access is intended through a trusted backend, not a browser service key.

BEGIN;

DO $migration$
DECLARE
    target_schema text := 'public';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = target_schema) THEN
        RAISE EXCEPTION 'Schema % does not exist. Set target_schema to your existing schema.', target_schema;
    END IF;

    EXECUTE format($table$
        CREATE TABLE %I.poker_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_code text NOT NULL UNIQUE,
            name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 70),
            currency text NOT NULL DEFAULT 'SGD' CHECK (currency = 'SGD'),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'settled')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            settled_at timestamptz,
            owner_hash text NOT NULL CHECK (owner_hash ~ '^[a-f0-9]{64}$'),
            member_hashes text[] NOT NULL DEFAULT '{}',
            players jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(players) = 'array'),
            buy_ins jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(buy_ins) = 'array'),
            transfers jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(transfers) = 'array'),
            total_buy_in_cents bigint NOT NULL DEFAULT 0 CHECK (total_buy_in_cents >= 0),
            total_cash_out_cents bigint CHECK (total_cash_out_cents >= 0),
            minimum_proven boolean NOT NULL DEFAULT false,
            revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
            CONSTRAINT poker_settlement_balanced CHECK (
                (status = 'active' AND settled_at IS NULL)
                OR (status = 'settled' AND settled_at IS NOT NULL
                    AND total_cash_out_cents IS NOT NULL
                    AND total_buy_in_cents = total_cash_out_cents)
            )
        )
    $table$, target_schema);

    EXECUTE format('CREATE INDEX poker_sessions_owner_date_idx ON %I.poker_sessions (owner_hash, created_at DESC)', target_schema);
    EXECUTE format('CREATE INDEX poker_sessions_member_idx ON %I.poker_sessions USING gin (member_hashes)', target_schema);

    EXECUTE format('ALTER TABLE %I.poker_sessions ENABLE ROW LEVEL SECURITY', target_schema);
    EXECUTE format('REVOKE ALL ON TABLE %I.poker_sessions FROM PUBLIC, anon, authenticated', target_schema);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.poker_sessions TO service_role', target_schema);

    EXECUTE format('COMMENT ON TABLE %I.poker_sessions IS %L', target_schema,
        'Poker sessions with player buy-ins, final amounts, and settlement history. Amounts are integer SGD cents. Server access only.');
END
$migration$;

COMMIT;

-- Payload shapes (amounts are integer cents):
-- players: [{"id":1,"name":"febie","cashout":94500,"device_hash":null}]
-- buy_ins: [{"id":1,"player_id":1,"amount":50000,"created_at":"2026-10-11T12:00:00Z","voided":false}]
-- transfers: [{"from_id":8,"to_id":1,"amount":44500}]
-- Keep all original buy-ins, including voided entries.
-- A rebuy is a new buy_ins entry, not a replacement of a player's total.
-- The backend must validate player references, sums, permissions, and settlement.
-- Save atomically using WHERE id = ... AND revision = <last seen revision>,
-- increment revision, and set updated_at = now(). A zero-row update is a conflict.
-- Never store a raw browser token or service key in this table.
