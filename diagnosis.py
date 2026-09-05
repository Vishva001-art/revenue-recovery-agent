"""
diagnosis.py
------------
Given a failed/abandoned transaction, determines:
    - failure_category   : one of the known failure reasons
    - confidence_score    : 0.0-1.0
    - suggested_intervention : retry | email_reminder | discount_offer | escalate | wait
    - reasoning           : short human-readable explanation (for the audit trail)

Two backends, chosen automatically:
    1. LLM backend (OpenAI): if OPENAI_API_KEY is set, we ask an LLM to classify the failure
       from the transaction's metadata (failure_reason as reported by the processor, payment
       method, retry history, amount) and to justify an intervention. Structured JSON output.
    2. Rule-based fallback: a transparent, deterministic classifier used when no API key is
       configured (e.g. for offline demos/tests, or if the LLM call errors out). This guarantees
       the system NEVER silently fails to diagnose a transaction -- a defense-only, explainable
       agent should degrade gracefully, not crash or return "unknown" for every case.

Edge cases handled:
    - Missing / null failure_reason -> falls back to "insufficient_data", low confidence, escalate.
    - Unrecognized failure_reason string -> mapped to "technical_error" with a reasoning note.
    - Retry count already at/near max -> intervention is capped to non-retry actions.
    - LLM call fails (network, auth, malformed response) -> automatic fallback to rule engine,
      never raises to the caller.
"""

import concurrent.futures
import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, Optional

FAILURE_CATEGORIES = [
    "insufficient_balance",
    "expired_card",
    "bank_decline",
    "network_timeout",
    "mandate_expired",
    "technical_error",
    "insufficient_data",   # edge case: not enough info to say anything more specific
    "abandoned_checkout",  # for pending/never-completed transactions
]

INTERVENTIONS = ["retry", "email_reminder", "discount_offer", "escalate", "wait"]

# One HTTP call per transaction (LLM backend) is what makes a large "recover all" batch feel
# slow -- diagnose_many() fans these out across a small thread pool instead of running them
# one at a time. 8 is plenty for a demo-sized batch without hammering the OpenAI rate limit.
DIAGNOSIS_MAX_WORKERS = int(os.environ.get("DIAGNOSIS_MAX_WORKERS", "8"))

# Hard cap per LLM call so one slow/hanging request can't stall the whole batch -- it just
# falls back to the rule-based engine for that transaction instead.
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "8"))

# Deterministic mapping used by the rule-based engine and as a sanity default for the LLM path.
DEFAULT_INTERVENTION_MAP = {
    "insufficient_balance": "email_reminder",   # ask them to top up, don't hammer the bank
    "expired_card": "email_reminder",           # need a new card on file -> can't auto-retry
    "bank_decline": "retry",                    # often transient; safe to retry a limited number of times
    "network_timeout": "retry",                 # classic transient failure -> retry
    "mandate_expired": "escalate",              # needs a fresh mandate/consent -> human/flow needed
    "technical_error": "retry",
    "insufficient_data": "escalate",
    "abandoned_checkout": "discount_offer",     # nudge hesitant customers with a small incentive
}


@dataclass
class Diagnosis:
    failure_category: str
    confidence_score: float
    suggested_intervention: str
    reasoning: str
    backend_used: str  # "llm" or "rule_based"

    def to_dict(self):
        return asdict(self)


def _rule_based_diagnose(txn) -> Diagnosis:
    """Transparent, deterministic diagnosis. Always succeeds."""
    reason = getattr(txn, "failure_reason", None)
    status = getattr(txn, "status", None)
    retry_count = getattr(txn, "retry_count", 0) or 0
    max_retries = getattr(txn, "max_retries", 3) or 3

    if status == "pending":
        category = "abandoned_checkout"
        confidence = 0.75
        reasoning = "Checkout was initiated but never completed within the window; likely hesitation or drop-off, not a technical failure."
    elif reason is None:
        category = "insufficient_data"
        confidence = 0.3
        reasoning = "No failure_reason was reported by the processor; cannot classify further without more data."
    elif reason not in FAILURE_CATEGORIES:
        category = "technical_error"
        confidence = 0.4
        reasoning = f"Unrecognized failure_reason '{reason}' from processor; defaulting to technical_error for safety."
    else:
        category = reason
        # Confidence heuristic: fewer retries already attempted = fresher signal = higher confidence
        confidence = round(max(0.55, 0.9 - retry_count * 0.1), 2)
        reasoning = f"Processor reported '{reason}' as the decline reason for a {txn.payment_method} payment."

    intervention = DEFAULT_INTERVENTION_MAP.get(category, "escalate")

    # Stopping rule: if retries are exhausted, never suggest another retry -- escalate instead.
    if intervention == "retry" and retry_count >= max_retries:
        intervention = "escalate"
        reasoning += f" Retry limit ({max_retries}) already reached; escalating to human review instead of retrying again."

    return Diagnosis(
        failure_category=category,
        confidence_score=confidence,
        suggested_intervention=intervention,
        reasoning=reasoning,
        backend_used="rule_based",
    )


