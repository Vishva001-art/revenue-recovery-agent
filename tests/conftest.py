"""
conftest.py
-----------
Shared pytest fixtures. Critically, this redirects the app to an isolated
SQLite file (test_recovery_agent.db) BEFORE any application module is imported,
so tests never touch the real demo database (recovery_agent.db).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_recovery_agent.db")
os.environ["RECOVERY_AGENT_DB_URL"] = f"sqlite:///{TEST_DB_PATH}"

import pytest
from datetime import datetime, timedelta, timezone

from database import Base, engine, SessionLocal, Transaction, reset_db
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean_db():
    """Runs before EVERY test: fresh, empty schema."""
    reset_db()
    yield
    # leave tables in place; next test resets again


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _mk_txn(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        transaction_id=f"test_txn_{overrides.get('transaction_id', 'x')}",
        merchant_id="merch_test",
        customer_id="cust_test",
        customer_email="test@example.com",
        customer_phone="9999999999",
        amount=1000.0,
        currency="INR",
        payment_method="card",
        status="failed",
        failure_reason="bank_decline",
        retry_count=0,
        max_retries=3,
        created_at=now,
        last_retry_at=None,
        checkout_initiated_at=None,
        consent_marketing=True,
    )
    defaults.update(overrides)
    return Transaction(**defaults)


@pytest.fixture
def seeded_transactions(db_session):
    """Seeds a small, deterministic set of transactions covering every status/reason."""
    now = datetime.now(timezone.utc)
    rows = [
        _mk_txn(transaction_id="t_success", status="success", failure_reason=None),
        _mk_txn(transaction_id="t_failed_fresh", status="failed", failure_reason="insufficient_balance",
                created_at=now - timedelta(days=1), retry_count=0),
        _mk_txn(transaction_id="t_failed_old", status="failed", failure_reason="expired_card",
                created_at=now - timedelta(days=20), retry_count=1),  # outside 7-day window
        _mk_txn(transaction_id="t_failed_maxed", status="failed", failure_reason="network_timeout",
                created_at=now - timedelta(days=1), retry_count=3, max_retries=3),  # retries exhausted
        _mk_txn(transaction_id="t_pending_abandoned", status="pending", failure_reason=None,
                checkout_initiated_at=now - timedelta(minutes=45)),  # past 30-min window
        _mk_txn(transaction_id="t_pending_fresh", status="pending", failure_reason=None,
                checkout_initiated_at=now - timedelta(minutes=5)),  # within window, not abandoned yet
        _mk_txn(transaction_id="t_no_consent", status="failed", failure_reason="mandate_expired",
                created_at=now - timedelta(days=1), consent_marketing=False),
        _mk_txn(transaction_id="t_unrecognized_reason", status="failed", failure_reason="weird_unmapped_reason",
                created_at=now - timedelta(days=1)),
        _mk_txn(transaction_id="t_missing_reason", status="failed", failure_reason=None,
                created_at=now - timedelta(days=1)),
    ]
    for r in rows:
        db_session.add(r)
    db_session.commit()
    for r in rows:
        db_session.refresh(r)
    return rows


@pytest.fixture
def client():
    """FastAPI TestClient wired to the isolated test DB (via env var already set above)."""
    from main import app
    reset_db()
    return TestClient(app)
