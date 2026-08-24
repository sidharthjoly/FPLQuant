import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.form.scoring import predicted_points_by_player
from fplquant.lineup.starts import lineup_multipliers_by_player
from fplquant.models.orm import Player, Team
from fplquant.optimizer.types import DEFENDER, GOALKEEPER
from fplquant.schedule import get_next_fixture_by_team

# Clamp the opponent-strength multiplier so a single very strong/weak opponent
# can't swing a player's expected points more than this — the FDR-style signal
# should nudge the ranking, not dominate it.
_MIN_MULTIPLIER = 0.7
_MAX_MULTIPLIER = 1.3


@dataclass(frozen=True)
class FixtureAdjustedScore:
    player_id: int
    web_name: str
    base_points: float  # the season-form estimate, before any fixture adjustment
    opponent_team_id: int | None
    opponent_short_name: str | None
    is_home: bool | None
    difficulty: int | None  # FPL's own 1 (easiest) - 5 (hardest) rating for this fixture
    fixture_multiplier: float | None  # our own continuous opponent-strength multiplier
    chance_of_playing: float  # 0.0-1.0
    lineup_multiplier: float  # rotation/rest nudge; 1.0 when we know nothing
    adjusted_points: float  # base * fixture_multiplier * chance_of_playing * lineup_multiplier


# The strength columns to read, in order of preference. FPL publishes a
# granular attack/defence rating per venue, and also a coarse `strength_overall`
# on a 1-5 scale. The granular pair is the better signal — it separates a side
# that scores freely and concedes freely from a solid one — but it is *not
# always populated*: for much of preseason and into the opening rounds FPL
# leaves all four granular columns at zero for all twenty clubs, at which point
# the only rating with any information in it is the coarse one.
_ATTACK_COLUMNS = ("strength_attack_home", "strength_attack_away")
_DEFENCE_COLUMNS = ("strength_defence_home", "strength_defence_away")
_OVERALL_COLUMNS = ("strength_overall_home", "strength_overall_away")


@dataclass(frozen=True)
class _StrengthScale:
    """Which team-strength column to read, and the league average of it.

    Bundled together because the two have to agree: comparing a club's rating
    on one scale against a league average computed on another produces a
    multiplier that is nonsense rather than merely noisy.
    """

    home_attribute: str
    away_attribute: str
    league_average: float

    def rating(self, team: Team, is_home: bool) -> float:
        attribute = self.home_attribute if is_home else self.away_attribute
        return float(getattr(team, attribute))

    @property
    def is_informative(self) -> bool:
        return self.league_average > 0


def _strength_scale(teams: list[Team], *candidates: tuple[str, str]) -> _StrengthScale:
    """The first of `candidates` whose column pair actually separates the clubs.

    A column where every club has the same number — the granular strengths in
    preseason, which are simply zero for all twenty — is not a weak signal to
    be used cautiously, it is the absence of one, and reading it yields a
    multiplier of exactly 1.0 for every player in the league. Falling through
    to the next candidate is what keeps the fixture adjustment alive at the
    point in the season when there is least else to go on.
    """
    for home_attribute, away_attribute in candidates:
        values = [
            float(getattr(team, attribute))
            for team in teams
            for attribute in (home_attribute, away_attribute)
        ]
        if values and max(values) > 0 and max(values) > min(values):
            return _StrengthScale(home_attribute, away_attribute, statistics.fmean(values))
    # Nothing to go on — every club looks identical on every scale. A neutral
    # scale makes `_fixture_multiplier` return 1.0, which is the honest answer.
    return _StrengthScale(*candidates[0], 0.0)


def _league_strength_scales(teams: list[Team]) -> tuple[_StrengthScale, _StrengthScale]:
    """The attack and defence scales to judge this league's fixtures on.

    Chosen independently, so a season where FPL has published attack ratings
    but not defence ones uses the best available for each rather than dropping
    both to the coarse rating.
    """
    return (
        _strength_scale(teams, _ATTACK_COLUMNS, _OVERALL_COLUMNS),
        _strength_scale(teams, _DEFENCE_COLUMNS, _OVERALL_COLUMNS),
    )


def _fixture_multiplier(
    element_type: int,
    opponent: Team,
    opponent_is_home: bool,
    attack_scale: _StrengthScale,
    defence_scale: _StrengthScale,
) -> float:
    """How much easier/harder this fixture is than average, for this position.

    Goalkeepers and defenders score heavily from clean sheets, so what
    matters to them is the opponent's *attack* strength. Midfielders and
    forwards score from goal involvements, so what matters to them is the
    opponent's *defence* strength. Either way, a stronger opponent in the
    relevant discipline means a smaller multiplier.

    Both directions read the same way whichever scale is in use: FPL's ratings
    go up as a club gets better, so the overall rating stands in for either
    discipline without inverting. It is a blunter instrument — four distinct
    values across twenty clubs, against a granular scale in the thousands — so
    the clamps below bind more often when it is the one being read. That is the
    right failure mode: a saturated nudge in the correct direction beats no
    nudge at all, which is what this returned before the fallback existed.
    """
    scale = attack_scale if element_type in (GOALKEEPER, DEFENDER) else defence_scale
    if not scale.is_informative:
        return 1.0

    relevant = scale.rating(opponent, opponent_is_home)
    if relevant <= 0:
        return 1.0
    multiplier = scale.league_average / relevant
    return max(_MIN_MULTIPLIER, min(_MAX_MULTIPLIER, multiplier))


