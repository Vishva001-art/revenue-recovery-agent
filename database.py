"""
database.py
------------
SQLAlchemy models and session/engine setup for the AI Revenue Recovery Agent.

Tables:
    - Transaction       : the raw payment record (from the payment processor)
    - RecoveryAction     : every action the agent took on a transaction (audit trail)
    - RecoveryOutcome    : the final measured result of a recovery attempt (money recovered or not)

Design notes:
    - SQLite is used for simplicity/portability during the hackathon; the models are plain
      SQLAlchemy ORM classes, so swapping to Postgres later is a one-line change (DATABASE_URL).
    - Every RecoveryAction row is immutable once written -> gives us a tamper-evident audit trail.
    - Monetary values are stored as integers in paise (smallest currency unit) to avoid float
      rounding errors, mirroring how Razorpay's own APIs represent amounts.
"""

import os
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.environ.get("RECOVERY_AGENT_DB_URL", "sqlite:///./recovery_agent.db")

# check_same_thread=False is needed because FastAPI can use the connection across threads;
# for a single-worker dev server with SQLite this is safe.
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(String, unique=True, index=True, nullable=False)
    merchant_id = Column(String, index=True, nullable=False)
    customer_id = Column(String, index=True, nullable=False)
    customer_email = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)

    amount = Column(Float, nullable=False)          # in INR
    currency = Column(String, default="INR")
    payment_method = Column(String, nullable=False)  # card, upi, netbanking

    status = Column(String, nullable=False, index=True)  # success, failed, pending, recovered, abandoned
    failure_reason = Column(String, nullable=True)        # insufficient_balance, expired_card, ...

    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)  # stopping rule: never retry forever

    created_at = Column(DateTime, default=utcnow)
    last_retry_at = Column(DateTime, nullable=True)
    checkout_initiated_at = Column(DateTime, nullable=True)  # for abandoned-checkout detection

    consent_marketing = Column(Boolean, default=True)  # compliance: can we email/offer discount?

    actions = relationship("RecoveryAction", back_populates="transaction", cascade="all, delete-orphan")
    outcomes = relationship("RecoveryOutcome", back_populates="transaction", cascade="all, delete-orphan")


class RecoveryAction(Base):
    """One row per action the agent took. This is the audit trail."""
    __tablename__ = "recovery_actions"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False)

    step = Column(String, nullable=False)  # detection, diagnosis, intervention, outcome
    action_type = Column(String, nullable=True)  # retry, email_reminder, discount_offer, escalate, wait
    failure_category = Column(String, nullable=True)
    confidence_score = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)  # human-readable explanation, for auditability

    performed_at = Column(DateTime, default=utcnow)
    performed_by = Column(String, default="ai_agent")  # ai_agent | human | system

    transaction = relationship("Transaction", back_populates="actions")


class RecoveryOutcome(Base):
    """Final measured result of a recovery attempt."""
    __tablename__ = "recovery_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False)

    outcome = Column(String, nullable=False)  # recovered, still_failed, pending, escalated
    amount_recovered = Column(Float, default=0.0)
    intervention_used = Column(String, nullable=True)
    recorded_at = Column(DateTime, default=utcnow)
    notes = Column(Text, nullable=True)

    transaction = relationship("Transaction", back_populates="outcomes")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency: yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def reset_db():
    """Drops and recreates all tables. Used by generate_data.py and tests."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DATABASE_URL}")
