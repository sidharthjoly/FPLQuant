"""Score the engine against the season currently being played.

`replay.py` replays the *archive* — four finished seasons, one row per
player-fixture in `historical_player_gameweeks`. This does the same job for
26/27, which lives in the live tables instead (`player_gameweek_stats`,
`fixtures`, `players`, `teams`). The engine itself is untouched: as in the
archive replay, the world is rebuilt as it stood before a deadline and
`project_horizon` is asked for its answer, so whatever ships is what is
measured.

Three things make this replay different from the archive one, and all three
matter when reading its numbers.

**It is a very small sample.** `replay.DEFAULT_FIRST_ROUND` is 6 because the
author concluded earlier rounds measure little. This season has nowhere near
that, so every figure here carries wide error bars and round 1 is reported
separately: with no prior gameweek, the rolling-mean baseline is zero for
everyone and the comparison degenerates.

**Only complete rounds are scored.** A gameweek still being played has rows for
the fixtures already finished and none for the rest, so every player awaiting a
kickoff reads as a genuine zero. Scoring that measures the fixture list, not the
model. `complete_rounds` gates on every fixture in the event carrying a
scoreline.

**The minutes model has not seen this season.** In the archive replay
`use_minutes_model` is a diagnostic, because the model was fitted on those very
seasons. 26/27 is not in its training set — the archive stops at 2025-26 — so
here the learned component can be evaluated honestly, end to end, for the first
time. That is the one measurement this replay can make that the archive one
structurally cannot.

The same two honest limits as the archive path apply. Team strength ratings are
published by FPL as zero for most of a season, so the goal model falls back to
squad value either way. And availability is *not* reconstructed: `status` is set
to available for everyone, because the live `players.status` describes today,
not the Tuesday before round 2 — using it would leak an injury backwards into a
week the player was fit. `player_snapshots` looked like the fix for this and is
not: its rows are written by the daily ingest whenever it runs, so the one
labelled `next_event=3` was captured on 2026-09-06, part-way through round 3.
"""

import datetime as dt
import logging
import statistics
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session, sessionmaker

from fplquant.backtest.replay import ROLLING_WINDOW, TOP_N, MethodScore, RoundResult, _score
from fplquant.engine.horizon import project_horizon
from fplquant.engine.minutes import compute_minutes_profiles
from fplquant.models.base import Base, make_engine
from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team

logger = logging.getLogger(__name__)

CURRENT_SEASON = "26/27"

# Columns copied verbatim from a source gameweek row into the rebuilt one. The
# engine fits goal rates against these, so an omission here is silent: the
# replay would still run and would simply measure a thinner model.
_STAT_COLUMNS = (
    "round",
    "fixture_fpl_id",
    "opponent_team_fpl_id",
    "was_home",
    "kickoff_time",
    "minutes",
    "total_points",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "bonus",
    "bps",
    "influence",
    "creativity",
    "threat",
    "ict_index",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "value",
    "selected",
    "starts",
    "defensive_contribution",
    "clearances_blocks_interceptions",
    "recoveries",
    "tackles",
)

_TEAM_COLUMNS = (
    "strength_overall_home",
    "strength_overall_away",
    "strength_attack_home",
    "strength_attack_away",
    "strength_defence_home",
    "strength_defence_away",
)


def complete_rounds(session: Session) -> list[int]:
    """Rounds whose every fixture has been played and carries a scoreline.

    A round still in progress is excluded rather than scored on what has
    finished so far. Half a gameweek looks exactly like a gameweek in which
    half the league was dropped, and nothing downstream could tell the
    difference.
    """
    played: dict[int, list[bool]] = {}
    for fixture in session.query(Fixture).all():
        if fixture.event is None:
            continue
        played.setdefault(fixture.event, []).append(fixture.team_h_score is not None)
    return sorted(event for event, flags in played.items() if flags and all(flags))


