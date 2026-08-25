"""Who is going to be on the pitch, and for how long.

Minutes are the single largest term in an FPL points model and the one most
often waved away. A player who doesn't start scores two points at best, and no
amount of underlying quality changes that; the difference between a nailed
starter and a rotation risk is worth more than the difference between a good
fixture and a bad one.

`fplquant.lineup.starts` already estimates start probabilities, but it does so
as a *relative* signal — a multiplier centred on 1.0, deliberately shrunk
toward a positional prior of "roughly half the squad plays" and explicitly
documented as not being an absolute probability. That's the right shape for the
pipeline it feeds, where it nudges an estimate that already has rotation risk
priced into it. It is the wrong shape here: a model built from components needs
to know that Haaland starts nine games in ten and his understudy starts one.

So this module estimates the absolute quantity, under a constraint the relative
version has no use for: **a club starts exactly eleven players**. Start
probabilities within a club's position group are normalised to the number of
slots that group's inferred formation actually fills. That constraint is what
makes the estimate self-correcting. It means a squad's start probabilities can
never sum to more than the shirts available; it means an expensive signing
pushes somebody else out rather than both of them starting; and it means an
injury to a first-choice striker automatically redistributes his minutes to the
rest of the forward line instead of evaporating.

The prior, before any match has been played, is price — through a softmax
within each club's position group. FPL prices are compressed (a first-choice
centre-back and the fourth choice might be £1.0m apart) but their *ordering* is
informative, and a softmax is the natural way to read an ordering as a set of
shares that has to add up to a fixed number of places.
"""

import datetime as dt
import math
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.form.fixtures import chance_of_playing
from fplquant.lineup.formation import compute_team_shapes
from fplquant.lineup.starts import compute_start_probabilities, did_start
from fplquant.ml.features import MinutesFeatures
from fplquant.ml.minutes_model import TrainedMinutesModel, load
from fplquant.models.orm import Player
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER
from fplquant.schedule import get_next_fixture_by_team

# Softmax temperature over price, in tenths of a million. At £1.0m, a player
# priced £1.0m above a teammate in the same position group carries e times
# their weight in the prior. Chosen so the price ladder inside a squad — where
# a £5.5m defender and a £4.0m one are usually first choice and fourth — maps
# to a believable spread of start odds rather than to near-certainty or to a
# coin flip.
PRICE_TEMPERATURE = 10.0

# Matches of evidence before a player's observed start rate is trusted as much
# as the price prior. Matches `fplquant.lineup.starts`, and for the same
# reason: starting is a low-variance binary a coach repeats week to week, so it
# reveals itself much faster than a scoring rate does.
START_CREDIBILITY_MATCHES = 3.0

# No player is ever certain to start: illness, a late knock, a rested legs
# call. Capping below 1.0 also keeps the normalisation from concentrating a
# whole position group's probability onto one player.
MAX_START_PROBABILITY = 0.97

# Minutes logged by a player who starts, averaged over the ones who play the
# full 90 and the ones who are substituted, and by one who comes off the bench.
MINUTES_IF_START = 84.0
MINUTES_IF_SUBSTITUTE = 20.0

# Prior probability of getting off the bench in a match you don't start, and
# the matches of evidence needed to move it. Five substitutions from a bench of
# nine puts this a little over a half for a fit squad player.
BENCH_APPEARANCE_PRIOR = 0.45
BENCH_CREDIBILITY_MATCHES = 4.0


POSITION_LABELS = {GOALKEEPER: "GK", DEFENDER: "DEF", MIDFIELDER: "MID", FORWARD: "FWD"}


@dataclass(frozen=True)
class MinutesProfile:
    player_id: int
    web_name: str
    team_id: int
    element_type: int
    availability: float  # the hard news gate: 0.0 if injured or suspended
    matches_observed: int
    start_credibility: float  # 0.0 (pure price prior) to 1.0 (pure observed rate)
    observed_start_rate: float
    prior_start_probability: float  # from price alone, before any evidence
    p_start: float
    p_bench_appearance: float
    expected_minutes: float
    source: str = "heuristic"  # or "model", when a trained one is available


def _softmax_weights(costs: list[int], temperature: float) -> list[float]:
    """Relative weights from a price ladder, largest normalised to 1.0.

    Subtracting the maximum before exponentiating is the standard guard
    against overflow, and here it also gives the most expensive player in the
    group a weight of exactly 1.0, which makes the numbers easy to read.
    """
    if not costs:
        return []
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    top = max(costs)
    return [math.exp((cost - top) / temperature) for cost in costs]


