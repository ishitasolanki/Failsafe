"""The stopping rules are the product. These tests are the proof they hold.

Written before the agent, on purpose: if any assertion here fails, the agent is
capable of taking a money action it should not take.
"""

from datetime import datetime, timedelta

import pytest

from policy import (
    ASK_UPDATE_CARD,
    BANK_DOWNTIME,
    CONTACT_COOLDOWN_HOURS,
    ESCALATE_HUMAN,
    EXPIRED_CARD,
    FRAUD_BURST_FAILURES,
    HIGH_VALUE_PAISA,
    HOLD_FOR_APPROVAL,
    INSUFFICIENT_FUNDS,
    IST,
    MANDATE_REVOKED,
    MAX_ATTEMPTS,
    NO_ACTION,
    PAYMENT_LINK_UPI,
    PaymentState,
    RETRY_SCHEDULED,
    SUSPECTED_FRAUD,
    THREEDS_ABANDON,
    decide,
    in_quiet_hours,
    next_salary_date,
)

MIDDAY = datetime(2026, 8, 12, 14, 0, tzinfo=IST)
NIGHT = datetime(2026, 8, 12, 23, 30, tzinfo=IST)
EARLY = datetime(2026, 8, 12, 6, 0, tzinfo=IST)


def payment(amount=5_000_00):
    return {"id": "pay_test", "amount": amount}


# --- terminal archetypes: acting is worse than doing nothing ----------------

@pytest.mark.parametrize("archetype", [MANDATE_REVOKED, SUSPECTED_FRAUD])
def test_terminal_archetypes_never_take_a_money_action(archetype):
    decision = decide(payment(), archetype, PaymentState(), MIDDAY)
    assert decision.action == ESCALATE_HUMAN
    assert decision.is_stop
    assert decision.action not in (RETRY_SCHEDULED, PAYMENT_LINK_UPI, ASK_UPDATE_CARD)


@pytest.mark.parametrize("archetype", [MANDATE_REVOKED, SUSPECTED_FRAUD])
def test_terminal_archetypes_stop_regardless_of_attempts_or_amount(archetype):
    """No combination of state may talk the policy out of a terminal stop."""
    for attempts in range(MAX_ATTEMPTS + 1):
        for amount in (100_00, HIGH_VALUE_PAISA + 1):
            decision = decide(
                payment(amount), archetype, PaymentState(attempts=attempts), MIDDAY
            )
            assert decision.is_stop
            assert decision.action == ESCALATE_HUMAN


# --- attempt budget ---------------------------------------------------------

def test_fourth_attempt_is_refused():
    state = PaymentState(attempts=MAX_ATTEMPTS)
    decision = decide(payment(), INSUFFICIENT_FUNDS, state, MIDDAY)
    assert decision.action == NO_ACTION
    assert decision.stop_reason == "attempts_exhausted"


def test_attempts_below_budget_still_act():
    state = PaymentState(attempts=MAX_ATTEMPTS - 1)
    decision = decide(payment(), INSUFFICIENT_FUNDS, state, MIDDAY)
    assert decision.action == RETRY_SCHEDULED
    assert not decision.is_stop


# --- high value -------------------------------------------------------------

def test_high_value_requires_human_approval():
    decision = decide(
        payment(HIGH_VALUE_PAISA + 1), INSUFFICIENT_FUNDS, PaymentState(), MIDDAY
    )
    assert decision.action == HOLD_FOR_APPROVAL


def test_high_value_proceeds_once_a_human_approves():
    """Escalation is a gate, not a bin. Approval must let the work continue."""
    decision = decide(
        payment(HIGH_VALUE_PAISA + 1),
        INSUFFICIENT_FUNDS,
        PaymentState(human_approved=True),
        MIDDAY,
    )
    assert decision.action == RETRY_SCHEDULED


def test_at_threshold_is_not_high_value():
    """The gate is strictly above the threshold; boundary must not silently hold."""
    decision = decide(
        payment(HIGH_VALUE_PAISA), INSUFFICIENT_FUNDS, PaymentState(), MIDDAY
    )
    assert decision.action == RETRY_SCHEDULED


# --- quiet hours ------------------------------------------------------------

def test_quiet_hours_wrap_past_midnight():
    assert in_quiet_hours(NIGHT)
    assert in_quiet_hours(EARLY)
    assert not in_quiet_hours(MIDDAY)


@pytest.mark.parametrize("now", [NIGHT, EARLY])
def test_contact_deferred_during_quiet_hours(now):
    decision = decide(payment(), EXPIRED_CARD, PaymentState(), now)
    assert decision.rule_fired == "deferred_quiet_hours"
    assert decision.not_before is not None
    assert not in_quiet_hours(decision.not_before)


