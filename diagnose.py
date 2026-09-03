"""Failure diagnosis: which archetype is this failed payment?

The LLM does one job here and only one: read the observable facts and name the
archetype. It never chooses the recovery action — policy.py does that from the
archetype. That split is deliberate. A misclassification costs a wasted action;
it can never cost a compliance breach or a double charge, because every action
still has to clear the deterministic rules.

Three ways an archetype can be produced, in order of preference:
  1. cache   - a previous LLM answer, committed to the repo so anyone can
               reproduce the reported numbers with no API key at all
  2. llm     - a live provider call, which then populates the cache
  3. rules   - keyword lookup on the error string; the honest fallback, and
               also the baseline the LLM is measured against

`rules` is not a hidden safety net dressed up as AI: evaluate.py reports LLM and
rules accuracy side by side, so the contribution of the model is visible.
"""

import hashlib
import json
import os

from policy import (
    ARCHETYPES,
    BANK_DOWNTIME,
    EXPIRED_CARD,
    INSUFFICIENT_FUNDS,
    MANDATE_REVOKED,
    SUSPECTED_FRAUD,
    THREEDS_ABANDON,
)
from seed import observable

import llm

CACHE_PATH = "diagnosis_cache.json"
UNKNOWN = "UNKNOWN"

# Bump when the prompt changes, so stale answers are never silently reused.
PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You are a payments failure analyst for an Indian merchant on Razorpay.

Classify one failed payment into exactly one recovery archetype:

INSUFFICIENT_FUNDS - customer lacked balance. Signals: balance/funds wording, \
repeat recent failures by the same customer, larger-than-usual amount, \
month-end timing.
BANK_DOWNTIME - the bank or gateway was failing, not the customer. Signals: \
bank/gateway error source, and above all a high bank_failure_count_last_hour \
(many other customers failing at the same bank in the same hour).
EXPIRED_CARD - the card is no longer valid. Card method only.
3DS_ABANDON - customer did not finish the OTP / 3D Secure step. Card method, \
authentication step, timeout wording.
MANDATE_REVOKED - a subscription mandate was cancelled. emandate method, \
subscription payments.
SUSPECTED_FRAUD - risk or issuer decline with an abnormal burst of recent \
failures from this customer.

Many error descriptions are generic and carry no answer. For those, decide from \
context: method, bank_failure_count_last_hour, prior_failures_30d, amount, \
is_subscription, time of day.

Reply with JSON only, no prose:
{"archetype": "<one of the six>", "confidence": <0.0-1.0>, "reason": "<max 20 words>"}"""


def _fingerprint(payment: dict) -> str:
    """Cache key over exactly the facts the model sees, plus prompt and model."""
    facts = json.dumps(observable(payment), sort_keys=True)
    provider = llm.active_provider() or "none"
    model = llm.model_name(provider) if provider != "none" else "none"
    blob = f"{PROMPT_VERSION}|{model}|{facts}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_cache(path=CACHE_PATH):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_cache(cache, path=CACHE_PATH):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=1, sort_keys=True)


def rules_diagnose(payment: dict) -> dict:
    """Keyword lookup on the error string. The baseline, and the fallback.

    On specific error strings this does well. On the ~third of payments with a
    generic error it has nothing to go on and must guess, which is precisely
    where a model that reads context should pull ahead.
    """
    description = payment["error_description"].lower()
    if "insufficient" in description or "balance" in description:
        archetype = INSUFFICIENT_FUNDS
    elif "bank" in description and "end" in description:
        archetype = BANK_DOWNTIME
    elif "not responding" in description:
        archetype = BANK_DOWNTIME
    elif "expired" in description:
        archetype = EXPIRED_CARD
    elif "3d secure" in description or "not completed on time" in description:
        archetype = THREEDS_ABANDON
    elif "mandate" in description:
        archetype = MANDATE_REVOKED
    elif "risk" in description:
        archetype = SUSPECTED_FRAUD
    else:
        # Generic error, no signal in the string. Guess the base rate.
        archetype = INSUFFICIENT_FUNDS
    return {
        "archetype": archetype,
        "confidence": 0.5,
        "reason": "keyword match on error description",
        "method": "rules",
    }


def _parse(raw: str) -> dict | None:
    """Pull the JSON object out of a model reply that may be wrapped in prose."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def diagnose(payment: dict, cache: dict, allow_llm=True) -> dict:
    """Return {archetype, confidence, reason, method}. Never raises."""
    key = _fingerprint(payment)
    if key in cache:
        cached = dict(cache[key])
        cached["method"] = "cache"
        return cached

    if not (allow_llm and llm.available()):
        return rules_diagnose(payment)

    facts = json.dumps(observable(payment), indent=1)
    try:
        parsed = _parse(llm.complete(SYSTEM_PROMPT, facts))
    except Exception as error:                      # network, rate limit, 5xx
        result = rules_diagnose(payment)
        result["reason"] = f"llm unavailable ({type(error).__name__}), used rules"
        return result

    if not parsed or parsed.get("archetype") not in ARCHETYPES:
        # Off-menu answer. Do not coerce it into something plausible — hand back
        # UNKNOWN and let policy.decide() fail closed to a human.
        return {
            "archetype": UNKNOWN,
            "confidence": 0.0,
            "reason": "model returned an unrecognised archetype",
            "method": "llm",
        }

    result = {
        "archetype": parsed["archetype"],
        "confidence": float(parsed.get("confidence", 0.5)),
        "reason": str(parsed.get("reason", ""))[:200],
        "method": "llm",
    }
    cache[key] = {k: result[k] for k in ("archetype", "confidence", "reason")}
    return result
