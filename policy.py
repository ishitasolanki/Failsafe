"""Deterministic policy layer.

This is the core of Failsafe. The LLM classifies a failure into an archetype;
everything in this file decides what is allowed to happen next. No LLM output
reaches a money action without passing through `decide()`.

Every branch returns the name of the rule that fired, so the audit trail can
answer "why did this happen" for any payment.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# IST is a fixed +05:30 offset with no DST, so a fixed-offset tzinfo is correct
# here and avoids depending on the `tzdata` package (not bundled on Windows).
IST = timezone(timedelta(hours=5, minutes=30))

# --- Archetypes -------------------------------------------------------------

INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
BANK_DOWNTIME = "BANK_DOWNTIME"
EXPIRED_CARD = "EXPIRED_CARD"
THREEDS_ABANDON = "3DS_ABANDON"
MANDATE_REVOKED = "MANDATE_REVOKED"
SUSPECTED_FRAUD = "SUSPECTED_FRAUD"

ARCHETYPES = [
    INSUFFICIENT_FUNDS,
    BANK_DOWNTIME,
    EXPIRED_CARD,
    THREEDS_ABANDON,
    MANDATE_REVOKED,
    SUSPECTED_FRAUD,
]

# Acting on these cannot help and can cause harm (compliance breach on a revoked
# mandate, chargeback risk on suspected fraud). They are terminal on sight.
TERMINAL_ARCHETYPES = {MANDATE_REVOKED, SUSPECTED_FRAUD}

# --- Actions ----------------------------------------------------------------

RETRY_NOW = "RETRY_NOW"
RETRY_SCHEDULED = "RETRY_SCHEDULED"
PAYMENT_LINK_UPI = "PAYMENT_LINK_UPI"
ASK_UPDATE_CARD = "ASK_UPDATE_CARD"
ESCALATE_HUMAN = "ESCALATE_HUMAN"
HOLD_FOR_APPROVAL = "HOLD_FOR_APPROVAL"
VERIFY_MANDATE = "VERIFY_MANDATE"
NO_ACTION = "NO_ACTION"

ACTIONS = [
    RETRY_NOW,
    RETRY_SCHEDULED,
    PAYMENT_LINK_UPI,
    ASK_UPDATE_CARD,
    ESCALATE_HUMAN,
    HOLD_FOR_APPROVAL,
    VERIFY_MANDATE,
    NO_ACTION,
]

# Actions that put a message in front of a human being. Quiet hours and the
# contact throttle apply to these and only these; a silent retry does not wake
# anyone at 3am.
CONTACT_ACTIONS = {PAYMENT_LINK_UPI, ASK_UPDATE_CARD}

# The archetype -> intervention table. This mapping is fixed and reviewable,
# which is the whole point: the LLM picks the row, not the contents.
ARCHETYPE_ACTION = {
    INSUFFICIENT_FUNDS: RETRY_SCHEDULED,
    BANK_DOWNTIME: RETRY_SCHEDULED,
    EXPIRED_CARD: ASK_UPDATE_CARD,
    THREEDS_ABANDON: PAYMENT_LINK_UPI,
    MANDATE_REVOKED: ESCALATE_HUMAN,
    SUSPECTED_FRAUD: ESCALATE_HUMAN,
}

# --- Bounds -----------------------------------------------------------------

MAX_ATTEMPTS = 3                      # per payment, ever
CONTACT_COOLDOWN_HOURS = 24           # per customer
QUIET_START_HOUR = 21                 # 21:00 IST
QUIET_END_HOUR = 9                    # 09:00 IST
HIGH_VALUE_PAISA = 50_000 * 100       # above this, a human approves first
BANK_DOWNTIME_WAIT = timedelta(minutes=45)

# Defence in depth. The archetype comes from a model, and a model can be wrong.
# These deterministic signals stop a payment regardless of what was diagnosed,
# so a misdiagnosis costs a missed recovery and never a compliance breach.
FRAUD_BURST_FAILURES = 8      # failures by one customer in 30d that force a stop


@dataclass
class Decision:
    action: str
    rule_fired: str
    not_before: datetime | None = None   # earliest time the action may run
    stop_reason: str | None = None       # set iff this payment is now closed

    @property
    def is_stop(self) -> bool:
        return self.stop_reason is not None


@dataclass
class PaymentState:
    """Mutable per-payment state the policy reads. The agent owns updates."""
    attempts: int = 0
    recovered: bool = False
    closed: bool = False
    opted_out: bool = False
    mandate_verified: bool = False
    human_approved: bool = False
    last_contact_at: datetime | None = None   # per customer, not per payment


def in_quiet_hours(now: datetime) -> bool:
    """Quiet hours wrap past midnight, so this is an OR, not a range check."""
    hour = now.astimezone(IST).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def next_allowed_contact_time(now: datetime) -> datetime:
    """Start of the next window in which contacting a customer is permitted."""
    ist_now = now.astimezone(IST)
    if not in_quiet_hours(ist_now):
        return now
    candidate = ist_now.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= ist_now:
        candidate += timedelta(days=1)
    return candidate


def next_salary_date(now: datetime) -> datetime:
    """Next 1st of the month, 10:00 IST.

    Salary credits in India cluster at month start, so an insufficient-funds
    retry is worth far more there than it is tomorrow.
    """
    ist_now = now.astimezone(IST)
    year, month = ist_now.year, ist_now.month
    candidate = ist_now.replace(
        day=1, hour=10, minute=0, second=0, microsecond=0
    )
    if candidate <= ist_now:
        month += 1
        if month > 12:
            month, year = 1, year + 1
        candidate = candidate.replace(year=year, month=month)
    return candidate


def schedule_for(archetype: str, anchor: datetime) -> datetime:
    """When the scheduled retry for this archetype should fire.

    `anchor` is the time the payment originally failed, NOT the current clock.
    Anchoring on `now` was a real bug: each pass through the agent loop pushed
    the retry another 45 minutes into the future, so the payment was deferred
    forever and never actually retried.
    """
    if archetype == INSUFFICIENT_FUNDS:
        return next_salary_date(anchor)
    if archetype == BANK_DOWNTIME:
        return anchor + BANK_DOWNTIME_WAIT
    return anchor


def decide(
    payment: dict,
    archetype: str,
    state: PaymentState,
    now: datetime,
    anchor: datetime | None = None,
) -> Decision:
    """Return the single action permitted for this payment right now.

    Rules are evaluated in order and the first match wins. Order matters: the
    terminal and budget checks sit above the archetype table so that no
    archetype mapping can ever route around them.

    `anchor` is the payment's original failure time and fixes when a scheduled
    retry is due. It defaults to `now` for direct callers and tests.
    """
    anchor = anchor or now
    # 1. Nothing to do for a payment that already resolved.
    if state.recovered:
        return Decision(NO_ACTION, "already_recovered", stop_reason="recovered")
    if state.closed:
        return Decision(NO_ACTION, "already_closed", stop_reason="closed")

    # 2. Terminal archetypes. Checked before everything else because acting on
    #    these is worse than doing nothing.
    if archetype in TERMINAL_ARCHETYPES:
        return Decision(
            ESCALATE_HUMAN,
            f"terminal_archetype:{archetype.lower()}",
            stop_reason=f"terminal:{archetype}",
        )

    # 3. Deterministic safety net, checked without reference to the diagnosis.
    #    An abnormal burst of recent failures from one customer is stopped even
    #    if the model called it something benign.
    if payment.get("prior_failures_30d", 0) >= FRAUD_BURST_FAILURES:
        return Decision(
            ESCALATE_HUMAN,
            "failure_burst_override",
            stop_reason="terminal:FAILURE_BURST",
        )

    # 4. Unknown archetype (LLM returned something off-menu). Fail closed.
    if archetype not in ARCHETYPE_ACTION:
        return Decision(
            ESCALATE_HUMAN,
            "unknown_archetype_fail_closed",
            stop_reason="unknown_archetype",
        )

    # 5. Recurring payments: check the mandate before retrying it, rather than
    #    trusting the diagnosis. Mandate status is a fact you can look up, so
    #    guessing at it is indefensible. This is a read, not a money action, and
    #    it does not consume the attempt budget.
    if payment.get("method") == "emandate" and not state.mandate_verified:
        return Decision(VERIFY_MANDATE, "verify_mandate_before_retry")

    # 6. Customer opted out of contact. Overrides any intervention.
    if state.opted_out:
        return Decision(NO_ACTION, "customer_opted_out", stop_reason="opted_out")

    # 7. Attempt budget. Hard ceiling, no archetype exempt.
    if state.attempts >= MAX_ATTEMPTS:
        return Decision(
            NO_ACTION, "attempt_budget_exhausted", stop_reason="attempts_exhausted"
        )

    # 8. High-value payments need a human before any automated money action.
    #    This is an escalation, not an abandonment: once a person approves, the
    #    normal flow resumes. Only a refusal ends the payment.
    if payment["amount"] > HIGH_VALUE_PAISA and not state.human_approved:
        return Decision(HOLD_FOR_APPROVAL, "high_value_needs_approval")

    action = ARCHETYPE_ACTION[archetype]

    # 9. Contact-specific bounds. Only reached by actions that message a person.
    if action in CONTACT_ACTIONS:
        if state.last_contact_at is not None:
            elapsed = now - state.last_contact_at
            if elapsed < timedelta(hours=CONTACT_COOLDOWN_HOURS):
                return Decision(
                    action,
                    "contact_throttled_24h",
                    not_before=state.last_contact_at
                    + timedelta(hours=CONTACT_COOLDOWN_HOURS),
                )
        if in_quiet_hours(now):
            return Decision(
                action, "deferred_quiet_hours", not_before=next_allowed_contact_time(now)
            )
        return Decision(action, f"archetype_action:{archetype.lower()}", not_before=now)

    # 10. Scheduled retries. Timing is the intervention.
    when = schedule_for(archetype, anchor)
    rule = (
        "retry_at_salary_cycle"
        if archetype == INSUFFICIENT_FUNDS
        else "retry_after_downtime_window"
    )
    return Decision(action, rule, not_before=when)
