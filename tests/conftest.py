from collections.abc import Iterator

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from fplquant.models.base import Base, make_engine


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def api_client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient wired to `db_session` (so tests seed data via the ORM
    directly) and an in-memory fake Redis (so caching is exercised without a
    real Redis server)."""
    from fplquant.api import cache as cache_module
    from fplquant.api.deps import get_session
    from fplquant.api.main import app

    app.dependency_overrides[get_session] = lambda: db_session
    cache_module.set_client(fakeredis.FakeRedis(decode_responses=True))

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def heuristic_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine tests run against the hand-built minutes heuristic by default.

    `compute_minutes_profiles` uses a trained model when one is present on
    disk, and that artefact is a build output — so without this the suite would
    take a different code path depending on whether anyone had run
    `fplquant-train-minutes`, and the synthetic fixtures these tests build
    would be fed to a model trained on real football. Tests that mean to
    exercise the model patch this back explicitly.
    """
    monkeypatch.setattr("fplquant.engine.minutes.load", lambda *args, **kwargs: None)
