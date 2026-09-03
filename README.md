# Failsafe

**An autonomous payment-failure recovery agent for Razorpay merchants.**

Razorpay AI Buildathon — Track 03, AI Revenue Recovery.

Named for the part that matters: Failsafe is defined by what it refuses to do,
not by what it attempts.

---

## The problem

When a payment fails on Razorpay, the merchant loses the sale silently. Nobody
is told. The money simply does not arrive.

Every failure has a cause, and each cause has a *different* correct response at
a *different* time:

| The failure | What actually helps | What a blind retry does |
|---|---|---|
| Insufficient funds | Retry on the 1st, when salary lands | Burns an attempt against an empty account |
| Bank downtime | Retry in ~45 minutes, same method | Fails again while the bank is still down |
| Expired card | Ask the customer to update it | Fails 100% of the time, forever |
| 3DS abandoned | Send a UPI link — cheaper to complete | Sends them back to the OTP screen they already left |
| Mandate revoked | Stop. Request re-authorisation. | **Compliance violation** |
| Suspected fraud | Stop. Human queue. | **Invites a chargeback** |

Merchants today run one blanket "retry tomorrow", or nothing at all. The last
two rows are the reason a smarter retry loop is not enough: some payments must
never be retried, and a system that cannot tell the difference is dangerous
rather than merely ineffective.

## Results

Measured on a **held-out 60-payment split** that was never used while building,
against a naive-retry baseline running over the **identical pre-rolled world**,
so the only difference between the two columns is strategy.

| Metric | Naive retry | Failsafe | Delta |
|---|---:|---:|---:|
| Recovered | 10 | **33** | +23 |
| Recovery rate | 16.7% | **55.0%** | +38.3pp |
| Money recovered | ₹155,297 | **₹332,341** | +₹177,044 |
| Actions spent | 163 | **96** | −67 |
| Wasted actions | 36 | **15** | −21 |
| Compliance violations | 21 | **0** | −21 |

₹574,984 was at risk in this split.

**More money, from fewer actions, with zero compliance violations.** The action
count matters as much as the recovery count: every action is a real API call and
some of them are messages to a human being.

Reproduce with one command — see below. Full numbers, per-archetype breakdown
and the complete exception list are in [report.md](report.md).

### The numbers that are not flattering

Reported because a metric without its cost is marketing:

- **15 wasted actions.** Money actions spent on payments that could never have
  been recovered by anything. Lower than the baseline's 36, not zero.
- **27 of 60 payments were not recovered.** Every one is listed in
  [report.md](report.md) with the reason it stopped. Nothing is filtered out.
- **Some of those stops are correct.** `MANDATE_REVOKED` and `SUSPECTED_FRAUD`
  recover 0% *by design*. Stopping is the right answer, and counting them as
  failures would misrepresent what the agent is for.
- **The diagnosis numbers in this repo are rules-only** until an LLM key is
  configured, because the run that produced them had none. See below.

## How it works

```
   failed payment
         |
         v
  +--------------+   the LLM's only job: name the archetype.
  |  diagnose    |   6 archetypes. Never picks an action.
  |  (LLM)       |   Off-menu answer -> UNKNOWN -> fails closed.
  +--------------+
         |  archetype
         v
  +--------------+   deterministic. No model output reaches money
  |  policy      |   without passing through here.
  |  decide()    |
  +--------------+
    |  terminal archetype?        -> stop, human queue
    |  failure burst >= 8?        -> stop, regardless of diagnosis
    |  recurring payment?         -> verify mandate first (a read, not a guess)
    |  attempts >= 3?             -> stop
    |  amount > Rs 50,000?        -> hold for human approval
    |  contact within 24h?        -> defer
    |  21:00-09:00 IST?           -> defer (silent retries exempt)
         |  one bounded action
         v
  +--------------+   Razorpay test mode. Idempotency key = payment + attempt,
  |  act         |   so a crash-and-retry cannot double-charge.
  +--------------+
         |
         v
  +--------------+   recovered only when the provider says captured.
  |  observe     |   never self-reported.
  +--------------+
         |
         v
     audit trail (append-only sqlite; every step, every rule that fired)
```

### The one design decision worth arguing about

**The LLM classifies. It never chooses the money action.**

The archetype→action table lives in [policy.py](policy.py), is about six lines
long, and a compliance officer can read it. The model's entire job is picking
which row applies.

