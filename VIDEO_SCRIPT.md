# Video Script Outline — AI Revenue Recovery Agent (5:00)

## 0:00 – 1:00 — Problem statement
- Open on a number: "X% of recurring payments fail on the first attempt — insufficient balance, expired cards, timeouts, expired mandates." (cite whatever figure you're comfortable defending, or reframe as "a meaningful share of MRR churns silently every month.")
- Merchants lose this money not because customers don't want to pay, but because **nobody diagnoses why it failed or does anything targeted about it** — most systems either blindly retry everything, or do nothing until a human notices.
- One-sentence thesis: "We built an agent that detects a failed or abandoned payment, figures out *why* it failed, picks the right recovery action for that specific reason, and proves — with a full audit trail — exactly how much money it got back."

## 1:00 – 2:00 — Architecture overview
- Walk through the pipeline diagram (see `ARCHITECTURE.md`): Detection → Diagnosis → Intervention → Tracking, sitting on top of a SQLite ledger of transactions, actions, and outcomes.
- Call out the AI integration point specifically: one function, `diagnosis.py`, classifies the failure and proposes an intervention; everything downstream is deterministic business logic (stopping rules, consent checks) that can override the AI's suggestion — "the model proposes, the rules dispose."
- Mention graceful degradation: if the LLM call fails or isn't configured, the agent falls back to a transparent rule-based classifier automatically — the pipeline never breaks.

## 2:00 – 3:00 — Demo: detection and diagnosis
- Show `GET /recovery-opportunities` in the UI: a prioritized list of failed/abandoned transactions, ranked by amount and retries remaining.
- Click into one failed transaction, show the audit-trail drawer starting to populate: the "detection" entry explaining *why* it was flagged (e.g. "failed payment within recovery window").
- Trigger `/recover/{id}` on it live, and narrate the "diagnosis" entry that appears: failure category, confidence score, and the one-line reasoning string.

## 3:00 – 4:00 — Demo: intervention and recovery
- Show the "intervention" audit entry: which action was actually chosen, and — for at least one transaction — call out a case where the **final action differs from the raw diagnosis suggestion** because a stopping rule or consent rule overrode it (e.g. retries exhausted → escalated instead of retried).
- Click "Recover all eligible" in the dashboard to run batch recovery across the whole opportunity list live.
- Watch the summary strip update in real time: Total Recovered ticks up, Failed count drops, Recovered count rises.

## 4:00 – 4:30 — Dashboard and metrics
- Tour the breakdown panels: failure reasons (insufficient balance, expired card, bank decline, ...) and intervention types used, side by side.
- Point at the "Total Money Recovered" figure and connect it back to the audit trail: "every rupee in that number traces back to a logged action with a timestamp and a reason — nothing here is a black box."

## 4:30 – 5:00 — Conclusion and impact
- Recap in one breath: detect → diagnose → intervene → prove it, fully auditable, compliance-aware (never contacts a customer without consent, never retries past the limit).
- Impact framing: for a merchant processing recurring payments, recovering even a modest share of failed transactions is direct, incremental revenue with no additional acquisition cost — this agent is built to find that money and show its work.
- Close on the number from the live demo run: "In this batch of 200 transactions, the agent recovered ₹[X] automatically, with zero manual review needed for the majority of cases."
