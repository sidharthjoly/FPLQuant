import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.form.ewma import ewma
from fplquant.models.orm import Player

# How many appearances a player needs before their own form is trusted as much
# as the prior. With `n` appearances, form carries weight n / (n + this), so a
# single match counts for ~14% and six for 50% — enough for one gameweek to
# nudge a ranking, never enough for it to rewrite one.
CREDIBILITY_APPEARANCES = 6.0


@dataclass(frozen=True)
class FormScore:
    player_id: int
    web_name: str
    matches_considered: int  # gameweek rows in this player's history
    appearances: int  # of those, the ones they actually played (minutes > 0)
    points_form: float
    underlying_form: float
    combined_score: float


def _zscores(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0] * len(values)
    return [(v - mean) / stdev for v in values]


def compute_form_scores(
    session: Session,
    halflife: float = 3.0,
    points_weight: float = 0.7,
    underlying_weight: float = 0.3,
    min_matches: int = 1,
) -> list[FormScore]:
    """Rank players by a blended form score.

    For each player: EWMA of total_points ("points_form") and EWMA of ict_index
    ("underlying_form") are computed from their gameweek history, in chronological
    order. Because points and ict_index live on different scales, each is
    z-scored across the eligible player pool before being combined, so neither
    metric dominates purely because of its raw magnitude — the same technique
    used to blend factors with different units in quant equity models.
    """
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()

    eligible: list[Player] = []
    raw_points_form: list[float] = []
    raw_underlying_form: list[float] = []
    matches_by_player: dict[int, int] = {}
    appearances_by_player: dict[int, int] = {}

    for player in players:
        stats = sorted(player.gameweek_stats, key=lambda s: s.round)
        if len(stats) < min_matches:
            continue
        eligible.append(player)
        matches_by_player[player.id] = len(stats)
        appearances_by_player[player.id] = sum(1 for s in stats if s.minutes > 0)
        raw_points_form.append(ewma([s.total_points for s in stats], halflife))
        raw_underlying_form.append(ewma([s.ict_index for s in stats], halflife))

    points_z = _zscores(raw_points_form)
    underlying_z = _zscores(raw_underlying_form)

    scores = [
        FormScore(
            player_id=player.id,
            web_name=player.web_name,
            matches_considered=matches_by_player[player.id],
            appearances=appearances_by_player[player.id],
            points_form=raw_points_form[i],
            underlying_form=raw_underlying_form[i],
            combined_score=points_weight * points_z[i] + underlying_weight * underlying_z[i],
        )
        for i, player in enumerate(eligible)
    ]
    return sorted(scores, key=lambda s: s.combined_score, reverse=True)


def predicted_points_by_player(
    session: Session,
    halflife: float = 3.0,
    credibility_appearances: float = CREDIBILITY_APPEARANCES,
) -> dict[int, float]:
    """Our best current expected-points estimate per player.

    A player's own EWMA `points_form` is a noisy estimator early in the season:
    after one gameweek it *is* that gameweek's score, so whoever hauled in GW1
    looks like a 13-point-per-week player and everyone else looks like a
    2-pointer. Taking that at face value makes the optimizer rebuild the entire
    squad out of last week's team of the week.

    So we shrink form toward a prior (FPL's own `ep_next`) by credibility
    weight, the standard actuarial treatment of a small-sample estimate:

        w = n / (n + credibility_appearances)
        predicted = w * points_form + (1 - w) * ep_next

    where `n` is *appearances*, not gameweeks elapsed — a player with six rows
    of `minutes = 0` has no evidence about their scoring, only about their
    benching, and that is already handled separately by `chance_of_playing`.
    Counting those rows would let the model become confident that an unused
    substitute is a true zero-point player.

    As n grows, form takes over; at n = 0 the weight is zero and this returns
    `ep_next` unchanged, which is what keeps the estimate usable in preseason
    before any gameweek history exists.
    """
    scores_by_player = {score.player_id: score for score in compute_form_scores(session, halflife)}
    predicted = {}
    for player in session.query(Player).all():
        score = scores_by_player.get(player.id)
        appearances = score.appearances if score else 0
        weight = appearances / (appearances + credibility_appearances)
        form = score.points_form if score else 0.0
        predicted[player.id] = weight * form + (1 - weight) * player.ep_next
    return predicted
