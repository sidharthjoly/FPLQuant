"""Export scraped injury data as SQL, for applying to a database elsewhere.

Transfermarkt blocks datacentre IPs. Measured, not assumed: a full run from the
Oracle VM resolved 0 of 623 players, and the weekly GitHub Actions job has been
returning 623 unmatched and 0 injury records since at least mid-August — while
reporting success, because nothing checked. The same code from a residential
connection matches players and pulls their history without trouble.

That leaves the scrape needing to happen somewhere it works and the rows needing
to get somewhere it doesn't. This writes the three things a scrape produces —
the Transfermarkt id/slug match, the nationality, and the injury records — as a
SQL script keyed on FPL player id, which is stable within a season and is what
both databases agree on.

    uv run python scripts/export_injury_data.py > injuries.sql
    scp injuries.sql ubuntu@fplquant.duckdns.org:~/
    ssh ubuntu@fplquant.duckdns.org \\
        'docker compose -f FPLQuant/docker-compose.yml exec -T api \\
         sqlite3 /app/data/fplquant.db < injuries.sql'

Idempotent: it clears each exported player's existing records before inserting,
so re-running replaces rather than duplicates.
"""

import sys

from fplquant.models.base import session_scope
from fplquant.models.orm import Player


def _sql_str(value: object) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def main() -> None:
    out = sys.stdout
    with session_scope() as session:
        matched = session.query(Player).filter_by(transfermarkt_lookup_status="matched").all()
        if not matched:
            print(
                "No matched players to export. Run fplquant-ingest-injuries first, "
                "from a connection Transfermarkt does not block.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        records = 0
        out.write("BEGIN;\n")
        for player in matched:
            # Keyed on fpl_id, not the local primary key: the two databases
            # assign their own row ids and only agree on FPL's.
            out.write(
                "UPDATE players SET "
                f"transfermarkt_id = {player.transfermarkt_id}, "
                f"transfermarkt_slug = {_sql_str(player.transfermarkt_slug)}, "
                f"transfermarkt_lookup_status = 'matched', "
                f"nationality = {_sql_str(player.nationality)} "
                f"WHERE fpl_id = {player.fpl_id};\n"
            )
            out.write(
                "DELETE FROM injury_records WHERE player_id = "
                f"(SELECT id FROM players WHERE fpl_id = {player.fpl_id});\n"
            )
            for record in player.injury_records:
                out.write(
                    "INSERT INTO injury_records "
                    "(player_id, season, injury_type, start_date, end_date, "
                    "days_out, games_missed) SELECT id, "
                    f"{_sql_str(record.season)}, {_sql_str(record.injury_type)}, "
                    f"{_sql_str(record.start_date)}, {_sql_str(record.end_date)}, "
                    f"{record.days_out if record.days_out is not None else 'NULL'}, "
                    f"{record.games_missed if record.games_missed is not None else 'NULL'} "
                    f"FROM players WHERE fpl_id = {player.fpl_id};\n"
                )
                records += 1
        out.write("COMMIT;\n")

    print(
        f"-- {len(matched)} matched players, {records} injury records",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
