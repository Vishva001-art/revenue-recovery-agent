from database import Transaction
from recovery_intelligence import build_customer_profiles, recovery_probability, enrich_opportunity


def test_customer_profile_aggregates_history(db_session):
    db_session.add_all([
        Transaction(transaction_id="p1", merchant_id="m1", customer_id="c1", amount=1000, currency="INR", payment_method="card", status="success"),
        Transaction(transaction_id="p2", merchant_id="m1", customer_id="c1", amount=2000, currency="INR", payment_method="card", status="failed", failure_reason="network_timeout"),
        Transaction(transaction_id="p3", merchant_id="m1", customer_id="c1", amount=3000, currency="INR", payment_method="card", status="recovered", failure_reason="network_timeout"),
    ])
    db_session.commit()
    p = build_customer_profiles(db_session)["c1"]
    assert p["total_transactions"] == 3
    assert p["successful_transactions"] == 1
    assert p["failed_transactions"] == 1
    assert p["recovered_transactions"] == 1
    assert p["historical_success_rate"] == 0.667


def test_expected_recovery_value_is_amount_times_probability(db_session):
    txn = Transaction(transaction_id="p10", merchant_id="m1", customer_id="c10", amount=5000, currency="INR", payment_method="card", status="failed", failure_reason="network_timeout", retry_count=0, max_retries=3)
    db_session.add(txn)
    db_session.commit()
    result = recovery_probability(txn, {"total_transactions": 1, "historical_success_rate": 0.0})
    assert 0.05 <= result["recovery_probability"] <= 0.95
    assert result["expected_recovery_value"] == round(5000 * result["recovery_probability"], 2)


def test_opportunity_contains_intelligence_signals(db_session):
    txn = Transaction(transaction_id="p20", merchant_id="m1", customer_id="c20", amount=2500, currency="INR", payment_method="upi", status="failed", failure_reason="bank_decline", retry_count=0, max_retries=3)
    db_session.add(txn)
    db_session.commit()
    opportunity = {
        "transaction_id": txn.transaction_id,
        "db_id": txn.id,
        "customer_id": txn.customer_id,
        "amount": txn.amount,
        "priority_score": 2500,
    }
    enriched = enrich_opportunity(db_session, opportunity)
    assert "customer_profile" in enriched
    assert "recovery_probability" in enriched
    assert "expected_recovery_value" in enriched
    assert "recovery_priority_score" in enriched
