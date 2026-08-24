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


def _league_average_strengths(teams: list[Team]) -> tuple[float, float]:
    """League-average attack and defence strength, blended across home/away.

    Computed from the pool itself rather than hardcoded, so this keeps
    working if FPL ever rescales their strength ratings.
    """
    attack_values = [t.strength_attack_home for t in teams] + [
        t.strength_attack_away for t in teams
    ]
    defence_values = [t.strength_defence_home for t in teams] + [
        t.strength_defence_away for t in teams
    ]
    avg_attack = statistics.fmean(attack_values) if attack_values else 1.0
    avg_defence = statistics.fmean(defence_values) if defence_values else 1.0
    return avg_attack, avg_defence


def _fixture_multiplier(
    element_type: int,
    opponent: Team,
    opponent_is_home: bool,
    league_avg_attack: float,
    league_avg_defence: float,
) -> float:
    """How much easier/harder this fixture is than average, for this position.

    Goalkeepers and defenders score heavily from clean sheets, so what
    matters to them is the opponent's *attack* strength. Midfielders and
    forwards score from goal involvements, so what matters to them is the
    opponent's *defence* strength. Either way, a stronger opponent in the
    relevant discipline means a smaller multiplier.
    """
    if element_type in (GOALKEEPER, DEFENDER):
        relevant = (
            opponent.strength_attack_home if opponent_is_home else opponent.strength_attack_away
        )
        league_avg = league_avg_attack
    else:
        relevant = (
            opponent.strength_defence_home if opponent_is_home else opponent.strength_defence_away
        )
        league_avg = league_avg_defence

    if relevant <= 0:
        return 1.0
    multiplier = league_avg / relevant
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
    session: Session, halflife: float = 3.0
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
    """
    base_points = predicted_points_by_player(session, halflife)
    lineup_by_player = lineup_multipliers_by_player(session)
    next_fixture_by_team = get_next_fixture_by_team(session)
    teams_by_id = {t.id: t for t in session.query(Team).all()}
    league_avg_attack, league_avg_defence = _league_average_strengths(list(teams_by_id.values()))

    scores = []
    for player in session.query(Player).all():
        base = base_points.get(player.id, 0.0)
        fixture = next_fixture_by_team.get(player.team_id)
        play_prob = chance_of_playing(player)
        lineup = lineup_by_player.get(player.id, 1.0)

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
                player.element_type, opponent, not is_home, league_avg_attack, league_avg_defence
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
