"""Turn archived player-gameweeks into a training frame for the minutes model.

Every feature here has to be knowable *before* the deadline it predicts. That
sounds obvious and is the single easiest thing to get wrong in this kind of
model: the archive row for a gameweek contains that gameweek's minutes, points
and bps, and a model handed any of those learns to read the answer off the
question. It would score beautifully and be worthless.

So the rule enforced throughout is that anything describing a player's
*performance* is lagged — computed only from rounds strictly before the one
being predicted — while the handful of things genuinely fixed ahead of a
deadline (price, venue, position, who else plays their position) may be read
from the row itself. Ownership and transfer counts are lagged too even though
they are partly visible before a deadline, because "partly" is not a property
you can encode in a feature.

Built with plain iteration rather than a dataframe library. Lag features are
where correctness actually matters, and an explicit walk in round order is
easier to check than a chain of groupbys — and it avoids a heavy dependency in
a project that has so far needed none.
"""

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from fplquant.models.orm import HistoricalPlayerGameweek

# FPL briefly published an "AM" position in 2024-25. It is a midfielder for
# every scoring purpose, and 322 rows is far too few to learn a separate
# category from, so it folds into MID rather than becoming a rare level the
# model would overfit.
POSITION_ALIASES = {"AM": "MID"}
POSITIONS = ("GK", "DEF", "MID", "FWD")

# Rolling windows for recent form, in appearances-eligible rounds.
SHORT_WINDOW = 3
LONG_WINDOW = 5

FEATURE_NAMES = (
    "value",
    "value_rank_in_team_position",
    "value_share_in_team_position",
    "was_home",
    "round",
    "rounds_observed",
    "started_prev",
    "start_rate_short",
    "start_rate_long",
    "start_rate_season",
    "minutes_prev",
    "minutes_mean_short",
    "minutes_mean_long",
    "rest_days",
    "selected_prev",
    "position_gk",
    "position_def",
    "position_mid",
    "position_fwd",
)


@dataclass(frozen=True)
class MinutesFeatures:
    """The raw inputs a minutes prediction needs, from whatever source.

    Deliberately source-agnostic. Training reads them from the archived
    seasons; serving reads them from the live database — and if those two
    paths each built their own vector, they would drift, silently, in a way no
    test catches and no metric shows. A model fed features assembled slightly
    differently from the ones it learned on is not a worse model, it is a
    different one. So both go through `feature_vector` below, and this struct
    is the only thing either side has to construct.
    """

    value: int
    price_rank_in_group: float
    price_share_in_group: float
    was_home: bool | None
    round: int
    rounds_observed: int
    recent_starts: list[int]  # oldest first, strictly before the round predicted
    recent_minutes: list[int]
    last_selected: int | None
    rest_days: float
    position: str | None


def feature_vector(features: MinutesFeatures) -> list[float]:
    """The single definition of the model's input row.

    Missing history is encoded as -1.0 rather than 0.0 throughout. Zero is a
    real value for every one of these — a player who started none of his last
    five is genuinely 0.0 — and collapsing "no evidence" into it would teach
    the model that a debutant looks exactly like someone repeatedly dropped.
    A tree can split -1.0 off on its own.
    """
    starts, minutes = features.recent_starts, features.recent_minutes
    observed = features.rounds_observed
    position = _normalize_position(features.position)
    return [
        float(features.value),
        features.price_rank_in_group,
        features.price_share_in_group,
        1.0 if features.was_home else 0.0,
        float(features.round),
        float(observed),
        float(starts[-1]) if starts else -1.0,
        _mean(starts, SHORT_WINDOW),
        _mean(starts, LONG_WINDOW),
        sum(starts) / len(starts) if starts else -1.0,
        float(minutes[-1]) if minutes else -1.0,
        _mean(minutes, SHORT_WINDOW),
        _mean(minutes, LONG_WINDOW),
        features.rest_days,
        float(features.last_selected) if features.last_selected is not None else -1.0,
        1.0 if position == "GK" else 0.0,
        1.0 if position == "DEF" else 0.0,
        1.0 if position == "MID" else 0.0,
        1.0 if position == "FWD" else 0.0,
    ]


@dataclass
class _PlayerHistory:
    """Rolling state for one player within one season."""

    starts: list[int] = field(default_factory=list)
    minutes: list[int] = field(default_factory=list)
    selected: list[int] = field(default_factory=list)
    last_kickoff: dt.datetime | None = None


