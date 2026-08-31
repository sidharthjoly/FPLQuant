"""The contract the news layer holds itself to, and the three ways time runs.

The contract is the important half. FPL's `chance_of_playing_next_round` is
derived from the same press-conference news this layer reads, so reading it
again and discounting a second time would charge the same evidence twice — the
mistake `lineup.starts` and `form.fixtures` both warn about. The tests below
pin the next round to FPL's own number exactly, which is what makes that
mistake structurally impossible rather than merely avoided.
"""

import datetime as dt

from sqlalchemy.orm import Session

from fplquant.form.fixtures import chance_of_playing
from fplquant.models.orm import Player
from fplquant.news.availability import (
    MAX_PROJECTED_AVAILABILITY,
    availability_by_event,
    availability_timeline,
)
from tests.engine_helpers import make_fixture, make_league, make_team

AS_OF = dt.datetime(2026, 8, 31, 12, 0, tzinfo=dt.UTC)


def _news(player: Player, text: str, status: str, chance: int | None = None) -> Player:
    player.news = text
    player.status = status
    player.chance_of_playing_next_round = chance
    return player


def _weekly_rounds(session: Session, teams: list, count: int = 5) -> list[int]:
    """One fixture per pair of clubs a week apart, starting the day after AS_OF."""
    for event in range(1, count + 1):
        for index in range(0, len(teams) - 1, 2):
            make_fixture(
                session,
                teams[index],
                teams[index + 1],
                fpl_id=event * 10 + index,
                event=event,
                kickoff=AS_OF + dt.timedelta(days=1 + 7 * (event - 1)),
            )
    return list(range(1, count + 1))


def test_the_next_round_is_fpls_own_number_and_nothing_else(db_session: Session) -> None:
    """The whole layer rests on this. If the first event ever disagreed with
    `chance_of_playing`, the model would be discounting the same news twice."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    players = db_session.query(Player).all()
    _news(players[0], "Knee injury - Expected back 5 Sep", "i", 0)
    _news(players[1], "Knock - 75% chance of playing", "d", 75)
    _news(players[2], "Suspended until 1 Sep", "s", 0)
    _news(players[3], "Has joined Barcelona permanently", "u", 0)
    db_session.flush()

    by_event = availability_by_event(db_session, events, as_of=AS_OF)

    for player in players:
        assert by_event[events[0]][player.id] == chance_of_playing(player)


def test_availability_is_only_ever_given_back_never_taken_away(db_session: Session) -> None:
    """This layer must not be able to newly rule anyone out. A spurious zero
    silently deletes a fit player from the optimizer; a spurious one merely
    makes it optimistic about somebody FPL has already flagged."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    for index, player in enumerate(db_session.query(Player).all()):
        text, status, chance = [
            ("Knee injury - Expected back 5 Sep", "i", 0),
            ("Knock - 75% chance of playing", "d", 75),
            ("Hamstring injury - 25% chance of playing", "d", 25),
            ("Suspended until 20 Sep", "s", 0),
            ("Thigh injury - Unknown return date", "i", 0),
            ("Something FPL has never published before", "i", 0),
            ("", "a", None),
        ][index % 7]
        _news(player, text, status, chance)
    db_session.flush()

    timeline = availability_timeline(db_session, events, as_of=AS_OF)

    for entry in timeline.values():
        series = [entry.by_event[event] for event in events]
        assert min(series) >= entry.news.next_round_availability
        assert series == sorted(series), "availability must never fall over the horizon"


