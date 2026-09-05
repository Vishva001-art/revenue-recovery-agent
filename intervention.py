"""
intervention.py
----------------
Executes the intervention chosen by diagnosis.py and writes the full audit trail.

Every recovery attempt for a transaction produces THREE audit rows in RecoveryAction:
    1. step="detection"   -> why this transaction was flagged
    2. step="diagnosis"   -> failure_category, confidence, suggested_intervention, reasoning
    3. step="intervention"-> the action actually executed (may differ from suggestion if
                              a stopping rule or compliance rule overrides it)
...and one RecoveryOutcome row recording the measured result (recovered / still_failed / etc).

Actions are SIMULATED (no real payment gateway, email server, or SMS calls happen here) --
this is a defense-only, audit-first system per the hackathon's constraints. Every "send" is a
structured log entry and a state transition, never an actual outbound network call to a bank
or a real customer.

Compliance / safety rules enforced here (not just in diagnosis):
    - discount_offer / email_reminder are skipped if txn.consent_marketing is False (data privacy).
    - retry is refused once retry_count >= max_retries, regardless of what diagnosis suggested
      (hard stopping rule, enforced at the point of execution -- defense in depth).
    - escalate never touches money; it only flags for a human, and always "succeeds" as an action.
"""

import random
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from database import Transaction, RecoveryAction, RecoveryOutcome
from diagnosis import Diagnosis, diagnose, diagnose_many

random.seed(7)  # deterministic simulated outcomes for reproducible demos/tests


def _log_action(db: Session, txn: Transaction, step: str, action_type: str = None,
                 failure_category: str = None, confidence_score: float = None,
                 reasoning: str = None, performed_by: str = "ai_agent"):
    row = RecoveryAction(
        transaction_id=txn.id,
        step=step,
        action_type=action_type,
        failure_category=failure_category,
        confidence_score=confidence_score,
        reasoning=reasoning,
        performed_by=performed_by,
    )
    db.add(row)
    return row


def _simulate_action_success(action_type: str, txn: Transaction) -> bool:
    """
    Simulates whether an action results in the payment being recovered.
    Probabilities are illustrative, tuned per intervention type to be realistic:
      - retry: moderate success (transient issues often resolve)
      - email_reminder: lower, delayed success (customer has to act)
      - discount_offer: boosts abandoned-checkout conversion
      - escalate / wait: no immediate recovery (human/monitoring handles it later)
    """
    base_rates = {
        "retry": 0.45,
        "email_reminder": 0.30,
        "discount_offer": 0.40,
        "escalate": 0.0,
        "wait": 0.0,
    }
    return random.random() < base_rates.get(action_type, 0.0)


def choose_intervention(db: Session, txn: Transaction, diag: Diagnosis) -> str:
    """
    Applies compliance/stopping-rule overrides on top of the diagnosis's suggestion.
    This is the single authoritative place where the FINAL action is decided.
    """
    action = diag.suggested_intervention

    # Hard stopping rule: never retry past max_retries, no matter what diagnosis said.
    if action == "retry" and txn.retry_count >= txn.max_retries:
        action = "escalate"

    # Compliance: no marketing/contact actions without consent.
    if action in ("email_reminder", "discount_offer") and not txn.consent_marketing:
        action = "escalate"

    return action


