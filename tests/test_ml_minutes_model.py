import datetime as dt
from pathlib import Path

from sqlalchemy.orm import Session

from fplquant.ml import minutes_model
from fplquant.ml.features import FEATURE_NAMES, MinutesFeatures
from fplquant.models.orm import HistoricalPlayerGameweek

KICKOFF = dt.datetime(2023, 8, 12, 14, 0, tzinfo=dt.UTC)


def _seed(session: Session, players: int = 40, rounds: int = 12) -> None:
    """A toy archive with a learnable rule: expensive players start."""
    for element in range(1, players + 1):
        nailed = element <= players // 2
        for round_number in range(1, rounds + 1):
            session.add(
                HistoricalPlayerGameweek(
                    season="2023-24" if round_number <= rounds // 2 else "2024-25",
                    element=element,
                    round=round_number,
                    fixture=round_number,
                    name=f"P{element}",
                    position="MID",
                    team="Arsenal" if element % 2 else "Chelsea",
                    was_home=True,
                    kickoff_time=KICKOFF + dt.timedelta(days=7 * round_number),
                    minutes=90 if nailed else 0,
                    starts=1 if nailed else 0,
                    value=100 if nailed else 45,
                    selected=1000,
                )
            )
    session.flush()


def _features() -> MinutesFeatures:
    return MinutesFeatures(
        value=100,
        price_rank_in_group=1.0,
        price_share_in_group=0.5,
        was_home=True,
        round=5,
        rounds_observed=4,
        recent_starts=[1, 1, 1, 1],
        recent_minutes=[90, 90, 90, 90],
        last_selected=1000,
        rest_days=7.0,
        position="MID",
    )


def test_a_trained_model_predicts_probabilities(db_session: Session) -> None:
    _seed(db_session)

    trained = minutes_model.train(db_session, holdout_season=None)
    probabilities = trained.predict([_features()])

    assert len(probabilities) == 1
    assert 0.0 <= probabilities[0] <= 1.0
    assert trained.feature_names == tuple(FEATURE_NAMES)


def test_it_learns_that_nailed_players_start(db_session: Session) -> None:
    _seed(db_session)
    trained = minutes_model.train(db_session, holdout_season=None)

    nailed = trained.predict([_features()])[0]
    fringe = trained.predict(
        [
            MinutesFeatures(
                value=45,
                price_rank_in_group=8.0,
                price_share_in_group=0.05,
                was_home=True,
                round=5,
                rounds_observed=4,
                recent_starts=[0, 0, 0, 0],
                recent_minutes=[0, 0, 0, 0],
                last_selected=1000,
                rest_days=7.0,
                position="MID",
            )
        ]
    )[0]

    assert nailed > 0.8
    assert fringe < 0.2


def test_holding_a_season_out_produces_an_evaluation(db_session: Session) -> None:
    """Split by season, never at random: one player's neighbouring gameweeks
    are strongly correlated, so a shuffled holdout measures memorisation."""
    _seed(db_session)

    trained = minutes_model.train(db_session, holdout_season="2024-25")

    assert trained.evaluation is not None
    assert trained.evaluation.holdout_season == "2024-25"
    assert trained.evaluation.n_train > 0 and trained.evaluation.n_test > 0
    assert 0.0 <= trained.evaluation.roc_auc <= 1.0
    assert trained.evaluation.calibration


def test_training_without_an_archive_says_so(db_session: Session) -> None:
    try:
        minutes_model.train(db_session)
    except ValueError as error:
        assert "import-history" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")


def test_a_model_survives_a_save_and_load(db_session: Session, tmp_path: Path) -> None:
    _seed(db_session)
    trained = minutes_model.train(db_session, holdout_season=None)
    path = tmp_path / "minutes.joblib"

    minutes_model.save(trained, path)
    reloaded = minutes_model.load(path)

    assert reloaded is not None
    assert reloaded.predict([_features()]) == trained.predict([_features()])


def test_a_missing_model_is_not_an_error(tmp_path: Path) -> None:
    """The engine falls back to its heuristic, so a fresh clone with no
    artefact still produces predictions."""
    assert minutes_model.load(tmp_path / "nothing-here.joblib") is None


def test_a_model_trained_on_different_features_is_refused(
    db_session: Session, tmp_path: Path
) -> None:
    """The feature builder can change after an artefact is written, and the
    vector would still be the right length often enough for the mismatch to
    pass silently — which is a model reading its inputs in the wrong order."""
    _seed(db_session)
    trained = minutes_model.train(db_session, holdout_season=None)
    path = tmp_path / "stale.joblib"
    minutes_model.save(trained, path)

    import joblib

    payload = joblib.load(path)
    payload["feature_names"] = ("something", "else")
    joblib.dump(payload, path)

    assert minutes_model.load(path) is None