def test_a_ban_expiring_mid_gameweek_clears_one_club_and_not_the_other(
    db_session: Session,
) -> None:
    """The signal lives entirely on this boundary. A gameweek is spread over
    three or four days, so a suspension ending on the Saturday clears a club
    playing Sunday and not one playing Friday — and both are the same round."""
    early = make_team(db_session, fpl_id=1, short_name="EAR")
    late = make_team(db_session, fpl_id=2, short_name="LAT")
    other = make_team(db_session, fpl_id=3, short_name="OTH")
    another = make_team(db_session, fpl_id=4, short_name="ANO")
    make_fixture(db_session, early, other, fpl_id=1, event=1, kickoff=AS_OF)
    make_fixture(db_session, late, another, fpl_id=2, event=1, kickoff=AS_OF)
    # Round two: one club plays the Friday, the other the Sunday.
    friday = AS_OF + dt.timedelta(days=11)
    sunday = AS_OF + dt.timedelta(days=13)
    make_fixture(db_session, early, other, fpl_id=3, event=2, kickoff=friday)
    make_fixture(db_session, late, another, fpl_id=4, event=2, kickoff=sunday)

    banned_early = Player(
        fpl_id=10,
        team_id=early.id,
        first_name="A",
        second_name="A",
        web_name="A",
        element_type=3,
        now_cost=50,
        status="s",
        news="Suspended until 12 Sep",
        chance_of_playing_next_round=0,
    )
    banned_late = Player(
        fpl_id=11,
        team_id=late.id,
        first_name="B",
        second_name="B",
        web_name="B",
        element_type=3,
        now_cost=50,
        status="s",
        news="Suspended until 12 Sep",
        chance_of_playing_next_round=0,
    )
    db_session.add_all([banned_early, banned_late])
    db_session.flush()

    by_event = availability_by_event(db_session, [1, 2], as_of=AS_OF)

    assert by_event[1][banned_early.id] == 0.0
    assert by_event[1][banned_late.id] == 0.0
    assert by_event[2][banned_early.id] == 0.0, "still banned on the Friday"
    assert by_event[2][banned_late.id] == 1.0, "back for the Sunday"


def test_a_served_ban_returns_a_fully_fit_player(db_session: Session) -> None:
    """A ban's end date is a fact, not a club's estimate, and the player was
    never hurt — so this steps to 1.0 rather than to the projection ceiling
    every guess about an injury is capped at."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    player = db_session.query(Player).first()
    assert player is not None
    _news(player, "Suspended until 3 Sep", "s", 0)
    db_session.flush()

    series = availability_timeline(db_session, events, as_of=AS_OF)[player.id].by_event

    assert series[1] == 0.0
    assert series[3] == 1.0
    assert series[3] > MAX_PROJECTED_AVAILABILITY


def test_a_ban_with_no_readable_end_date_keeps_the_player_out(db_session: Session) -> None:
    """The fail-closed path for the one category that otherwise steps straight
    to full availability. Without a date there is nothing to step *at*."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    player = db_session.query(Player).first()
    assert player is not None
    _news(player, "Suspended until further notice", "s", 0)
    db_session.flush()

    entry = availability_timeline(db_session, events, as_of=AS_OF)[player.id]

    assert set(entry.by_event.values()) == {0.0}
    assert not entry.is_time_varying


def test_a_stated_return_date_ramps_in_rather_than_switching_on(
    db_session: Session,
) -> None:
    """Return dates slip late far more often than early, so the stated day is
    read as optimistic. A step function here would be the threshold switchover
    this codebase refuses everywhere else."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    player = db_session.query(Player).first()
    assert player is not None
    _news(player, "Calf injury - Expected back 8 Sep", "i", 0)
    db_session.flush()

    series = availability_timeline(db_session, events, as_of=AS_OF)[player.id].by_event

    assert series[1] == 0.0
    assert series[2] == 0.0, "the round before the stated date"
    assert 0.0 < series[3] < MAX_PROJECTED_AVAILABILITY, "part way back, not switched on"
    assert series[5] == MAX_PROJECTED_AVAILABILITY


def test_an_injury_with_no_return_date_says_nothing_at_all(db_session: Session) -> None:
    """The commonest kind of news in the pool — 47 of 118 strings — and it
    carries no time information whatsoever. Improvising a recovery curve for it
    would put injured players back in the optimizer's squad on no evidence."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    player = db_session.query(Player).first()
    assert player is not None
    _news(player, "Knee injury - Unknown return date", "i", 0)
    db_session.flush()

    entry = availability_timeline(db_session, events, as_of=AS_OF)[player.id]

    assert set(entry.by_event.values()) == {0.0}
    assert not entry.is_time_varying
    assert entry.return_event is None


