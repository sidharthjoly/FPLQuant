import datetime as dt
from dataclasses import dataclass
from typing import Any

import requests
from sqlalchemy.orm import Session, selectinload

from fplquant.data.fpl_client import FPLClient
from fplquant.form.fixtures import compute_fixture_adjusted_scores
from fplquant.models.orm import Player
from fplquant.optimizer.candidates import FORM, next_match_points
from fplquant.optimizer.types import PlayerCandidate


class TeamNotFoundError(RuntimeError):
    """No locked-in squad is available yet for this FPL team ID."""


@dataclass(frozen=True)
class CurrentTeam:
    team_name: str
    event_id: int  # the gameweek this squad snapshot reflects
    bank: int  # tenths of a million, e.g. 5 = £0.5m
    squad: list[PlayerCandidate]


def latest_locked_event(events: list[dict[str, Any]]) -> int | None:
    """The highest gameweek id whose deadline has already passed.

    FPL only exposes picks for a gameweek once its deadline has passed —
    the currently-open-for-transfers gameweek has no public picks data, even
    for the team's own entry. Returns None before any deadline has passed
    (e.g. preseason), since there's no locked-in squad yet at all.
    """
    now = dt.datetime.now(dt.UTC)
    locked_ids = []
    for event in events:
        deadline = event.get("deadline_time")
        if not deadline:
            continue
        deadline_dt = dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if deadline_dt <= now:
            locked_ids.append(event["id"])
    return max(locked_ids) if locked_ids else None


def fetch_current_squad(
    client: FPLClient,
    session: Session,
    fpl_team_id: int,
    halflife: float = 3.0,
    projection: str = FORM,
) -> CurrentTeam:
    """Pull `fpl_team_id`'s most recently locked-in squad from FPL's public
    API and translate it into our own fixture-adjusted PlayerCandidates.

    `projection` has to match the one the replacement pool was built with.
    A squad priced on one scale and compared against a pool priced on another
    does not fail — it quietly answers "no transfer is worth making", because
    nothing in the pool can out-score an incumbent whose points were inflated
    by a different model.

    Uses FPL's public, unauthenticated `/entry/{id}/` and
    `/entry/{id}/event/{gw}/picks/` endpoints — the same data the FPL
    website itself shows on a manager's public profile, no login needed.
    """
    bootstrap = client.get_bootstrap_static()
    event_id = latest_locked_event(bootstrap["events"])
    if event_id is None:
        raise TeamNotFoundError(
            "No gameweek deadline has passed yet, so there's no locked-in squad to plan "
            "transfers from. This becomes available once your first gameweek deadline passes."
        )

    try:
        entry = client.get_entry(fpl_team_id)
        picks_payload = client.get_entry_picks(fpl_team_id, event_id)
    except requests.HTTPError as exc:
        raise TeamNotFoundError(
            f"Couldn't find a squad for FPL team ID {fpl_team_id} in gameweek {event_id} — "
            "double check the team ID."
        ) from exc

    element_ids = [pick["element"] for pick in picks_payload["picks"]]
    players_by_fpl_id = {
        player.fpl_id: player
        for player in session.query(Player)
        .options(selectinload(Player.team))
        .filter(Player.fpl_id.in_(element_ids))
        .all()
    }
    fixtures_by_player = {
        s.player_id: s for s in compute_fixture_adjusted_scores(session, halflife)
    }
    points_by_player, start_probability = next_match_points(
        session, projection, fixtures_by_player, halflife
    )

    squad: list[PlayerCandidate] = []
    for pick in picks_payload["picks"]:
        player = players_by_fpl_id.get(pick["element"])
        if player is None:
            continue  # stale/unmatched element id — skip rather than fail the whole request
        fixture = fixtures_by_player.get(player.id)
        squad.append(
            PlayerCandidate(
                player_id=player.id,
                web_name=player.web_name,
                team_id=player.team_id,
                team_short_name=player.team.short_name,
                element_type=player.element_type,
                now_cost=player.now_cost,
                predicted_points=points_by_player.get(player.id, 0.0),
                next_opponent=fixture.opponent_short_name if fixture else None,
                next_opponent_is_home=fixture.is_home if fixture else None,
                fixture_difficulty=fixture.difficulty if fixture else None,
                chance_of_playing=fixture.chance_of_playing if fixture else 1.0,
                start_probability=start_probability.get(player.id),
            )
        )

    return CurrentTeam(
        team_name=entry.get("name", f"Team {fpl_team_id}"),
        event_id=event_id,
        bank=picks_payload["entry_history"]["bank"],
        squad=squad,
    )
