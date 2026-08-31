#!/bin/bash
# Run via cron on the deployed VM to keep press-derived return dates fresh.
# See DEPLOYMENT.md for the crontab entry. -T disables pseudo-TTY allocation,
# required for docker compose exec to work correctly from a non-interactive
# cron context.
#
# Unlike the injury scrape, this one *can* run on the server: it reads public
# RSS feeds rather than a site that blocks datacentre IPs. Schedule it after
# the FPL ingest, since the resolver matches against the player pool that
# ingest refreshes.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T api uv run fplquant-ingest-news --require-mentions