def hydrate_current(source: Session, up_to_round: int) -> tuple[Session, dict[int, int]]:
    """An in-memory season as it stood before `up_to_round`'s deadline.

    Returns the session and a map from FPL element id to the rebuilt player's
    primary key. Rounds strictly before `up_to_round` arrive as played fixtures
    with gameweek history; `up_to_round` itself is the upcoming fixture, with no
    scoreline and no stats. Nothing from `up_to_round` or later reaches a player
    row — price included, which comes from the most recent earlier round,
    because that is when it became knowable.
    """
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    teams_by_source_id: dict[int, Team] = {}
    for row in source.query(Team).all():
        team = Team(
            fpl_id=row.fpl_id,
            name=row.name,
            short_name=row.short_name,
            **{column: getattr(row, column) for column in _TEAM_COLUMNS},
        )
        session.add(team)
        teams_by_source_id[row.id] = team
    session.flush()

    stats = source.query(PlayerGameweekStat).all()
    # Price and identity as of the last round before the one being predicted,
    # plus a (player, round) index so the debutant fallback below is a lookup
    # rather than a scan of every stat row per player.
    latest: dict[int, PlayerGameweekStat] = {}
    by_player_round: dict[tuple[int, int], PlayerGameweekStat] = {}
    for stat in stats:
        by_player_round.setdefault((stat.player_id, stat.round), stat)
        if stat.round >= up_to_round:
            continue
        seen = latest.get(stat.player_id)
        if seen is None or stat.round > seen.round:
            latest[stat.player_id] = stat

    players: dict[int, Player] = {}
    elements: dict[int, int] = {}
    for source_player in source.query(Player).all():
        club = teams_by_source_id.get(source_player.team_id)
        if club is None:
            continue
        prior = latest.get(source_player.id)
        # A debutant has no earlier round to price from. The archive replay
        # falls back to the round's own value for exactly this case; mirroring
        # it keeps the two backtests comparable, and a price is the one column
        # where the round itself is very nearly the round before.
        if prior is None:
            prior = by_player_round.get((source_player.id, up_to_round))
        player = Player(
            fpl_id=source_player.fpl_id,
            team_id=club.id,
            first_name=source_player.first_name,
            second_name=source_player.second_name,
            web_name=source_player.web_name,
            element_type=source_player.element_type,
            now_cost=prior.value if prior is not None and prior.value else source_player.now_cost,
            # Availability is not reconstructed — see the module docstring.
            status="a",
            ep_next=0.0,
            birth_date=source_player.birth_date,
        )
        session.add(player)
        players[source_player.id] = player
        elements[source_player.fpl_id] = source_player.id
    session.flush()

    for source_fixture in source.query(Fixture).all():
        event = source_fixture.event
        if event is None or event > up_to_round:
            continue
        home = teams_by_source_id.get(source_fixture.team_h_id)
        away = teams_by_source_id.get(source_fixture.team_a_id)
        if home is None or away is None:
            continue
        behind_us = event < up_to_round
        session.add(
            Fixture(
                fpl_id=source_fixture.fpl_id,
                event=event,
                team_h_id=home.id,
                team_a_id=away.id,
                kickoff_time=source_fixture.kickoff_time or dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
                finished=behind_us,
                # The scoreline of the round being predicted is precisely the
                # future this replay exists to keep out.
                team_h_score=source_fixture.team_h_score if behind_us else None,
                team_a_score=source_fixture.team_a_score if behind_us else None,
                team_h_difficulty=source_fixture.team_h_difficulty,
                team_a_difficulty=source_fixture.team_a_difficulty,
            )
        )

    for stat in stats:
        if stat.round >= up_to_round or stat.player_id not in players:
            continue
        session.add(
            PlayerGameweekStat(
                player_id=players[stat.player_id].id,
                **{column: getattr(stat, column) for column in _STAT_COLUMNS},
            )
        )

    session.flush()
    return session, {
        element: players[pid].id for element, pid in elements.items() if pid in players
    }


@dataclass(frozen=True)
class RoundRecord:
    """Everything one replayed round yields, from a single hydrate.

    Scores, per-player predictions and start-probability calibration are three
    readings of the same projection, so they are produced together. Asking for
    them separately meant hydrating an in-memory season and re-running the
    engine three times over identical inputs.
    """

    round: int
    #: (fpl element id, predicted points, actual points)
    predictions: list[tuple[int, float, float]]
    #: (predicted start probability, actually started)
    starts: list[tuple[float, int]]
    #: point-in-time rolling mean of the player's last `ROLLING_WINDOW` rounds
    rolling: dict[int, float]

    def scores(self) -> dict[str, MethodScore]:
        if len(self.predictions) < TOP_N:
            return {}
        actual = np.array([row[2] for row in self.predictions], dtype=np.float64)
        engine = np.array([row[1] for row in self.predictions], dtype=np.float64)
        rolling = np.array(
            [self.rolling.get(row[0], 0.0) for row in self.predictions], dtype=np.float64
        )
        return {
            "engine": _score("engine", engine, actual),
            "rolling_mean": _score("rolling_mean", rolling, actual),
        }

    def result(self) -> RoundResult | None:
        scores = self.scores()
        if not scores:
            return None
        return RoundResult(
            season=CURRENT_SEASON,
            round=self.round,
            players=len(self.predictions),
            scores=scores,
        )