def test_a_knock_clears_faster_the_milder_it_is(db_session: Session) -> None:
    """A percentage with no date is a statement about *one match*. It resolves;
    the model just doesn't know which way. How fast follows the grade."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    players = db_session.query(Player).all()
    mild, serious = players[0], players[1]
    _news(mild, "Knock - 75% chance of playing", "d", 75)
    _news(serious, "Hamstring injury - 25% chance of playing", "d", 25)
    db_session.flush()

    timeline = availability_timeline(db_session, events, as_of=AS_OF)

    assert timeline[mild.id].by_event[1] == 0.75
    assert timeline[serious.id].by_event[1] == 0.25
    assert timeline[mild.id].by_event[5] == MAX_PROJECTED_AVAILABILITY
    assert timeline[serious.id].by_event[5] < MAX_PROJECTED_AVAILABILITY
    assert timeline[mild.id].by_event[3] > timeline[serious.id].by_event[3]


def test_a_player_who_has_left_the_league_never_comes_back(db_session: Session) -> None:
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    player = db_session.query(Player).first()
    assert player is not None
    _news(player, "Has joined Juventus on loan for the rest of the season", "u", 0)
    db_session.flush()

    entry = availability_timeline(db_session, events, as_of=AS_OF)[player.id]

    assert set(entry.by_event.values()) == {0.0}
    assert entry.return_event is None


def test_wording_we_cannot_read_leaves_every_gameweek_alone(db_session: Session) -> None:
    """FPL changes this text without notice, so the default branch has to be
    "no change" rather than "best effort"."""
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)
    player = db_session.query(Player).first()
    assert player is not None
    _news(player, "Being assessed by the club's medical staff", "i", 0)
    db_session.flush()

    entry = availability_timeline(db_session, events, as_of=AS_OF)[player.id]

    assert set(entry.by_event.values()) == {0.0}
    assert not entry.is_time_varying


def test_a_fit_player_is_untouched_in_every_gameweek(db_session: Session) -> None:
    teams = make_league(db_session, teams=4)
    events = _weekly_rounds(db_session, teams)

    timeline = availability_timeline(db_session, events, as_of=AS_OF)

    assert timeline, "every player should get an entry, news or not"
    for entry in timeline.values():
        assert set(entry.by_event.values()) == {1.0}
        assert not entry.is_time_varying


def test_a_club_blanking_the_first_round_is_anchored_on_the_round_it_plays(
    db_session: Session,
) -> None:
    """FPL's percentage describes a player's *next match*, which is not always
    the horizon's first round — a single postponed fixture keeps a round alive
    for the nineteen clubs not in it. Projecting a recovery across that gap
    would credit healing the published number has already priced in."""
    playing = make_team(db_session, fpl_id=1, short_name="PLA")
    opponent = make_team(db_session, fpl_id=2, short_name="OPP")
    blanking = make_team(db_session, fpl_id=3, short_name="BLA")
    other = make_team(db_session, fpl_id=4, short_name="OTH")
    # Round one is a lone rearranged fixture; everyone else starts in round two.
    make_fixture(db_session, playing, opponent, fpl_id=1, event=1, kickoff=AS_OF)
    make_fixture(db_session, blanking, other, fpl_id=2, event=2, kickoff=AS_OF + dt.timedelta(7))

    doubtful = Player(
        fpl_id=10,
        team_id=blanking.id,
        first_name="C",
        second_name="C",
        web_name="C",
        element_type=3,
        now_cost=50,
        status="d",
        news="Knock - 50% chance of playing",
        chance_of_playing_next_round=50,
    )
    db_session.add(doubtful)
    db_session.flush()

    series = availability_timeline(db_session, [1, 2], as_of=AS_OF)[doubtful.id].by_event

    assert series[1] == 0.5
    assert series[2] == 0.5, "round two *is* their next match, so it keeps FPL's number"
