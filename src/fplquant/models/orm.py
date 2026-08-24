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
    """One row per (player, gameweek): points, price, ownership and underlying stats.

    This is the time series that form/momentum/volatility calculations are built on.
    """

    __tablename__ = "player_gameweek_stats"
    __table_args__ = (UniqueConstraint("player_id", "round", name="uq_player_round"),)

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