@dataclass(frozen=True)
class TrainingFrame:
    """Feature matrix, labels, and the season each row came from.

    `seasons` is kept alongside so evaluation can split on it. A random split
    would leak: the same player's neighbouring gameweeks are highly correlated,
    so a model that memorises "this player starts" scores well on a shuffled
    holdout and tells you nothing about next season.
    """

    features: list[list[float]]
    labels: list[int]
    seasons: list[str]
    rounds: list[int]

    def __len__(self) -> int:
        return len(self.labels)


def _normalize_position(position: str | None) -> str | None:
    if position is None:
        return None
    return POSITION_ALIASES.get(position, position)


def _mean(values: list[int], window: int) -> float:
    recent = values[-window:]
    return sum(recent) / len(recent) if recent else 0.0


def _rest_days(kickoff: dt.datetime | None, previous: dt.datetime | None) -> float:
    """Days between a player's last match and this one.

    Rotation's most reliable driver, and one of the few signals that carries
    information in the opening rounds because it comes from the calendar rather
    than from match history. -1.0 marks "no previous match", which the model can
    split on; a zero would be a lie about a real quantity.
    """
    if kickoff is None or previous is None:
        return -1.0
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=dt.UTC)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.UTC)
    return (kickoff - previous).total_seconds() / 86400


def _team_price_context(
    rows: list[HistoricalPlayerGameweek],
) -> dict[int, tuple[float, float]]:
    """Per row id: the player's price rank and price share within their club's
    position group that round.

    The competition feature, and the one closest to what the hand-built model
    encodes as a softmax over price. Being the fourth most expensive centre-back
    at your club says something no individual attribute does, and it is
    available before any ball is kicked — which is exactly when a minutes model
    is least able to lean on history.
    """
    groups: dict[tuple[str, str | None, str | None, int], list[HistoricalPlayerGameweek]] = {}
    for row in rows:
        key = (row.season, row.team, _normalize_position(row.position), row.round)
        groups.setdefault(key, []).append(row)

    context: dict[int, tuple[float, float]] = {}
    for group in groups.values():
        ordered = sorted(group, key=lambda r: -r.value)
        total = sum(r.value for r in group) or 1
        for rank, row in enumerate(ordered, start=1):
            context[row.id] = (float(rank), row.value / total)
    return context


def build_training_frame(
    session: Session, seasons: tuple[str, ...] | list[str] | None = None
) -> TrainingFrame:
    """Assemble features and labels from the imported archive."""
    query = session.query(HistoricalPlayerGameweek)
    if seasons:
        query = query.filter(HistoricalPlayerGameweek.season.in_(list(seasons)))
    rows = query.all()

    # Chronological order, not round order. Postponed and rearranged fixtures
    # mean a round-7 match can be played after a round-9 one, and ordering by
    # round would then feed a later match's result into an earlier match's
    # history — leakage, and the reason `rest_days` came out negative for a
    # thousand rows before this sort was fixed. Rows with no kickoff time fall
    # back to round so they stay in a sensible place rather than all landing
    # at the epoch.
    def _chronological(row: HistoricalPlayerGameweek) -> tuple[str, int, float, int]:
        when = row.kickoff_time
        if when is None:
            return (row.season, row.element, float(row.round), row.fixture)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return (row.season, row.element, when.timestamp(), row.fixture)

    rows.sort(key=_chronological)

    price_context = _team_price_context(rows)
    histories: dict[tuple[str, int], _PlayerHistory] = {}

    features: list[list[float]] = []
    labels: list[int] = []
    row_seasons: list[str] = []
    row_rounds: list[int] = []

    for row in rows:
        if row.starts is None:
            continue  # no published label; see history.SEASONS_WITH_STARTS
        position = _normalize_position(row.position)
        key = (row.season, row.element)
        history = histories.setdefault(key, _PlayerHistory())

        rank, share = price_context.get(row.id, (0.0, 0.0))
        vector = feature_vector(
            MinutesFeatures(
                value=row.value,
                price_rank_in_group=rank,
                price_share_in_group=share,
                was_home=row.was_home,
                round=row.round,
                rounds_observed=len(history.starts),
                recent_starts=history.starts,
                recent_minutes=history.minutes,
                last_selected=history.selected[-1] if history.selected else None,
                rest_days=_rest_days(row.kickoff_time, history.last_kickoff),
                position=position,
            )
        )
        features.append(vector)
        labels.append(int(row.starts))
        row_seasons.append(row.season)
        row_rounds.append(row.round)

        # Update history *after* emitting the row, so nothing describing this
        # match can reach this match's features.
        history.starts.append(int(row.starts))
        history.minutes.append(row.minutes)
        history.selected.append(row.selected)
        if row.kickoff_time is not None:
            history.last_kickoff = row.kickoff_time

    return TrainingFrame(features=features, labels=labels, seasons=row_seasons, rounds=row_rounds)
