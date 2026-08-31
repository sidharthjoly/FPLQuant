import datetime as dt

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fplquant.models.base import Base


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    fpl_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    short_name: Mapped[str] = mapped_column(String(8))
    strength_overall_home: Mapped[int] = mapped_column(Integer, default=0)
    strength_overall_away: Mapped[int] = mapped_column(Integer, default=0)
    strength_attack_home: Mapped[int] = mapped_column(Integer, default=0)
    strength_attack_away: Mapped[int] = mapped_column(Integer, default=0)
    strength_defence_home: Mapped[int] = mapped_column(Integer, default=0)
    strength_defence_away: Mapped[int] = mapped_column(Integer, default=0)

    players: Mapped[list["Player"]] = relationship(back_populates="team")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    fpl_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    first_name: Mapped[str] = mapped_column(String(64))
    second_name: Mapped[str] = mapped_column(String(64))
    web_name: Mapped[str] = mapped_column(String(64))
    code: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FPL asset id -> headshot URL
    element_type: Mapped[int] = mapped_column(Integer)  # 1=GKP 2=DEF 3=MID 4=FWD
    now_cost: Mapped[int] = mapped_column(Integer)  # tenths of a million, e.g. 105 = £10.5m
    selected_by_percent: Mapped[float] = mapped_column(Float, default=0.0)
    form: Mapped[float] = mapped_column(Float, default=0.0)
    ep_next: Mapped[float] = mapped_column(Float, default=0.0)  # FPL's own next-GW points estimate
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(8), default="a")  # a/d/i/s/u
    chance_of_playing_next_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    news: Mapped[str] = mapped_column(String(512), default="")
    birth_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC))

    # Season-to-date defensive actions per 90, as FPL publishes them. The input
    # to the Defensive Contribution term in `fplquant.engine.scoring`.
    defensive_contribution_per_90: Mapped[float] = mapped_column(Float, default=0.0)

    # FPL's own price-change forecast, published from 2026-27 onward. Prices
    # used to move at most once a day, which is why the market layer was built
    # to infer momentum from per-gameweek snapshots; they now move continuously
    # and the game publishes both the current rate and a signed multi-day
    # projection. `price_change_percent` is progress toward the next change,
    # `price_change_hourly_rate` its speed, and `price_change_likelihood` the
    # number of changes projected over the next few days — negative for falls.
    price_change_percent: Mapped[float] = mapped_column(Float, default=0.0)
    price_change_hourly_rate: Mapped[float] = mapped_column(Float, default=0.0)
    price_change_likelihood: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Transfermarkt match cache: avoids re-searching every ingest run.
    # transfermarkt_lookup_status: "unresolved" (never tried), "matched", or "unmatched".
    transfermarkt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfermarkt_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transfermarkt_lookup_status: Mapped[str] = mapped_column(String(16), default="unresolved")
    nationality: Mapped[str | None] = mapped_column(String(64), nullable=True)

    team: Mapped[Team] = relationship(back_populates="players")
    gameweek_stats: Mapped[list["PlayerGameweekStat"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    injury_records: Mapped[list["InjuryRecord"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )


class Fixture(Base):
    __tablename__ = "fixtures"
    __table_args__ = (UniqueConstraint("fpl_id", name="uq_fixture_fpl_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fpl_id: Mapped[int] = mapped_column(Integer, index=True)
    event: Mapped[int | None] = mapped_column(Integer, nullable=True)  # gameweek number
    kickoff_time: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    finished: Mapped[bool] = mapped_column(default=False)
    team_h_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_h_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_a_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_h_difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_a_difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    team_h: Mapped[Team] = relationship(foreign_keys=[team_h_id])
    team_a: Mapped[Team] = relationship(foreign_keys=[team_a_id])


class PlayerGameweekStat(Base):
    """One row per (player, fixture): points, price, ownership and underlying stats.

    This is the time series that form/momentum/volatility calculations are built on.

    Keyed on the fixture, not the gameweek. FPL's per-player summary returns one
    row per *match*, and in a double gameweek that is two rows in the same round
    — so a key of (player, round) does not deduplicate them, it discards one.
    Silently: the second row overwrites the first, no error, no log line. Double
    gameweeks carried between 2.6% and 11% of all player-minutes in the last four
    seasons, and they are concentrated in exactly the rounds the multi-period
    planner exists to exploit.

    The consequence for callers is that a round is not a row. Anything that means
    "this player's points in gameweek 12" has to sum the rounds's rows rather than
    take one — see `fplquant.market.volatility`, `.correlation` and `.momentum`,
    which all do. Anything that means "one match of evidence" — start rates,
    per-90 rates, rest days — wants the rows as they come.
    """

    __tablename__ = "player_gameweek_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "round", "fixture_fpl_id", name="uq_player_round_fixture"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    round: Mapped[int] = mapped_column(Integer, index=True)  # gameweek number
    fixture_fpl_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opponent_team_fpl_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    was_home: Mapped[bool | None] = mapped_column(nullable=True)
    kickoff_time: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Whether the player was in the starting XI. Nullable because rows ingested
    # before this column existed have no value — see lineup.starts.did_start for
    # the minutes-based fallback used in that case.
    starts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    goals_scored: Mapped[int] = mapped_column(Integer, default=0)
    assists: Mapped[int] = mapped_column(Integer, default=0)
    clean_sheets: Mapped[int] = mapped_column(Integer, default=0)
    goals_conceded: Mapped[int] = mapped_column(Integer, default=0)
    bonus: Mapped[int] = mapped_column(Integer, default=0)
    bps: Mapped[int] = mapped_column(Integer, default=0)
    influence: Mapped[float] = mapped_column(Float, default=0.0)
    creativity: Mapped[float] = mapped_column(Float, default=0.0)
    threat: Mapped[float] = mapped_column(Float, default=0.0)
    ict_index: Mapped[float] = mapped_column(Float, default=0.0)
    expected_goals: Mapped[float] = mapped_column(Float, default=0.0)
    expected_assists: Mapped[float] = mapped_column(Float, default=0.0)
    expected_goal_involvements: Mapped[float] = mapped_column(Float, default=0.0)
    expected_goals_conceded: Mapped[float] = mapped_column(Float, default=0.0)

    # Defensive Contribution and its components. Nullable because rows written
    # before this column existed carry no value, and because a zero here would
    # be indistinguishable from a genuine zero — see
    # `fplquant.engine.scoring.defensive_contribution_points` for what they buy.
    defensive_contribution: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clearances_blocks_interceptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recoveries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tackles: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # "stock market" fields: price and ownership as of this gameweek
    value: Mapped[int] = mapped_column(Integer, default=0)  # tenths of a million
    selected: Mapped[int] = mapped_column(Integer, default=0)  # number of managers owning
    transfers_in: Mapped[int] = mapped_column(Integer, default=0)
    transfers_out: Mapped[int] = mapped_column(Integer, default=0)

    player: Mapped[Player] = relationship(back_populates="gameweek_stats")


class InjuryRecord(Base):
    """One past injury spell for a player, scraped from Transfermarkt.

    Rows are fully replaced (delete + reinsert) per player on each sync,
    rather than upserted field-by-field — see fplquant.data.ingest_injuries.
    """

    __tablename__ = "injury_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    season: Mapped[str] = mapped_column(String(8))  # e.g. "24/25"
    injury_type: Mapped[str] = mapped_column(String(128))
    start_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    end_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    days_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_missed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    player: Mapped[Player] = relationship(back_populates="injury_records")


class PlayerSnapshot(Base):
    """A player's point-in-time state, as FPL published it on one day.

    Every other table here is either immutable history or a current snapshot.
    `Player` is the latter: `now_cost`, `ep_next`, `status` and
    `chance_of_playing_next_round` are overwritten on every ingest, so the
    database always knows what is true today and never what was true on the
    morning of gameweek five.

    That is fine for making predictions and fatal for checking them. A backtest
    has to rebuild the world as it looked before a deadline, and replaying an
    old gameweek against today's `status` leaks the future in both directions:
    a player who is injured now gets zeroed out for a week he actually played,
    and one who missed that week but is fit now gets counted as available.
    `chance_of_playing` is a hard gate on every player's expected points, so a
    backtest built on it would produce numbers that look plausible and mean
    nothing.

    `PlayerGameweekStat` already preserves per-round price and ownership, so
    those are recoverable. The fields here are not recoverable from anywhere —
    FPL publishes no history for them — which is why this table is append-only
    and why it has to start collecting before the data is wanted, not when.

    One row per player per day: prices move once daily and deadlines fall in
    the afternoon, so a day is fine resolution for "the state going into this
    gameweek", and it bounds the table at roughly 600 rows a day.
    """

    __tablename__ = "player_snapshots"
    __table_args__ = (UniqueConstraint("player_id", "captured_on", name="uq_player_snapshot_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    captured_on: Mapped[dt.date] = mapped_column(index=True)  # the dedup key
    captured_at: Mapped[dt.datetime]  # the actual moment, for ordering within a day
    # The gameweek this snapshot describes the run-up to: the next one whose
    # deadline hasn't passed. What a backtest keys on — "the state going into
    # GW5" — rather than having to re-derive it from the fixture calendar.
    next_event: Mapped[int | None] = mapped_column(Integer, nullable=True)

    now_cost: Mapped[int] = mapped_column(Integer)
    ep_next: Mapped[float] = mapped_column(Float, default=0.0)
    form: Mapped[float] = mapped_column(Float, default=0.0)
    selected_by_percent: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(8), default="a")
    chance_of_playing_next_round: Mapped[int | None] = mapped_column(Integer, nullable=True)

    player: Mapped[Player] = relationship()


class TeamSnapshot(Base):
    """A club's published strength ratings on one day.

    Same problem as `PlayerSnapshot`, one level up. FPL revises these through a
    season and the ingest overwrites them, so the prior that
    `fplquant.engine.rates` starts from is unreconstructable after the fact.
    They are currently zero for every club — which is itself worth recording,
    since it is the reason the prior falls back to squad value, and a backtest
    needs to know which of the two was actually in play that week.
    """

    __tablename__ = "team_snapshots"
    __table_args__ = (UniqueConstraint("team_id", "captured_on", name="uq_team_snapshot_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    captured_on: Mapped[dt.date] = mapped_column(index=True)
    captured_at: Mapped[dt.datetime]
    next_event: Mapped[int | None] = mapped_column(Integer, nullable=True)

    strength_overall_home: Mapped[int] = mapped_column(Integer, default=0)
    strength_overall_away: Mapped[int] = mapped_column(Integer, default=0)
    strength_attack_home: Mapped[int] = mapped_column(Integer, default=0)
    strength_attack_away: Mapped[int] = mapped_column(Integer, default=0)
    strength_defence_home: Mapped[int] = mapped_column(Integer, default=0)
    strength_defence_away: Mapped[int] = mapped_column(Integer, default=0)

    team: Mapped[Team] = relationship()


class HistoricalPlayerGameweek(Base):
    """One player-fixture row from a *past* season, imported from a public archive.

    Deliberately a separate table from `PlayerGameweekStat` rather than more
    rows in it. The live table is written by the FPL API ingest and is the
    single source of truth for everything the app serves; this one is bulk
    historical data from a third party, covers players and clubs that no longer
    exist in the current pool, and keys on FPL element ids that are *not stable
    across seasons* — element 169 is a different footballer each year. Merging
    them would put untrusted data on the path that serves predictions and make
    the id ambiguous. Keeping them apart means nothing the app does today can
    be affected by an import, and a bad import is a `DELETE FROM` away.

    The archive carries several fields FPL's per-player summaries do not expose
    and this schema therefore lacks entirely — `saves`, `yellow_cards`,
    `red_cards`, `bps` — which is useful beyond the model it was imported for:
    the save and card rates in `fplquant.engine.scoring` are currently
    league-typical constants, and this is the data that could replace them with
    measured ones.

    Keyed on the fixture as well as the round, because a double gameweek gives
    a player two rows in the same round — 983 of them in 2023-24 alone.
    """

    __tablename__ = "historical_player_gameweeks"
    __table_args__ = (
        UniqueConstraint(
            "season", "element", "round", "fixture", name="uq_historical_player_fixture"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    season: Mapped[str] = mapped_column(String(16), index=True)  # e.g. "2023-24"
    element: Mapped[int] = mapped_column(Integer, index=True)  # FPL id *within that season*
    round: Mapped[int] = mapped_column(Integer, index=True)
    fixture: Mapped[int] = mapped_column(Integer)

    name: Mapped[str] = mapped_column(String(128))
    # Nullable: the archive only began publishing these in 2021-22.
    position: Mapped[str | None] = mapped_column(String(8), nullable=True)
    team: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opponent_team: Mapped[int | None] = mapped_column(Integer, nullable=True)
    was_home: Mapped[bool | None] = mapped_column(nullable=True)
    kickoff_time: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Nullable: only published from 2022-23. Earlier seasons fall back to the
    # same minutes-based rule as `fplquant.lineup.starts.did_start`.
    starts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    bps: Mapped[int] = mapped_column(Integer, default=0)
    bonus: Mapped[int] = mapped_column(Integer, default=0)

    goals_scored: Mapped[int] = mapped_column(Integer, default=0)
    assists: Mapped[int] = mapped_column(Integer, default=0)
    clean_sheets: Mapped[int] = mapped_column(Integer, default=0)
    goals_conceded: Mapped[int] = mapped_column(Integer, default=0)
    own_goals: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    yellow_cards: Mapped[int] = mapped_column(Integer, default=0)
    red_cards: Mapped[int] = mapped_column(Integer, default=0)
    penalties_missed: Mapped[int] = mapped_column(Integer, default=0)
    penalties_saved: Mapped[int] = mapped_column(Integer, default=0)

    influence: Mapped[float] = mapped_column(Float, default=0.0)
    creativity: Mapped[float] = mapped_column(Float, default=0.0)
    threat: Mapped[float] = mapped_column(Float, default=0.0)
    ict_index: Mapped[float] = mapped_column(Float, default=0.0)
    # Nullable: FPL only started publishing xG/xA in 2022-23.
    expected_goals: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_assists: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_goals_conceded: Mapped[float | None] = mapped_column(Float, nullable=True)

    value: Mapped[int] = mapped_column(Integer, default=0)  # price, tenths of a million
    selected: Mapped[int] = mapped_column(Integer, default=0)
    transfers_in: Mapped[int] = mapped_column(Integer, default=0)
    transfers_out: Mapped[int] = mapped_column(Integer, default=0)

    # The fixture's scoreline, repeated on every player row in that match. It
    # is the only place a replay can get it: the archive publishes no fixture
    # table, and `fplquant.engine.rates` will not count a match as played
    # without one, so a backtest missing this fits nothing and silently returns
    # every club's prior. Repeated per row rather than normalised out because
    # the archive is loaded as one flat bulk insert and a second table would
    # have to be derived from these rows anyway.
    team_h_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_a_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Defensive Contribution, the scoring stat FPL added for 2025-26: two
    # points at 10 clearances/blocks/interceptions/tackles for a defender, or
    # 12 defensive actions including recoveries for anyone else. Absent —
    # stored NULL, not zero — for seasons played before the rule existed, so a
    # model can tell "made no defensive actions" from "nobody was counting".
    defensive_contribution: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clearances_blocks_interceptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recoveries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tackles: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # FPL's own expected-points projection, taken from the archive's `xP`
    # column. Emphatically *not* a pre-deadline projection: holding the player
    # and season fixed and looking only at 60+ minute rounds, this number runs
    # 1.44 points higher in the weeks that player happened to score, for 88% of
    # players. It saw the result. Kept because it is still the only external
    # reference in the archive, but see `fplquant.backtest.replay` for why it
    # is excluded from the headline comparison.
    expected_points: Mapped[float | None] = mapped_column(Float, nullable=True)
