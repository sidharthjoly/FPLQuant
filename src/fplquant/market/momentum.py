from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, selectinload

from fplquant.models.orm import Player, PlayerGameweekStat


@dataclass(frozen=True)
class PriceMomentum:
    player_id: int
    web_name: str
    gameweeks_considered: int
    price_change: int  # tenths of a million, e.g. 3 = +£0.3m
    price_change_pct: float
    net_transfers: int  # sum(transfers_in - transfers_out) over the window
    ownership_change: int  # change in total managers owning the player
    ownership_change_pct: float
    # FPL's own forecast, published from 2026-27 onward. `progress_percent` is
    # how far along the player is toward their next price change and
    # `projected_changes` how many the game expects over the next few days,
    # signed. Both are None where the game does not publish them.
    progress_percent: float | None = None
    projected_changes: int | None = None


def compute_price_momentum(
    web_name: str, stats: list[PlayerGameweekStat], lookback: int = 5
) -> PriceMomentum | None:
    """Momentum over the most recent `lookback` gameweeks (or fewer if unavailable).

    Mirrors classic price/volume momentum indicators: the change in price and
    ownership over the window, plus net transfer flow (in - out) as an
    "order flow" signal. Returns None with fewer than 2 gameweeks of history,
    since there's nothing to compute a change over.

    The window counts *gameweeks*, so a double contributes one entry rather than
    two — otherwise a five-gameweek lookback would silently shrink to four weeks
    of price history for anyone who happened to play a double in it. Within a
    round the last fixture's row carries the price and ownership (they are
    end-of-round values either way), while transfers are summed across both.
    """
    by_round: dict[int, list[PlayerGameweekStat]] = {}
    for stat in stats:
        by_round.setdefault(stat.round, []).append(stat)
    rounds = sorted(by_round)[-lookback:]
    window = [by_round[r][-1] for r in rounds]
    if len(window) < 2:
        return None

    first, last = window[0], window[-1]
    price_change = last.value - first.value
    price_change_pct = price_change / first.value if first.value else 0.0
    ownership_change = last.selected - first.selected
    ownership_change_pct = ownership_change / first.selected if first.selected else 0.0
    net_transfers = sum(s.transfers_in - s.transfers_out for r in rounds for s in by_round[r])

    return PriceMomentum(
        player_id=first.player_id,
        web_name=web_name,
        gameweeks_considered=len(window),
        price_change=price_change,
        price_change_pct=price_change_pct,
        net_transfers=net_transfers,
        ownership_change=ownership_change,
        ownership_change_pct=ownership_change_pct,
    )


def _as_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _projected_changes(element: dict[str, Any]) -> int | None:
    """How many price changes FPL projects for this player, signed.

    `price_change_projections` holds one entry per day ahead with a cumulative
    `likelihood`; the furthest-out entry is the informative one.
    """
    projections = element.get("price_change_projections")
    if not projections:
        return None
    furthest = max(projections, key=lambda entry: entry.get("offset", 0))
    likelihood = furthest.get("likelihood")
    return None if likelihood is None else int(likelihood)


def compute_live_market_movers(
    elements: list[dict[str, Any]], players_by_fpl_id: dict[int, Player], total_players: int
) -> list[PriceMomentum]:
    """Today's movers straight from FPL's live bootstrap-static fields.

    `compute_price_momentum_scores` needs at least two *finished* gameweeks
    of ingested history, so it sits empty for the first couple of weeks of
    the season and between gameweeks until FPL finalizes bonus points. FPL
    itself updates `cost_change_event` (price move so far this gameweek) and
    `transfers_in_event`/`transfers_out_event` (transfer flow so far this
    gameweek) multiple times a day regardless of match state — this is the
    same signal fplstatistics-style "price change" trackers use, and it
    keeps the market ticker moving day to day instead of waiting on results.

    From 2026-27 the game also publishes its *own* forecast — how far along a
    player is toward their next change, and how many changes it projects over
    the coming days — which is strictly better than anything this module can
    infer from gameweek snapshots, and is carried through here rather than
    reconstructed. The inferred signals stay because they are what is available
    for past seasons and because a published forecast is worth checking against
    something.
    """
    scores = []
    for element in elements:
        player = players_by_fpl_id.get(element["id"])
        if player is None:
            continue
        price_change = element.get("cost_change_event", 0)
        net_transfers = element.get("transfers_in_event", 0) - element.get("transfers_out_event", 0)
        progress = _as_optional_float(element.get("price_change_percent"))
        projected = _projected_changes(element)
        if price_change == 0 and net_transfers == 0 and not progress and not projected:
            continue
        price_change_pct = price_change / player.now_cost if player.now_cost else 0.0
        ownership_change_pct = net_transfers / total_players if total_players else 0.0
        scores.append(
            PriceMomentum(
                player_id=player.id,
                web_name=player.web_name,
                gameweeks_considered=0,
                price_change=price_change,
                price_change_pct=price_change_pct,
                net_transfers=net_transfers,
                ownership_change=net_transfers,
                ownership_change_pct=ownership_change_pct,
                progress_percent=progress,
                projected_changes=projected,
            )
        )
    # Ranked on the game's own projection where it exists, since a player 95%
    # of the way to a rise is a more urgent mover than one who rose yesterday
    # and has since gone quiet. Falls back to the realised move otherwise.
    return sorted(
        scores,
        key=lambda m: (
            m.projected_changes if m.projected_changes is not None else 0,
            m.progress_percent if m.progress_percent is not None else 0.0,
            m.price_change_pct,
        ),
        reverse=True,
    )


def compute_price_momentum_scores(session: Session, lookback: int = 5) -> list[PriceMomentum]:
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    scores = []
    for player in players:
        momentum = compute_price_momentum(player.web_name, player.gameweek_stats, lookback)
        if momentum is not None:
            scores.append(momentum)
    return sorted(scores, key=lambda m: m.price_change_pct, reverse=True)
