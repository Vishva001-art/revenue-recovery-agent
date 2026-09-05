# Architecture — AI Revenue Recovery Agent

## 1. High-level data flow

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                    SQLite Database                       │
                    │  transactions | recovery_actions | recovery_outcomes     │
                    └───────────────────────────▲───────────────────────────┬─┘
                                                  │ read                     │ write (audit)
                    ┌─────────────────────────────┴───────────────────────────▼─┐
                    │                                                            │
   ┌────────────┐   │   ┌────────────┐    ┌────────────┐    ┌───────────────┐   │
   │  generate_ │──▶│   │ DETECTION  │───▶│ DIAGNOSIS  │───▶│ INTERVENTION  │   │
   │  data.py   │   │   │detection.py│    │diagnosis.py│    │intervention.py│   │
   └────────────┘   │   └────────────┘    └─────┬──────┘    └───────┬───────┘   │
   (synthetic txns)  │   at-risk +              │ LLM or            │           │
                     │   abandoned              │ rule-based        │ action    │
                     │   checkouts,             │ classifier        │ executed +│
                     │   prioritized            │                   │ outcome   │
                     │                                               logged     │
                    └────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │                 TRACKING & REPORTING                     │
                    │                    reporting.py                          │
                    │   dashboard metrics · audit trail · breakdowns           │
                    └───────────────────────────▲───────────────────────────┬─┘
                                                  │                          │
                    ┌─────────────────────────────┴──────────────────────────▼─┐
                    │                     FastAPI (main.py)                     │
                    │  /transactions  /recovery-opportunities  /recover/{id}    │
                    │  /batch-recover  /dashboard  /audit/{id}                  │
                    └───────────────────────────▲────────────────────────────┘
                                                  │ HTTP (JSON)
                    ┌─────────────────────────────┴───────────────────────────┐
                    │           Web UI (static/index.html)                     │
                    │   summary strip · breakdown bars · transaction table ·   │
                    │   audit-trail drawer · "Recover all eligible" button     │
                    └───────────────────────────────────────────────────────────┘
```

## 2. Components and their interactions

| Component | File | Responsibility |
|---|---|---|
| Data model | `database.py` | SQLAlchemy ORM: `Transaction`, `RecoveryAction`, `RecoveryOutcome`. Single source of truth; everything else reads/writes through this layer. |
| Synthetic data | `generate_data.py` | Produces realistic transaction batches with method-biased failure reasons, plus a ground-truth label export (`data/labels.json`) used to score the diagnosis module in tests. |
| Detection | `detection.py` | Pure functions over a DB session: finds at-risk failed payments (7-day window, retries remaining), abandoned checkouts (30-min window), groups by merchant/customer, and produces a priority-scored opportunity list. Stateless — no writes. |
| Diagnosis | `diagnosis.py` | Given one transaction, returns `(failure_category, confidence_score, suggested_intervention, reasoning)`. Two backends: LLM (OpenAI, if `OPENAI_API_KEY` set) and a deterministic rule-based engine used as both the default and the automatic fallback. Never raises. |
| Intervention | `intervention.py` | Takes the diagnosis's suggestion, applies compliance and stopping-rule overrides (`choose_intervention`), simulates the action's effect, and writes the full audit trail (`RecoveryAction` ×3 + `RecoveryOutcome` ×1) per transaction. |
| Reporting | `reporting.py` | Aggregation queries: dashboard totals, money recovered, breakdowns by failure reason / intervention / outcome, and full audit-trail retrieval for one transaction. |
| API | `main.py` | FastAPI app wiring all of the above to HTTP endpoints, plus serving the static UI. |
| UI | `static/index.html` | Single-page dashboard: summary strip, breakdown bars, filterable transaction table, and a slide-out audit-trail drawer per transaction. Talks to the API via `fetch()`. |

## 3. AI integration point

The **only** AI call in the system is in `diagnosis.py::_llm_diagnose()`. It is deliberately isolated to one function with one job: turn transaction metadata into a structured `(category, confidence, intervention, reasoning)` tuple. This keeps the "AI surface area" small and auditable:

- Prompted with a fixed set of valid categories/interventions and asked for JSON-only output.
- Response is validated (category and intervention must be in the known enums) before being trusted; a malformed or out-of-vocabulary response is treated as a failure and triggers fallback, not a runtime error.
- `temperature=0` for reproducibility in a financial-decision context.
- The stopping-rule and compliance overrides in `intervention.py::choose_intervention()` run **after** the LLM's suggestion and can override it — the LLM proposes, deterministic business rules dispose. This means a hallucinated or edge-case LLM suggestion (e.g. "retry" on an exhausted transaction) can never bypass the hard safety rules.

## 4. Failure handling and fallback mechanisms

| Failure mode | Handling |
|---|---|
| No `OPENAI_API_KEY` configured | `_llm_diagnose()` returns `None` immediately; `diagnose()` uses the rule-based engine. No error surfaced to the user. |
| OpenAI API error (network, auth, rate limit) | Caught inside `_llm_diagnose()`'s try/except; falls back to rule-based engine. |
| LLM returns malformed JSON or an out-of-vocabulary category/intervention | Treated the same as an API error — fallback, not a crash. |
| Transaction has `failure_reason = None` | Rule-based engine classifies as `insufficient_data` with low confidence (0.3) and escalates rather than guessing. |
| Transaction has an unrecognized `failure_reason` string | Classified as `technical_error` (safe default) with a reasoning note explaining the fallback. |
| Retries already exhausted (`retry_count >= max_retries`) | Hard stopping rule enforced at **two** layers: inside the rule-based diagnosis (downgrades suggestion) and again inside `intervention.choose_intervention()` (defense in depth) — retry can never be selected once the limit is hit, regardless of what either backend suggested. |
| Customer has not opted into marketing contact (`consent_marketing = False`) | `email_reminder` / `discount_offer` are blocked at execution time and downgraded to `escalate`, regardless of diagnosis output. |
| One transaction in a batch throws an unexpected exception | `batch_recover()` wraps each transaction's `execute_recovery()` call individually; a failure rolls back that transaction's partial writes and records an `error` field in its result, but does **not** abort the rest of the batch. |
| Transaction already settled (`success` / `recovered`) | `execute_recovery()` short-circuits and returns `skipped: true` — no duplicate audit rows, no double-counted recovered money. |

## 5. Data privacy / compliance considerations

- `Transaction.consent_marketing` gates any customer-facing contact action (email, discount offer); the system will escalate to a human rather than contact a customer without recorded consent.
- No real customer data is used — all records are Faker-generated.
- Every money-affecting action is logged with a `reasoning` string and `performed_by` field (`ai_agent` vs `human` vs `system`), so any recovered/escalated amount can be traced back to exactly which decision produced it.
- The system never calls a real payment gateway, SMS/email provider, or bank API — all interventions are simulated and logged, per the "defense-only" constraint.

## Recovery Intelligence Enhancement

The opportunity-ranking layer now enriches each eligible transaction with two additional signals:

1. **Customer Recovery Profile**: historical transaction count, successful/failed/recovered counts, average transaction value, historical success rate, and a simple customer segment.
2. **Recovery Probability + Expected Recovery Value**: a deterministic probability estimate based on failure category, customer history, and retry runway. Expected Recovery Value is calculated as `amount × recovery_probability`.

The expected-recovery signal is used as the primary opportunity ordering key, while the original priority score remains available as a secondary explainability signal. This avoids prioritizing purely by transaction size.

These signals are intentionally transparent and offline-safe; they are a heuristic decision engine rather than a trained probability model.