def execute_recovery(db: Session, txn: Transaction, diag: Diagnosis = None) -> dict:
    """
    Runs the full pipeline for ONE transaction: diagnosis -> intervention -> outcome,
    writing the audit trail at every step. Returns a summary dict.

    Idempotent-ish: if the transaction is already 'success' or 'recovered', it's skipped
    (no duplicate actions on money that's already settled).

    `diag` lets a caller (e.g. batch_recover) pass in an already-computed Diagnosis so this
    function doesn't repeat a (potentially network-bound) diagnosis call. If omitted, it's
    diagnosed here as before -- single-transaction callers like POST /recover/{id} don't pay
    any extra cost either way.
    """
    if txn.status in ("success", "recovered"):
        return {
            "transaction_id": txn.transaction_id,
            "skipped": True,
            "reason": "transaction already settled; no recovery needed",
        }

    # --- Detection step audit (why we're even looking at this transaction) ---
    detection_reason = (
        "failed payment within recovery window" if txn.status == "failed"
        else "abandoned checkout past completion window"
    )
    _log_action(db, txn, step="detection", reasoning=detection_reason)

    # --- Diagnosis step ---
    diag = diag or diagnose(txn)
    _log_action(
        db, txn, step="diagnosis",
        failure_category=diag.failure_category,
        confidence_score=diag.confidence_score,
        reasoning=f"[{diag.backend_used}] {diag.reasoning}",
    )

    # --- Intervention step ---
    final_action = choose_intervention(db, txn, diag)
    _log_action(
        db, txn, step="intervention", action_type=final_action,
        reasoning=f"Selected '{final_action}' (diagnosis suggested '{diag.suggested_intervention}')",
    )

    now = datetime.now(timezone.utc)

    if final_action == "retry":
        txn.retry_count += 1
        txn.last_retry_at = now
        recovered = _simulate_action_success("retry", txn)
    elif final_action in ("email_reminder", "discount_offer"):
        recovered = _simulate_action_success(final_action, txn)
    elif final_action == "wait":
        recovered = False
    elif final_action == "escalate":
        recovered = False
    else:
        recovered = False

    if recovered:
        txn.status = "recovered"
        outcome_label = "recovered"
        amount_recovered = txn.amount
        notes = f"Customer completed payment after '{final_action}' intervention."
    elif final_action == "escalate":
        txn.status = "escalated"
        outcome_label = "escalated"
        amount_recovered = 0.0
        notes = "Flagged for human review; no automated action taken on funds."
    elif final_action == "wait":
        outcome_label = "pending"
        amount_recovered = 0.0
        notes = "Monitoring window active; will re-check within 24 hours."
    else:
        outcome_label = "still_failed"
        amount_recovered = 0.0
        notes = f"'{final_action}' did not result in recovery this attempt."

    outcome_row = RecoveryOutcome(
        transaction_id=txn.id,
        outcome=outcome_label,
        amount_recovered=amount_recovered,
        intervention_used=final_action,
        notes=notes,
    )
    db.add(outcome_row)
    db.add(txn)
    db.commit()
    db.refresh(txn)

    return {
        "transaction_id": txn.transaction_id,
        "skipped": False,
        "failure_category": diag.failure_category,
        "confidence_score": diag.confidence_score,
        "intervention": final_action,
        "outcome": outcome_label,
        "amount_recovered": amount_recovered,
        "new_status": txn.status,
    }


def batch_recover(db: Session, transactions) -> list:
    """
    Runs execute_recovery for a list of transactions, continuing past individual errors.

    Diagnosis is the only network-bound step (LLM backend, when OPENAI_API_KEY is set) --
    running it one transaction at a time is what made "recover all eligible" slow on a
    larger batch. We diagnose every eligible transaction concurrently up front
    (diagnose_many), then apply each result sequentially -- the remaining work per
    transaction is pure DB writes on one session, which is fast and safest kept sequential.
    """
    eligible = [t for t in transactions if getattr(t, "status", None) not in ("success", "recovered")]
    diagnoses = diagnose_many(eligible)

    results = []
    for txn in transactions:
        try:
            results.append(execute_recovery(db, txn, diag=diagnoses.get(txn.id)))
        except Exception as e:
            db.rollback()
            results.append({
                "transaction_id": getattr(txn, "transaction_id", "unknown"),
                "skipped": True,
                "error": str(e),
            })
    return results


if __name__ == "__main__":
    from database import SessionLocal
    from detection import get_recovery_opportunities

    session = SessionLocal()
    try:
        opps = get_recovery_opportunities(session)
        txns = [session.get(Transaction, o["db_id"]) for o in opps[:10]]
        results = batch_recover(session, txns)
        for r in results:
            print(r)
    finally:
        session.close()
