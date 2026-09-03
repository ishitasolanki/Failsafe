# Failsafe - evaluation report (heldout split)

Diagnosis source: **live groq calls, cached for reproducibility**. Outcomes come from the pre-rolled world in `seed.py`; both strategies face the identical world, so the comparison is strategy alone.

## Headline

| Metric | Naive retry (baseline) | Failsafe | Delta |
|---|---:|---:|---:|
| Payments | 60 | 60 | |
| Recovered | 10 | 35 | +25 |
| Recovery rate | 16.7% | 58.3% | +41.7% |
| Money recovered | Rs 155,297 | Rs 472,599 | Rs 317,302 |
| Actions spent | 163 | 102 | -61 |
| **Wasted actions** | 36 | 15 | -21 |
| **Compliance violations** | 21 | 0 | -21 |

Money at risk in this split: **Rs 574,984**.

Wasted actions are money actions spent on payments that could never have been recovered. Compliance violations are retries against revoked mandates or suspected fraud — the baseline commits 21, Failsafe commits 0 because the policy layer stops them before any action is taken.

## Diagnosis quality

The LLM's only job is naming the archetype. Measured against the keyword-lookup baseline on the same 60 payments:

| | Keyword lookup | Model | 
|---|---:|---:|
| All payments | 75.0% | 80.0% |
| Generic error string (18 payments) | 16.7% | 33.3% |

The second row is the one that matters: those payments carry an error string with no answer in it, so the archetype has to come from context. That is where a model earns its place, and where a lookup table cannot follow.

Most common misdiagnoses (truth -> predicted):

- `3DS_ABANDON` -> `INSUFFICIENT_FUNDS` x5
- `EXPIRED_CARD` -> `INSUFFICIENT_FUNDS` x2
- `EXPIRED_CARD` -> `BANK_DOWNTIME` x2
- `3DS_ABANDON` -> `BANK_DOWNTIME` x1
- `INSUFFICIENT_FUNDS` -> `BANK_DOWNTIME` x1
- `INSUFFICIENT_FUNDS` -> `MANDATE_REVOKED` x1

## Recovery by archetype

| Archetype | Payments | Recovered | Rate | Actions |
|---|---:|---:|---:|---:|
| 3DS_ABANDON | 16 | 10 | 62% | 32 |
| BANK_DOWNTIME | 13 | 12 | 92% | 18 |
| INSUFFICIENT_FUNDS | 13 | 9 | 69% | 28 |
| EXPIRED_CARD | 11 | 4 | 36% | 24 |
| MANDATE_REVOKED | 5 | 0 | 0% | 0 |
| SUSPECTED_FRAUD | 2 | 0 | 0% | 0 |

`MANDATE_REVOKED` and `SUSPECTED_FRAUD` recover 0% by design. Those are not failures of the agent — they are the cases where the correct action is to stop and hand over to a human.

## Exception list

Every payment Failsafe did not recover (25 of 60), grouped by why it stopped. Nothing here is hidden or hand-filtered.

| Stop reason | Count | Money left on the table |
|---|---:|---:|
| stopped: attempt budget spent | 15 | Rs 33,032 |
| stopped: mandate revoked (re-auth needed) | 6 | Rs 58,974 |
| stopped: window expired | 2 | Rs 6,411 |
| stopped: suspected fraud (human queue) | 2 | Rs 3,968 |

<details><summary>Full unrecovered list</summary>

| Payment | Amount | True archetype | Diagnosed | Stop reason |
|---|---:|---|---|---|
| `pay_FS0032` | Rs 128 | 3DS_ABANDON | BANK_DOWNTIME (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0046` | Rs 3,034 | INSUFFICIENT_FUNDS | INSUFFICIENT_FUNDS | stopped: window expired |
| `pay_FS0010` | Rs 3,445 | BANK_DOWNTIME | BANK_DOWNTIME | stopped: attempt budget spent |
| `pay_FS0088` | Rs 299 | MANDATE_REVOKED | MANDATE_REVOKED | stopped: mandate revoked (re-auth needed) |
| `pay_FS0139` | Rs 3,377 | EXPIRED_CARD | INSUFFICIENT_FUNDS (misdiagnosed) | stopped: window expired |
| `pay_FS0009` | Rs 694 | EXPIRED_CARD | BANK_DOWNTIME (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0054` | Rs 2,017 | SUSPECTED_FRAUD | SUSPECTED_FRAUD | stopped: suspected fraud (human queue) |
| `pay_FS0192` | Rs 499 | MANDATE_REVOKED | MANDATE_REVOKED | stopped: mandate revoked (re-auth needed) |
| `pay_FS0152` | Rs 499 | MANDATE_REVOKED | MANDATE_REVOKED | stopped: mandate revoked (re-auth needed) |
| `pay_FS0078` | Rs 7,122 | EXPIRED_CARD | EXPIRED_CARD | stopped: attempt budget spent |
| `pay_FS0030` | Rs 598 | 3DS_ABANDON | INSUFFICIENT_FUNDS (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0171` | Rs 909 | INSUFFICIENT_FUNDS | BANK_DOWNTIME (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0037` | Rs 230 | EXPIRED_CARD | EXPIRED_CARD | stopped: attempt budget spent |
| `pay_FS0012` | Rs 2,444 | 3DS_ABANDON | 3DS_ABANDON | stopped: attempt budget spent |
| `pay_FS0143` | Rs 3,203 | 3DS_ABANDON | INSUFFICIENT_FUNDS (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0184` | Rs 979 | EXPIRED_CARD | INSUFFICIENT_FUNDS (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0105` | Rs 1,660 | EXPIRED_CARD | EXPIRED_CARD | stopped: attempt budget spent |
| `pay_FS0166` | Rs 686 | EXPIRED_CARD | BANK_DOWNTIME (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0101` | Rs 6,082 | INSUFFICIENT_FUNDS | MANDATE_REVOKED (misdiagnosed) | stopped: mandate revoked (re-auth needed) |
| `pay_FS0180` | Rs 51,396 | MANDATE_REVOKED | MANDATE_REVOKED | stopped: mandate revoked (re-auth needed) |
| `pay_FS0051` | Rs 1,317 | INSUFFICIENT_FUNDS | INSUFFICIENT_FUNDS | stopped: attempt budget spent |
| `pay_FS0181` | Rs 8,758 | 3DS_ABANDON | 3DS_ABANDON | stopped: attempt budget spent |
| `pay_FS0151` | Rs 859 | 3DS_ABANDON | INSUFFICIENT_FUNDS (misdiagnosed) | stopped: attempt budget spent |
| `pay_FS0112` | Rs 1,951 | SUSPECTED_FRAUD | SUSPECTED_FRAUD | stopped: suspected fraud (human queue) |
| `pay_FS0062` | Rs 199 | MANDATE_REVOKED | MANDATE_REVOKED | stopped: mandate revoked (re-auth needed) |

</details>