The consequence is the point: a misdiagnosis costs a wasted action. It cannot
cost a compliance breach, a double charge, or a 3am message to a customer,
because those are bounded by rules the model never touches.

And the safety checks do not sit downstream of the model either. This was
learned the hard way — see [BROKE.md](BROKE.md) §2. A fraud case misdiagnosed as
insufficient funds arrives at the policy layer already wearing the wrong label,
so terminal safety had to be made independent of the diagnosis entirely:

- an abnormal burst of failures from one customer stops the payment whatever the
  model said
- a recurring payment has its **mandate verified** before any retry, because
  mandate status is a fact you can look up, and guessing at a fact you can look
  up is indefensible

Both are deterministic and neither asks the model's opinion.

## Quickstart

```bash
pip install -r requirements.txt
python seed.py                        # generate the 200-payment batch
python -m pytest test_policy.py -q    # prove the stopping rules hold
python evaluate.py --split heldout    # the headline numbers + report.md
```

**No API keys needed.** Diagnoses are read from the committed
`diagnosis_cache.json`, so anyone can reproduce the exact numbers above with
zero credentials and zero cost.

See one payment's full decision chain:

```bash
python agent.py --split heldout --limit 5 --run-id demo
```

Browse the audit trail in a dashboard:

```bash
uvicorn app:app --reload
```

### Running it for real

Copy `.env.example` to `.env` and add keys.

- **Razorpay** — test mode only. Failsafe refuses to start against an
  `rzp_live_` key; this project must never touch real money.
- **One LLM key** — Anthropic, Groq or Google. All three are plain REST, handled
  by one ~20-line `if/elif` in [llm.py](llm.py). No vendor SDK.

```bash
python agent.py --split heldout --live   # real Razorpay test-mode calls
python evaluate.py --split heldout       # re-measure with live diagnoses
```

Every LLM answer is written to `diagnosis_cache.json` as it is produced, so the
first keyed run makes the numbers reproducible for everyone afterwards.

## What is real and what is simulated

Stated plainly, because a demo that blurs this is worthless:

| Part | Status |
|---|---|
| Payment link / order creation | **Real** Razorpay test-mode API calls (`--live`) |
| Failure diagnosis | **Real** LLM calls, cached |
| Policy, stopping rules, audit trail | **Real** code, this is the product |
| The failed payments themselves | **Synthetic** — generated by `seed.py` |
| Whether a customer then pays | **Simulated** — cannot be driven programmatically in test mode |

The recovery numbers are therefore *simulated outcomes on real API plumbing*.
The success model that produces them is a single readable table at the top of
[seed.py](seed.py), stated so that a reader can disagree with the assumptions
explicitly rather than having to reverse-engineer them.

Customer messages are generated and rendered, **never sent**. `notify.sms` and
`notify.email` are hard-coded off in every Razorpay call.

## Measurement honesty

- **Held-out split.** 60 of 200 payments, split after shuffling, never looked at
  while tuning.
- **Shared world.** Both strategies query the same pre-rolled outcomes, decided
  before either ran. Neither can get lucky.
- **A baseline, always.** A recovery rate with nothing to compare it against
  proves nothing.
- **No leakage.** The diagnosis step can only see fields listed in
  `OBSERVABLE_FIELDS`; ground truth is not reachable from it.
- **The LLM is measured against a keyword lookup**, so its contribution is
  visible rather than assumed. If a lookup table matches it, the model is
  decoration and the report will say so.

## Files

| File | What it is |
|---|---|
| [policy.py](policy.py) | Stopping rules and bounded actions. **The core.** |
| [test_policy.py](test_policy.py) | 24 tests proving every rule holds |
| [seed.py](seed.py) | Synthetic batch + ground truth + pre-rolled world |
| [diagnose.py](diagnose.py) | LLM classifier, cache, keyword baseline |
| [llm.py](llm.py) | One provider-agnostic call, three providers |
| [razorpay_client.py](razorpay_client.py) | Test-mode REST client, mock fallback |
| [agent.py](agent.py) | The loop + append-only audit trail |
| [evaluate.py](evaluate.py) | Metrics, baseline comparison, exception list |
| [app.py](app.py) | Dashboard over the audit trail |
| [BROKE.md](BROKE.md) | What broke while building, and how I got out |

Zero vendor SDKs. `httpx` talks to Razorpay and to all three LLM providers;
`sqlite3` is stdlib.
