import datetime as dt

from sqlalchemy.orm import Session

from fplquant.ml.features import (
    FEATURE_NAMES,
    MinutesFeatures,
    build_training_frame,
    feature_vector,
)
from fplquant.models.orm import HistoricalPlayerGameweek

SEASON = "2023-24"
KICKOFF = dt.datetime(2023, 8, 12, 14, 0, tzinfo=dt.UTC)


def _row(
    session: Session,
    *,
    element: int = 1,
    round_number: int,
    starts: int,
    minutes: int,
    value: int = 60,
    fixture: int | None = None,
    kickoff: dt.datetime | None = None,
    team: str = "Arsenal",
    position: str = "MID",
) -> HistoricalPlayerGameweek:
    row = HistoricalPlayerGameweek(
        season=SEASON,
        element=element,
        round=round_number,
        fixture=fixture if fixture is not None else round_number,
        name=f"P{element}",
        position=position,
        team=team,
        was_home=True,
        kickoff_time=kickoff or KICKOFF + dt.timedelta(days=7 * (round_number - 1)),
        minutes=minutes,
        starts=starts,
        value=value,
        selected=1000,
    )
    session.add(row)
    session.flush()
    return row


def test_a_rows_features_never_include_its_own_outcome(db_session: Session) -> None:
    """The one bug that would make every metric look excellent and mean
    nothing. The archive row for a gameweek holds that gameweek's minutes, so a
    feature builder that reads them is handing the model the answer."""
    _row(db_session, round_number=1, starts=0, minutes=0)
    _row(db_session, round_number=2, starts=1, minutes=90)

    frame = build_training_frame(db_session)

    first = dict(zip(FEATURE_NAMES, frame.features[0], strict=True))
    second = dict(zip(FEATURE_NAMES, frame.features[1], strict=True))
    # Round 1 has no history at all: sentinels, not the round's own values.
    assert first["rounds_observed"] == 0
    assert first["started_prev"] == -1.0
    assert first["minutes_prev"] == -1.0
    # Round 2 sees exactly round 1, and nothing of itself.
    assert second["rounds_observed"] == 1
    assert second["started_prev"] == 0.0
    assert second["minutes_prev"] == 0.0
    assert frame.labels == [0, 1]


def test_history_is_ordered_by_kickoff_not_by_round(db_session: Session) -> None:
    """A postponed round-2 fixture played after round 3 must not appear in
    round 3's history. Ordering by round instead of kickoff let a later match
    feed an earlier one — which showed up as negative rest days."""
    _row(db_session, round_number=1, starts=1, minutes=90, kickoff=KICKOFF)
    # Round 2, rearranged to a month later.
    _row(
        db_session,
        round_number=2,
        starts=0,
        minutes=0,
        kickoff=KICKOFF + dt.timedelta(days=30),
    )
    _row(db_session, round_number=3, starts=1, minutes=90, kickoff=KICKOFF + dt.timedelta(days=14))

    frame = build_training_frame(db_session)

    rest = [dict(zip(FEATURE_NAMES, row, strict=True))["rest_days"] for row in frame.features]
    assert all(days == -1.0 or days > 0 for days in rest)
    # Chronological order: round 1, then round 3, then the rearranged round 2.
    assert frame.rounds == [1, 3, 2]


def test_a_double_gameweek_keeps_both_fixtures(db_session: Session) -> None:
    _row(db_session, round_number=7, starts=1, minutes=90, fixture=70)
    _row(db_session, round_number=7, starts=0, minutes=20, fixture=71)

    frame = build_training_frame(db_session)

    assert len(frame) == 2
    assert frame.rounds == [7, 7]


def test_missing_history_is_a_sentinel_not_a_zero() -> None:
    """Zero is a real value for every one of these — a player who started none
    of his last five genuinely scores 0.0 — so collapsing "no evidence" into it
    would teach the model that a debutant looks like someone repeatedly
    dropped."""
    vector = dict(
        zip(
            FEATURE_NAMES,
            feature_vector(
                MinutesFeatures(
                    value=50,
                    price_rank_in_group=1.0,
                    price_share_in_group=0.3,
                    was_home=True,
                    round=1,
                    rounds_observed=0,
                    recent_starts=[],
                    recent_minutes=[],
                    last_selected=None,
                    rest_days=-1.0,
                    position="MID",
                )
            ),
            strict=True,
        )
    )
    assert vector["started_prev"] == -1.0
    assert vector["minutes_prev"] == -1.0
    assert vector["start_rate_season"] == -1.0
    assert vector["selected_prev"] == -1.0
    assert vector["position_mid"] == 1.0


def test_the_attacking_midfield_position_folds_into_midfield(db_session: Session) -> None:
    """FPL published an "AM" position for one season and 322 rows. Too few to
    learn a category from, and a midfielder for every scoring purpose."""
    _row(db_session, round_number=1, starts=1, minutes=90, position="AM")

    vector = dict(zip(FEATURE_NAMES, build_training_frame(db_session).features[0], strict=True))

    assert vector["position_mid"] == 1.0
    assert vector["position_fwd"] == 0.0


def test_price_rank_reflects_competition_within_the_club(db_session: Session) -> None:
    """The feature closest to what the hand-built model encodes: being the
    fourth-most-expensive midfielder at your club says something no individual
    attribute does, and it exists before a ball is kicked."""
    _row(db_session, element=1, round_number=1, starts=1, minutes=90, value=120)
    _row(db_session, element=2, round_number=1, starts=0, minutes=0, value=45)

    frame = build_training_frame(db_session)

    ranks = [
        dict(zip(FEATURE_NAMES, row, strict=True))["value_rank_in_team_position"]
        for row in frame.features
    ]
    assert sorted(ranks) == [1.0, 2.0]


def test_rows_without_a_published_label_are_skipped(db_session: Session) -> None:
    row = _row(db_session, round_number=1, starts=1, minutes=90)
    row.starts = None
    db_session.flush()

    assert len(build_training_frame(db_session)) == 0


def test_an_empty_archive_yields_an_empty_frame(db_session: Session) -> None:
    assert len(build_training_frame(db_session)) == 0
