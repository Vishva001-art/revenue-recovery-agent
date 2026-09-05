# AI Revenue Recovery Agent

A Smart Payment Recovery Agent built for the Razorpay AI Buildathon (Track 3: Revenue Recovery).

Detects failed recurring payments and abandoned checkouts, diagnoses the root cause, chooses
an appropriate recovery intervention, executes it (simulated, audit-logged), and tracks the
total money recovered — all with a full, explainable audit trail.

## Features

- **Detection** — flags failed payments within a 7-day window and abandoned checkouts (pending
  >30 minutes), prioritized by amount, retries remaining, and customer repeat-failure context.
- **Diagnosis** — classifies the failure (`insufficient_balance`, `expired_card`, `bank_decline`,
  `network_timeout`, `mandate_expired`, `technical_error`, ...) using an LLM (OpenAI, if configured)
  with an automatic, transparent rule-based fallback.
- **Intervention** — selects `retry` / `email_reminder` / `discount_offer` / `escalate` / `wait`,
  enforcing hard stopping rules (never retry past `max_retries`) and consent checks (never
  email/offer a discount without `consent_marketing`).
- **Tracking** — every action and outcome is logged; a dashboard shows total money recovered,
  breakdowns by failure reason and intervention type, and a full per-transaction audit trail.

## Project structure

```
revenue-recovery-agent/
├── database.py          # SQLAlchemy models: Transaction, RecoveryAction, RecoveryOutcome
├── generate_data.py      # Synthetic transaction generator (200 by default)
├── detection.py          # At-risk / abandoned-checkout detection + prioritization
├── diagnosis.py          # AI (LLM + rule-based fallback) failure classification
├── intervention.py       # Action selection, execution (simulated), audit logging
├── reporting.py          # Dashboard metrics + audit trail aggregation
├── main.py                # FastAPI app (all endpoints + serves the UI)
├── static/index.html     # Web dashboard (vanilla HTML/CSS/JS)
├── setup_env.sh           # venv + dependency setup script
├── requirements.txt
├── tests/
│   ├── conftest.py        # Isolated test DB + fixtures
│   ├── test_unit.py
│   ├── test_integration.py
│   ├── test_edge_cases.py
│   └── test_performance.py
├── data/labels.json       # Ground-truth failure labels (written by generate_data.py)
├── ARCHITECTURE.md
├── VIDEO_SCRIPT.md
└── VERIFICATION_REPORT.md
```

## Setup

```bash
chmod +x setup_env.sh
./setup_env.sh
source venv/bin/activate   # if not already active
```

Or manually:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Optional: enable the LLM diagnosis backend

By default the agent uses a transparent rule-based classifier (no API key needed — works fully
offline). To use OpenAI for diagnosis instead:

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-4o-mini"   # optional, this is the default
```

If the key is missing, invalid, or the API call fails for any reason, the agent automatically
and silently falls back to the rule-based engine — the pipeline never breaks.

## Running

```bash
# 1. Generate synthetic data (200 transactions by default)
python generate_data.py
# or: python generate_data.py --n 500

# 2. Start the API + dashboard
uvicorn main:app --reload

# 3. Open the dashboard
#    http://127.0.0.1:8000/
```

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/transactions` | List transactions, filterable by `status` / `merchant_id` |
| GET | `/transactions/{id}` | Single transaction details |
| GET | `/recovery-opportunities` | Prioritized list of recoverable transactions |
| POST | `/recover/{id}` | Run detection→diagnosis→intervention→tracking on one transaction |
| POST | `/batch-recover` | Run recovery on all eligible opportunities |
| GET | `/dashboard` | Aggregated metrics (totals, money recovered, breakdowns) |
| GET | `/audit/{id}` | Full audit trail for one transaction |

## Testing

```bash
pytest tests/ -v
```

61 tests across unit, integration, edge-case, and performance suites — see
`VERIFICATION_REPORT.md` for the full results and checklist. Tests run against an isolated
SQLite file and never touch your working demo database.

## Design notes / constraints honored

- **Defense-only**: no real payment gateway, SMTP, or SMS calls are made — every "action" is
  simulated and logged.
- **Explainable**: every money-affecting decision has a `reasoning` string and timestamp in the
  audit trail (`GET /audit/{id}`).
- **Stopping rules**: retries are hard-capped at `max_retries`, enforced independently in both
  the diagnosis and intervention layers.
- **Compliance**: customers without `consent_marketing=True` are never emailed or offered a
  discount — they're escalated to a human instead.

See `ARCHITECTURE.md` for the full data-flow diagram and failure-handling design, and
`VIDEO_SCRIPT.md` for the 5-minute demo walkthrough.

## Recovery Intelligence (Buildathon enhancement)

RecoverAI now adds two explainable decision signals on every recovery opportunity:

- **Customer Recovery Profile** — aggregates transaction history, successful/recovered payments, failure count, average transaction value, and historical success rate. Customers are segmented into `high-trust`, `established`, or `new-or-uncertain`.
- **Recovery Probability** — a deterministic, offline-safe estimate based on failure category, customer history, and retry runway. This is explicitly a heuristic rather than a trained probability model.
- **Expected Recovery Value** — `transaction amount × recovery probability`. Opportunities are ordered by expected recoverable revenue, with the original priority score retained as a secondary signal.

The API exposes `GET /customer-profile/{customer_id}`, and `/recovery-opportunities` now returns `customer_profile`, `recovery_probability`, `expected_recovery_value`, `probability_factors`, and `recovery_priority_score`.
