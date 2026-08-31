"""Import past FPL seasons from a public archive, for model training.

Nothing in the app serves this data. It exists because the models this project
wants next — a learned minutes model first — need thousands of player-gameweeks
to train on, and the live database holds one season that is a fortnight old.
Waiting for it to fill up means waiting until midwinter to find out whether any
of this works.

The source is vaastav/Fantasy-Premier-League, an MIT-licensed archive of FPL's
own API responses going back to 2016-17. It is the same data this project's
ingest collects, recorded at the time and kept — which is exactly what cannot
be reconstructed after the fact (see `PlayerSnapshot` for the same problem
handled going forward).

Two things about the archive shape the import. Its schema drifts between
seasons: `position` and `team` appear in 2021-22, `starts` and the expected-goals
columns in 2022-23, so anything older is missing the label a minutes model
needs. And a double gameweek gives a player two rows in the same round, so the
row key has to include the fixture.
"""

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any

import requests
from sqlalchemy.orm import Session

from fplquant.config import settings
from fplquant.models.orm import HistoricalPlayerGameweek
from fplquant.utils import as_float

logger = logging.getLogger(__name__)

# Seasons carrying an explicit `starts` column, which is the label a minutes
# model is trained against. Earlier seasons can still be imported — the
# minutes-based fallback in `fplquant.lineup.starts.did_start` applies — but
# they are not the default, because a derived label and a published one are not
# the same evidence and mixing them silently is how a model ends up learning
# the rule you used to derive it.
SEASONS_WITH_STARTS = ("2022-23", "2023-24", "2024-25", "2025-26")

_INT_FIELDS = (
    "minutes",
    "total_points",
    "bps",
    "bonus",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "saves",
    "yellow_cards",
    "red_cards",
    "penalties_missed",
    "penalties_saved",
    "value",
    "selected",
    "transfers_in",
    "transfers_out",
)
_FLOAT_FIELDS = ("influence", "creativity", "threat", "ict_index")
# Absent before 2022-23, and a genuine absence rather than a zero — stored as
# NULL so a model can tell "no chances created" from "not measured that year".
_OPTIONAL_FLOAT_FIELDS = ("expected_goals", "expected_assists", "expected_goals_conceded")
# The fixture scoreline, repeated on every player row in that match. Cheap to
# carry and not reconstructable from anything else here, and without it a
# replayed season has no played fixtures at all: `engine.rates.played_fixtures`
# requires a scoreline, so a backtest built on an archive missing these fits
# nothing and returns every club's prior for every gameweek.
# Absent before 2021-22 in some exports, hence optional.
_OPTIONAL_INT_FIELDS = (
    "team_h_score",
    "team_a_score",
    # Defensive Contribution and its components, published from 2025-26 —
    # NULL for earlier seasons, where the rule did not exist and a zero would
    # be a lie rather than a measurement.
    "defensive_contribution",
    "clearances_blocks_interceptions",
    "recoveries",
    "tackles",
)
# The archive's `xP` column. Stored, but not a pre-deadline projection — see
# `HistoricalPlayerGameweek.expected_points` and `fplquant.backtest.replay`.
_XP_COLUMN = "xP"


@dataclass(frozen=True)
class ImportResult:
    season: str
    rows_read: int
    rows_written: int


def season_url(season: str) -> str:
    return f"{settings.fpl_history_base_url}/data/{season}/gws/merged_gw.csv"


def fetch_season_csv(season: str) -> str:
    """Download one season's merged gameweek file."""
    url = season_url(season)
    logger.info("Fetching %s", url)
    response = requests.get(url, timeout=settings.http_timeout_seconds)
    response.raise_for_status()
    return response.text


def _as_int(value: Any) -> int:
    """Ints in the archive arrive as strings, occasionally blank, and
    occasionally as floats ("3.0") where a season's exporter differed."""
    if value in (None, ""):
        return 0
    return int(float(value))


