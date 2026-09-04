"""Dashboard over the audit trail.

Reads audit.db and payments.json, serves JSON, and lets a reader click any
payment to see the entire decision chain: what was diagnosed, which rule fired,
what was done, and why it stopped.

Run: uvicorn app:app --reload   (after: python evaluate.py --split heldout)
"""

import os
import sqlite3
from collections import Counter, defaultdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import agent
import diagnose as diagnosis
import seed as seed_module
from evaluate import STOP_REASON_LABELS, is_unrecoverable
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


def payments_in_run(run_id):
    """The payment records this run covered, with ground truth attached."""
    ids = {r["payment_id"] for r in rows(
        "SELECT DISTINCT payment_id FROM event WHERE run_id = ?", (run_id,))}
    if not ids:
        raise HTTPException(404, f"No run {run_id!r}. Run evaluate.py first.")
    return [p for p in seed_module.load() if p["id"] in ids]


def outcomes_for(run_id, strategy):
    """Per-payment result, read back out of the audit trail rather than re-run."""
    result = defaultdict(lambda: {"recovered": False, "actions": 0,
                                  "stop_reason": None, "archetype": None})
    for row in rows(
        """SELECT payment_id, stage, action_result, stop_reason, archetype
           FROM event WHERE run_id = ? AND strategy = ? ORDER BY id""",
        (run_id, strategy),
    ):
        entry = result[row["payment_id"]]
        if row["stage"] == "diagnose":
            entry["archetype"] = row["archetype"]
        if row["stage"] == "act" and row["action_result"] != "no_money_action":
            entry["actions"] += 1
        if row["stop_reason"]:
            entry["stop_reason"] = row["stop_reason"]
            entry["recovered"] = row["stop_reason"] == "recovered"
    return result


@app.get("/api/overview")
def overview(run_id: str = DEFAULT_RUN):
    """Everything the dashboard needs, in one call."""
    payments = payments_in_run(run_id)
    by_id = {p["id"]: p for p in payments}
    terminal = {p["id"] for p in payments if p["_archetype"] in TERMINAL_ARCHETYPES}

    strategies, results = {}, {}
    for name, rid in (("failsafe", run_id), ("naive", f"{run_id}_naive")):
        outcome = outcomes_for(rid, name)
        results[name] = outcome
        recovered = [pid for pid, o in outcome.items() if o["recovered"]]
        strategies[name] = {
            "payments": len(payments),
            "recovered": len(recovered),
            "money_recovered": sum(by_id[pid]["amount"] for pid in recovered),
            "actions": sum(o["actions"] for o in outcome.values()),
            "wasted_actions": sum(o["actions"] for pid, o in outcome.items()
                                  if is_unrecoverable(by_id[pid])),
            "compliance_violations": sum(o["actions"] for pid, o in outcome.items()
                                         if pid in terminal),
        }

    # Recovery per true cause. One measure: payments recovered out of n.
    archetypes = {}
    for payment in payments:
        row = archetypes.setdefault(payment["_archetype"], {
            "archetype": payment["_archetype"], "total": 0, "failsafe": 0,
            "naive": 0, "money_at_risk": 0,
            "terminal": payment["_archetype"] in TERMINAL_ARCHETYPES,
        })
        row["total"] += 1
        row["money_at_risk"] += payment["amount"]
        row["failsafe"] += results["failsafe"][payment["id"]]["recovered"]
        row["naive"] += results["naive"][payment["id"]]["recovered"]

    # Why the unrecovered ones stopped, and how much sat behind each reason.
    counts, money = Counter(), Counter()
    for payment in payments:
        outcome = results["failsafe"][payment["id"]]
        if not outcome["recovered"]:
            reason = outcome["stop_reason"] or "horizon_expired"
            counts[reason] += 1
            money[reason] += payment["amount"]

    # Does the model beat a keyword lookup? Scored on the same payments.
    cache = diagnosis.load_cache()
    model_all = rules_all = model_generic = rules_generic = generic = 0
    for payment in payments:
        truth = payment["_archetype"]
        predicted = results["failsafe"][payment["id"]]["archetype"]
        rule_guess = diagnosis.rules_diagnose(payment)["archetype"]
        model_all += predicted == truth
        rules_all += rule_guess == truth
        if payment["_error_is_generic"]:
            generic += 1
            model_generic += predicted == truth
            rules_generic += rule_guess == truth

    return {
        "strategies": strategies,
        "money_at_risk": sum(p["amount"] for p in payments),
        "archetypes": sorted(archetypes.values(), key=lambda r: -r["total"]),
        "exceptions": [
            {"reason": reason, "label": STOP_REASON_LABELS.get(reason, reason),
             "count": count, "money": money[reason]}
            for reason, count in counts.most_common()
        ],
        "diagnosis": {
            "model_name": next(
                (v.get("model") for v in cache.values() if v.get("model")), "cached"
            ),
            "total": len(payments),
            "generic_total": generic,
            "model_all": model_all / len(payments),
            "rules_all": rules_all / len(payments),
            "model_generic": model_generic / generic if generic else 0,
            "rules_generic": rules_generic / generic if generic else 0,
        },
    }


@app.get("/api/payments")
def payments_list(run_id: str = DEFAULT_RUN):
    """One row per payment: what it was, what happened, why it stopped."""
    truth = {p["id"]: p for p in payments_in_run(run_id)}
    listing = rows(
        """SELECT payment_id, amount,
                  MAX(CASE WHEN stage='diagnose' THEN archetype END) AS archetype,
                  MAX(CASE WHEN stage='diagnose' THEN confidence END) AS confidence,
                  MAX(CASE WHEN stage='diagnose' THEN diagnosis_method END) AS method,
                  MAX(CASE WHEN stop_reason IS NOT NULL THEN stop_reason END) AS stop_reason,
                  COUNT(CASE WHEN stage='act' THEN 1 END) AS actions
           FROM event WHERE run_id = ? AND strategy = 'failsafe'
           GROUP BY payment_id, amount ORDER BY amount DESC""",
        (run_id,),
    )
    for row in listing:
        payment = truth[row["payment_id"]]
        row["truth"] = payment["_archetype"]
        row["misdiagnosed"] = row["truth"] != row["archetype"]
        row["recovered"] = row["stop_reason"] == "recovered"
        row["stop_label"] = STOP_REASON_LABELS.get(row["stop_reason"], row["stop_reason"])
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
    payment = next((p for p in seed_module.load() if p["id"] == payment_id), {})
    return {
        "payment_id": payment_id,
        "truth": payment.get("_archetype"),
        "amount": payment.get("amount"),
        "method": payment.get("method"),
        "bank": payment.get("bank"),
        "error_description": payment.get("error_description"),
        "bank_failures_last_hour": payment.get("bank_failure_count_last_hour"),
        "prior_failures_30d": payment.get("prior_failures_30d"),
        "chain": chain,
    }


@app.get("/")
def index():
    return FileResponse("static/index.html")
