"""
detection.py
------------
The Detection module scans the Transaction table and surfaces "recovery opportunities":
    1. At-risk transactions   : status == failed, failed within the last 7 days, and
                                 retry_count < max_retries (stopping rule respected).
    2. Abandoned checkouts    : status == pending with checkout_initiated_at more than
                                 30 minutes in the past (customer started paying, never finished).
    3. Grouping               : opportunities are grouped by merchant_id and customer_id so
                                 downstream modules have context (e.g. a customer with 3 failures
                                 this week is a stronger recovery priority than a first-time miss).
    4. Prioritization         : higher amount + higher retry urgency + fewer prior retries used
                                 = higher priority (we don't want to burn retries on lost causes).
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from database import Transaction
from recovery_intelligence import build_customer_profiles, enrich_opportunity

AT_RISK_WINDOW_DAYS = 7
ABANDONED_CHECKOUT_MINUTES = 30


def _now():
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite strips tzinfo on read; normalize to UTC-aware for safe comparison."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def find_at_risk_transactions(db: Session, window_days: int = AT_RISK_WINDOW_DAYS):
    """Failed payments within the last `window_days` that haven't exhausted retries."""
    cutoff = _now() - timedelta(days=window_days)
    candidates = db.query(Transaction).filter(Transaction.status == "failed").all()

    at_risk = []
    for txn in candidates:
        created = _aware(txn.created_at)
        if created is not None and created >= cutoff and txn.retry_count < txn.max_retries:
            at_risk.append(txn)
    return at_risk


def find_abandoned_checkouts(db: Session, minutes: int = ABANDONED_CHECKOUT_MINUTES):
    """Pending transactions whose checkout was initiated but never completed."""
    cutoff = _now() - timedelta(minutes=minutes)
    candidates = db.query(Transaction).filter(Transaction.status == "pending").all()

    abandoned = []
    for txn in candidates:
        initiated = _aware(txn.checkout_initiated_at)
        if initiated is not None and initiated <= cutoff:
            abandoned.append(txn)
    return abandoned


def group_by_merchant_and_customer(transactions):
    """Returns {merchant_id: {customer_id: [transactions]}} for contextual grouping."""
    grouped = {}
    for txn in transactions:
        grouped.setdefault(txn.merchant_id, {}).setdefault(txn.customer_id, []).append(txn)
    return grouped


def _priority_score(txn: Transaction, customer_txn_count: int) -> float:
    """
    Higher score = recover this first.
    Heuristics:
      - Bigger amount recovered matters more (revenue impact)
      - Fewer retries already used = more "runway" left, and reflects fresher failures
      - A customer with repeated recent failures is weighted slightly higher (real intent, likely
        a fixable systemic issue like an expired card on file) but capped so we don't over-index.
    """
    amount_score = txn.amount
    retries_left = max(txn.max_retries - txn.retry_count, 0)
    retry_bonus = 1 + (retries_left * 0.1)
    repeat_customer_bonus = 1 + min(customer_txn_count - 1, 3) * 0.05
    return amount_score * retry_bonus * repeat_customer_bonus


def get_recovery_opportunities(db: Session, window_days: int = AT_RISK_WINDOW_DAYS,
                                abandoned_minutes: int = ABANDONED_CHECKOUT_MINUTES):
    """
    Main entry point: returns a prioritized list of dicts, each describing one
    recovery opportunity, highest priority first.
    """
    at_risk = find_at_risk_transactions(db, window_days)
    abandoned = find_abandoned_checkouts(db, abandoned_minutes)
    all_opps = at_risk + abandoned

    # Count transactions per customer (across the batch) for grouping context
    customer_counts = {}
    for txn in all_opps:
        customer_counts[txn.customer_id] = customer_counts.get(txn.customer_id, 0) + 1

    scored = []
    for txn in all_opps:
        opp_type = "at_risk_failed" if txn.status == "failed" else "abandoned_checkout"
        score = _priority_score(txn, customer_counts.get(txn.customer_id, 1))
        scored.append({
            "transaction_id": txn.transaction_id,
            "db_id": txn.id,
            "merchant_id": txn.merchant_id,
            "customer_id": txn.customer_id,
            "amount": txn.amount,
            "payment_method": txn.payment_method,
            "status": txn.status,
            "failure_reason": txn.failure_reason,
            "retry_count": txn.retry_count,
            "max_retries": txn.max_retries,
            "opportunity_type": opp_type,
            "priority_score": round(score, 2),
        })

    # Add explainable customer context and expected recovery value.
    profiles = build_customer_profiles(db)
    enriched = [enrich_opportunity(db, item, profiles) for item in scored]

    # Revenue-aware ordering: expected recoverable money first, with the original
    # priority score retained as a secondary signal for explainability.
    enriched.sort(key=lambda x: (x["recovery_priority_score"], x["priority_score"]), reverse=True)
    return enriched


if __name__ == "__main__":
    from database import SessionLocal
    session = SessionLocal()
    try:
        opps = get_recovery_opportunities(session)
        print(f"Found {len(opps)} recovery opportunities")
        for o in opps[:5]:
            print(o)
    finally:
        session.close()