def chance_of_playing(player: Player) -> float:
    """Estimated probability `player` plays their next match, 0.0-1.0.

    FPL's own `chance_of_playing_next_round` is authoritative when set (it's
    how they surface manager press-conference news, e.g. 75/50/25/0). When
    it's absent, an "a" (available) status means fully expected to play;
    any other status (injured/suspended/unavailable/on loan) with no percent
    given is treated as not expected to play, matching FPL's own convention
    that those statuses default to no percentage only when the outlook is
    clear-cut.
    """
    if player.chance_of_playing_next_round is not None:
        return player.chance_of_playing_next_round / 100
    return 1.0 if player.status == "a" else 0.0


def compute_fixture_adjusted_scores(
    session: Session,
    halflife: float = 3.0,
    lineup_multipliers: dict[int, float] | None = None,
) -> list[FixtureAdjustedScore]:
    """Expected points for each player's next match specifically — folding in
    FPL's official fixture difficulty, our own continuous opponent-strength
    multiplier, home/away venue, and the chance the player actually plays.

    This is the "will this player have a good game against this opponent at
    this venue" signal, built on top of the season-form baseline from
    `fplquant.form.scoring.predicted_points_by_player`.

    Two separate availability terms apply here and they are not redundant.
    `chance_of_playing` is the hard news gate — injured or suspended means zero,
    and it must stay hard. `lineup_multiplier` is the soft rotation nudge from
    `fplquant.lineup.starts`: given that they're fit, is this a week they're
    more or less likely than usual to be picked, on the evidence of their rest
    and their side's shape. It is centred on 1.0 and does nothing until there's
    something to say.

    `lineup_multipliers` is accepted pre-computed for callers that already have
    the full start-probability breakdown in hand, so they don't pay for a second
    walk over every player's history — the same idiom as the `players` argument
    on `fplquant.lineup.fatigue.compute_fatigue_scores`.
    """
    base_points = predicted_points_by_player(session, halflife)
    if lineup_multipliers is None:
        lineup_multipliers = lineup_multipliers_by_player(session)
    next_fixture_by_team = get_next_fixture_by_team(session)
    teams_by_id = {t.id: t for t in session.query(Team).all()}
    attack_scale, defence_scale = _league_strength_scales(list(teams_by_id.values()))

    scores = []
    for player in session.query(Player).all():
        base = base_points.get(player.id, 0.0)
        fixture = next_fixture_by_team.get(player.team_id)
        play_prob = chance_of_playing(player)
        lineup = lineup_multipliers.get(player.id, 1.0)

        if fixture is None:
            # No fixture data to adjust by (a genuine blank gameweek, or
            # fixtures just haven't been ingested yet) — degrade to the
            # unadjusted season-form estimate rather than zeroing out, same
            # philosophy as predicted_points_by_player falling back to
            # ep_next with no gameweek history: a fixture signal is never a
            # hard dependency for producing *some* estimate.
            scores.append(
                FixtureAdjustedScore(
                    player_id=player.id,
                    web_name=player.web_name,
                    base_points=base,
                    opponent_team_id=None,
                    opponent_short_name=None,
                    is_home=None,
                    difficulty=None,
                    fixture_multiplier=None,
                    chance_of_playing=play_prob,
                    lineup_multiplier=lineup,
                    adjusted_points=base * play_prob * lineup,
                )
            )
            continue

        is_home = fixture.team_h_id == player.team_id
        opponent_id = fixture.team_a_id if is_home else fixture.team_h_id
        opponent = teams_by_id.get(opponent_id)
        difficulty = fixture.team_h_difficulty if is_home else fixture.team_a_difficulty

        if opponent is None:
            multiplier = 1.0
        else:
            multiplier = _fixture_multiplier(
                player.element_type, opponent, not is_home, attack_scale, defence_scale
            )

        scores.append(
            FixtureAdjustedScore(
                player_id=player.id,
                web_name=player.web_name,
                base_points=base,
                opponent_team_id=opponent_id,
                opponent_short_name=opponent.short_name if opponent else None,
                is_home=is_home,
                difficulty=difficulty,
                fixture_multiplier=multiplier,
                chance_of_playing=play_prob,
                lineup_multiplier=lineup,
                adjusted_points=base * multiplier * play_prob * lineup,
            )
        )
    return sorted(scores, key=lambda s: s.adjusted_points, reverse=True)
