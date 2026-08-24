from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.form.ewma import ewma
from fplquant.models.orm import Player, Team
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

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


def _starts_by_round(players: list[Player]) -> dict[int, dict[int, dict[int, int]]]:
    """team_id -> round -> element_type -> how many players started there."""
    from fplquant.lineup.starts import did_start

    counts: dict[int, dict[int, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    for player in players:
        for stat in player.gameweek_stats:
            if not did_start(stat):
                continue
            by_position = counts[player.team_id][stat.round]
            by_position[player.element_type] = by_position.get(player.element_type, 0) + 1
    return counts


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
    counts = _starts_by_round(players)
    teams = session.query(Team).all()

    shapes = []
    for team in teams:
        by_round = counts.get(team.id, {})
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
    """A readable "4-4-2"-style label for a shape, for CLI and API display."""
    return "-".join(
        f"{slots.get(position, 0.0):.0f}" for position in (DEFENDER, MIDFIELDER, FORWARD)
    )
