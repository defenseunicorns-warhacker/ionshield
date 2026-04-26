"""
Shared pytest fixtures for the IonShield test suite.

The module-level TestClient in several test files does NOT run the FastAPI
lifespan (init_db is only called during startup). In CI there is no pre-existing
ionshield.db, so tables must be created explicitly before any DB-touching tests.

This session-scoped autouse fixture calls init_db() once per pytest session,
which is idempotent (SQLAlchemy uses CREATE TABLE IF NOT EXISTS).
"""

import asyncio

import pytest

from app.data.db import init_db


@pytest.fixture(scope="session", autouse=True)
def initialise_database():
    """Create all SQLite tables before the test session starts."""
    asyncio.run(init_db())
    yield
