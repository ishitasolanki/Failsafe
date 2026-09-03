# What broke, and how I got out

Kept live while building, not reconstructed afterwards. In order.

## 1. The agent never actually retried anything

First end-to-end run: 8 payments, 2 actions taken, most rows ending in
`horizon_expired`. The audit trail showed the same payment being "decided" a
dozen times and never acted on:

```
08:05  decide  BANK_DOWNTIME  retry_after_downtime_window  RETRY_SCHEDULED
08:50  decide  BANK_DOWNTIME  retry_after_downtime_window  RETRY_SCHEDULED
09:35  decide  BANK_DOWNTIME  retry_after_downtime_window  RETRY_SCHEDULED
...
17:05  stop    horizon_expired
```

`schedule_for()` computed the retry time as `now + 45 minutes`. Every pass
through the loop advanced `now` to that time and then recomputed the schedule
from the *new* now, pushing the target another 45 minutes away. A moving target
the agent could never reach.

Fix: schedule against the payment's original failure time, not the current
clock. `decide()` now takes an explicit `anchor`. The bug is pinned by
`test_scheduled_retry_does_not_recede_as_the_clock_moves`.

What I took from it: the audit trail found this in about thirty seconds. Without
per-step logging I would have seen "low recovery rate" and gone looking in the
success model, which was fine all along.

## 2. Nine compliance violations that should have been zero

The policy layer refuses to retry a revoked mandate. The measured run still
showed 9 retries against revoked mandates and suspected fraud.

The policy was never the problem. The *diagnosis* was: a fraud case
misclassified as `INSUFFICIENT_FUNDS` arrives at `decide()` already wearing the
wrong label, and the terminal check has nothing to fire on.

This was the moment the design changed. Safety cannot sit downstream of a
model's opinion. Two deterministic guards now run without reference to the
diagnosis at all:

- an abnormal burst of recent failures from one customer forces a stop
- a recurring payment has its mandate **verified** before any retry — mandate
  status is a fact you can look up, so guessing at it was indefensible

Violations went 9 → 0. The lesson generalises: if a wrong model output can cause
harm, the guard against that harm must not be downstream of the model.

## 3. My agent recovered less money than the dumb baseline

Uncomfortable number, and it was real:

```
money recovered    naive Rs 155,297    failsafe Rs 52,060
```

Failsafe recovered three times as many payments and a third of the money. The
₹50,000 approval gate was implemented as a terminal stop, so every high-value
payment was escalated to a human and then simply abandoned. The baseline, having
no such scruples, retried them and collected.

The gate was right; treating escalation as a bin was wrong. Escalation is a
gate: a human answers, and if they approve, the work resumes. Now modelled as a
4-hour wait plus a decision that can be no.

Result: ₹332,341 vs the baseline's ₹155,297, with the gate still intact.

Worth saying plainly — I only caught this because the report prints the
baseline next to my own number. If I had only measured myself I would have
shipped "3x recovery rate!" and never noticed I was losing money.

## 4. The subscription tell was too good

Early runs classified `MANDATE_REVOKED` almost perfectly, which was suspicious.
The generator only ever produced `emandate` payments for revoked mandates, so
the method alone gave the answer away and the whole diagnosis step was being
scored against a rigged dataset.

Fixed in the generator: insufficient-funds failures now also occur on
`emandate`, because in reality recurring payments fail for ordinary reasons too.
Accuracy dropped, which was the correct direction.

## 5. Rupees crashed the console on Windows

```
UnicodeEncodeError: 'charmap' codec can't encode character '₹'
```

Windows terminals default to cp1252. Every CLI entry point now calls
`sys.stdout.reconfigure(encoding="utf-8")`. Small, but it would have killed a
live demo.
