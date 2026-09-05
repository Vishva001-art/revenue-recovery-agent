"""
test_edge_cases.py
-------------------
Edge cases the verification checklist calls out explicitly.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Transaction, RecoveryOutcome
import detection
import diagnosis
import intervention
from reporting import get_dashboard_metrics


class TestEmptyAndTrivialInputs:
    def test_empty_transaction_list_detection(self, db_session):
        assert detection.get_recovery_opportunities(db_session) == []

    def test_empty_db_dashboard_does_not_crash(self, db_session):
        metrics = get_dashboard_metrics(db_session)
        assert metrics["total_transactions"] == 0
        assert metrics["total_money_recovered"] == 0.0

    def test_all_successful_transactions_no_recovery_needed(self, db_session):
        now = datetime.now(timezone.utc)
        for i in range(5):
            db_session.add(Transaction(
                transaction_id=f"all_success_{i}", merchant_id="m", customer_id="c",
                amount=100.0, payment_method="card", status="success", created_at=now,
            ))
        db_session.commit()

        opps = detection.get_recovery_opportunities(db_session)
        assert opps == []

        results = intervention.batch_recover(db_session, db_session.query(Transaction).all())
        assert all(r["skipped"] for r in results)


class TestMalformedData:
    def test_failed_transaction_with_missing_failure_reason(self, db_session):
        txn = Transaction(
            transaction_id="malformed_1", merchant_id="m", customer_id="c",
            amount=100.0, payment_method="card", status="failed", failure_reason=None,
        )
        db_session.add(txn)
        db_session.commit()

        d = diagnosis.diagnose(txn)
        assert d.failure_category == "insufficient_data"

        result = intervention.execute_recovery(db_session, txn)
        assert result["skipped"] is False  # pipeline still completes, doesn't crash

    def test_null_amount_rejected_or_handled(self, db_session):
        # amount is a NOT NULL column -- verify the DB layer actually enforces it
        # rather than silently accepting invalid financial data.
        txn = Transaction(
            transaction_id="malformed_2", merchant_id="m", customer_id="c",
            amount=None, payment_method="card", status="failed",
        )
        db_session.add(txn)
        with pytest.raises(Exception):
            db_session.commit()
        db_session.rollback()

    def test_missing_customer_contact_info_does_not_crash_pipeline(self, db_session):
        txn = Transaction(
            transaction_id="malformed_3", merchant_id="m", customer_id="c",
            amount=100.0, payment_method="card", status="failed", failure_reason="bank_decline",
            customer_email=None, customer_phone=None,
        )
        db_session.add(txn)
        db_session.commit()

        result = intervention.execute_recovery(db_session, txn)
        assert result["skipped"] is False


class TestDuplicatesAndConcurrency:
    def test_duplicate_transaction_ids_rejected(self, db_session):
        t1 = Transaction(transaction_id="dup_x", merchant_id="m", customer_id="c",
                          amount=100.0, payment_method="card", status="failed")
        db_session.add(t1)
        db_session.commit()

        t2 = Transaction(transaction_id="dup_x", merchant_id="m", customer_id="c",
                          amount=200.0, payment_method="card", status="failed")
        db_session.add(t2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_concurrent_recovery_attempts_on_same_transaction_are_safe(self, db_session):
        """
        Simulates two 'concurrent' recovery calls on the same transaction (sequential in this
        single-threaded test, but exercising the idempotency guard). The second call must not
        double-count recovered money or duplicate the transaction's audit trail unsafely.
        """
        txn = Transaction(
            transaction_id="concurrent_1", merchant_id="m", customer_id="c",
            amount=500.0, payment_method="card", status="failed", failure_reason="network_timeout",
        )
        db_session.add(txn)
        db_session.commit()
        db_session.refresh(txn)

        result1 = intervention.execute_recovery(db_session, txn)
        # Re-fetch to see the updated status, as a second caller would
        db_session.refresh(txn)
        result2 = intervention.execute_recovery(db_session, txn)

        if result1["outcome"] == "recovered":
            # Once recovered, a second attempt must be skipped (no double recovery)
            assert result2["skipped"] is True
        # Total recovered amount must never exceed the original transaction amount
        total_recovered = sum(
            o.amount_recovered for o in db_session.query(RecoveryOutcome).filter_by(transaction_id=txn.id)
        )
        assert total_recovered <= txn.amount


class TestExtremeValues:
    def test_very_large_amount(self, db_session):
        txn = Transaction(
            transaction_id="large_amount", merchant_id="m", customer_id="c",
            amount=999999999.99, payment_method="card", status="failed", failure_reason="bank_decline",
        )
        db_session.add(txn)
        db_session.commit()

        result = intervention.execute_recovery(db_session, txn)
        assert result["skipped"] is False
        assert result["amount_recovered"] <= 999999999.99

    def test_zero_amount_transaction(self, db_session):
        txn = Transaction(
            transaction_id="zero_amount", merchant_id="m", customer_id="c",
            amount=0.0, payment_method="upi", status="failed", failure_reason="technical_error",
        )
        db_session.add(txn)
        db_session.commit()
        result = intervention.execute_recovery(db_session, txn)
        assert result["skipped"] is False

    def test_special_characters_in_customer_email(self, db_session):
        txn = Transaction(
            transaction_id="special_chars", merchant_id="m", customer_id="c",
            amount=100.0, payment_method="card", status="failed", failure_reason="bank_decline",
            customer_email="O'Brien+test<script>@example.com", customer_phone="+91-98765-43210",
        )
        db_session.add(txn)
        db_session.commit()  # must not raise (no SQL injection issue with ORM parameter binding)

        fetched = db_session.query(Transaction).filter_by(transaction_id="special_chars").first()
        assert fetched.customer_email == "O'Brien+test<script>@example.com"

        result = intervention.execute_recovery(db_session, txn)
        assert result["skipped"] is False

    def test_retry_count_never_exceeds_max_after_repeated_recovery_attempts(self, db_session):
        """Stopping-rule stress test: hammer retries on one transaction, confirm it stops."""
        txn = Transaction(
            transaction_id="retry_stress", merchant_id="m", customer_id="c",
            amount=100.0, payment_method="card", status="failed", failure_reason="network_timeout",
            retry_count=0, max_retries=2,
        )
        db_session.add(txn)
        db_session.commit()
        db_session.refresh(txn)

        for _ in range(10):  # try to force it past the limit
            if txn.status in ("success", "recovered", "escalated"):
                break
            intervention.execute_recovery(db_session, txn)
            db_session.refresh(txn)

        assert txn.retry_count <= txn.max_retries
