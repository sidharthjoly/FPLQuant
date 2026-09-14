#!/bin/bash
# Scrape Transfermarkt injury history here, and ship it to production.
#
# Transfermarkt refuses datacentre IPs. Measured 2026-08-31: 0 of 623 players
# resolved from the Oracle VM, 0 of 623 from a GitHub Actions runner, ~90% from
# a laptop on a home connection. So the scrape has to happen on a residential
# connection and the rows have to travel to a machine where it cannot. That is
# two steps, and until now only the first had a command that complained when it
# failed — which is how the laptop came to hold a 2026-09-06 scrape that
# production never saw, for eight days, with both databases answering happily.
#
# This is that pair of steps as one thing that either completes or fails loudly.
# Run it from a home connection; see DEPLOYMENT.md for the launchd agent that
# runs it weekly.
#
# The ordering is the safety story. Nothing touches production until the scrape
# has proved it resolved somebody and the generated SQL has proved it is whole:
# the export deletes each player's existing rows before inserting their new
# ones, so a truncated file applied over a good snapshot is the one outcome
# worse than doing nothing at all.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="${UV_BIN:-$HOME/.local/bin/uv}"
REMOTE="${FPLQUANT_REMOTE:-ubuntu@fplquant.duckdns.org}"
REMOTE_DIR="${FPLQUANT_REMOTE_DIR:-FPLQuant}"
# Deliberately not the repo root: a previous run left its export sitting there
# for a fortnight.
EXPORT="$(mktemp -t fplquant-injuries)"
trap 'rm -f "$EXPORT"' EXIT

cd "$REPO"

say() { printf '\n=== %s ===\n' "$1"; }
die() { printf '\nFAILED: %s\n' "$1" >&2; exit 1; }

say "$(date -u '+%Y-%m-%d %H:%M:%S UTC')  scraping from $(hostname)"

# --require-progress exits non-zero when the run reached a verdict on nobody.
# That is the signature of being blocked, and it is otherwise indistinguishable
# from a quiet week: both exit cleanly having written nothing.
"$UV" run fplquant-ingest-injuries --require-progress "$@" \
    || die "the scrape resolved nobody. If this machine is on a home connection and
this still happens, Transfermarkt has started blocking it too — check by hand
before trusting the next run. Production has NOT been touched."

say "exporting"
"$UV" run python scripts/export_injury_data.py > "$EXPORT" \
    || die "export failed. Production has NOT been touched."

# A truncated export is the dangerous case, and it is quiet: the file is valid
# SQL right up to wherever it stopped, and every DELETE before that point still
# runs. The transaction is only closed by COMMIT on the last line.
[ -s "$EXPORT" ] || die "the export is empty. Production has NOT been touched."
tail -1 "$EXPORT" | grep -qx 'COMMIT;' \
    || die "the export does not end in COMMIT; — it is truncated, and applying it
would delete rows it never replaces. Production has NOT been touched."

RECORDS=$(grep -c '^INSERT INTO injury_records' "$EXPORT" || true)
PLAYERS=$(grep -c '^UPDATE players' "$EXPORT" || true)
printf 'export holds %s injury records across %s players\n' "$RECORDS" "$PLAYERS"

say "backing up production"
# Root-owned because it is written inside the container; `*.db.bak*` is in
# .gitignore so `git clean -fd` on the next deploy leaves it alone. That
# combination broke a deploy on 2026-08-31 before the ignore rule existed.
ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
    "cd $REMOTE_DIR && docker compose exec -T api cp /app/data/fplquant.db /app/data/fplquant.db.bak-\$(date +%F)" \
    || die "could not reach $REMOTE to take a backup. Production has NOT been touched."

say "shipping"
scp -o BatchMode=yes -o ConnectTimeout=15 "$EXPORT" "$REMOTE:~/injuries.sql" \
    || die "scp failed. Production has NOT been touched."

say "applying"
# Through Python inside the container: the image ships no sqlite3 binary, and
# data/ is not writable by the login user.
ssh -o BatchMode=yes -o ConnectTimeout=60 "$REMOTE" "cd $REMOTE_DIR && docker compose exec -T api uv run python -c \"
import sqlite3, sys
con = sqlite3.connect('/app/data/fplquant.db')
before = con.execute('SELECT COUNT(*) FROM injury_records').fetchone()[0]
con.executescript(sys.stdin.read())
con.commit()
after = con.execute('SELECT COUNT(*) FROM injury_records').fetchone()[0]
matched = con.execute(\\\"SELECT COUNT(*) FROM players WHERE transfermarkt_lookup_status='matched'\\\").fetchone()[0]
newest = con.execute('SELECT MAX(start_date) FROM injury_records').fetchone()[0]
print(f'injury_records {before} -> {after}')
print(f'matched players: {matched}')
print(f'newest injury: {newest}')
print('integrity:', con.execute('PRAGMA integrity_check').fetchone()[0])
if after == 0:
    sys.exit('applied and the table is empty — that cannot be right')
\" < ~/injuries.sql" || die "the apply failed. The backup taken above is at
/app/data/fplquant.db.bak-<today> inside the container."

say "done"
