"""The recovery loop: detect -> diagnose -> decide -> act -> observe -> stop.

Two strategies run over the identical pre-rolled world from seed.py:

  failsafe - diagnose the archetype, then let policy.decide() pick a bounded
             action. Every step is written to the audit trail.
  naive    - what a merchant does today: retry the same payment every 24h until
             the attempt budget runs out. No diagnosis, no stopping rules, no
             regard for who should never be retried.

The naive run is the baseline. Both see the same outcomes for the same actions,
so the difference between them is strategy and nothing else.
"""

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta

import diagnose as diagnosis
import razorpay_client
import seed as seed_module
from policy import (
    CONTACT_ACTIONS,
    HOLD_FOR_APPROVAL,
    MANDATE_REVOKED,
    ESCALATE_HUMAN,
    HOLD_FOR_APPROVAL,
    MAX_ATTEMPTS,
    NO_ACTION,
    PaymentState,
    RETRY_NOW,
    VERIFY_MANDATE,
    decide,
)

DB_PATH = "audit.db"
APPROVAL_LATENCY = timedelta(hours=4)   # how long a human takes to answer
APPROVAL_RATE = 0.8                     # share of escalations a human approves
MAX_STEPS = 12          # guard against a deferral loop that never advances
RETRY_GAP = timedelta(hours=24)

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    payment_id     TEXT NOT NULL,
    split          TEXT NOT NULL,
    stage          TEXT NOT NULL,
    attempt        INTEGER,
    archetype      TEXT,
    confidence     REAL,
    diagnosis_method TEXT,
    llm_reason     TEXT,
    rule_fired     TEXT,
    action         TEXT,
    action_result  TEXT,
    provider_ref   TEXT,
    amount         INTEGER,
    stop_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_payment ON event(run_id, payment_id, id);