def _optional_float(row: dict[str, str], field: str) -> float | None:
    value = row.get(field)
    return None if value in (None, "") else float(value)


def _optional_int(row: dict[str, str], field: str) -> int | None:
    """A column absent from this season's export, versus a measured zero.

    The distinction carries real information here: Defensive Contribution did
    not exist before 2025-26, so a zero in an older season would claim a player
    made no defensive actions when in fact nobody was counting.
    """
    value = row.get(field)
    return None if value in (None, "") else int(float(value))


def _parse_kickoff(value: str | None) -> Any:
    if not value:
        return None
    import datetime as dt

    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_rows(season: str, csv_text: str) -> list[dict[str, Any]]:
    """Turn one season's CSV into rows ready for insertion.

    Rows missing the identifying key are dropped rather than guessed at: a row
    with no element or no fixture cannot be deduplicated on re-import, and one
    bad line is not worth corrupting the uniqueness of the table for.
    """
    rows: list[dict[str, Any]] = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        if not raw.get("element") or not raw.get("fixture"):
            continue
        # `GW` is the archive's own gameweek column; `round` comes from FPL's
        # per-player summary and can be absent in some seasons.
        gameweek = raw.get("GW") or raw.get("round")
        if not gameweek:
            continue

        row: dict[str, Any] = {
            "season": season,
            "element": _as_int(raw["element"]),
            "round": _as_int(gameweek),
            "fixture": _as_int(raw["fixture"]),
            "name": raw.get("name", ""),
            "position": raw.get("position") or None,
            "team": raw.get("team") or None,
            "opponent_team": _as_int(raw["opponent_team"]) if raw.get("opponent_team") else None,
            "was_home": raw.get("was_home", "").lower() == "true" if raw.get("was_home") else None,
            "kickoff_time": _parse_kickoff(raw.get("kickoff_time")),
            "starts": _as_int(raw["starts"]) if raw.get("starts") not in (None, "") else None,
        }
        for field in _INT_FIELDS:
            row[field] = _as_int(raw.get(field))
        for field in _FLOAT_FIELDS:
            row[field] = as_float(raw.get(field))
        for field in _OPTIONAL_FLOAT_FIELDS:
            row[field] = _optional_float(raw, field)
        for field in _OPTIONAL_INT_FIELDS:
            row[field] = _optional_int(raw, field)
        row["expected_points"] = _optional_float(raw, _XP_COLUMN)
        rows.append(row)
    return rows


def import_season(session: Session, season: str, csv_text: str | None = None) -> ImportResult:
    """Load one season into `historical_player_gameweeks`, replacing any prior import.

    Delete-then-insert rather than upsert row by row: this is a static archive
    of a finished season, so a re-import means the source changed or the
    previous run was interrupted, and in both cases the whole season is what
    you want replaced. It also keeps the import a single fast bulk insert
    instead of ~30,000 individual lookups.
    """
    csv_text = csv_text if csv_text is not None else fetch_season_csv(season)
    rows = parse_rows(season, csv_text)

    session.query(HistoricalPlayerGameweek).filter(
        HistoricalPlayerGameweek.season == season
    ).delete()
    # Deduplicate within the file itself; the archive has been known to repeat
    # a row, and the unique constraint would otherwise fail the whole season.
    unique: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in rows:
        unique[(row["element"], row["round"], row["fixture"])] = row
    session.bulk_insert_mappings(HistoricalPlayerGameweek, list(unique.values()))
    session.flush()

    return ImportResult(season=season, rows_read=len(rows), rows_written=len(unique))


def import_seasons(session: Session, seasons: tuple[str, ...] | list[str]) -> list[ImportResult]:
    results = []
    for season in seasons:
        result = import_season(session, season)
        logger.info(
            "%s: %d rows read, %d written", result.season, result.rows_read, result.rows_written
        )
        results.append(result)
    return results
