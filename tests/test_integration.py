"""
test_integration.py
--------------------
End-to-end tests: detection -> diagnosis -> intervention -> tracking, plus the FastAPI
endpoints wired to the isolated test database.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Transaction, RecoveryAction, RecoveryOutcome
import detection
import intervention
import generate_data
from reporting import get_dashboard_metrics, get_audit_trail


class TestEndToEndPipeline:
    def test_full_pipeline_on_single_transaction(self, db_session, seeded_transactions):
        txn = next(t for t in seeded_transactions if t.transaction_id == "t_failed_fresh")
        result = intervention.execute_recovery(db_session, txn)

        assert result["skipped"] is False
        assert result["failure_category"] is not None
        assert result["intervention"] in ("retry", "email_reminder", "discount_offer", "escalate", "wait")
        assert result["outcome"] in ("recovered", "still_failed", "escalated", "pending")

        # Audit trail must contain all three steps
        actions = db_session.query(RecoveryAction).filter_by(transaction_id=txn.id).all()
        steps = {a.step for a in actions}
        assert steps == {"detection", "diagnosis", "intervention"}

        # Exactly one outcome recorded
        outcomes = db_session.query(RecoveryOutcome).filter_by(transaction_id=txn.id).all()
        assert len(outcomes) == 1

    def test_already_settled_transaction_is_skipped(self, db_session, seeded_transactions):
        txn = next(t for t in seeded_transactions if t.transaction_id == "t_success")
        result = intervention.execute_recovery(db_session, txn)
        assert result["skipped"] is True

        actions = db_session.query(RecoveryAction).filter_by(transaction_id=txn.id).all()
        assert actions == []  # no wasted audit entries on already-settled transactions

    def test_batch_recover_processes_all_opportunities(self, db_session, seeded_transactions):
        opps = detection.get_recovery_opportunities(db_session)
        txns = [db_session.get(Transaction, o["db_id"]) for o in opps]
        results = intervention.batch_recover(db_session, txns)

        assert len(results) == len(opps)
        assert all("transaction_id" in r for r in results)

    def test_audit_trail_reflects_full_history_after_recovery(self, db_session, seeded_transactions):
        txn = next(t for t in seeded_transactions if t.transaction_id == "t_failed_fresh")
        intervention.execute_recovery(db_session, txn)

        trail = get_audit_trail(db_session, "t_failed_fresh")
        assert trail is not None
        assert len(trail["audit_trail"]) == 3
        assert len(trail["outcomes"]) == 1
        assert trail["transaction"]["transaction_id"] == "t_failed_fresh"

    def test_dashboard_metrics_reflect_recovery_results(self, db_session, seeded_transactions):
        opps = detection.get_recovery_opportunities(db_session)
        txns = [db_session.get(Transaction, o["db_id"]) for o in opps]
        intervention.batch_recover(db_session, txns)

        metrics = get_dashboard_metrics(db_session)
        assert metrics["total_transactions"] == len(seeded_transactions)
        # total recovered amount must equal sum of RecoveryOutcome.amount_recovered for 'recovered' rows
        expected = sum(
            o.amount_recovered for o in db_session.query(RecoveryOutcome).filter_by(outcome="recovered").all()
        )
        assert abs(metrics["total_money_recovered"] - round(expected, 2)) < 0.01


class TestAPIEndpoints:
    def test_dashboard_endpoint(self, client):
        generate_data.generate(n=60)
        r = client.get("/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["total_transactions"] == 60
        assert "total_money_recovered" in body

    def test_transactions_list_endpoint(self, client):
        generate_data.generate(n=60)
        r = client.get("/transactions")
        assert r.status_code == 200
        assert r.json()["count"] > 0

    def test_transactions_filter_by_status(self, client):
        generate_data.generate(n=60)
        r = client.get("/transactions?status=failed")
        assert r.status_code == 200
        for t in r.json()["transactions"]:
            assert t["status"] == "failed"

    def test_get_single_transaction_404_for_missing(self, client):
        generate_data.generate(n=10)
        r = client.get("/transactions/does_not_exist")
        assert r.status_code == 404

    def test_get_single_transaction_success(self, client):
        generate_data.generate(n=10)
        listing = client.get("/transactions").json()["transactions"]
        txn_id = listing[0]["transaction_id"]
        r = client.get(f"/transactions/{txn_id}")
        assert r.status_code == 200
        assert r.json()["transaction_id"] == txn_id

    def test_recovery_opportunities_endpoint_shape(self, client):
        generate_data.generate(n=60)
        r = client.get("/recovery-opportunities")
        assert r.status_code == 200
        body = r.json()
        assert "opportunities" in body
        if body["opportunities"]:
            opp = body["opportunities"][0]
            for key in ("transaction_id", "priority_score", "opportunity_type"):
                assert key in opp

    def test_recover_single_endpoint(self, client):
        generate_data.generate(n=60)
        opps = client.get("/recovery-opportunities").json()["opportunities"]
        assert len(opps) > 0
        txn_id = opps[0]["transaction_id"]
        r = client.post(f"/recover/{txn_id}")
        assert r.status_code == 200
        assert r.json()["transaction_id"] == txn_id

    def test_recover_single_endpoint_404_for_missing(self, client):
        r = client.post("/recover/does_not_exist")
        assert r.status_code == 404

    def test_batch_recover_endpoint(self, client):
        generate_data.generate(n=60)
        opportunities_before = len(client.get("/recovery-opportunities").json()["opportunities"])
        r = client.post("/batch-recover")
        assert r.status_code == 200
        body = r.json()
        assert "processed" in body
        assert "total_recovered_amount" in body
        # batch-recover must act on exactly the opportunities that existed beforehand
        assert body["processed"] == opportunities_before

    def test_audit_endpoint(self, client):
        generate_data.generate(n=60)
        opps = client.get("/recovery-opportunities").json()["opportunities"]
        txn_id = opps[0]["transaction_id"]
        client.post(f"/recover/{txn_id}")
        r = client.get(f"/audit/{txn_id}")
        assert r.status_code == 200
        assert len(r.json()["audit_trail"]) == 3

    def test_audit_endpoint_404_for_missing(self, client):
        r = client.get("/audit/does_not_exist")
        assert r.status_code == 404

    def test_ui_root_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
