from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fplquant.optimizer.starting_xi import VALID_FORMATIONS

_VALID_FORMATION_STRINGS = {f"{d}-{m}-{f}" for d, m, f in VALID_FORMATIONS}


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fpl_id: int
    web_name: str
    full_name: str
    team_id: int
    team_short_name: str
    element_type: int
    now_cost: int
    status: str
    selected_by_percent: float
    form: float
    ep_next: float
    nationality: str | None = None
    photo_url: str | None = None


class FormScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    web_name: str
    matches_considered: int
    points_form: float
    underlying_form: float
    combined_score: float


class InjuryRiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    web_name: str
    age: float | None
    age_component: float
    position_component: float
    history_component: float
    load_component: float
    status_component: float
    risk_pct: float


class StartOddsOut(BaseModel):
    """How likely a player is to be *named in the XI* for their next match, with
    the evidence behind it — see `fplquant.lineup.starts`.

    Distinct from `PlayerDetailOut.chance_of_playing`, which is only the fitness
    news. `start_probability` is that news combined with how often this player's
    coach actually picks them, moved by the rest they've had before this kickoff
    and by any shift in their club's shape. `evidence_weight` says how much of
    that rests on their own record rather than a positional prior — near zero
    early in a season, when the number should not be presented as a finding.
    """

    player_id: int
    appearances: int
    start_probability: float  # selection odds, gated by the fitness news
    availability: float  # the news gate on its own, 0.0-1.0
    baseline_probability: float  # how often they start, all else equal
    adjusted_probability: float  # ...after rest and their club's shape
    evidence_weight: float  # 0.0-1.0, own record vs. positional prior
    fatigue_index: float  # 0.0 (fresh) - 1.0 (short turnaround after a full workload)
    minutes_load: float  # share of available minutes played recently
    rest_days: float | None  # days between their last appearance and this kickoff
    formation_factor: float  # >1 if their club has shifted toward their position
    team_shape: str  # their club's season-long shape, e.g. "4-4-2"
    recent_team_shape: str  # ...and the shape it has been naming lately


class PlayerDetailOut(PlayerOut):
    form_score: FormScoreOut | None = None
    injury_risk: InjuryRiskOut | None = None
    next_opponent: str | None = None
    next_opponent_is_home: bool | None = None
    fixture_difficulty: int | None = None
    chance_of_playing: float = 1.0
    start_odds: StartOddsOut | None = None


class PlayerGameweekStatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    round: int
    total_points: int
    minutes: int
    value: int
    selected: int


class PriceMomentumOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    web_name: str
    team_short_name: str = ""
    gameweeks_considered: int
    price_change: int
    price_change_pct: float
    net_transfers: int
    ownership_change: int
    ownership_change_pct: float
    # FPL's own price forecast, where the game publishes it (2026-27 onward).
    progress_percent: float | None = None
    projected_changes: int | None = None


class VolatilityScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    web_name: str
    gameweeks_considered: int
    points_mean: float
    points_stdev: float
    coefficient_of_variation: float | None


class TeammateCorrelationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    team_id: int
    player_a_id: int
    player_a_web_name: str
    player_b_id: int
    player_b_web_name: str
    overlap_gameweeks: int
    correlation: float


class SimilarPlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    web_name: str
    team_id: int
    team_short_name: str
    now_cost: int
    similarity: float


class SquadPlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    web_name: str
    team_id: int
    team_short_name: str
    element_type: int
    now_cost: int
    predicted_points: float
    next_opponent: str | None = None
    next_opponent_is_home: bool | None = None
    fixture_difficulty: int | None = None
    chance_of_playing: float = 1.0


class OptimizeRequest(BaseModel):
    budget: float = Field(default=100.0, gt=0, description="Budget in millions, e.g. 100.0")
    max_per_club: int = Field(default=3, ge=1)
    risk_adjusted: bool = Field(
        default=False, description="Maximize risk-adjusted points instead of raw points"
    )
    risk_aversion: float = Field(default=1.0, ge=0, description="Only with risk_adjusted=true")
    injury_weight: float = Field(default=1.0, ge=0, description="Only with risk_adjusted=true")
    formation: str | None = Field(
        default=None,
        description=(
            "Force a specific starting XI formation, e.g. '3-4-3'. "
            "Omit to auto-select the highest-scoring formation."
        ),
    )

    @field_validator("formation")
    @classmethod
    def _validate_formation(cls, value: str | None) -> str | None:
        if value is not None and value not in _VALID_FORMATION_STRINGS:
            raise ValueError(f"formation must be one of {sorted(_VALID_FORMATION_STRINGS)}")
        return value


class StartingXIOut(BaseModel):
    formation: str
    starters: list[SquadPlayerOut]
    bench: list[SquadPlayerOut]
    captain: SquadPlayerOut
    vice_captain: SquadPlayerOut
    starting_predicted_points: float
    bench_boost_value: float
    triple_captain_value: float


class OptimizeResponse(BaseModel):
    total_cost: int
    total_predicted_points: float
    squad: list[SquadPlayerOut]
    starting_xi: StartingXIOut


