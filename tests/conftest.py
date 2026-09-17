"""Pytest fixtures.

Environment variables are set *before* importing the application so that
``app.config`` / ``app.database`` build their singletons against a throwaway
SQLite file instead of the developer database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TEST_DB = PROJECT_ROOT / "data" / "test_app.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
if TEST_DB.exists():
    TEST_DB.unlink()

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"sqlite:///{TEST_DB.as_posix()}",
        "ENGINE_MODE": "builtin",
        "LLM_ENABLED": "false",
        "AUTH_MODE": "dev",
        "JWT_SECRET": "test-secret-value-for-unit-tests-only",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "test-admin-pass",
        "AUTO_SEED": "true",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "HANDOFF_MAX_FALLBACK_TURNS": "2",
        "FAQ_MIN_SCORE": "1.2",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.tools.business import seed_demo_data  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    init_db()
    with SessionLocal() as db:
        seed_demo_data(db)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user_headers():
    return {"X-User-Id": "user001"}


@pytest.fixture
def admin_headers(client):
    response = client.post("/api/admin/login", json={"username": "admin", "password": "test-admin-pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}
