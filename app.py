"""Dashboard over the audit trail.

Reads audit.db and serves three JSON endpoints plus one static page. The point
of the UI is a single claim: click any payment and see the entire decision
chain, including the rule that fired at each step and why it stopped.

Run: uvicorn app:app --reload   (after: python evaluate.py --split heldout)
"""

import os
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

import agent
import seed as seed_module
from policy import TERMINAL_ARCHETYPES

DEFAULT_RUN = "eval_heldout"

app = FastAPI(title="Failsafe")


def rows(query, params=()):
    if not os.path.exists(agent.DB_PATH):
        raise HTTPException(
            status_code=503,
            detail="No audit.db yet. Run: python evaluate.py --split heldout",
        )
    connection = sqlite3.connect(agent.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params)]
    finally:
        connection.close()


def ground_truth():
    """Archetype labels, used only to mark misdiagnoses in the UI."""
    if not os.path.exists("payments.json"):
        return {}
    return {p["id"]: p["_archetype"] for p in seed_module.load()}


@app.get("/api/summary")
def summary(run_id: str = DEFAULT_RUN):
    """Headline counters for the two strategies, straight from the audit trail."""
    out = {}
    for strategy, rid in (("failsafe", run_id), ("naive", f"{run_id}_naive")):
        stats = rows(
            """SELECT COUNT(DISTINCT payment_id) AS payments,
                      COUNT(DISTINCT CASE WHEN stop_reason='recovered'
                            THEN payment_id END) AS recovered,
                      COUNT(CASE WHEN stage='act'
                            AND action_result != 'no_money_action'
                            THEN 1 END) AS actions
               FROM event WHERE run_id = ? AND strategy = ?""",
            (rid, strategy),
        )
        money = rows(
            """SELECT COALESCE(SUM(amount), 0) AS money FROM (
                   SELECT DISTINCT payment_id, amount FROM event
                   WHERE run_id = ? AND strategy = ? AND stop_reason = 'recovered')""",
            (rid, strategy),
        )
        # Compliance violations: money actions taken against payments whose true
        # archetype must never be actioned. Counted from ground truth, not from
        # what the agent believed at the time.
        terminal = {pid for pid, a in ground_truth().items()
                    if a in TERMINAL_ARCHETYPES}
        acted = rows(
            """SELECT DISTINCT payment_id, attempt FROM event
               WHERE run_id = ? AND strategy = ? AND stage = 'act'
                 AND action_result != 'no_money_action'""",
            (rid, strategy),
        )
        violations = sum(1 for row in acted if row["payment_id"] in terminal)
        out[strategy] = {**stats[0], "money_recovered": money[0]["money"],
                         "compliance_violations": violations}
    if not out["failsafe"]["payments"]:
        raise HTTPException(404, f"No run {run_id!r}. Run evaluate.py first.")
    return out


@app.get("/api/payments")
def payments(run_id: str = DEFAULT_RUN):
    """One row per payment: what it was, what happened, why it stopped."""
    truth = ground_truth()
    listing = rows(
        """SELECT payment_id, amount, split,
                  MAX(archetype) AS archetype,
                  MAX(CASE WHEN stage='diagnose' THEN confidence END) AS confidence,
                  MAX(CASE WHEN stage='diagnose' THEN diagnosis_method END) AS method,
                  MAX(CASE WHEN stage='diagnose' THEN llm_reason END) AS reason,
                  MAX(CASE WHEN stop_reason IS NOT NULL THEN stop_reason END) AS stop_reason,
                  COUNT(CASE WHEN stage='act' THEN 1 END) AS actions
           FROM event WHERE run_id = ? AND strategy = 'failsafe'
           GROUP BY payment_id, amount, split ORDER BY payment_id""",
        (run_id,),
    )
    for row in listing:
        row["truth"] = truth.get(row["payment_id"])
        row["misdiagnosed"] = bool(row["truth"] and row["truth"] != row["archetype"])
        row["recovered"] = row["stop_reason"] == "recovered"
    return listing


@app.get("/api/payment/{payment_id}")
def payment_chain(payment_id: str, run_id: str = DEFAULT_RUN):
    """The whole decision chain for one payment. This is the explainability claim."""
    chain = rows(
        """SELECT ts, stage, attempt, archetype, confidence, diagnosis_method,
                  llm_reason, rule_fired, action, action_result, provider_ref,
                  stop_reason
           FROM event WHERE run_id = ? AND strategy = 'failsafe' AND payment_id = ?
           ORDER BY id""",
        (run_id, payment_id),
    )
    if not chain:
        raise HTTPException(404, f"No audit trail for {payment_id}")
    return {"payment_id": payment_id, "truth": ground_truth().get(payment_id),
            "chain": chain}


@app.get("/api/report")
def report():
    """The generated markdown report, so the UI can show the honest numbers."""
    if not os.path.exists("report.md"):
        raise HTTPException(404, "No report.md. Run evaluate.py first.")
    with open("report.md", encoding="utf-8") as handle:
        return JSONResponse({"markdown": handle.read()})


@app.get("/")
def index():
    return FileResponse("static/index.html")