"""


def connect(path=DB_PATH):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def log(connection, run_id, strategy, payment, **fields):
    """Append one audit row. Rows are only ever inserted, never updated."""
    row = {
        "run_id": run_id,
        "strategy": strategy,
        "ts": fields.pop("ts").isoformat(),
        "payment_id": payment["id"],
        "split": payment["split"],
        "amount": payment["amount"],
        "stage": fields.pop("stage"),
        "attempt": fields.pop("attempt", None),
        "archetype": fields.pop("archetype", None),
        "confidence": fields.pop("confidence", None),
        "diagnosis_method": fields.pop("diagnosis_method", None),
        "llm_reason": fields.pop("llm_reason", None),
        "rule_fired": fields.pop("rule_fired", None),
        "action": fields.pop("action", None),
        "action_result": fields.pop("action_result", None),
        "provider_ref": fields.pop("provider_ref", None),
        "stop_reason": fields.pop("stop_reason", None),
    }
    assert not fields, f"unknown audit fields: {list(fields)}"
    columns = ", ".join(row)
    placeholders = ", ".join("?" * len(row))
    connection.execute(
        f"INSERT INTO event ({columns}) VALUES ({placeholders})", list(row.values())
    )


def execute_action(client, payment, action, attempt, dry_run):
    """Perform the bounded action. Returns (result, provider_reference)."""
    if action in (NO_ACTION, ESCALATE_HUMAN, HOLD_FOR_APPROVAL):
        return "no_money_action", None

    reference = razorpay_client.idempotency_key(payment["id"], attempt)
    if dry_run:
        return "dry_run", reference

    try:
        if action in CONTACT_ACTIONS:
            created = client.create_payment_link(
                amount=payment["amount"],
                reference_id=reference,
                description=f"Recovery for payment {payment['id']}",
                method=razorpay_client.METHOD_HINT.get(action),
            )
            return "link_created", created.get("short_url") or created.get("id")
        created = client.create_order(amount=payment["amount"], receipt=reference)
        return "order_created", created.get("id")
    except Exception as error:
        # A provider failure must not be mistaken for a recovery failure. It is
        # logged as its own outcome and never counted as a recovery.
        return f"provider_error:{type(error).__name__}", reference


def run_failsafe(payment, cache, connection, run_id, client, dry_run, contact_ledger):
    """Diagnose once, then act under policy until recovered or stopped."""
    failed_at = datetime.fromisoformat(payment["created_at"])
    now = failed_at
    deadline = failed_at + timedelta(days=seed_module.HORIZON_DAYS)
    state = PaymentState()

    result = diagnosis.diagnose(payment, cache)
    archetype = result["archetype"]
    log(connection, run_id, "failsafe", payment, ts=now, stage="diagnose",
        archetype=archetype, confidence=result["confidence"],
        diagnosis_method=result["method"], llm_reason=result["reason"])

    outcome = {"recovered": False, "actions": 0, "stop_reason": "horizon_expired",
               "archetype": archetype, "diagnosis_method": result["method"]}

    for _ in range(MAX_STEPS):
        if now >= deadline:
            break
        state.last_contact_at = contact_ledger.get(payment["customer_id"])
        decision = decide(payment, archetype, state, now, anchor=failed_at)

        log(connection, run_id, "failsafe", payment, ts=now, stage="decide",
            archetype=archetype, attempt=state.attempts + 1,
            rule_fired=decision.rule_fired, action=decision.action,
            stop_reason=decision.stop_reason)

        if decision.is_stop:
            outcome["stop_reason"] = decision.stop_reason
            return outcome

        # The action is not due yet: advance the clock rather than acting early.
        if decision.not_before and decision.not_before > now:
            if decision.not_before >= deadline:
                # The right moment to act falls outside the window we are
                # willing to chase. Stop and say so rather than acting early.
                outcome["stop_reason"] = "scheduled_beyond_horizon"
                log(connection, run_id, "failsafe", payment, ts=now, stage="stop",
                    archetype=archetype, stop_reason="scheduled_beyond_horizon")
                return outcome
            now = decision.not_before
            continue

        # A high-value payment waits for a person. The wait is real (a human
        # is not instant) and the answer can be no, so this is modelled as a
        # delay plus a decision rather than as free money.
        if decision.action == HOLD_FOR_APPROVAL:
            digest = hashlib.sha256(f"approve_{payment['id']}".encode()).hexdigest()
            approved = (int(digest[:8], 16) / 0xFFFFFFFF) < APPROVAL_RATE
            log(connection, run_id, "failsafe", payment, ts=now, stage="escalate",
                archetype=archetype, action=HOLD_FOR_APPROVAL,
                rule_fired=decision.rule_fired,
                action_result="human_approved" if approved else "human_declined")
            if not approved:
                outcome["stop_reason"] = "human_declined"
                log(connection, run_id, "failsafe", payment, ts=now, stage="stop",
                    archetype=archetype, stop_reason="human_declined")
                return outcome
            state.human_approved = True
            now += APPROVAL_LATENCY
            continue

        # Verification is a read against the provider, not a money action. It
        # costs nothing from the attempt budget and it removes the guesswork
        # that a misdiagnosed mandate would otherwise turn into a breach.
        if decision.action == VERIFY_MANDATE:
            revoked = payment["_archetype"] == MANDATE_REVOKED
            log(connection, run_id, "failsafe", payment, ts=now, stage="verify",
                archetype=archetype, action=VERIFY_MANDATE,
                rule_fired=decision.rule_fired,
                action_result="mandate_revoked" if revoked else "mandate_active")
            if revoked:
                outcome["stop_reason"] = "terminal:MANDATE_REVOKED"
                log(connection, run_id, "failsafe", payment, ts=now, stage="stop",
                    archetype=archetype, stop_reason="terminal:MANDATE_REVOKED")
                return outcome
            state.mandate_verified = True
            continue

        state.attempts += 1
        outcome["actions"] += 1
        action_result, reference = execute_action(
            client, payment, decision.action, state.attempts, dry_run
        )
        if decision.action in CONTACT_ACTIONS:
            contact_ledger[payment["customer_id"]] = now

        recovered = seed_module.would_succeed(payment, decision.action, state.attempts)
        log(connection, run_id, "failsafe", payment, ts=now, stage="act",
            archetype=archetype, attempt=state.attempts, action=decision.action,
            rule_fired=decision.rule_fired, action_result=action_result,
            provider_ref=reference)
        log(connection, run_id, "failsafe", payment, ts=now, stage="observe",
            archetype=archetype, attempt=state.attempts, action=decision.action,
            action_result="captured" if recovered else "still_failed")

        if recovered:
            state.recovered = True
            outcome.update(recovered=True, stop_reason="recovered")
            log(connection, run_id, "failsafe", payment, ts=now, stage="stop",
                archetype=archetype, stop_reason="recovered")
            return outcome

        now += RETRY_GAP

    log(connection, run_id, "failsafe", payment, ts=now, stage="stop",
        archetype=archetype, stop_reason=outcome["stop_reason"])
    return outcome


def run_naive(payment, connection, run_id):
    """The baseline merchants actually run: blind 24h retries, nothing else.

    No diagnosis, no quiet hours, no terminal check. It will happily retry a
    revoked mandate three times, which is the point of measuring it.
    """
    now = datetime.fromisoformat(payment["created_at"])
    outcome = {"recovered": False, "actions": 0, "stop_reason": "attempts_exhausted"}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        outcome["actions"] += 1
        recovered = seed_module.would_succeed(payment, RETRY_NOW, attempt)
        log(connection, run_id, "naive", payment, ts=now, stage="act",
            attempt=attempt, action=RETRY_NOW, rule_fired="blind_retry",
            action_result="captured" if recovered else "still_failed")
        if recovered:
            outcome.update(recovered=True, stop_reason="recovered")
            log(connection, run_id, "naive", payment, ts=now, stage="stop",
                attempt=attempt, stop_reason="recovered")
            return outcome
        now += RETRY_GAP

    # The baseline stops for exactly one reason: it ran out of attempts. Logged
    # so the audit trail tells the same story for both strategies.
    log(connection, run_id, "naive", payment, ts=now, stage="stop",
        stop_reason="attempts_exhausted")
    return outcome


def run_batch(payments, strategy, run_id, dry_run=True, db_path=DB_PATH):
    connection = connect(db_path)
    # Re-running the same run_id replaces it, so the audit trail never mixes two
    # runs of the same name.
    connection.execute("DELETE FROM event WHERE run_id = ?", (run_id,))
    cache = diagnosis.load_cache()
    client = razorpay_client.get_client()
    # ponytail: the contact ledger is keyed by customer and walked in payment
    # order, which works because payments are sorted by created_at. If payments
    # ever arrive out of order this needs a real event queue.
    contact_ledger = {}

    outcomes = {}
    for payment in payments:
        if strategy == "naive":
            outcomes[payment["id"]] = run_naive(payment, connection, run_id)
        else:
            outcomes[payment["id"]] = run_failsafe(
                payment, cache, connection, run_id, client, dry_run, contact_ledger
            )
    connection.commit()
    connection.close()
    if strategy != "naive":
        diagnosis.save_cache(cache)
    return outcomes


def main():
    parser = argparse.ArgumentParser(description="Run the Failsafe recovery agent.")
    parser.add_argument("--split", choices=["train", "heldout", "all"], default="all")
    parser.add_argument("--limit", type=int, help="process only the first N payments")
    parser.add_argument("--strategy", choices=["failsafe", "naive"], default="failsafe")
    parser.add_argument("--live", action="store_true",
                        help="make real Razorpay test-mode calls (default: dry run)")
    parser.add_argument("--run-id", default="cli")
    args = parser.parse_args()

    payments = seed_module.load()
    if args.split != "all":
        payments = [p for p in payments if p["split"] == args.split]
    if args.limit:
        payments = payments[: args.limit]

    client = razorpay_client.get_client()
    mode = "LIVE test-mode calls" if args.live else "dry run"
    print(f"payments   {len(payments)} ({args.split})")
    print(f"strategy   {args.strategy}")
    print(f"razorpay   {client.mode}, {mode}")
    print(f"llm        {diagnosis.llm.active_provider() or 'none (cache/rules only)'}")
    print()

    outcomes = run_batch(payments, args.strategy, args.run_id, dry_run=not args.live)

    recovered = sum(1 for o in outcomes.values() if o["recovered"])
    money = sum(p["amount"] for p in payments if outcomes[p["id"]]["recovered"])
    actions = sum(o["actions"] for o in outcomes.values())
    print(f"recovered  {recovered}/{len(payments)}")
    print(f"money      Rs {money / 100:,.0f}")
    print(f"actions    {actions}")
    print(f"\naudit trail written to {DB_PATH} (run_id={args.run_id!r})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
