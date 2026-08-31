"""What the rest of the codebase has to do once a round can hold two matches.

`PlayerGameweekStat` is keyed on the fixture, so a double gameweek is two rows.
That is right — it is what FPL publishes and what the goal model needs to
attribute xG to the correct match — but it splits every consumer into two
camps, and putting a consumer in the wrong one is silent.

Anything asking "what did this player score in gameweek 12" has to sum the
round's rows: a fourteen-point double is one 14, not two 7s, and treating it as
two understates exactly the weeks with the most variance in them. Anything
asking "how much evidence is there about this player" wants the rows as they
come, because two matches genuinely are two matches.
"""

import datetime as dt

from sqlalchemy.orm import Session

from fplquant.lineup.formation import compute_team_shapes
from fplquant.market.correlation import compute_teammate_correlations
from fplquant.market.momentum import compute_price_momentum
from fplquant.market.volatility import compute_volatility_scores
from fplquant.models.orm import Player, PlayerGameweekStat
from fplquant.risk.injury import compute_injury_risk_scores
from tests.engine_helpers import make_league, make_stat


def _double(
    session: Session, player: Player, round_number: int, *, points: tuple[int, int]
) -> None:
    """Two fixtures in one round, as FPL publishes them."""
    for index, scored in enumerate(points):
        stat = make_stat(
            session, player, round_number=round_number, total_points=scored, minutes=90
        )
        stat.fixture_fpl_id = 9000 + round_number * 10 + index
        stat.kickoff_time = dt.datetime(2026, 9, 1, tzinfo=dt.UTC) + dt.timedelta(
            days=7 * round_number + index * 3
        )
    session.flush()


def test_volatility_treats_a_double_as_one_gameweek(db_session: Session) -> None:
    teams = make_league(db_session, teams=1)
    player = teams[0].players[0]

    make_stat(db_session, player, round_number=1, total_points=2)
    make_stat(db_session, player, round_number=2, total_points=2)
    _double(db_session, player, 3, points=(7, 7))

    score = next(s for s in compute_volatility_scores(db_session) if s.player_id == player.id)

    # Three gameweeks (2, 2, 14) — not four rows of (2, 2, 7, 7), which would
    # hide the very week that made the player volatile.
    assert score.gameweeks_considered == 3
    assert score.points_mean == 6.0


def test_correlation_sums_a_double_rather_than_dropping_half_of_it(
    db_session: Session,
) -> None:
    """The old dict comprehension kept whichever row iterated last."""
    teams = make_league(db_session, teams=1)
    first, second = teams[0].players[0], teams[0].players[1]
    for player in (first, second):
        make_stat(db_session, player, round_number=1, total_points=2)
        make_stat(db_session, player, round_number=2, total_points=10)
        _double(db_session, player, 3, points=(6, 6))

    pairs = compute_teammate_correlations(db_session, min_overlap=3)

    pair = next(p for p in pairs if {p.player_a_id, p.player_b_id} == {first.id, second.id})
    assert pair.overlap_gameweeks == 3


def test_a_formation_is_read_per_match_not_per_round(db_session: Session) -> None:
    """Counting starts per round would read a side that twice named four
    defenders as having lined up with eight."""
    teams = make_league(db_session, teams=1)
    team = teams[0]
    defenders = [p for p in team.players if p.element_type == 2][:4]
    for player in defenders:
        _double(db_session, player, 1, points=(2, 2))

    shape = next(s for s in compute_team_shapes(db_session) if s.team_id == team.id)

    assert shape.slots[2] < 6, "a back four counted twice is not a back eight"


def test_price_momentum_counts_gameweeks_not_fixtures(db_session: Session) -> None:
    teams = make_league(db_session, teams=1)
    player = teams[0].players[0]
    for round_number in (1, 2, 3):
        make_stat(db_session, player, round_number=round_number)
    _double(db_session, player, 4, points=(3, 3))
    for stat in db_session.query(PlayerGameweekStat).all():
        stat.value = 50 + stat.round
    db_session.flush()

    momentum = compute_price_momentum(player.web_name, player.gameweek_stats, lookback=3)

    assert momentum is not None
    # Rounds 2, 3, 4 — a three-gameweek window really covering three gameweeks,
    # not two once the double eats a slot.
    assert momentum.gameweeks_considered == 3
    assert momentum.price_change == 2


def test_injury_load_sees_a_double_as_one_heavy_week(db_session: Session) -> None:
    """Two matches in one week is a harder week than the same two matches a
    fortnight apart, and the load component exists to notice that."""
    teams = make_league(db_session, teams=1)
    crammed, spread = teams[0].players[0], teams[0].players[1]

    for index in range(2):  # both matches inside round 1
        stat = make_stat(db_session, crammed, round_number=1, minutes=45)
        stat.fixture_fpl_id = 8000 + index
    make_stat(db_session, spread, round_number=1, minutes=45)
    make_stat(db_session, spread, round_number=2, minutes=45)
    db_session.flush()

    scores = {s.player_id: s for s in compute_injury_risk_scores(db_session)}

    assert scores[crammed.id].load_component > scores[spread.id].load_component
