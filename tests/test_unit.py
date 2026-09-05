"""
test_unit.py
------------
Unit tests for individual modules in isolation.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Transaction, RecoveryAction, RecoveryOutcome
import generate_data
import detection
import diagnosis
import intervention


# ---------- Data generation ----------

class TestDataGeneration:
    def test_generates_requested_count(self, db_session, monkeypatch):
        # generate_data.generate() uses its own SessionLocal + reset_db, which now points
        # at the test DB because conftest set RECOVERY_AGENT_DB_URL before import.
        generate_data.generate(n=50)
        count = db_session.query(Transaction).count()
        assert count == 50

    def test_schema_fields_present_and_typed(self, db_session):
        generate_data.generate(n=20)
        txn = db_session.query(Transaction).first()
        assert isinstance(txn.transaction_id, str) and txn.transaction_id.startswith("txn_")
        assert isinstance(txn.merchant_id, str)
        assert isinstance(txn.customer_id, str)
        assert isinstance(txn.amount, float) and txn.amount > 0
        assert txn.payment_method in ("card", "upi", "netbanking")
        assert txn.status in ("success", "failed", "pending")
        assert txn.customer_email and "@" in txn.customer_email
        assert txn.customer_phone

    def test_failed_transactions_have_valid_failure_reason(self, db_session):
        generate_data.generate(n=100)
        failed = db_session.query(Transaction).filter(Transaction.status == "failed").all()
        assert len(failed) > 0
        for t in failed:
            assert t.failure_reason in generate_data.FAILURE_REASONS

    def test_success_transactions_have_no_failure_reason(self, db_session):
        generate_data.generate(n=100)
        success = db_session.query(Transaction).filter(Transaction.status == "success").all()
        for t in success:
            assert t.failure_reason is None

    def test_labels_file_written_and_matches_db(self, db_session, tmp_path):
        generate_data.generate(n=80)
        assert os.path.exists("data/labels.json")
        with open("data/labels.json") as f:
            labels = json.load(f)
        failed_ids = {t.transaction_id for t in db_session.query(Transaction).filter(Transaction.status == "failed")}
        assert set(labels.keys()) == failed_ids


# ---------- Detection ----------

class TestDetection:
    def test_identifies_at_risk_failed_within_window(self, db_session, seeded_transactions):
        at_risk = detection.find_at_risk_transactions(db_session, window_days=7)
        ids = {t.transaction_id for t in at_risk}
        assert "t_failed_fresh" in ids
        assert "t_failed_old" not in ids  # outside the 7-day window

    def test_excludes_transactions_with_exhausted_retries(self, db_session, seeded_transactions):
        at_risk = detection.find_at_risk_transactions(db_session, window_days=7)
        ids = {t.transaction_id for t in at_risk}
        assert "t_failed_maxed" not in ids

    def test_identifies_abandoned_checkouts_past_window(self, db_session, seeded_transactions):
        abandoned = detection.find_abandoned_checkouts(db_session, minutes=30)
        ids = {t.transaction_id for t in abandoned}
        assert "t_pending_abandoned" in ids
        assert "t_pending_fresh" not in ids  # still within the grace window

    def test_grouping_by_merchant_and_customer(self, db_session, seeded_transactions):
        opps = detection.find_at_risk_transactions(db_session) + detection.find_abandoned_checkouts(db_session)
        grouped = detection.group_by_merchant_and_customer(opps)
        assert "merch_test" in grouped
        assert "cust_test" in grouped["merch_test"]

    def test_prioritized_list_sorted_descending(self, db_session, seeded_transactions):
        opps = detection.get_recovery_opportunities(db_session)
        scores = [o["priority_score"] for o in opps]
        assert scores == sorted(scores, reverse=True)

    def test_empty_db_returns_empty_list(self, db_session):
        opps = detection.get_recovery_opportunities(db_session)
        assert opps == []


# ---------- Diagnosis ----------

class TestDiagnosis:
    @pytest.mark.parametrize("reason", diagnosis.FAILURE_CATEGORIES[:6])  # the 6 real processor reasons
    def test_returns_valid_category_for_each_known_reason(self, reason):
        class Fake:
            status = "failed"
            failure_reason = reason
            payment_method = "card"
            amount = 500
            retry_count = 0
            max_retries = 3

        d = diagnosis.diagnose(Fake())
        assert d.failure_category in diagnosis.FAILURE_CATEGORIES
        assert d.suggested_intervention in diagnosis.INTERVENTIONS
        assert 0.0 <= d.confidence_score <= 1.0

    def test_handles_missing_failure_reason(self):
        class Fake:
            status = "failed"
            failure_reason = None
            payment_method = "upi"
            amount = 500
            retry_count = 0
            max_retries = 3

        d = diagnosis.diagnose(Fake())
        assert d.failure_category == "insufficient_data"
        assert d.confidence_score < 0.5  # low confidence when data is missing

    def test_handles_unrecognized_failure_reason(self):
        class Fake:
            status = "failed"
            failure_reason = "some_new_gateway_code_we_have_never_seen"
            payment_method = "card"
            amount = 500
            retry_count = 0
            max_retries = 3

        d = diagnosis.diagnose(Fake())
        assert d.failure_category == "technical_error"  # safe default, doesn't crash

    def test_pending_status_classified_as_abandoned_checkout(self):
        class Fake:
            status = "pending"
            failure_reason = None
            payment_method = "upi"
            amount = 500
            retry_count = 0
            max_retries = 3

        d = diagnosis.diagnose(Fake())
        assert d.failure_category == "abandoned_checkout"

    def test_never_raises_even_with_odd_input(self):
        class Fake:
            status = "failed"
            failure_reason = ""
            payment_method = "card"
            amount = 0
            retry_count = 0
            max_retries = 3

        d = diagnosis.diagnose(Fake())  # should not raise
        assert d is not None

    def test_diagnosis_accuracy_against_ground_truth_labels(self, db_session):
        """
        Verification checklist requirement: diagnosis correctly categorizes at least
        80% of failures, scored against generate_data's known ground-truth labels.
        """
        generate_data.generate(n=200)
        with open("data/labels.json") as f:
            labels = json.load(f)

        failed_txns = db_session.query(Transaction).filter(Transaction.status == "failed").all()
        assert len(failed_txns) == len(labels)

        correct = 0
        for txn in failed_txns:
            d = diagnosis.diagnose(txn)
            expected = labels[txn.transaction_id]
            if d.failure_category == expected:
                correct += 1

        accuracy = correct / len(failed_txns)
        assert accuracy >= 0.80, f"Diagnosis accuracy {accuracy:.1%} below 80% threshold"


# ---------- Intervention selection ----------

class TestInterventionSelection:
    def test_retry_downgraded_to_escalate_when_retries_exhausted(self, db_session, seeded_transactions):
        txn = next(t for t in seeded_transactions if t.transaction_id == "t_failed_maxed")
        d = diagnosis.diagnose(txn)  # network_timeout -> would normally suggest retry
        final = intervention.choose_intervention(db_session, txn, d)
        assert final != "retry"

    def test_marketing_actions_blocked_without_consent(self, db_session, seeded_transactions):
        txn = next(t for t in seeded_transactions if t.transaction_id == "t_no_consent")
        d = diagnosis.diagnose(txn)  # mandate_expired -> escalate anyway, but let's force a marketing case
        d.suggested_intervention = "email_reminder"
        final = intervention.choose_intervention(db_session, txn, d)
        assert final != "email_reminder"
        assert final != "discount_offer"

    def test_normal_case_keeps_diagnosis_suggestion(self, db_session, seeded_transactions):
        txn = next(t for t in seeded_transactions if t.transaction_id == "t_failed_fresh")
        d = diagnosis.diagnose(txn)
        final = intervention.choose_intervention(db_session, txn, d)
        assert final in diagnosis.INTERVENTIONS


# ---------- Database CRUD ----------

class TestDatabaseCRUD:
    def test_create_and_read_transaction(self, db_session):
        txn = Transaction(
            transaction_id="crud_1", merchant_id="m1", customer_id="c1",
            amount=100.0, payment_method="card", status="failed", failure_reason="bank_decline",
        )
        db_session.add(txn)
        db_session.commit()

        fetched = db_session.query(Transaction).filter_by(transaction_id="crud_1").first()
        assert fetched is not None
        assert fetched.amount == 100.0

    def test_update_transaction_status(self, db_session):
        txn = Transaction(
            transaction_id="crud_2", merchant_id="m1", customer_id="c1",
            amount=100.0, payment_method="card", status="failed",
        )
        db_session.add(txn)
        db_session.commit()

        txn.status = "recovered"
        db_session.commit()

        fetched = db_session.query(Transaction).filter_by(transaction_id="crud_2").first()
        assert fetched.status == "recovered"

    def test_delete_transaction_cascades_actions(self, db_session):
        txn = Transaction(
            transaction_id="crud_3", merchant_id="m1", customer_id="c1",
            amount=100.0, payment_method="card", status="failed",
        )
        db_session.add(txn)
        db_session.commit()
        db_session.refresh(txn)

        action = RecoveryAction(transaction_id=txn.id, step="detection")
        db_session.add(action)
        db_session.commit()

        db_session.delete(txn)
        db_session.commit()

        remaining_actions = db_session.query(RecoveryAction).filter_by(transaction_id=txn.id).all()
        assert remaining_actions == []

    def test_unique_transaction_id_constraint(self, db_session):
        t1 = Transaction(transaction_id="dup_1", merchant_id="m1", customer_id="c1",
                          amount=100.0, payment_method="card", status="failed")
        db_session.add(t1)
        db_session.commit()

        t2 = Transaction(transaction_id="dup_1", merchant_id="m1", customer_id="c1",
                          amount=200.0, payment_method="card", status="failed")
        db_session.add(t2)
        with pytest.raises(Exception):
            db_session.commit()
        db_session.rollback()
