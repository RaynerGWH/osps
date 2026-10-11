# o$p$

A small session ledger with buy-ins, rebuys, final chip values, and settlement.

## Run locally

Python 3.9 or newer is enough. No packages need installing.

```sh
python3 -B server.py --port 8765
```

Open http://127.0.0.1:8765. The top navigation has **Current session**, **History**, and **New session**. Use **Add buy-in** to add a player or another buy-in. Click a player's name to view or remove their entries. **Settle up** accepts final chip values for everyone and produces a payment plan. **Copy summary** copies the results and payment statuses.

The interface uses Source Serif 4 for headings and Open Sans for controls and amounts. These are locally hosted, open-source alternatives for the requested serif and sans-serif pairing, not JPMorganChase's proprietary font files. Their font licenses are in `static/fonts/`. Page scrolling stays native with hidden scrollbars, and entrance animations respect reduced-motion preferences.

New sessions default to the current Singapore date, such as `11 Oct 2026`. The name is editable before starting a session.

The player picker starts with Aloysius, Arden, Brendon, Byron, Febie, Isaiah, Min, Rayner, Vernon, and ZS, sorted alphabetically. Search for a name, select one, or type a new name. Choosing someone already at the table adds another buy-in to their existing entry. Each new session starts with its own amounts.

Names are remembered from non-sample sessions hosted or joined by the current browser, using the existing Supabase data (or SQLite locally). New names become available after they are saved in a session. Capitalisation variants are combined in the picker, and unrelated private sessions are excluded. No new table or migration is needed. The saved list reuses names; it does not grant another player's permissions or create a cross-device account.

**Buy-in history** below the table shows every entry, newest first, with the player, amount, date, and Singapore time. Removed entries remain visible and are excluded from totals. The same log remains available after settlement.

**Session history** lists saved games with their date, status, and total buy-ins. Open any game to see its full ledger and settlement. Older games load in pages, with no lifetime session limit or automatic expiry. Starting a new session leaves all earlier sessions intact. Records remain in the configured database; they do not depend on keeping the app open.

Each settlement transfer has a **Paid** checkbox. The host or the receiving player can mark it paid or undo a mistaken tick. Other players can see its status. These changes persist with the session, and copied settlements include PAID or UNPAID beside each transfer. Session history shows the number of unpaid transfers, or **All paid** when none remain. Ticking a payment only records its status; it does not transfer money.

The host can enter all amounts. Players can join through a session URL and manage their own entries. Use the same browser to retain session access. There is no account recovery or cross-device login yet.

## Money and settlement

- All values are integer SGD cents.
- A player's net result is their final chip value minus all non-voided buy-ins.
- Final amounts must match total buy-ins exactly. A missing amount differs from an explicit zero.
- Settlement is atomic and locks the session. It does not send payments.
- The exact solver proves the fewest transfers for up to 16 players with nonzero balances. Above that size, it uses debt matching and only labels a result "proven minimum" when it meets a mathematical lower bound.
- Repeated requests cannot duplicate a buy-in. Removed entries remain in the log.

## Storage and Supabase

The server loads `.env` at startup. When `SUPABASE_URL` and `SUPABASE_SECRET_KEY` are set, Supabase is the source of truth for every session, buy-in, cash-out, and transfer. The browser keeps an opaque session-access cookie; it does not store the ledger. The server keeps the secret key out of browser responses and access logs.

`supabase/create_poker_sessions.sql` is the additive migration for one Supabase table. Set `target_schema` to the existing schema before first use. It adds `poker_sessions` and two indexes without modifying unrelated tables. A name collision fails the transaction instead of altering an existing table. Once the table exists, do not rerun the creation script.

Fill in the project-root `.env` file locally:

- `SUPABASE_URL`: your Supabase project URL.
- `SUPABASE_SECRET_KEY`: a server-side secret key or legacy `service_role` key.
- `SUPABASE_SCHEMA`: the schema used by the migration, defaulting to `public`.

The `.env` file is excluded from Git; `.env.example` is a blank reference. Shell environment settings take precedence over `.env`. Partially configured or unreachable cloud storage raises an error instead of silently saving to a different database.

```sh
python3 -B server.py --check-storage
python3 -B server.py --import-local
python3 -B server.py --port 8765
```

`--import-local` copies missing sessions from `data/ledger.sqlite3`, preserving their URLs and browser ownership. It never overwrites an existing cloud session and is safe to rerun. A local copy is kept in `data/ledger-before-supabase.sqlite3`. Without Supabase settings, the app uses SQLite. `--local` explicitly selects the local database for offline development; cloud changes are not mirrored back to it.

The table stores each session's metadata, player list, individual buy-ins, final amounts, and computed transfers. Updates compare and increment the row revision, so simultaneous edits retry against the latest data. Request receipts in private player JSON metadata prevent duplicate buy-ins after a lost response or server restart. Backend access is restricted to the service role. The application checks the host or player identity before every write; raw table data and device hashes never go to the browser. See [Supabase row security](https://supabase.com/docs/guides/database/postgres/row-level-security).

## Deploy from GitHub

The repository holds the application code. [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages) only serves static websites, so this app needs a Python web host for its API and server-side Supabase connection.

The included [Render Blueprint](https://render.com/docs/blueprint-spec), `render.yaml`, configures the app in Singapore with HTTPS and secure cookies:

1. In Render, choose **New > Blueprint**, connect GitHub, and select this repository.
2. Enter `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and `SUPABASE_SCHEMA` from your local `.env`. Use `public` for the schema if that is where the existing table lives.
3. Deploy and open the HTTPS URL Render provides.

The existing Supabase table and saved sessions are reused. Do not rerun the table creation migration. Credentials belong in Render's environment settings and stay out of GitHub. The startup script requires Supabase settings so a deployment cannot silently fall back to temporary local storage.

The configuration selects Render's free plan. [Free web services sleep after 15 minutes without inbound traffic](https://render.com/docs/free), so the first visit after a break can take longer. Supabase still retains the game data. Later pushes to `main` redeploy after the GitHub checks pass.

Session ownership currently relies on a browser cookie. A new hosted domain cannot read the cookie from localhost, so existing games will not automatically appear in its history and their host controls will not carry over. Keep the local app available for existing games; start new games on the hosted URL. Account login or a secure ownership-transfer feature is needed to move those controls between domains.

For another Python host, provide the same Supabase settings and run `sh start.sh` behind its HTTPS reverse proxy. The script uses the host's `PORT`, defaulting to `10000`. Local development remains `python3 -B server.py --port 8765`.

## Checks

```sh
python3 -B -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_client.mjs
```

Tests cover the supplied eight-player example, exact transfer optimality against an independent exhaustive solver, cents, rounding boundaries, rebuys, corrections, permissions, atomic settlement, history, replay protection, cross-origin writes, concurrent cloud edits, lost responses, and non-destructive local imports. Cloud adapter tests use an isolated in-memory service and never read credentials or write to the real Supabase project.
