import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.form.ewma import ewma
from fplquant.models.orm import InjuryRecord, Player
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

# Currently-unavailable / doubtful statuses, per the FPL API's `status` field.
INJURED_OR_SUSPENDED = {"i", "s"}
DOUBTFUL = "d"

# Injury exposure by position is a broad simplification (attacking/wide
# positions generally carry higher soft-tissue injury rates from sprint load
# than goalkeepers), not a rigorously fitted estimate.
POSITION_RISK = {GOALKEEPER: 0.3, DEFENDER: 0.6, MIDFIELDER: 0.8, FORWARD: 1.0}

HISTORY_LOOKBACK_DAYS = 3 * 365
HISTORY_FREQUENCY_CAP = 6  # 6+ injuries in the lookback window maxes out the frequency signal
HISTORY_SEVERITY_CAP_DAYS = 270  # ~9 months out in the lookback window maxes out severity


@dataclass(frozen=True)
class RiskWeights:
    """Component weights for the combined risk score. Need not sum to 1.0 —
    the combined score is clipped to [0, 1] before conversion to a percentage.
    """

    age: float = 0.15
    position: float = 0.10
    history: float = 0.30
    load: float = 0.15
    status: float = 0.30


@dataclass(frozen=True)
class InjuryRiskScore:
    player_id: int
    web_name: str
    age: float | None
    age_component: float
    position_component: float
    history_component: float
    load_component: float
    status_component: float
    risk_pct: float


def compute_age(birth_date: dt.date | None, as_of: dt.date) -> float | None:
    if birth_date is None:
        return None
    return (as_of - birth_date).days / 365.25


def _age_component(age: float | None) -> float:
    """Risk rises past 28, roughly tracking slower injury recovery with age.

    Unknown age (missing birth_date) gets a small flat default rather than 0,
    since "unknown" shouldn't be treated as "definitely young and low-risk".
    """
    if age is None:
        return 0.3
    if age <= 28:
        return 0.0
    return min(1.0, (age - 28) / 10)


def _position_component(element_type: int) -> float:
    return POSITION_RISK.get(element_type, 0.7)


def _history_component(
    injury_records: list[InjuryRecord], as_of: dt.date, lookback_days: int = HISTORY_LOOKBACK_DAYS
) -> float:
    """Blends injury frequency and total days lost within the lookback window."""
    recent = [
        r for r in injury_records if r.start_date and (as_of - r.start_date).days <= lookback_days
    ]
    if not recent:
        return 0.0
    frequency_score = min(1.0, len(recent) / HISTORY_FREQUENCY_CAP)
    days_out_total = sum(r.days_out or 0 for r in recent)
    severity_score = min(1.0, days_out_total / HISTORY_SEVERITY_CAP_DAYS)
    return 0.5 * frequency_score + 0.5 * severity_score


def _load_component(minutes_history: list[int], halflife: float = 4.0) -> float:
    """EWMA of recent minutes as a fatigue/accumulated-load proxy.

    This is a simplification: it treats sustained heavy minutes as the risk
    signal, distinct from current-unavailability (captured separately by the
    status component below).
    """
    if not minutes_history:
        return 0.0
    average_minutes = ewma([float(m) for m in minutes_history], halflife)
    return min(1.0, average_minutes / 90.0)


def _status_component(status: str, chance_of_playing_next_round: int | None) -> float:
    if status in INJURED_OR_SUSPENDED:
        return 1.0
    if status == DOUBTFUL:
        if chance_of_playing_next_round is not None:
            return max(0.0, (100 - chance_of_playing_next_round) / 100)
        return 0.5
    return 0.0


def compute_injury_risk_scores(
    session: Session,
    as_of: dt.date | None = None,
    weights: RiskWeights | None = None,
) -> list[InjuryRiskScore]:
    """Rank all players by a weighted injury risk score (0-100%).

    Combines: age (older players score higher), position (attacking/wide
    positions score higher), injury history (frequency + severity in the last
    3 years, from Transfermarkt), recent minutes load (EWMA, fatigue proxy),
    and current FPL status (injured/suspended/doubtful overrides everything
    else, since it reflects real-time availability).
    """
    as_of = as_of or dt.date.today()
    weights = weights or RiskWeights()

    players = (
        session.query(Player)
        .options(selectinload(Player.injury_records), selectinload(Player.gameweek_stats))
        .all()
    )

    scores = []
    for player in players:
        age = compute_age(player.birth_date, as_of)
        # Minutes per gameweek, summed over a double's two fixtures — the
        # load component asks how hard a player has been worked lately, and a
        # week in which he played twice is the answer, not two lighter weeks.
        minutes_by_round: dict[int, int] = {}
        for stat in player.gameweek_stats:
            minutes_by_round[stat.round] = minutes_by_round.get(stat.round, 0) + stat.minutes
        minutes_history = [minutes_by_round[r] for r in sorted(minutes_by_round)]

        age_c = _age_component(age)
        position_c = _position_component(player.element_type)
        history_c = _history_component(player.injury_records, as_of)
        load_c = _load_component(minutes_history)
        status_c = _status_component(player.status, player.chance_of_playing_next_round)

        combined = (
            weights.age * age_c
            + weights.position * position_c
            + weights.history * history_c
            + weights.load * load_c
            + weights.status * status_c
        )

        scores.append(
            InjuryRiskScore(
                player_id=player.id,
                web_name=player.web_name,
                age=age,
                age_component=age_c,
                position_component=position_c,
                history_component=history_c,
                load_component=load_c,
                status_component=status_c,
                risk_pct=round(min(1.0, combined) * 100, 1),
            )
        )
    return sorted(scores, key=lambda s: s.risk_pct, reverse=True)