def _build_llm_prompt(txn) -> str:
    return f"""You are a payments risk analyst. Classify this failed/abandoned transaction.

Transaction:
- status: {txn.status}
- reported_failure_reason: {txn.failure_reason}
- payment_method: {txn.payment_method}
- amount: {txn.amount}
- retry_count: {txn.retry_count} / max_retries: {txn.max_retries}

Valid failure_category values: {FAILURE_CATEGORIES}
Valid suggested_intervention values: {INTERVENTIONS}

Respond ONLY with JSON, no markdown, in this exact shape:
{{"failure_category": "...", "confidence_score": 0.0, "suggested_intervention": "...", "reasoning": "one sentence"}}
"""


def _llm_diagnose(txn) -> Optional[Diagnosis]:
    """Attempts an LLM-backed diagnosis. Returns None (never raises) if unavailable/fails."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": _build_llm_prompt(txn)}],
            temperature=0,
            max_tokens=200,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        category = data.get("failure_category")
        intervention = data.get("suggested_intervention")
        if category not in FAILURE_CATEGORIES or intervention not in INTERVENTIONS:
            return None  # malformed -> let caller fall back to rule engine

        retry_count = getattr(txn, "retry_count", 0) or 0
        max_retries = getattr(txn, "max_retries", 3) or 3
        reasoning = data.get("reasoning", "")
        if intervention == "retry" and retry_count >= max_retries:
            intervention = "escalate"
            reasoning += " (capped: retry limit already reached)"

        return Diagnosis(
            failure_category=category,
            confidence_score=float(data.get("confidence_score", 0.6)),
            suggested_intervention=intervention,
            reasoning=reasoning or "LLM classification.",
            backend_used="llm",
        )
    except Exception:
        # Network error, auth error, malformed JSON, etc. -- degrade gracefully.
        return None


def diagnose(txn) -> Diagnosis:
    """
    Main entry point. Tries the LLM backend first (if configured), falls back to the
    rule-based engine on any failure. Never raises -- diagnosis must always return something
    actionable so the pipeline can continue.
    """
    result = _llm_diagnose(txn)
    if result is not None:
        return result
    return _rule_based_diagnose(txn)


def diagnose_many(txns: Iterable) -> Dict[int, Diagnosis]:
    """
    Diagnoses a batch of transactions concurrently, keyed by txn.id.

    Why this exists: diagnosis is the only network-bound step in the whole pipeline (one
    HTTP call to OpenAI per transaction when the LLM backend is configured). Running that
    one-at-a-time for a batch of dozens of failed/pending transactions is the main reason
    "recover all eligible" can feel slow. Fanning the calls out across a small thread pool
    cuts that wall-clock time roughly by the pool size. When no LLM is configured, diagnose()
    is already near-instant (pure Python, no I/O), so this is still safe and simply adds a
    little thread-pool overhead in that case.

    Never raises: any per-transaction failure falls back to the rule-based diagnosis for
    that transaction, same guarantee as diagnose().
    """
    txns = list(txns)
    if not txns:
        return {}

    results: Dict[int, Diagnosis] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=DIAGNOSIS_MAX_WORKERS) as pool:
        future_to_txn = {pool.submit(diagnose, txn): txn for txn in txns}
        for future in concurrent.futures.as_completed(future_to_txn):
            txn = future_to_txn[future]
            try:
                results[txn.id] = future.result()
            except Exception:
                results[txn.id] = _rule_based_diagnose(txn)
    return results


if __name__ == "__main__":
    from database import SessionLocal, Transaction
    session = SessionLocal()
    try:
        sample = session.query(Transaction).filter(Transaction.status.in_(["failed", "pending"])).limit(5).all()
        for txn in sample:
            d = diagnose(txn)
            print(txn.transaction_id, "->", d.to_dict())
    finally:
        session.close()
