"""
main.py
-------
FastAPI application for the AI Revenue Recovery Agent.

Endpoints:
    GET  /transactions              - list transactions (filterable by status, merchant_id)
    GET  /transactions/{txn_id}     - single transaction details
    GET  /recovery-opportunities    - prioritized list from the detection module
    POST /recover/{txn_id}          - run the full pipeline on one transaction
    POST /batch-recover             - run the full pipeline on all eligible opportunities
    GET  /dashboard                 - aggregated metrics
    GET  /audit/{txn_id}            - full audit trail for one transaction
    GET  /                          - serves the web UI
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db, init_db, Transaction
from detection import get_recovery_opportunities
from intervention import execute_recovery, batch_recover
from reporting import get_dashboard_metrics, get_audit_trail
from recovery_intelligence import build_customer_profiles, enrich_opportunity, recovery_probability


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="AI Revenue Recovery Agent",
    description="Detects, diagnoses, and recovers failed/abandoned payments.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_ui():
    return FileResponse("static/index.html")


def _serialize_txn(t: Transaction) -> dict:
    return {
        "transaction_id": t.transaction_id,
        "merchant_id": t.merchant_id,
        "customer_id": t.customer_id,
        "customer_email": t.customer_email,
        "amount": t.amount,
        "currency": t.currency,
        "payment_method": t.payment_method,
        "status": t.status,
        "failure_reason": t.failure_reason,
        "retry_count": t.retry_count,
        "max_retries": t.max_retries,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.get("/transactions")
def list_transactions(
    status: Optional[str] = Query(None, description="Filter by status: success, failed, pending, recovered, escalated"),
    merchant_id: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None, description="Filter to a single customer's transactions"),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    q = db.query(Transaction)
    if status:
        q = q.filter(Transaction.status == status)
    if merchant_id:
        q = q.filter(Transaction.merchant_id == merchant_id)
    if customer_id:
        q = q.filter(Transaction.customer_id == customer_id)
    rows = q.order_by(Transaction.created_at.desc()).limit(limit).all()
    return {"count": len(rows), "transactions": [_serialize_txn(t) for t in rows]}


@app.get("/transactions/{transaction_id}")
def get_transaction(transaction_id: str, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.transaction_id == transaction_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _serialize_txn(txn)


@app.get("/recovery-opportunities")
def recovery_opportunities(db: Session = Depends(get_db)):
    opps = get_recovery_opportunities(db)
    return {"count": len(opps), "opportunities": opps}


@app.get("/customer-profile/{customer_id}")
def customer_profile(customer_id: str, db: Session = Depends(get_db)):
    profiles = build_customer_profiles(db)
    profile = profiles.get(customer_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Customer not found")
    return profile


@app.get("/recovery-probability/{transaction_id}")
def recovery_probability_for_transaction(transaction_id: str, db: Session = Depends(get_db)):
    """
    Recovery probability + expected recoverable value for ONE specific transaction,
    looked up directly by id. Unlike /recovery-opportunities (which only lists
    currently-eligible opportunities), this works for any failed or pending
    transaction so the UI can show "how much of this ₹X payment is recoverable"
    the moment a payment fails, not just when it shows up in the batch queue.
    """
    txn = db.query(Transaction).filter(Transaction.transaction_id == transaction_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if txn.status not in ("failed", "pending"):
        return {
            "transaction_id": txn.transaction_id,
            "status": txn.status,
            "amount": txn.amount,
            "applicable": False,
            "message": "Recovery probability only applies to failed or pending payments.",
        }

    profiles = build_customer_profiles(db)
    profile = profiles.get(txn.customer_id, {
        "customer_id": txn.customer_id,
        "total_transactions": 0,
        "historical_success_rate": 0.0,
    })
    result = recovery_probability(txn, profile)
    result.update({
        "transaction_id": txn.transaction_id,
        "customer_id": txn.customer_id,
        "status": txn.status,
        "amount": txn.amount,
        "applicable": True,
    })
    return result


@app.post("/recover/{transaction_id}")
def recover_one(transaction_id: str, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.transaction_id == transaction_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    result = execute_recovery(db, txn)
    return result


@app.post("/batch-recover")
def batch_recover_endpoint(db: Session = Depends(get_db)):
    opps = get_recovery_opportunities(db)
    txns = [db.get(Transaction, o["db_id"]) for o in opps]
    txns = [t for t in txns if t is not None]
    results = batch_recover(db, txns)

    recovered_amount = sum(r.get("amount_recovered", 0) or 0 for r in results if not r.get("skipped"))
    return {
        "processed": len(results),
        "total_recovered_amount": round(recovered_amount, 2),
        "results": results,
    }


@app.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return get_dashboard_metrics(db)


@app.get("/audit/{transaction_id}")
def audit_trail(transaction_id: str, db: Session = Depends(get_db)):
    trail = get_audit_trail(db, transaction_id)
    if trail is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return trail