def _model_probabilities(
    session: Session, players: list[Player], trained: TrainedMinutesModel
) -> dict[int, float]:
    """Start probability per player from the trained model.

    Features are assembled into the same `MinutesFeatures` struct the training
    frame uses and pushed through the same `feature_vector`, so the serving path
    cannot drift from the training one — the failure mode this guards against
    is silent, and no metric would show it.

    A player with no upcoming fixture is left out rather than given a guess;
    the caller falls back to the heuristic for them.
    """
    next_fixture = get_next_fixture_by_team(session)

    groups: dict[tuple[int, int], list[Player]] = {}
    for player in players:
        groups.setdefault((player.team_id, player.element_type), []).append(player)
    rank_and_share: dict[int, tuple[float, float]] = {}
    for group in groups.values():
        total = sum(p.now_cost for p in group) or 1
        ordered = sorted(group, key=lambda p: -p.now_cost)
        for position_rank, player in enumerate(ordered, start=1):
            rank_and_share[player.id] = (float(position_rank), player.now_cost / total)

    rows: list[MinutesFeatures] = []
    ids: list[int] = []
    for player in players:
        fixture = next_fixture.get(player.team_id)
        if fixture is None or fixture.event is None:
            continue
        stats = sorted(
            player.gameweek_stats,
            key=lambda s: (s.kickoff_time is None, s.kickoff_time, s.round),
        )
        last_kickoff = next((s.kickoff_time for s in reversed(stats) if s.kickoff_time), None)
        rank, share = rank_and_share.get(player.id, (0.0, 0.0))
        rows.append(
            MinutesFeatures(
                value=player.now_cost,
                price_rank_in_group=rank,
                price_share_in_group=share,
                was_home=fixture.team_h_id == player.team_id,
                round=fixture.event,
                rounds_observed=len(stats),
                recent_starts=[1 if did_start(s) else 0 for s in stats],
                recent_minutes=[s.minutes for s in stats],
                last_selected=stats[-1].selected if stats else None,
                rest_days=_rest_days(fixture.kickoff_time, last_kickoff),
                position=POSITION_LABELS.get(player.element_type),
            )
        )
        ids.append(player.id)

    return dict(zip(ids, trained.predict(rows), strict=True))


def _rest_days(kickoff: dt.datetime | None, previous: dt.datetime | None) -> float:
    """Days between a player's last match and the coming one; -1.0 if unknown.

    Mirrors the training-side definition exactly, including the sentinel.
    """
    if kickoff is None or previous is None:
        return -1.0
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=dt.UTC)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.UTC)
    return (kickoff - previous).total_seconds() / 86400


def _redistribute_unavailable(
    estimates: list[float], availability: list[float], cap: float
) -> list[float]:
    """Move an unavailable player's start probability to his teammates.

    Used when a trained model supplies the estimates, in place of normalising
    the group to its formation's slots. The model's probabilities are already
    calibrated in absolute terms, and forcing them onto a 4-4-2 prior actively
    corrupts them: measured against the current pool that prior expects 1.83
    forwards per club while the model expects 0.97 — because modern sides start
    one striker and FPL classifies wingers as midfielders — so every forward in
    the league was being scaled up by nearly a factor of two.

    What the slots normalisation was genuinely good for is redistribution: when
    a first choice is ruled out, somebody else plays. That property is kept here
    without importing a formation prior, by handing exactly the probability mass
    the unavailable players gave up to whoever is left, in proportion to their
    own estimates. The group's total is therefore unchanged, which is the point
    — the model chose it.
    """
    gated = [
        estimate * available for estimate, available in zip(estimates, availability, strict=True)
    ]
    freed = sum(estimates) - sum(gated)
    if freed <= 0:
        return [min(cap, value) for value in gated]

    share_base = sum(gated)
    if share_base <= 0:
        return [min(cap, value) for value in gated]
    return [min(cap, value + freed * value / share_base) for value in gated]


def _normalise_to_slots(values: list[float], slots: float, cap: float) -> list[float]:
    """Scale `values` so they sum to `slots`, without any exceeding `cap`.

    A plain rescale would routinely push a club's first-choice striker past
    certainty — two forwards sharing two slots both want a probability of 1.0 —
    and clipping afterwards would silently lose the excess, leaving the group
    short of eleven players and quietly under-rating every forward line in the
    league relative to every defence.

    So the excess is redistributed instead: cap whoever overflows, subtract
    their fixed allocation from the target, and rescale the rest into what's
    left. This is water-filling, and it terminates because each pass either
    caps at least one more player or finishes.
    """
    result = [0.0] * len(values)
    remaining = [i for i, value in enumerate(values) if value > 0]
    target = slots

    while remaining:
        if target <= 0:
            break
        total = sum(values[i] for i in remaining)
        if total <= 0:
            break
        scale = target / total
        overflowing = [i for i in remaining if values[i] * scale > cap]
        if not overflowing:
            for i in remaining:
                result[i] = values[i] * scale
            break
        for i in overflowing:
            result[i] = cap
            target -= cap
        remaining = [i for i in remaining if i not in overflowing]

    return result


