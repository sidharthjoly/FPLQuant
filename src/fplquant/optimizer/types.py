from dataclasses import dataclass, field

# FPL element_type codes.
GOALKEEPER = 1
DEFENDER = 2
MIDFIELDER = 3
FORWARD = 4

POSITION_NAMES = {
    GOALKEEPER: "GKP",
    DEFENDER: "DEF",
    MIDFIELDER: "MID",
    FORWARD: "FWD",
}


@dataclass(frozen=True)
class PlayerCandidate:
    """A player as seen by the optimizer: just the fields the ILP needs."""

    player_id: int
    web_name: str
    team_id: int
    team_short_name: str
    element_type: int
    now_cost: int  # tenths of a million
    predicted_points: float  # fixture-adjusted expected points for the next match
    next_opponent: str | None = None
    next_opponent_is_home: bool | None = None
    fixture_difficulty: int | None = None  # FPL's own 1 (easiest) - 5 (hardest) rating
    # Two different questions, and they must not be conflated. `chance_of_playing`
    # is fitness — FPL's own press-conference number, "will he be available".
    # `start_probability` is selection — "will he be named in the XI" — which
    # folds that fitness together with how often his coach actually picks him.
    # A fully fit squad player has chance_of_playing 1.0 and a start_probability
    # well below it. Only the horizon path models selection, so it is the only
    # one that populates the second; None means "not modelled here", not "zero".
    chance_of_playing: float = 1.0  # 0.0-1.0
    start_probability: float | None = None  # 0.0-1.0, horizon path only


@dataclass(frozen=True)
class SquadConstraints:
    budget: int = 1000  # £100.0m, in tenths of a million
    position_limits: dict[int, int] = field(
        default_factory=lambda: {GOALKEEPER: 2, DEFENDER: 5, MIDFIELDER: 5, FORWARD: 3}
    )
    max_per_club: int = 3

    @property
    def squad_size(self) -> int:
        return sum(self.position_limits.values())


@dataclass(frozen=True)
class OptimizedSquad:
    players: list[PlayerCandidate]
    total_cost: int
    total_predicted_points: float


@dataclass(frozen=True)
class StartingXI:
    """The best valid starting XI from a 15-man squad, plus the
    decision-support numbers that come along with it for free."""

    formation: str  # e.g. "3-4-3" (DEF-MID-FWD; GKP is always 1, omitted)
    starters: list[PlayerCandidate]
    bench: list[PlayerCandidate]  # bench GKP first, then outfield subs by predicted_points
    captain: PlayerCandidate
    vice_captain: PlayerCandidate
    starting_predicted_points: float  # sum over starters, captain NOT doubled
    bench_boost_value: float  # extra points if the bench also counted this week
    triple_captain_value: float  # extra points from 3x vs normal 2x captaincy


class InfeasibleSquadError(RuntimeError):
    """Raised when no squad satisfies the given constraints (e.g. budget too low)."""
