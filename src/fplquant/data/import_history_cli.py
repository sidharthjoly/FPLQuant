import argparse
import logging

from fplquant.data.history import SEASONS_WITH_STARTS, import_seasons
from fplquant.models.base import session_scope


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import past FPL seasons from the public archive, for model training."
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        default=list(SEASONS_WITH_STARTS),
        help=(
            "Seasons to import, e.g. 2023-24. Defaults to the ones that publish an explicit "
            "`starts` column, which is the label the minutes model trains on."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with session_scope() as session:
        results = import_seasons(session, args.seasons)

    total = sum(r.rows_written for r in results)
    print(f"\nImported {total} player-gameweek rows across {len(results)} seasons.")
    for result in results:
        dropped = result.rows_read - result.rows_written
        note = f"  ({dropped} duplicate rows collapsed)" if dropped else ""
        print(f"  {result.season}  {result.rows_written:>6} rows{note}")
    print("\nSource: github.com/vaastav/Fantasy-Premier-League (MIT). Training data only —")
    print("nothing the app serves reads this table.")


if __name__ == "__main__":
    main()
