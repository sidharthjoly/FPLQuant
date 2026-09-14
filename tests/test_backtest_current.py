"""The current-season replay, whose whole job is to not see the future."""

import datetime as dt

from sqlalchemy.orm import Session

from fplquant.backtest.current import (
    complete_rounds,
    hydrate_current,
    start_calibration,
)
from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team

KICKOFF = dt.datetime(2026, 8, 21, 19, 0, tzinfo=dt.UTC)


def _season(session: Session, rounds: int = 3, per_team: int = 15) -> None:
    """Two clubs, one fixture between them per round, full squads."""
    clubs = [
        Team(fpl_id=1, name="Arsenal", short_name="ARS"),
        Team(fpl_id=2, name="Chelsea", short_name="CHE"),
    ]
    for club in clubs:
        session.add(club)
    session.flush()

    players: list[Player] = []
    for index, club in enumerate(clubs):
        for slot in range(per_team):
            player = Player(
                fpl_id=index * 100 + slot + 1,
                team_id=club.id,
                first_name="P",
                second_name=str(slot),
                web_name=f"p{index}{slot}",
                element_type=1 if slot < 2 else 2 if slot < 7 else 3 if slot < 12 else 4,
                now_cost=50 + slot,
                status="a",
                ep_next=0.0,
            )
            session.add(player)
            players.append(player)
    session.flush()

    for round_number in range(1, rounds + 1):
        session.add(
            Fixture(
                fpl_id=round_number,
                event=round_number,
                team_h_id=clubs[0].id,
                team_a_id=clubs[1].id,
                kickoff_time=KICKOFF + dt.timedelta(days=7 * round_number),
                finished=True,
                team_h_score=2,
                team_a_score=0,
                team_h_difficulty=3,
                team_a_difficulty=3,
            )
        )
        for player in players:
            slot = player.fpl_id % 100
            session.add(
                PlayerGameweekStat(
                    player_id=player.id,
                    round=round_number,
                    fixture_fpl_id=round_number,
                    opponent_team_fpl_id=2,
                    was_home=True,
                    kickoff_time=KICKOFF + dt.timedelta(days=7 * round_number),
                    minutes=90 if slot < 11 else 0,
                    starts=1 if slot < 11 else 0,
                    total_points=round_number,
                    goals_scored=0,
                    assists=0,
                    clean_sheets=0,
                    goals_conceded=0,
                    bonus=0,
                    bps=0,
                    influence=0.0,
                    creativity=0.0,
                    threat=0.0,
                    ict_index=0.0,
                    value=50 + slot + round_number,
                )
            )
    session.commit()


def test_complete_rounds_excludes_a_round_still_being_played(db_session: Session) -> None:
    """A gameweek with a fixture still to kick off is not scoreable.

    Its absent rows are indistinguishable from genuine zeros, so scoring it
    would measure the fixture list rather than the model.
    """
    _season(db_session, rounds=2)
    db_session.add(
        Fixture(
            fpl_id=99,
            event=3,
            team_h_id=1,
            team_a_id=2,
            kickoff_time=KICKOFF + dt.timedelta(days=21),
            finished=False,
            team_h_score=None,
            team_a_score=None,
            team_h_difficulty=3,
            team_a_difficulty=3,
        )
    )
    db_session.commit()

    assert complete_rounds(db_session) == [1, 2]


def test_hydrate_keeps_the_predicted_round_out_of_the_past(db_session: Session) -> None:
    """Round N's scoreline and stats are the future the replay exists to hide."""
    _season(db_session, rounds=3)

    replay, _ = hydrate_current(db_session, up_to_round=3)
    try:
        stats = replay.query(PlayerGameweekStat).all()
        assert stats, "history should carry the rounds before the one predicted"
        assert max(row.round for row in stats) == 2

        upcoming = replay.query(Fixture).filter(Fixture.event == 3).one()
        assert upcoming.team_h_score is None
        assert upcoming.team_a_score is None
        assert upcoming.finished is False

        behind = replay.query(Fixture).filter(Fixture.event == 2).one()
        assert behind.team_h_score == 2, "a played fixture must carry its scoreline"
        assert behind.finished is True
    finally:
        replay.close()


def test_hydrate_prices_a_player_from_before_the_deadline(db_session: Session) -> None:
    """Price is only knowable once the earlier gameweek exists."""
    _season(db_session, rounds=3)

    replay, _ = hydrate_current(db_session, up_to_round=3)
    try:
        player = replay.query(Player).filter(Player.fpl_id == 1).one()
        # The source prices this player at 51 + round, so round 2 is 53 and the
        # round being predicted is 54. Picking up 54 would mean the replay had
        # read a price that was not published until after the deadline.
        assert player.now_cost == 53
    finally:
        replay.close()


def test_hydrate_excludes_later_rounds_entirely(db_session: Session) -> None:
    """Nothing from beyond the predicted round reaches the rebuilt world."""
    _season(db_session, rounds=3)

    replay, _ = hydrate_current(db_session, up_to_round=2)
    try:
        assert replay.query(Fixture).filter(Fixture.event > 2).count() == 0
        assert replay.query(PlayerGameweekStat).filter(PlayerGameweekStat.round >= 2).count() == 0
    finally:
        replay.close()


def test_start_calibration_reports_observed_against_predicted() -> None:
    """Bins are quantiles of the prediction, and report the observed rate."""
    rows = [(0.1, 0) for _ in range(50)] + [(0.9, 1) for _ in range(50)]
    bins = start_calibration(rows, bins=2)

    assert len(bins) == 2
    low, high = bins
    assert low[1] < high[1], "bins run from least to most likely to start"
    assert low[2] == 0.0
    assert high[2] == 1.0


def test_start_calibration_needs_enough_rows_to_bin() -> None:
    assert start_calibration([(0.5, 1)], bins=10) == []