def test_silent_retry_is_not_blocked_by_quiet_hours():
    """A retry wakes nobody, so quiet hours must not apply to it."""
    decision = decide(payment(), BANK_DOWNTIME, PaymentState(), NIGHT)
    assert decision.action == RETRY_SCHEDULED
    assert decision.rule_fired == "retry_after_downtime_window"


# --- contact throttle -------------------------------------------------------

def test_contact_throttled_within_24h():
    state = PaymentState(last_contact_at=MIDDAY - timedelta(hours=2))
    decision = decide(payment(), THREEDS_ABANDON, state, MIDDAY)
    assert decision.rule_fired == "contact_throttled_24h"
    assert decision.not_before == state.last_contact_at + timedelta(
        hours=CONTACT_COOLDOWN_HOURS
    )


def test_contact_allowed_after_cooldown():
    state = PaymentState(last_contact_at=MIDDAY - timedelta(hours=25))
    decision = decide(payment(), THREEDS_ABANDON, state, MIDDAY)
    assert decision.action == PAYMENT_LINK_UPI
    assert decision.rule_fired.startswith("archetype_action")


# --- fail closed ------------------------------------------------------------

def test_unknown_archetype_fails_closed():
    """An LLM returning something off-menu must not fall through to an action."""
    decision = decide(payment(), "WHATEVER_THE_MODEL_SAID", PaymentState(), MIDDAY)
    assert decision.action == ESCALATE_HUMAN
    assert decision.stop_reason == "unknown_archetype"


def test_opted_out_customer_is_never_contacted():
    decision = decide(
        payment(), EXPIRED_CARD, PaymentState(opted_out=True), MIDDAY
    )
    assert decision.action == NO_ACTION
    assert decision.stop_reason == "opted_out"


def test_recovered_payment_takes_no_further_action():
    decision = decide(
        payment(), INSUFFICIENT_FUNDS, PaymentState(recovered=True), MIDDAY
    )
    assert decision.action == NO_ACTION
    assert decision.stop_reason == "recovered"


# --- scheduling -------------------------------------------------------------

def test_insufficient_funds_waits_for_salary_cycle():
    decision = decide(payment(), INSUFFICIENT_FUNDS, PaymentState(), MIDDAY)
    assert decision.not_before == next_salary_date(MIDDAY)
    assert decision.not_before.day == 1
    assert decision.not_before > MIDDAY


def test_salary_date_rolls_over_year_boundary():
    december = datetime(2026, 12, 20, 12, 0, tzinfo=IST)
    assert next_salary_date(december) == datetime(2027, 1, 1, 10, 0, tzinfo=IST)


def test_bank_downtime_retries_soon_not_next_month():
    decision = decide(payment(), BANK_DOWNTIME, PaymentState(), MIDDAY)
    assert MIDDAY < decision.not_before < MIDDAY + timedelta(hours=2)


# --- regression -------------------------------------------------------------

def test_scheduled_retry_does_not_recede_as_the_clock_moves():
    """Regression: the retry time must be fixed to the failure, not to `now`.

    The first version computed the schedule from the current clock, so every
    pass through the agent loop pushed the retry 45 minutes further away and the
    payment was never actually retried — it just aged out of the horizon.
    """
    anchor = MIDDAY
    first = decide(payment(), BANK_DOWNTIME, PaymentState(), anchor, anchor=anchor)
    later = anchor + timedelta(minutes=30)
    second = decide(payment(), BANK_DOWNTIME, PaymentState(), later, anchor=anchor)
    assert first.not_before == second.not_before

    # And once the clock passes it, the action is due rather than deferred again.
    due = decide(
        payment(), BANK_DOWNTIME, PaymentState(), first.not_before, anchor=anchor
    )
    assert due.not_before <= first.not_before


def test_failure_burst_stops_even_when_diagnosis_says_benign():
    """Defence in depth: safety must not depend on the model being right.

    A misdiagnosed fraud case reaching decide() as INSUFFICIENT_FUNDS must still
    be stopped by the deterministic burst signal.
    """
    burst = {"id": "pay_burst", "amount": 5_000_00,
             "prior_failures_30d": FRAUD_BURST_FAILURES}
    decision = decide(burst, INSUFFICIENT_FUNDS, PaymentState(), MIDDAY)
    assert decision.action == ESCALATE_HUMAN
    assert decision.stop_reason == "terminal:FAILURE_BURST"


def test_normal_failure_count_is_not_stopped():
    ok = {"id": "pay_ok", "amount": 5_000_00,
          "prior_failures_30d": FRAUD_BURST_FAILURES - 1}
    assert decide(ok, INSUFFICIENT_FUNDS, PaymentState(), MIDDAY).action != ESCALATE_HUMAN
