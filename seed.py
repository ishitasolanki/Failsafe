"""Synthetic failed-payment generator with ground truth.

Produces a batch of failed Razorpay-shaped payments. Each carries a hidden
ground-truth archetype (used only to score diagnosis afterwards) and a
pre-rolled "world" that decides whether a given recovery action would have
worked.

Two design choices matter for the honesty of the numbers:

1. **The world is pre-rolled per payment.** Success or failure of every possible
   action at every attempt number is decided here, before any agent runs. The
   agent and the naive baseline therefore face an identical world and the
   comparison between them is fair. Neither can get lucky.

2. **Roughly a third of failures carry a generic error string.** Real payment
   gateways do not hand you a clean label. For those, the archetype is only
   recoverable from context — bank failure rate in the last hour, prior attempt
   history, method, amount, time of day. This is what stops the diagnosis step
   from being a lookup table, and it is why an LLM earns its place here.

Ground truth never reaches the diagnosis step: use `observable()` to get the
fields an agent is allowed to see.
"""

import json
import random
import sys
from datetime import datetime, timedelta

from policy import (
    ACTIONS,
    BANK_DOWNTIME,
    EXPIRED_CARD,
    INSUFFICIENT_FUNDS,
    IST,
    MANDATE_REVOKED,
    MAX_ATTEMPTS,
    SUSPECTED_FRAUD,
    THREEDS_ABANDON,
    ASK_UPDATE_CARD,
    PAYMENT_LINK_UPI,
    RETRY_NOW,
    RETRY_SCHEDULED,
)

SEED = 7
N_PAYMENTS = 200
N_HELDOUT = 60
BASE_START = datetime(2026, 8, 10, 0, 0, tzinfo=IST)
WINDOW_DAYS = 18
HORIZON_DAYS = 21          # how long the agent may keep working a payment

BANKS = ["HDFC", "ICICI", "SBIN", "UTIB", "KKBK", "PUNB", "YESB", "IDFB"]
CARD_NETWORKS = ["Visa", "MasterCard", "RuPay", "Amex"]

# How likely each action is to recover a payment of a given archetype, on the
# first attempt. These are the assumptions the whole simulation rests on; they
# are stated here in one place so a reader can disagree with them explicitly.
# Numbers are directionally modelled on published payment-retry behaviour, not
# measured from production data.
SUCCESS_MODEL = {
    INSUFFICIENT_FUNDS: {RETRY_SCHEDULED: 0.55, RETRY_NOW: 0.08,
                         PAYMENT_LINK_UPI: 0.18, ASK_UPDATE_CARD: 0.04},
    BANK_DOWNTIME:      {RETRY_SCHEDULED: 0.78, RETRY_NOW: 0.18,
                         PAYMENT_LINK_UPI: 0.30, ASK_UPDATE_CARD: 0.03},
    EXPIRED_CARD:       {RETRY_SCHEDULED: 0.00, RETRY_NOW: 0.00,
                         PAYMENT_LINK_UPI: 0.20, ASK_UPDATE_CARD: 0.32},
    THREEDS_ABANDON:    {RETRY_SCHEDULED: 0.12, RETRY_NOW: 0.12,
                         PAYMENT_LINK_UPI: 0.46, ASK_UPDATE_CARD: 0.05},
    # Nothing works on these. Any action taken is a wasted action by definition,
    # which is exactly what the wasted-action metric is there to catch.
    MANDATE_REVOKED:    {},
    SUSPECTED_FRAUD:    {},
}

ARCHETYPE_WEIGHTS = {
    INSUFFICIENT_FUNDS: 0.30,
    BANK_DOWNTIME: 0.22,
    THREEDS_ABANDON: 0.18,
    EXPIRED_CARD: 0.14,
    MANDATE_REVOKED: 0.09,
    SUSPECTED_FRAUD: 0.07,
}

