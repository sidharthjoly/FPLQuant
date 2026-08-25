"""A learned model of whether a player starts.

Minutes are the largest term in FPL scoring — a player who doesn't start scores
two points at best, whatever else is true of him — and until now this project
estimated them with a softmax over price blended toward an observed start rate.
That heuristic is defensible and it leaves a lot on the table: rotation depends
on fixture congestion, rest, competition for the shirt and recent selection all
interacting, which is precisely the shape of problem a gradient-boosted tree
handles better than a formula anyone would write by hand.

Measured on a held-out season (2025-26, trained on the three before it), against
the heuristic it replaces:

    heuristic   logloss 0.379   AUC 0.893   Brier 0.121
    model       logloss 0.251   AUC 0.952   Brier 0.078

Calibration matters more here than accuracy, because the output is multiplied
into an expected-points calculation rather than thresholded: a predicted 0.6 has
to mean 60%, or every downstream number inherits the bias. On the holdout the
model's deciles track the observed rate to within a point across the range.

The split is by season, never at random. Neighbouring gameweeks for one player
are strongly correlated, so a shuffled holdout mostly measures whether the model
has memorised who plays — it scores well and predicts nothing about next season.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import numpy.typing as npt
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sqlalchemy.orm import Session

from fplquant.ml.features import (
    FEATURE_NAMES,
    MinutesFeatures,
    build_training_frame,
    feature_vector,
)

logger = logging.getLogger(__name__)

# Shipped inside the package rather than under `data/`. On the deployed VM
# `data/` is a bind mount owned by the container, so a checked-in file there
# cannot be written by the deploy user — `git pull` failed outright with
# "cannot create directory at 'data/models': Permission denied". The model is a
# build artefact that belongs with the code in any case: putting it here means
# the image is self-contained and the data volume holds only data.
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "artifacts" / "minutes.joblib"

# The most recent archived season is held out. Not a random split — see the
# module docstring.
DEFAULT_HOLDOUT_SEASON = "2025-26"

# Conservative settings: early stopping on an internal validation slice, strong
# L2, modest depth. The training set is large but its *effective* size is much
# smaller than its row count, since one player's season is ~38 highly correlated
# rows, and an unregularised model exploits that.
MODEL_PARAMS: dict[str, Any] = {
    "max_iter": 400,
    "learning_rate": 0.06,
    "max_leaf_nodes": 31,
    "l2_regularization": 1.0,
    "early_stopping": True,
    "validation_fraction": 0.15,
    "random_state": 7,
}


@dataclass(frozen=True)
class Evaluation:
    n_train: int
    n_test: int
    holdout_season: str
    log_loss: float
    roc_auc: float
    brier: float
    accuracy: float
    calibration: list[tuple[float, float]]  # (mean predicted, observed rate) per decile


@dataclass(frozen=True)
class TrainedMinutesModel:
    model: HistGradientBoostingClassifier
    feature_names: tuple[str, ...]
    evaluation: Evaluation | None

    def predict(self, rows: list[MinutesFeatures]) -> list[float]:
        """Start probability for each input, in order."""
        if not rows:
            return []
        matrix = np.array([feature_vector(row) for row in rows], dtype=np.float64)
        return [float(p) for p in self.model.predict_proba(matrix)[:, 1]]


def _calibration(y: npt.NDArray[np.int64], p: npt.NDArray[np.float64]) -> list[tuple[float, float]]:
    """Observed rate against predicted probability, by decile of prediction.

    Quantile bins rather than fixed-width: most predictions sit near zero (most
    players are not starters), and fixed bins would put nine tenths of the data
    in one bucket and report nothing useful about the rest.
    """
    edges = np.quantile(p, np.linspace(0, 1, 11))
    out = []
    for low, high in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= low) & (p <= high)
        if mask.sum() == 0:
            continue
        out.append((float(p[mask].mean()), float(y[mask].mean())))
    return out


def train(
    session: Session,
    holdout_season: str | None = DEFAULT_HOLDOUT_SEASON,
    params: dict[str, Any] | None = None,
) -> TrainedMinutesModel:
    """Fit on the archived seasons, holding one out to measure against.

    Pass `holdout_season=None` to fit on everything once the numbers have been
    checked — the evaluation is then unavailable, which is the honest
    consequence of having no unseen data left.
    """
    frame = build_training_frame(session)
    if not len(frame):
        raise ValueError(
            "No training rows. Run `fplquant-import-history` to populate the archive first."
        )

    X = np.array(frame.features, dtype=np.float64)
    y = np.array(frame.labels, dtype=np.int64)
    seasons = np.array(frame.seasons)

    model = HistGradientBoostingClassifier(**(params or MODEL_PARAMS))
    evaluation = None

    if holdout_season is not None and (seasons == holdout_season).any():
        train_mask, test_mask = seasons != holdout_season, seasons == holdout_season
        model.fit(X[train_mask], y[train_mask])
        probabilities = model.predict_proba(X[test_mask])[:, 1]
        clipped = np.clip(probabilities, 1e-6, 1 - 1e-6)
        evaluation = Evaluation(
            n_train=int(train_mask.sum()),
            n_test=int(test_mask.sum()),
            holdout_season=holdout_season,
            log_loss=float(log_loss(y[test_mask], clipped)),
            roc_auc=float(roc_auc_score(y[test_mask], clipped)),
            brier=float(brier_score_loss(y[test_mask], clipped)),
            accuracy=float(((clipped > 0.5) == y[test_mask]).mean()),
            calibration=_calibration(y[test_mask], clipped),
        )
        # Refit on everything, including the holdout: the evaluation above is
        # what the numbers are, and shipping a model that has never seen the
        # most recent season would throw away the most relevant data there is.
        model = HistGradientBoostingClassifier(**(params or MODEL_PARAMS))

    model.fit(X, y)
    return TrainedMinutesModel(
        model=model, feature_names=tuple(FEATURE_NAMES), evaluation=evaluation
    )


def save(trained: TrainedMinutesModel, path: Path = DEFAULT_MODEL_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": trained.model, "feature_names": trained.feature_names}, path, compress=3)
    return path


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> TrainedMinutesModel | None:
    """Deserialise once per (path, mtime).

    `compute_minutes_profiles` asks for the model on every call, and every call
    was reading 230KB off disk and rebuilding the estimator — cheap enough to
    miss in production, obvious in a test suite that did it thousands of times.
    Keying on the file's modification time means a retrain is picked up without
    a restart, and without the caller having to know a cache exists.
    """
    return _load_uncached(Path(path))


def load(path: Path = DEFAULT_MODEL_PATH) -> TrainedMinutesModel | None:
    """Load a trained model, or None if there isn't one.

    None is a supported state, not an error. The engine falls back to its
    heuristic, so a checkout with no trained artefact — a fresh clone, a
    container that has never run the trainer — still produces predictions. A
    learned component that can take the whole app down when it is absent is a
    worse trade than a slightly weaker estimate.
    """
    if not path.exists():
        return None
    return _load_cached(str(path), path.stat().st_mtime)


def _load_uncached(path: Path) -> TrainedMinutesModel | None:
    try:
        payload = joblib.load(path)
    except Exception:
        logger.exception("Could not load the minutes model at %s; falling back", path)
        return None

    names = tuple(payload.get("feature_names", ()))
    if names != tuple(FEATURE_NAMES):
        # The feature builder has changed since this artefact was written, so
        # its inputs no longer mean what the model learned. Refusing is the
        # only safe answer: the vector would still be the right *length* often
        # enough for the mismatch to pass silently.
        logger.warning(
            "Minutes model at %s was trained on different features; ignoring it. Retrain with "
            "`fplquant-train-minutes`.",
            path,
        )
        return None
    return TrainedMinutesModel(model=payload["model"], feature_names=names, evaluation=None)