def compute_minutes_profiles(session: Session) -> dict[int, MinutesProfile]:
    """Absolute start probabilities and expected minutes, keyed by player id."""
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    if not players:
        return {}

    slots_by_team = {
        shape.team_id: shape.slots for shape in compute_team_shapes(session, players=players)
    }
    # A trained model replaces the price-and-history blend below when one
    # exists, and is simply absent otherwise — see `ml.minutes_model.load`.
    trained = load()
    model_probabilities = _model_probabilities(session, players, trained) if trained else {}

    # The rotation nudge from the lineup module: rest days before this specific
    # kickoff, and whether their side has been shifting toward their position.
    # It is centred on 1.0, so inside a position group it only ever reorders —
    # the normalisation below strips out any overall level it might carry, which
    # is exactly right for a signal that was built to be relative.
    #
    # Skipped entirely when the model is driving, both because the model already
    # reads rest days and recent selection, and because computing it walks every
    # player's history for a number nothing would then use.
    rotation = (
        {}
        if model_probabilities
        else {p.player_id: p.lineup_multiplier for p in compute_start_probabilities(session)}
    )

    groups: dict[tuple[int, int], list[Player]] = {}
    for player in players:
        groups.setdefault((player.team_id, player.element_type), []).append(player)

    profiles: dict[int, MinutesProfile] = {}
    for (team_id, position), group in groups.items():
        available_slots = slots_by_team.get(team_id, {}).get(position, 0.0)
        weights = _softmax_weights([p.now_cost for p in group], PRICE_TEMPERATURE)
        weight_total = sum(weights)

        blended: list[float] = []
        availability_gates: list[float] = []
        priors: list[float] = []
        for player, weight in zip(group, weights, strict=True):
            prior = available_slots * weight / weight_total if weight_total > 0 else 0.0
            priors.append(prior)

            stats = player.gameweek_stats
            matches = len(stats)
            starts = sum(1 for s in stats if did_start(s))
            observed = starts / matches if matches else 0.0
            credibility = matches / (matches + START_CREDIBILITY_MATCHES) if matches else 0.0

            if player.id in model_probabilities:
                # The model already sees rest days and recent selection, so the
                # rotation nudge is not applied on top — it would charge the
                # same evidence twice.
                blended.append(model_probabilities[player.id])
                availability_gates.append(chance_of_playing(player))
            else:
                estimate = credibility * observed + (1 - credibility) * prior
                # Availability is applied *before* normalisation on purpose: an
                # injured player's share of the position group's slots is then
                # redistributed to his teammates by the scaling below, which is
                # what actually happens when a first choice is ruled out.
                blended.append(estimate * chance_of_playing(player) * rotation.get(player.id, 1.0))
                availability_gates.append(1.0)

        if model_probabilities:
            normalised = _redistribute_unavailable(
                blended, availability_gates, MAX_START_PROBABILITY
            )
        else:
            normalised = _normalise_to_slots(blended, available_slots, MAX_START_PROBABILITY)

        for player, prior, p_start in zip(group, priors, normalised, strict=True):
            stats = player.gameweek_stats
            matches = len(stats)
            starts = sum(1 for s in stats if did_start(s))
            appearances = sum(1 for s in stats if s.minutes > 0)
            availability = chance_of_playing(player)

            non_starts = max(0, matches - starts)
            bench_appearances = max(0, appearances - starts)
            bench_weight = non_starts / (non_starts + BENCH_CREDIBILITY_MATCHES)
            observed_bench = bench_appearances / non_starts if non_starts else 0.0
            p_bench = availability * (
                bench_weight * observed_bench + (1 - bench_weight) * BENCH_APPEARANCE_PRIOR
            )

            expected_minutes = (
                p_start * MINUTES_IF_START + (1 - p_start) * p_bench * MINUTES_IF_SUBSTITUTE
            )

            profiles[player.id] = MinutesProfile(
                player_id=player.id,
                web_name=player.web_name,
                team_id=player.team_id,
                element_type=player.element_type,
                availability=availability,
                matches_observed=matches,
                start_credibility=(
                    matches / (matches + START_CREDIBILITY_MATCHES) if matches else 0.0
                ),
                observed_start_rate=starts / matches if matches else 0.0,
                prior_start_probability=prior,
                p_start=p_start,
                p_bench_appearance=p_bench,
                expected_minutes=expected_minutes,
                source="model" if player.id in model_probabilities else "heuristic",
            )
    return profiles
