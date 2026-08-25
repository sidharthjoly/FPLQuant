from sqlalchemy.orm import Session

from fplquant.data.history import import_season, parse_rows
from fplquant.models.orm import HistoricalPlayerGameweek

HEADER = (
    "name,position,team,element,fixture,round,GW,minutes,starts,value,selected,"
    "was_home,kickoff_time,total_points,bps,expected_goals\n"
)
ROW = "Saka,MID,Arsenal,17,3,2,2,90,1,95,120000,True,2023-08-12T14:00:00Z,8,32,0.44\n"


def test_a_row_parses_into_the_fields_the_model_needs() -> None:
    rows = parse_rows("2023-24", HEADER + ROW)

    assert len(rows) == 1
    row = rows[0]
    assert row["season"] == "2023-24"
    assert row["element"] == 17 and row["round"] == 2 and row["fixture"] == 3
    assert row["starts"] == 1 and row["minutes"] == 90 and row["value"] == 95
    assert row["was_home"] is True
    assert row["expected_goals"] == 0.44
    assert row["kickoff_time"] is not None


def test_columns_missing_from_older_seasons_become_null_not_zero() -> None:
    """`starts` and the expected-goals columns only appear from 2022-23. A
    genuine absence stored as 0.0 would tell a model that nobody created a
    chance that year."""
    header = "name,position,team,element,fixture,GW,minutes,value,selected,was_home\n"
    rows = parse_rows("2019-20", header + "Kane,FWD,Spurs,100,5,5,90,110,50000,False\n")

    assert rows[0]["starts"] is None
    assert rows[0]["expected_goals"] is None
    assert rows[0]["minutes"] == 90  # a real value still parses


def test_rows_that_cannot_be_keyed_are_dropped() -> None:
    """One malformed line is not worth breaking the table's uniqueness for."""
    rows = parse_rows("2023-24", HEADER + ROW + "Broken,MID,Arsenal,,,,,,,,,,,,,\n")

    assert len(rows) == 1


def test_a_double_gameweek_keeps_both_rows() -> None:
    second = ROW.replace(",3,2,2,", ",9,2,2,")  # same round, different fixture
    rows = parse_rows("2023-24", HEADER + ROW + second)

    assert len(rows) == 2
    assert {row["fixture"] for row in rows} == {3, 9}


def test_importing_a_season_twice_replaces_rather_than_duplicates(db_session: Session) -> None:
    """A finished season is a static archive, so a re-import means the source
    changed or the last run was interrupted — both want the season replaced."""
    import_season(db_session, "2023-24", HEADER + ROW)
    import_season(db_session, "2023-24", HEADER + ROW)

    assert db_session.query(HistoricalPlayerGameweek).count() == 1


def test_duplicate_rows_within_one_file_are_collapsed(db_session: Session) -> None:
    """The archive has been known to repeat a row; the unique constraint would
    otherwise fail the whole season's import."""
    result = import_season(db_session, "2023-24", HEADER + ROW + ROW)

    assert result.rows_read == 2
    assert result.rows_written == 1


def test_seasons_are_kept_apart(db_session: Session) -> None:
    import_season(db_session, "2023-24", HEADER + ROW)
    import_season(db_session, "2024-25", HEADER + ROW)

    assert db_session.query(HistoricalPlayerGameweek).count() == 2
    import_season(db_session, "2023-24", HEADER + ROW)  # re-import one
    assert db_session.query(HistoricalPlayerGameweek).count() == 2  # the other survives
