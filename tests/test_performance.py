"""
test_performance.py
--------------------
Lightweight performance checks. These are not strict benchmarks (CI hardware varies),
but they guard against gross regressions -- e.g. an accidental N+1 query turning a
200-row batch job into a multi-minute operation.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_data
import detection
import intervention
from database import Transaction


class TestPerformance:
    def test_processing_200_transactions_completes_quickly(self, db_session):
        t0 = time.perf_counter()
        generate_data.generate(n=200)
        gen_time = time.perf_counter() - t0

        t1 = time.perf_counter()
        opps = detection.get_recovery_opportunities(db_session)
        txns = [db_session.get(Transaction, o["db_id"]) for o in opps]
        results = intervention.batch_recover(db_session, txns)
        recover_time = time.perf_counter() - t1

        print(f"\n  generate_data(200): {gen_time:.3f}s")
        print(f"  detect+recover {len(txns)} opportunities: {recover_time:.3f}s")

        assert gen_time < 10.0, "Data generation for 200 rows should be well under 10s"
        assert recover_time < 10.0, "Recovering all opportunities from a 200-row batch should be well under 10s"
        assert len(results) == len(opps)

    def test_api_response_times_are_reasonable(self, client):
        generate_data.generate(n=200)

        t0 = time.perf_counter()
        r1 = client.get("/dashboard")
        dashboard_time = time.perf_counter() - t0
        assert r1.status_code == 200
        assert dashboard_time < 2.0

        t0 = time.perf_counter()
        r2 = client.get("/transactions?limit=200")
        list_time = time.perf_counter() - t0
        assert r2.status_code == 200
        assert list_time < 2.0

        t0 = time.perf_counter()
        r3 = client.get("/recovery-opportunities")
        opp_time = time.perf_counter() - t0
        assert r3.status_code == 200
        assert opp_time < 2.0

        print(f"\n  /dashboard: {dashboard_time*1000:.1f}ms  "
              f"/transactions: {list_time*1000:.1f}ms  "
              f"/recovery-opportunities: {opp_time*1000:.1f}ms")

    def test_batch_recover_endpoint_completes_within_budget(self, client):
        generate_data.generate(n=200)
        t0 = time.perf_counter()
        r = client.post("/batch-recover")
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200
        print(f"\n  /batch-recover on 200-row dataset: {elapsed:.3f}s "
              f"({r.json()['processed']} transactions processed)")
        assert elapsed < 15.0
