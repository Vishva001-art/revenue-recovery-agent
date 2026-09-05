"""
generate_data.py
-----------------
Generates 200 synthetic payment transactions with realistic failure patterns
and loads them into the SQLite database via the Transaction model.

Realism choices (so the diagnosis module has something meaningful to classify):
    - ~65% success, ~25% failed, ~10% pending/abandoned  (roughly mirrors real recurring-payment mixes)
    - Failure reasons are NOT uniformly random: certain payment methods bias toward certain
      failure reasons (e.g. UPI/netbanking mandates -> mandate_expired, cards -> expired_card,
      generic bank issues -> insufficient_balance / bank_decline), so the classifier has real signal.
    - A `known_failure_label` column-equivalent is kept in a side JSON export (data/labels.json)
      so the diagnosis module can be scored against ground truth in tests (Phase 10 requirement:
      "correctly categorizes at least 80% of failures").

Usage:
    python generate_data.py            # generates 200 transactions (default)
    python generate_data.py --n 500    # custom count
"""

import argparse
import json
import random
from datetime import datetime, timedelta, timezone

from faker import Faker

from database import Transaction, reset_db, SessionLocal

fake = Faker()
Faker.seed(42)
random.seed(42)

PAYMENT_METHODS = ["card", "upi", "netbanking"]
FAILURE_REASONS = [
    "insufficient_balance",
    "expired_card",
    "bank_decline",
    "network_timeout",
    "mandate_expired",
    "technical_error",
]

# Bias table: payment_method -> weighted failure reasons (method, list of (reason, weight))
FAILURE_BIAS = {
    "card": [
        ("expired_card", 0.35),
        ("insufficient_balance", 0.20),
        ("bank_decline", 0.20),
        ("technical_error", 0.15),
        ("network_timeout", 0.10),
    ],
    "upi": [
        ("insufficient_balance", 0.30),
        ("mandate_expired", 0.25),
        ("network_timeout", 0.20),
        ("technical_error", 0.15),
        ("bank_decline", 0.10),
    ],
    "netbanking": [
        ("bank_decline", 0.30),
        ("network_timeout", 0.25),
        ("insufficient_balance", 0.20),
        ("technical_error", 0.15),
        ("mandate_expired", 0.10),
    ],
}

MERCHANT_IDS = [f"merch_{i:03d}" for i in range(1, 16)]  # 15 synthetic merchants


def weighted_choice(pairs):
    reasons, weights = zip(*pairs)
    return random.choices(reasons, weights=weights, k=1)[0]


def make_transaction(idx: int, now: datetime):
    method = random.choice(PAYMENT_METHODS)
    status_roll = random.random()

    created_at = now - timedelta(days=random.randint(0, 14), hours=random.randint(0, 23))
    amount = round(random.choice([
        random.uniform(99, 999),        # small subscriptions
        random.uniform(1000, 4999),     # mid-tier plans
        random.uniform(5000, 25000),    # enterprise / big-ticket
    ]), 2)

    customer_id = f"cust_{random.randint(1, 90):04d}"  # some repeat customers, for grouping
    name = fake.name()
    email = fake.email()
    phone = fake.phone_number()

    txn = {
        "transaction_id": f"txn_{idx:05d}",
        "merchant_id": random.choice(MERCHANT_IDS),
        "customer_id": customer_id,
        "customer_email": email,
        "customer_phone": phone,
        "amount": amount,
        "currency": "INR",
        "payment_method": method,
        "created_at": created_at,
        "consent_marketing": random.random() > 0.15,  # 85% opted in to marketing/email contact
    }

    if status_roll < 0.65:
        # Successful payment
        txn.update(status="success", failure_reason=None, retry_count=0,
                    last_retry_at=None, checkout_initiated_at=None)
    elif status_roll < 0.90:
        # Failed payment -> pick a biased failure reason for this method
        reason = weighted_choice(FAILURE_BIAS[method])
        retry_count = random.choice([0, 0, 1, 1, 2, 3])  # most haven't been retried much yet
        last_retry = created_at + timedelta(hours=random.randint(1, 48)) if retry_count > 0 else None
        txn.update(
            status="failed",
            failure_reason=reason,
            retry_count=retry_count,
            last_retry_at=last_retry,
            checkout_initiated_at=None,
        )
    else:
        # Pending / abandoned checkout: initiated but never completed
        checkout_time = created_at + timedelta(minutes=random.randint(1, 120))
        txn.update(
            status="pending",
            failure_reason=None,
            retry_count=0,
            last_retry_at=None,
            checkout_initiated_at=checkout_time,
        )

    return txn


def generate(n: int = 200):
    reset_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    labels = {}  # transaction_id -> ground-truth failure_reason, for test scoring
    records = []

    try:
        for i in range(1, n + 1):
            data = make_transaction(i, now)
            row = Transaction(**data)
            db.add(row)
            records.append(data)
            if data["status"] == "failed":
                labels[data["transaction_id"]] = data["failure_reason"]

        db.commit()
    finally:
        db.close()

    # Export ground-truth labels for evaluating the diagnosis module (Phase 10)
    with open("data/labels.json", "w") as f:
        json.dump(labels, f, indent=2)

    success = sum(1 for r in records if r["status"] == "success")
    failed = sum(1 for r in records if r["status"] == "failed")
    pending = sum(1 for r in records if r["status"] == "pending")

    print(f"Generated {n} transactions:")
    print(f"  success : {success}")
    print(f"  failed  : {failed}")
    print(f"  pending : {pending}")
    print(f"Ground-truth labels for {len(labels)} failed transactions written to data/labels.json")

    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200, help="number of transactions to generate")
    args = parser.parse_args()
    generate(args.n)
