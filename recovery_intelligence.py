"""
Recovery intelligence layer.

Adds two explainable signals to every recovery opportunity:
1. Customer Recovery Profile - historical payment behavior for the customer.
2. Recovery Probability / Expected Recovery Value - an explainable probability estimate
   used to prioritize opportunities by likely recoverable revenue, not amount alone.

This is intentionally deterministic and offline-safe. It is a decisioning heuristic, not a
trained probability model, so the demo remains reproducible without an external ML service.
"""
from dataclasses import dataclass, asdict
from typing import Dict

from sqlalchemy.orm import Session

from database import Transaction
from diagnosis import diagnose


FAILURE_BASE_PROBABILITY = {
    "network_timeout": 0.72,
    "technical_error": 0.62,
    "bank_decline": 0.48,
    "insufficient_balance": 0.38,
    "expired_card": 0.30,
    "mandate_expired": 0.24,
    "abandoned_checkout": 0.52,
    "insufficient_data": 0.20,
}


def clamp(value: float, low: float = 0.05, high: float = 0.95) -> float:
    return max(low, min(high, value))


def build_customer_profiles(db: Session) -> Dict[str, dict]:
    """Build historical customer behavior profiles from transactions."""
    transactions = db.query(Transaction).all()
    profiles: Dict[str, dict] = {}

    for txn in transactions:
        p = profiles.setdefault(txn.customer_id, {
            "customer_id": txn.customer_id,
            "total_transactions": 0,
            "successful_transactions": 0,
            "failed_transactions": 0,
            "recovered_transactions": 0,
            "pending_transactions": 0,
            "escalated_transactions": 0,
            "total_value": 0.0,
            "successful_value": 0.0,
            "average_transaction_value": 0.0,
        })
        p["total_transactions"] += 1
        p["total_value"] += float(txn.amount or 0)
        if txn.status == "success":
            p["successful_transactions"] += 1
            p["successful_value"] += float(txn.amount or 0)
        elif txn.status == "failed":
            p["failed_transactions"] += 1
        elif txn.status == "recovered":
            p["recovered_transactions"] += 1
            p["successful_value"] += float(txn.amount or 0)
        elif txn.status == "pending":
            p["pending_transactions"] += 1
        elif txn.status == "escalated":
            p["escalated_transactions"] += 1

    for p in profiles.values():
        total = p["total_transactions"]
        positive = p["successful_transactions"] + p["recovered_transactions"]
        p["average_transaction_value"] = round(p["total_value"] / total, 2) if total else 0.0
        p["historical_success_rate"] = round(positive / total, 3) if total else 0.0
        if positive >= 5 and p["historical_success_rate"] >= 0.80:
            p["customer_value_segment"] = "high-trust"
        elif positive >= 2 and p["historical_success_rate"] >= 0.60:
            p["customer_value_segment"] = "established"
        else:
            p["customer_value_segment"] = "new-or-uncertain"

    return profiles


def recovery_probability(txn: Transaction, profile: dict) -> dict:
    """Estimate probability of recovery with transparent factors."""
    diagnosis = diagnose(txn)
    base = FAILURE_BASE_PROBABILITY.get(diagnosis.failure_category, 0.30)

    # Historical customer behavior is the strongest contextual adjustment.
    history_rate = profile.get("historical_success_rate", 0.0)
    history_adjustment = (history_rate - 0.50) * 0.30 if profile.get("total_transactions", 0) >= 2 else 0.0

    # More retry runway generally means a fresher, more recoverable opportunity.
    retries_left = max((txn.max_retries or 3) - (txn.retry_count or 0), 0)
    retry_adjustment = min(retries_left, 3) * 0.04

    # Abandoned checkout gets a small customer-history boost, while expired/mandate failures
    # remain conservative because the underlying payment credential/consent may need changes.
    amount = float(txn.amount or 0)
    amount_adjustment = 0.02 if amount <= 5000 else -0.01
    if diagnosis.failure_category in ("expired_card", "mandate_expired"):
        amount_adjustment = 0.0

    probability = clamp(base + history_adjustment + retry_adjustment + amount_adjustment)
    probability = round(probability, 3)
    expected_value = round(amount * probability, 2)

    return {
        "recovery_probability": probability,
        "expected_recovery_value": expected_value,
        "probability_factors": {
            "base_failure_signal": round(base, 3),
            "customer_history_adjustment": round(history_adjustment, 3),
            "retry_runway_adjustment": round(retry_adjustment, 3),
            "transaction_size_adjustment": round(amount_adjustment, 3),
            "diagnosis_category": diagnosis.failure_category,
        },
    }


def enrich_opportunity(db: Session, opportunity: dict, profiles: Dict[str, dict] = None) -> dict:
    """Add customer profile and recovery-value signals to an opportunity."""
    if profiles is None:
        profiles = build_customer_profiles(db)
    txn = db.query(Transaction).filter(Transaction.id == opportunity["db_id"]).first()
    profile = profiles.get(opportunity["customer_id"], {
        "customer_id": opportunity["customer_id"],
        "total_transactions": 0,
        "successful_transactions": 0,
        "failed_transactions": 0,
        "recovered_transactions": 0,
        "pending_transactions": 0,
        "escalated_transactions": 0,
        "total_value": 0.0,
        "successful_value": 0.0,
        "average_transaction_value": 0.0,
        "historical_success_rate": 0.0,
        "customer_value_segment": "new-or-uncertain",
    })
    probability = recovery_probability(txn, profile)
    result = dict(opportunity)
    result["customer_profile"] = profile
    result.update(probability)
    # Expected recovery value is the primary revenue-aware priority signal.
    result["recovery_priority_score"] = round(
        probability["expected_recovery_value"]
        * (1 + max((txn.max_retries or 3) - (txn.retry_count or 0), 0) * 0.05),
        2,
    )
    return result
