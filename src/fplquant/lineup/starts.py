from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.form.ewma import ewma
from fplquant.lineup.fatigue import compute_fatigue_scores
from fplquant.lineup.formation import compute_team_shapes
from fplquant.models.orm import Player, PlayerGameweekStat
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

# A player is treated as having started if they played at least this long, when
# the explicit `starts` flag isn't on the row (see `did_start`).
STARTER_MINUTES = 60
# Appearances needed before a player's own start rate is trusted as much as the
# positional prior. Lower than the equivalent constant for points form, and
# deliberately so: whether a player starts is a low-variance binary that a
# manager repeats week to week, so it reveals itself far faster than a scoring
# rate does. Three or four consecutive starts really is decent evidence.
CREDIBILITY_MATCHES = 3.0
# Prior probability of starting, by position, for a player with no history. Set
# from squad sizes: roughly 1 of 2 keepers, 4 of ~8 defenders, and so on.
POSITION_PRIOR: dict[int, float] = {
    GOALKEEPER: 0.5,
    DEFENDER: 0.5,
    MIDFIELDER: 0.45,
    FORWARD: 0.4,
}
START_HALFLIFE = 4.0
# Rounds of evidence before a *change* in a club's shape is taken at face value.
# Deliberately higher than the credibility used to estimate the shape itself in
# `fplquant.lineup.formation`: noticing that a side has switched systems is a
# strictly harder inference than describing the system they've been playing, and
# a two-game blip in how many defenders got named is usually personnel, not
# tactics. Without this the ratio swings on a couple of matches.
FORMATION_CHANGE_CREDIBILITY = 8.0
# The most a fully-loaded player facing a midweek turnaround has their start
# odds cut. Rotation is real but managers don't bench their best players wholesale.
MAX_FATIGUE_PENALTY = 0.25
# The lineup signal is a nudge, not a verdict — clamp it so a noisy early-season
# start record can't dominate a prediction.
MIN_MULTIPLIER = 0.75
MAX_MULTIPLIER = 1.10


@dataclass(frozen=True)
class StartProbability:
    player_id: int
    web_name: str
    appearances: int
    baseline_probability: float  # how often this player starts, all else equal
    adjusted_probability: float  # ...adjusted for this week's rest and their side's shape
    fatigue_index: float
    rest_days: float | None
    formation_factor: float  # >1 if their side has shifted toward using their position
    lineup_multiplier: float  # adjusted / baseline, clamped — what the optimizer consumes


def did_start(stat: PlayerGameweekStat) -> bool:
    """Whether a player started this match.

    FPL's history carries an explicit `starts` flag, but rows written before we
    began ingesting it have it as NULL. Rather than read those as "did not
    start" — which would collapse every start probability on a database that
    hasn't been re-ingested yet — fall back to a minutes threshold, which gets
    the answer right for all but genuine early substitutions.
    """
    if stat.starts is not None:
        return bool(stat.starts)
    return stat.minutes >= STARTER_MINUTES


def compute_start_probabilities(
    session: Session, credibility_matches: float = CREDIBILITY_MATCHES
) -> list[StartProbability]:
    """How likely each player is to be in the starting XI for their next match.

    The output the rest of the model actually consumes is `lineup_multiplier`,
    not `adjusted_probability`, and the distinction matters. Our points estimate
    is already an *unconditional* per-gameweek number: both FPL's `ep_next` and
    our own EWMA of gameweek points average in the weeks a player was benched,
    so rotation risk is priced into them once already. Multiplying such a number
    by an absolute probability of starting would charge a rotation risk twice
    and systematically underrate squad players.

    So we ask a narrower question: is this player *more or less* likely to start
    this week than their own history would suggest?

        lineup_multiplier = adjusted_probability / baseline_probability

    `baseline_probability` is their own start rate, so whatever is already baked
    into their points history divides straight back out. What survives is only
    the part that isn't in the history: the rest they've had going into this
    specific fixture, and whether their side has recently shifted to a shape
    that uses more or fewer players in their position. When neither says
    anything — which is most of the time, and nearly always before about
    gameweek 8 — the ratio is 1.0 and nothing changes, which is the honest
    answer rather than a confident-looking one.

    Note this deliberately does *not* fold in injury and suspension news:
    `fplquant.form.fixtures.chance_of_playing` applies that separately as a hard
    gate, and it has to stay hard. Blending a flagged-out player's 0.0 toward
    "he usually starts" would put injured players back in the optimizer's squad.

    Once several seasons of `starts` data exist, the better structure is to
    predict points-per-start and multiply by an absolute start probability. That
    needs a points-per-start estimator this codebase doesn't have yet, and at
    one or two gameweeks played it would be far noisier than this ratio.
    """
    # Load every player's gameweek history exactly once and hand it to both
    # helpers — all three of these walk the same rows.
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    fatigue_by_player = {s.player_id: s for s in compute_fatigue_scores(session, players=players)}
    shapes_by_team = {s.team_id: s for s in compute_team_shapes(session, players=players)}

    probabilities = []
    for player in players:
        stats = sorted(player.gameweek_stats, key=lambda s: s.round)
        prior = POSITION_PRIOR.get(player.element_type, 0.45)

        appearances = len(stats)
        weight = appearances / (appearances + credibility_matches)
        observed = ewma([1.0 if did_start(s) else 0.0 for s in stats], START_HALFLIFE)
        baseline = weight * observed + (1 - weight) * prior

        fatigue = fatigue_by_player.get(player.id)
        fatigue_index = fatigue.fatigue_index if fatigue else 0.0
        rest_days = fatigue.rest_days if fatigue else None

        # Has their side moved toward or away from using their position? Both
        # numbers are shrunk toward the same 4-4-2 prior, so with little history
        # they're equal and this is exactly 1.0.
        shape = shapes_by_team.get(player.team_id)
        if shape is None:
            formation_factor = 1.0
        else:
            long_run = shape.slots.get(player.element_type, 0.0)
            recent = shape.recent_slots.get(player.element_type, 0.0)
            raw_ratio = recent / long_run if long_run > 0 else 1.0
            change_confidence = shape.rounds_observed / (
                shape.rounds_observed + FORMATION_CHANGE_CREDIBILITY
            )
            formation_factor = 1.0 + change_confidence * (raw_ratio - 1.0)

        adjusted = baseline * (1 - MAX_FATIGUE_PENALTY * fatigue_index) * formation_factor
        adjusted = max(0.0, min(1.0, adjusted))

        multiplier = adjusted / baseline if baseline > 0 else 1.0
        multiplier = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, multiplier))

        probabilities.append(
            StartProbability(
                player_id=player.id,
                web_name=player.web_name,
                appearances=appearances,
                baseline_probability=baseline,
                adjusted_probability=adjusted,
                fatigue_index=fatigue_index,
                rest_days=rest_days,
                formation_factor=formation_factor,
                lineup_multiplier=multiplier,
            )
        )
    return sorted(probabilities, key=lambda p: p.adjusted_probability, reverse=True)


def lineup_multipliers_by_player(session: Session) -> dict[int, float]:
    """Just the multipliers, keyed by player id, for the expected-points pipeline."""
    return {p.player_id: p.lineup_multiplier for p in compute_start_probabilities(session)}