def replay_round_record(
    source: Session,
    round_number: int,
    use_minutes_model: bool = True,
) -> RoundRecord | None:
    """Rebuild the world before `round_number` and read the engine three ways."""
    actual_rows = (
        source.query(PlayerGameweekStat).filter(PlayerGameweekStat.round == round_number).all()
    )
    if not actual_rows:
        return None

    fpl_id_by_player = {p.id: p.fpl_id for p in source.query(Player).all()}

    # A double gameweek is two rows; the outcome is their sum, which is also
    # what the projection produces for the event. A player who started either
    # match counts as having started.
    actual_by_element: dict[int, float] = {}
    started_by_element: dict[int, int] = {}
    for row in actual_rows:
        element = fpl_id_by_player.get(row.player_id)
        if element is None:
            continue
        actual_by_element[element] = actual_by_element.get(element, 0.0) + row.total_points
        started_by_element[element] = max(
            started_by_element.get(element, 0), 1 if (row.starts or 0) > 0 else 0
        )

    history: dict[int, list[int]] = {}
    for row in source.query(PlayerGameweekStat).filter(PlayerGameweekStat.round < round_number):
        element = fpl_id_by_player.get(row.player_id)
        if element is not None:
            history.setdefault(element, []).append(row.total_points)

    session, player_ids = hydrate_current(source, up_to_round=round_number)
    try:
        projections = {
            p.player_id: p
            for p in project_horizon(session, horizon=1, use_minutes_model=use_minutes_model)
        }
        profiles = compute_minutes_profiles(session, use_model=use_minutes_model)
    finally:
        session.close()

    predictions: list[tuple[int, float, float]] = []
    starts: list[tuple[float, int]] = []
    rolling: dict[int, float] = {}
    for element, points in actual_by_element.items():
        player_id = player_ids.get(element)
        if player_id is None:
            continue
        projection = projections.get(player_id)
        if projection is not None:
            predictions.append((element, float(projection.next_event_points), float(points)))
            recent = history.get(element, [])[-ROLLING_WINDOW:]
            rolling[element] = statistics.fmean(recent) if recent else 0.0
        profile = profiles.get(player_id)
        if profile is not None and element in started_by_element:
            starts.append((float(profile.p_start), started_by_element[element]))

    return RoundRecord(round=round_number, predictions=predictions, starts=starts, rolling=rolling)


def replay_current_round(
    source: Session,
    round_number: int,
    use_minutes_model: bool = True,
) -> RoundResult | None:
    """Replay one round of the season in progress and score every method."""
    record = replay_round_record(source, round_number, use_minutes_model=use_minutes_model)
    return record.result() if record is not None else None


def run_current_backtest(
    source: Session,
    rounds: list[int] | None = None,
    use_minutes_model: bool = True,
) -> list[RoundRecord]:
    """Replay every complete round of the season in progress."""
    candidates = rounds if rounds is not None else complete_rounds(source)
    records = []
    for round_number in candidates:
        record = replay_round_record(source, round_number, use_minutes_model=use_minutes_model)
        if record is None or not record.predictions:
            logger.warning("Round %d produced no scoreable population", round_number)
            continue
        records.append(record)
    return records


def start_calibration(
    starts: list[tuple[float, int]], bins: int = 10
) -> list[tuple[int, float, float]]:
    """(count, mean predicted, observed rate) per quantile bin of prediction.

    The same shape `ml.minutes_model._calibration` reports at training time,
    applied to a season the model was not fitted on. Quantile bins rather than
    fixed-width, because most players are not starters and fixed bins would put
    nine tenths of the rows in one bucket.

    Calibration is the property that matters for this number: `p_start` is
    multiplied into expected points rather than thresholded, so a predicted 0.6
    has to mean 60% or every downstream figure inherits the error.
    """
    if len(starts) < bins:
        return []
    predicted = np.array([row[0] for row in starts], dtype=np.float64)
    observed = np.array([row[1] for row in starts], dtype=np.float64)
    edges = np.quantile(predicted, np.linspace(0, 1, bins + 1))
    out = []
    for low, high in zip(edges[:-1], edges[1:], strict=True):
        mask = (predicted >= low) & (predicted <= high)
        if mask.sum() == 0:
            continue
        out.append((int(mask.sum()), float(predicted[mask].mean()), float(observed[mask].mean())))
    return out
