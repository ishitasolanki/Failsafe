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

## 6. Three calls failed the moment it hit the real API

First live run against Razorpay test mode, 12 payments:

```
PAYMENT_LINK_UPI   link_created                    2
RETRY_SCHEDULED    order_created                   7
RETRY_SCHEDULED    provider_error:HTTPStatusError  3
```

The three failures were all first attempts fired in a burst, on unrelated
amounts, and retrying one by hand succeeded immediately. Test mode rate-limits.

Fixed with bounded backoff — but only on statuses where the request provably
did not land: `429` and `5xx`, plus transport errors that never reached them. A
`4xx` is a real rejection and is never retried. Retrying a request that *did*
land is how you double-charge someone.

## 7. The double-charge guard caught me first

Re-ran the same batch after the backoff fix. The orders went through and the two
payment links now failed:

```
payment link with given reference_id: failsafe_pay_FS0028_a1 already exists
```

My own idempotency key, refusing to create a second link for an attempt that had
already happened. Working exactly as designed — and I had been about to call it
a bug.

The right handling is not to bypass it and not to report it as a failure.
Re-running an attempt that already succeeded should return *the thing that
already exists*. The client now catches the duplicate, fetches the existing link
by `reference_id`, and returns it marked `_replayed`, which the audit trail
records as `link_replayed` rather than `link_created` so the distinction stays
visible.

```
link_replayed   2
order_created  10
```

Zero errors, and the batch is genuinely safe to re-run against live test mode.

## 8. My "model accuracy" was the baseline wearing a costume

Plugged in a Groq key, re-ran, and the report said:

```
diagnosis accuracy   rules 75.0%   model 75.0%
  on generic errors  rules 16.7%   model 16.7%
```

Identical to three decimal places. Two numbers that agree that precisely are not
a finding, they are the same number printed twice.

The audit trail said why:

```
diagnosis_method  llm   35
diagnosis_method  rules 25
llm_reason  "llm unavailable (HTTPStatusError), used rules"  x25
```

Groq's free tier was rate-limiting, and `diagnose()` catches any exception and
falls back to the keyword classifier. Twenty-five of sixty diagnoses were never
the model at all — but they were still being counted in the "model" column.

The fallback itself is right: a rate limit should not crash a batch. What was
wrong is that it was **silent**, so a degraded run looked exactly like a healthy
one. Added the same bounded backoff the Razorpay client got, honouring
`Retry-After`. All 60 diagnoses now come from the model:

```
diagnosis accuracy   rules 75.0%   model 80.0%
  on generic errors  rules 16.7%   model 33.3%
```

The real contribution was hiding behind a fallback that reported success.

Worth stating what nearly happened: I could have shipped "the LLM matches a
keyword lookup, so the AI adds nothing", concluded the model was decoration, and
cut it. The measurement was broken, not the model.

## 9. The model that thought itself out of an answer

Groq had retired `llama-3.3-70b-versatile`, so the first live call 404'd. Swapped
to `openai/gpt-oss-120b` and got empty strings back instead:

```
finish_reason: length
completion_tokens: 60
content:   ''
reasoning: 'The user asks: "Say {"ok": true}..." The developer says...'
```

It is a reasoning model. Its thinking is billed against `max_tokens`, and at 60
tokens it deliberated until the budget ran out and never wrote the answer.
Raised the budget and set `reasoning_effort: "low"` — this is classification, it
needs a label, not a meditation.

## 10. The reproducibility claim in my own README was false

The README promised a judge could clone the repo with no API keys and reproduce
every number from the committed diagnosis cache. I tested it by hiding `.env`
and re-running, expecting an identical report.

It came back with the baseline's numbers. Every cache lookup had missed.

The cache key was a hash of the prompt version, the payment facts **and the
currently configured model**. With no key configured that last part resolved to
`"none"`, so every fingerprint differed from the ones generated on a machine
that had keys. Sixty misses, sixty silent fallbacks to the keyword classifier,
and a report that looked plausible while measuring the wrong thing.

Keyed on prompt version and facts only now; the model that produced each answer
is recorded in the cached value instead, where it stays visible without
fragmenting the key space.

Fixing that exposed a second one. The reports *still* differed — accuracy now
matched, but recovery did not. `evaluate.py` loaded the cache once for scoring
while `run_batch` loaded its own copy, so within a single process the recovery
run and the scoring pass each made independent LLM calls and disagreed about the
same payment. One cache is now loaded once and passed in, and sampling is pinned
to `temperature: 0`.

Verified rather than assumed:

```
$ mv .env .env.hidden && python evaluate.py --split heldout --out report_nokeys.md
$ diff report.md report_nokeys.md
IDENTICAL - a judge with zero credentials reproduces every number
```

The lesson is the one from §8 again, in a different costume: a fallback that
reports success is worse than a crash. Both times the system looked healthy
while quietly measuring something else, and both times only an explicit test of
the claim caught it.
