import logging
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.form.ewma import ewma
from fplquant.models.orm import Fixture, Player, Team
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

logger = logging.getLogger(__name__)

# Outfield players in a starting XI. Not a tuning knob — it is the rule of the
# game, and it is what makes a shape that does not add up to it a bug rather
# than an opinion.
OUTFIELD_STARTERS = 10

# The shape a side is assumed to play before we've seen them play one, in FPL
# position terms: a 4-4-2. Used as the prior that observed shapes are shrunk
# toward, so one match can't convince us a club has switched systems.
DEFAULT_SLOTS: dict[int, float] = {GOALKEEPER: 1.0, DEFENDER: 4.0, MIDFIELDER: 4.0, FORWARD: 2.0}
# Rounds of evidence needed before an observed shape is trusted as much as the
# prior — same credibility logic as `fplquant.form.scoring`.
CREDIBILITY_ROUNDS = 4.0
# Short halflife: a shape change (new system, new coach) should show up quickly.
SHAPE_HALFLIFE = 2.0


@dataclass(frozen=True)
class TeamShape:
    team_id: int
    short_name: str
    rounds_observed: int
    slots: dict[int, float]  # element_type -> expected players started in that position
    recent_slots: dict[int, float]  # the same, weighted hard toward the latest rounds


def _starts_by_round(
    players: list[Player], sides: dict[int, tuple[int, int]]
) -> dict[int, dict[int, dict[int, int]]]:
    """team_id -> fixture -> element_type -> how many players started there.

    Keyed on the fixture rather than the round, because a formation is a
    property of a match. Counting per round would add up both halves of a
    double gameweek and read a side that twice named four defenders as having
    lined up with eight.

    The club is the one the player turned out for *in that match*, resolved
    from the fixture and `was_home`, not the club they are at today. A
    player's history follows them through a transfer, so attributing all of it
    to their current club invents lineups: Man City's shape was being computed
    partly from two fixtures in which exactly one Man City player appeared,
    because one man had joined from elsewhere and brought his old matches with
    him. Those phantom one-man teams dragged their average XI down to under
    seven outfield players.
    """
    from fplquant.lineup.starts import did_start

    counts: dict[int, dict[int, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    for player in players:
        for stat in player.gameweek_stats:
            if not did_start(stat):
                continue
            match = stat.fixture_fpl_id if stat.fixture_fpl_id is not None else stat.round
            team_id = _club_that_match(stat, player, sides)
            by_position = counts[team_id][match]
            by_position[player.element_type] = by_position.get(player.element_type, 0) + 1
    return counts


def _club_that_match(stat: object, player: Player, sides: dict[int, tuple[int, int]]) -> int:
    """Which club the player was playing for in this match.

    Falls back to their current club when the fixture is unknown or the row
    predates `was_home` being stored — which is the old behaviour, and wrong
    only for players who have since transferred.
    """
    fixture_id = getattr(stat, "fixture_fpl_id", None)
    was_home = getattr(stat, "was_home", None)
    if fixture_id is None or was_home is None or fixture_id not in sides:
        return player.team_id
    home_id, away_id = sides[fixture_id]
    return home_id if was_home else away_id


def _is_a_whole_lineup(by_position: dict[int, int]) -> bool:
    """Whether this match looks like a full XI rather than a partial record.

    A gameweek still being played, a club whose eleventh starter has since
    left the league and is no longer in the player table, a transfer the
    fixture data cannot resolve: all of them produce a count that is not a
    lineup, and averaging them in moves a club's shape toward a formation
    nobody played. Dropping the match leaves the prior carrying more weight,
    which is the correct response to weaker evidence.
    """
    outfield = sum(count for position, count in by_position.items() if position != GOALKEEPER)
    return outfield == OUTFIELD_STARTERS


def compute_team_shapes(
    session: Session,
    credibility_rounds: float = CREDIBILITY_ROUNDS,
    players: list[Player] | None = None,
) -> list[TeamShape]:
    """The formation each club actually lines up in, inferred from who gets picked.

    The FPL API exposes no coach and no formation, so rather than scrape one we
    read the coach's *revealed* preference: count how many defenders, midfielders
    and forwards each club actually started in each gameweek. A side that keeps
    naming three defenders is playing a back three, whoever the coach is and
    whatever they say in the press. This has the useful property of surviving a
    managerial change on its own — a new coach's different selections move the
    estimate without anyone having to tell the model the coach changed.

    Two caveats worth being explicit about. FPL's `element_type` is fixed for the
    season and doesn't track a player's real role — a wing-back is a DEF here —
    so this is a formation in *FPL position* terms, not a tactics-board shape.
    That happens to be the version that matters for FPL scoring. And with few
    rounds played the count is noisy, so it's shrunk toward a 4-4-2 prior by
    rounds observed, exactly as form is shrunk toward `ep_next`.

    `slots` is the season-long estimate; `recent_slots` weights the latest
    rounds far more heavily. Comparing the two is how `fplquant.lineup.starts`
    detects a side that has *changed* shape.
    """
    # `players` is accepted pre-loaded for the same reason as in
    # `fplquant.lineup.fatigue.compute_fatigue_scores`.
    if players is None:
        players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    sides = {
        fixture.fpl_id: (fixture.team_h_id, fixture.team_a_id)
        for fixture in session.query(Fixture).all()
    }
    counts = _starts_by_round(players, sides)
    teams = session.query(Team).all()

    shapes = []
    for team in teams:
        by_round = {
            match: by_position
            for match, by_position in counts.get(team.id, {}).items()
            if _is_a_whole_lineup(by_position)
        }
        dropped = len(counts.get(team.id, {})) - len(by_round)
        if dropped:
            logger.debug(
                "%s: ignoring %d match(es) that did not come back as a full XI",
                team.short_name,
                dropped,
            )
        rounds = sorted(by_round)
        weight = len(rounds) / (len(rounds) + credibility_rounds)

        slots: dict[int, float] = {}
        recent_slots: dict[int, float] = {}
        for position, prior in DEFAULT_SLOTS.items():
            observed = [float(by_round[r].get(position, 0)) for r in rounds]
            flat = sum(observed) / len(observed) if observed else prior
            recent = ewma(observed, SHAPE_HALFLIFE) if observed else prior
            slots[position] = weight * flat + (1 - weight) * prior
            recent_slots[position] = weight * recent + (1 - weight) * prior

        shapes.append(
            TeamShape(
                team_id=team.id,
                short_name=team.short_name,
                rounds_observed=len(rounds),
                slots=slots,
                recent_slots=recent_slots,
            )
        )
    return shapes


def describe_shape(slots: dict[int, float]) -> str:
    """A readable "4-4-2"-style label for a shape, for CLI and API display.

    The three numbers have to add up to ten, because ten is how many outfield
    players a team may put on the pitch. Rounding each one on its own does not
    give you that: Brentford's 3.44/5.11/1.44 sums to exactly 10.00 and prints
    as "3-5-1", nine men, while Spurs' 3.78/4.56/1.67 prints as "4-5-2" and
    fields twelve. Python's format rounds half to even on top of it, so 2.5
    becomes 2 and a plausible shape reads as "2-5-2".

    So the rounding is apportioned rather than done position by position:
    everyone gets their floor, and the remaining places go to whoever was
    closest to earning another — the largest-remainder method, which is the
    same problem as handing out seats in a parliament.
    """
    positions = (DEFENDER, MIDFIELDER, FORWARD)
    values = [max(0.0, slots.get(position, 0.0)) for position in positions]
    whole = [int(value) for value in values]
    remaining = OUTFIELD_STARTERS - sum(whole)
    # Largest fractional part first; ties go to the more advanced position,
    # which is arbitrary but has to be decided by something stable.
    order = sorted(range(len(values)), key=lambda i: (-(values[i] - whole[i]), -i))
    for index in order[: max(0, remaining)]:
        whole[index] += 1
    return "-".join(str(count) for count in whole)