class TransferPlanRequest(BaseModel):
    fpl_team_id: int = Field(
        ..., gt=0, description="Your public FPL team ID (from your team's URL)"
    )
    free_transfers: int = Field(default=1, ge=0, le=5)
    chip: Literal["none", "wildcard", "free_hit"] = Field(
        default="none",
        description="Playing wildcard or free hit removes the transfer limit and the point hit",
    )
    max_per_club: int = Field(default=3, ge=1)
    risk_adjusted: bool = Field(
        default=False, description="Maximize risk-adjusted points instead of raw points"
    )
    risk_aversion: float = Field(default=1.0, ge=0, description="Only with risk_adjusted=true")
    injury_weight: float = Field(default=1.0, ge=0, description="Only with risk_adjusted=true")


class TransferPairOut(BaseModel):
    out: SquadPlayerOut
    player_in: SquadPlayerOut


class TransferPlanResponse(BaseModel):
    team_name: str
    event_id: int  # the gameweek the current squad snapshot was taken from
    bank: int  # tenths of a million, e.g. 5 = £0.5m
    chip: Literal["none", "wildcard", "free_hit"]
    current_squad: list[SquadPlayerOut]
    transfers: list[TransferPairOut]
    transfers_made: int
    free_transfers: int
    hit_cost: int
    points_gain_before_hit: float
    points_gain_after_hit: float
    worth_it: bool
    resulting_squad: list[SquadPlayerOut]
    starting_xi: StartingXIOut


class NextDeadlineOut(BaseModel):
    deadline: str | None = Field(
        default=None, description="ISO 8601 UTC deadline for the next gameweek, or null preseason"
    )
    gameweek: int | None = None


class RemainingGameweeksOut(BaseModel):
    """Which gameweeks still have football left in them.

    Read from the fixture list rather than counted forward from the current
    round, so a gameweek that has been entirely wiped out never appears and a
    part-played one still does.
    """

    count: int
    events: list[int]


class PointsBreakdownOut(BaseModel):
    """Expected points for one player in one fixture, split by scoring rule."""

    model_config = ConfigDict(from_attributes=True)

    appearance: float
    goals: float
    assists: float
    clean_sheet: float
    goals_conceded: float
    saves: float
    bonus: float
    cards: float
    defensive_contribution: float
    clean_sheet_probability: float
    total: float


class FixtureProjectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fixture_id: int
    event: int
    opponent_short_name: str | None
    is_home: bool
    lambda_for: float = Field(description="Expected goals for the player's side in this fixture")
    lambda_against: float
    breakdown: PointsBreakdownOut


class EventProjectionOut(BaseModel):
    event: int
    points: float
    is_blank: bool = Field(description="Their club has no fixture this gameweek")
    is_double: bool = Field(description="Their club plays twice this gameweek")
    fixtures: list[FixtureProjectionOut]


class PlayerOutcomeOut(BaseModel):
    """The simulated distribution of a player's points, from a Monte Carlo run."""

    model_config = ConfigDict(from_attributes=True)

    mean: float
    median: float
    stdev: float
    floor: float
    ceiling: float
    blank_probability: float
    haul_probability: float


class HorizonProjectionOut(BaseModel):
    player_id: int
    web_name: str
    team_id: int
    team_short_name: str
    element_type: int
    now_cost: int
    total_points: float
    discounted_points: float
    next_event_points: float
    p_start: float
    expected_minutes: float
    goal_share: float
    assist_share: float
    rate_credibility: float = Field(
        description="0.0 = the estimate is entirely price-implied, 1.0 = entirely their own record"
    )
    events: list[EventProjectionOut]
    outcome: PlayerOutcomeOut | None = None


class ProjectionResponse(BaseModel):
    horizon: int
    events: list[int]
    decay: float
    simulated_event: int | None = None
    simulations: int | None = None
    players: list[HorizonProjectionOut]


class PlanRequest(BaseModel):
    horizon: int = Field(default=5, ge=1, le=10, description="Gameweeks to plan over")
    budget: float = Field(default=100.0, gt=0, description="Budget in millions")
    max_per_club: int = Field(default=3, ge=1)
    decay: float = Field(default=0.9, gt=0, le=1, description="Per-gameweek discount")
    fpl_team_id: int | None = Field(
        default=None, description="Plan from a real FPL squad instead of building from scratch"
    )
    free_transfers: int = Field(default=1, ge=0, le=5)
    chips: list[Literal["wildcard", "bench_boost", "triple_captain", "free_hit"]] = Field(
        default_factory=list,
        description=(
            "Chips the planner may schedule, each played at most once per half-season "
            "(gameweeks 1-19 and 20-38, matching FPL's two chip sets). A free hit is never "
            "scheduled in the final gameweek of the horizon, because its cost falls in the "
            "week the squad reverts and that week would be outside the plan."
        ),
    )


class TransferMoveOut(BaseModel):
    out: SquadPlayerOut | None = None
    in_: SquadPlayerOut | None = Field(default=None, alias="in")

    model_config = ConfigDict(populate_by_name=True)


class GameweekPlanOut(BaseModel):
    event: int
    expected_points: float
    free_transfers_available: int
    hits_taken: int
    hit_cost: int
    chip: str | None
    transfers: list[TransferMoveOut]
    squad: list[SquadPlayerOut]
    starting_xi: StartingXIOut


class PlanResponse(BaseModel):
    events: list[int]
    total_expected_points: float
    total_hit_cost: int
    solver_status: str
    team_name: str | None = None
    gameweeks: list[GameweekPlanOut]
