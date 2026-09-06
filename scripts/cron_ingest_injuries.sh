#!/bin/bash
# Run via cron on the deployed VM — resolves/syncs Transfermarkt injury data.
# Weekly, not daily: this scrapes the full player pool at ~1.5s/request, see
# DEPLOYMENT.md and .github/workflows/ingest_injuries.yml for why.
set -euo pipefail
cd "$(dirname "$0")/.."
# --require-progress exits non-zero when the run resolved nobody. On the VM
# that is the expected outcome, not a rare one: Transfermarkt blocks datacentre
# IPs, so this cron cannot succeed where it runs and must not look like it did.
# The table is already full of a hand-applied snapshot, so "rows exist" proves
# nothing — see DEPLOYMENT.md for moving a fresh scrape up from a laptop.
docker compose exec -T api uv run fplquant-ingest-injuries --require-progress