# Error strings shaped like Razorpay's. Synthetic, not copied from live traffic.
SPECIFIC_ERRORS = {
    INSUFFICIENT_FUNDS: [
        ("BAD_REQUEST_ERROR", "customer", "payment_authorization",
         "Your payment failed due to insufficient funds in your account."),
        ("BAD_REQUEST_ERROR", "customer", "payment_authorization",
         "Account balance is insufficient to complete this transaction."),
    ],
    BANK_DOWNTIME: [
        ("GATEWAY_ERROR", "bank", "payment_authorization",
         "Payment processing failed due to an error at the bank's end."),
        ("GATEWAY_ERROR", "bank", "payment_authorization",
         "The bank is not responding. Please try again later."),
    ],
    EXPIRED_CARD: [
        ("BAD_REQUEST_ERROR", "customer", "payment_initiation",
         "Your card has expired. Please use a different card."),
    ],
    THREEDS_ABANDON: [
        ("BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "3D Secure authentication was not completed."),
        ("BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "Payment was not completed on time."),
    ],
    MANDATE_REVOKED: [
        ("BAD_REQUEST_ERROR", "customer", "payment_authorization",
         "The mandate for this subscription has been cancelled by the customer."),
    ],
    SUSPECTED_FRAUD: [
        ("BAD_REQUEST_ERROR", "issuer", "payment_authorization",
         "Payment failed due to risk checks."),
    ],
}

# Deliberately uninformative. The archetype must be inferred from context.
GENERIC_ERRORS = [
    ("BAD_REQUEST_ERROR", "customer", "payment_authorization",
     "Payment failed. Please try again."),
    ("BAD_REQUEST_ERROR", "customer", "payment_authorization",
     "Your payment could not be processed."),
    ("GATEWAY_ERROR", "gateway", "payment_authorization",
     "Payment failed at the payment gateway."),
]
GENERIC_ERROR_RATE = 0.35

# Fields an agent is permitted to see. Anything outside this list is ground
# truth or simulation internals and must never reach the diagnosis step.
OBSERVABLE_FIELDS = [
    "id", "created_at", "amount", "currency", "method", "bank", "card_network",
    "is_subscription", "error_code", "error_source", "error_step",
    "error_description", "customer_id", "prior_failures_30d",
    "prior_successes_30d", "bank_failure_count_last_hour",
]


def _weighted_choice(rng, weights: dict):
    roll = rng.random()
    cumulative = 0.0
    for key, weight in weights.items():
        cumulative += weight
        if roll < cumulative:
            return key
    return list(weights)[-1]


def _amount_paise(rng, archetype):
    """Rupee amounts, log-ish spread, with a few above the ₹50k approval gate."""
    if rng.random() < 0.06:
        rupees = rng.randint(50_001, 180_000)      # trips high-value approval
    elif archetype == MANDATE_REVOKED:
        rupees = rng.choice([199, 299, 499, 799, 1499])   # subscription-sized
    else:
        rupees = int(round(rng.lognormvariate(7.0, 1.0)))
        rupees = max(99, min(rupees, 49_000))
    return rupees * 100


def _build_world(rng, archetype):
    """Pre-roll the outcome of every action at every attempt number.

    Repeat attempts of the same action decay, so a naive strategy that burns
    three identical retries gets steadily less for each one.
    """
    model = SUCCESS_MODEL[archetype]
    world = {}
    for action in ACTIONS:
        base = model.get(action, 0.0)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            probability = base * (0.75 ** (attempt - 1))
            world[f"{action}:{attempt}"] = rng.random() < probability
    return world


def _downtime_windows(rng):
    """A few real bank outages, so BANK_DOWNTIME clusters instead of scattering."""
    windows = []
    for _ in range(4):
        start = BASE_START + timedelta(
            days=rng.randint(0, WINDOW_DAYS - 1), hours=rng.randint(6, 22)
        )
        windows.append({
            "bank": rng.choice(BANKS),
            "start": start,
            "end": start + timedelta(hours=rng.randint(1, 3)),
        })
    return windows


def generate(n=N_PAYMENTS, seed=SEED):
    rng = random.Random(seed)
    windows = _downtime_windows(rng)
    payments = []

    for i in range(n):
        archetype = _weighted_choice(rng, ARCHETYPE_WEIGHTS)

        # Method has to be consistent with the archetype. An expired card cannot
        # fail a UPI payment.
        if archetype == MANDATE_REVOKED:
            method, is_subscription = "emandate", True
        elif archetype in (EXPIRED_CARD, THREEDS_ABANDON):
            method = "card"
            is_subscription = rng.random() < 0.15
        elif archetype == BANK_DOWNTIME:
            method = rng.choice(["netbanking", "upi", "card"])
            is_subscription = rng.random() < 0.10
        elif archetype == INSUFFICIENT_FUNDS:
            # Insufficient funds hits recurring mandates too, so `emandate` is
            # not a free giveaway for MANDATE_REVOKED.
            method = rng.choice(
                ["card", "upi", "netbanking", "wallet", "emandate", "emandate"]
            )
            is_subscription = method == "emandate" or rng.random() < 0.12
        else:
            method = rng.choice(["card", "upi", "netbanking", "wallet"])
            is_subscription = rng.random() < 0.12

        # Bank downtime happens inside a real outage window; everything else is
        # scattered across the period.
        if archetype == BANK_DOWNTIME:
            window = rng.choice(windows)
            bank = window["bank"]
            span = (window["end"] - window["start"]).total_seconds()
            created = window["start"] + timedelta(seconds=rng.uniform(0, span))
            bank_failures_last_hour = rng.randint(9, 44)
        else:
            bank = rng.choice(BANKS)
            created = BASE_START + timedelta(
                days=rng.uniform(0, WINDOW_DAYS), hours=rng.uniform(0, 24)
            )
            bank_failures_last_hour = rng.randint(0, 3)

        if rng.random() < GENERIC_ERROR_RATE:
            code, source, step, description = rng.choice(GENERIC_ERRORS)
            error_is_generic = True
        else:
            code, source, step, description = rng.choice(SPECIFIC_ERRORS[archetype])
            error_is_generic = False

        # Repeat failers skew towards insufficient funds; fraud rings show an
        # abnormal burst. Both are weak signals on their own.
        if archetype == INSUFFICIENT_FUNDS:
            prior_failures = rng.randint(1, 5)
        elif archetype == SUSPECTED_FRAUD:
            prior_failures = rng.randint(3, 11)
        else:
            prior_failures = rng.randint(0, 2)

        payment = {
            "id": f"pay_FS{i:04d}",
            "created_at": created.isoformat(),
            "amount": _amount_paise(rng, archetype),
            "currency": "INR",
            "method": method,
            "bank": bank,
            "card_network": rng.choice(CARD_NETWORKS) if method == "card" else None,
            "is_subscription": is_subscription,
            "error_code": code,
            "error_source": source,
            "error_step": step,
            "error_description": description,
            "customer_id": f"cust_{rng.randint(1, 140):04d}",
            "prior_failures_30d": prior_failures,
            "prior_successes_30d": rng.randint(0, 6),
            "bank_failure_count_last_hour": bank_failures_last_hour,
            # --- ground truth and simulation internals, never shown to agents ---
            "_archetype": archetype,
            "_error_is_generic": error_is_generic,
            "_world": _build_world(rng, archetype),
        }
        payments.append(payment)

    # Split after shuffling so the held-out set is not the tail of generation
    # order and cannot correlate with anything in the generator.
    rng.shuffle(payments)
    for index, payment in enumerate(payments):
        payment["split"] = "heldout" if index < N_HELDOUT else "train"
    payments.sort(key=lambda p: p["created_at"])
    return payments


def observable(payment: dict) -> dict:
    """Strip ground truth. The only entry point the diagnosis step may use."""
    return {field: payment[field] for field in OBSERVABLE_FIELDS}


def would_succeed(payment: dict, action: str, attempt: int) -> bool:
    """Consult the pre-rolled world. Same answer for the agent and the baseline."""
    return payment["_world"].get(f"{action}:{attempt}", False)


def load(path="payments.json"):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main():
    payments = generate()
    with open("payments.json", "w", encoding="utf-8") as handle:
        json.dump(payments, handle, indent=1)

    counts = {}
    for payment in payments:
        counts[payment["_archetype"]] = counts.get(payment["_archetype"], 0) + 1
    generic = sum(1 for p in payments if p["_error_is_generic"])
    heldout = sum(1 for p in payments if p["split"] == "heldout")
    total_paise = sum(p["amount"] for p in payments)

    print(f"wrote payments.json  ({len(payments)} failed payments)")
    print(f"  at risk       ₹{total_paise / 100:,.0f}")
    print(f"  split         {len(payments) - heldout} train / {heldout} heldout")
    print(f"  generic error {generic} ({generic / len(payments):.0%}) "
          f"- archetype inferable only from context")
    for archetype, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {archetype:<20} {count:>3}")


if __name__ == "__main__":
    # Windows consoles default to cp1252, which cannot encode the rupee sign.
    sys.stdout.reconfigure(encoding="utf-8")
    main()
