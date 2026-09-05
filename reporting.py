"""
reporting.py
------------
Aggregation layer for the dashboard: totals, money recovered, breakdowns, and
per-transaction audit trails. Kept separate from main.py so it's independently
unit-testable and reusable (e.g. from a CLI or a notebook).
"""

from sqlalchemy.orm import Session
from sqlalchemy import func

from database import Transaction, RecoveryAction, RecoveryOutcome


def get_dashboard_metrics(db: Session) -> dict:
    total = db.query(func.count(Transaction.id)).scalar() or 0

    status_counts = dict(
        db.query(Transaction.status, func.count(Transaction.id))
        .group_by(Transaction.status)
        .all()
    )

    total_recovered_amount = (
        db.query(func.coalesce(func.sum(RecoveryOutcome.amount_recovered), 0.0))
        .filter(RecoveryOutcome.outcome == "recovered")
        .scalar()
        or 0.0
    )

    # Breakdown by failure reason (of transactions that were ever failed)
    failure_breakdown = dict(
        db.query(Transaction.failure_reason, func.count(Transaction.id))
        .filter(Transaction.failure_reason.isnot(None))
        .group_by(Transaction.failure_reason)
        .all()
    )

    # Breakdown by intervention type actually used
    intervention_breakdown = dict(
        db.query(RecoveryOutcome.intervention_used, func.count(RecoveryOutcome.id))
        .filter(RecoveryOutcome.intervention_used.isnot(None))
        .group_by(RecoveryOutcome.intervention_used)
        .all()
    )

    outcome_breakdown = dict(
        db.query(RecoveryOutcome.outcome, func.count(RecoveryOutcome.id))
        .group_by(RecoveryOutcome.outcome)
        .all()
    )

    return {
        "total_transactions": total,
        "status_counts": status_counts,
        "failed_count": status_counts.get("failed", 0),
        "pending_count": status_counts.get("pending", 0),
        "recovered_count": status_counts.get("recovered", 0),
        "escalated_count": status_counts.get("escalated", 0),
        "success_count": status_counts.get("success", 0),
        "total_money_recovered": round(total_recovered_amount, 2),
        "failure_breakdown": failure_breakdown,
        "intervention_breakdown": intervention_breakdown,
        "outcome_breakdown": outcome_breakdown,
    }


def get_audit_trail(db: Session, transaction_id: str) -> dict:
    txn = db.query(Transaction).filter(Transaction.transaction_id == transaction_id).first()
    if txn is None:
        return None

    actions = (
        db.query(RecoveryAction)
        .filter(RecoveryAction.transaction_id == txn.id)
        .order_by(RecoveryAction.performed_at.asc(), RecoveryAction.id.asc())
        .all()
    )
    outcomes = (
        db.query(RecoveryOutcome)
        .filter(RecoveryOutcome.transaction_id == txn.id)
        .order_by(RecoveryOutcome.recorded_at.asc(), RecoveryOutcome.id.asc())
        .all()
    )

    return {
        "transaction": {
            "transaction_id": txn.transaction_id,
            "merchant_id": txn.merchant_id,
            "customer_id": txn.customer_id,
            "amount": txn.amount,
            "status": txn.status,
            "failure_reason": txn.failure_reason,
            "retry_count": txn.retry_count,
            "max_retries": txn.max_retries,
        },
        "audit_trail": [
            {
                "step": a.step,
                "action_type": a.action_type,
                "failure_category": a.failure_category,
                "confidence_score": a.confidence_score,
                "reasoning": a.reasoning,
                "performed_at": a.performed_at.isoformat() if a.performed_at else None,
                "performed_by": a.performed_by,
            }
            for a in actions
        ],
        "outcomes": [
            {
                "outcome": o.outcome,
                "amount_recovered": o.amount_recovered,
                "intervention_used": o.intervention_used,
                "recorded_at": o.recorded_at.isoformat() if o.recorded_at else None,
                "notes": o.notes,
            }
            for o in outcomes
        ],
    }
