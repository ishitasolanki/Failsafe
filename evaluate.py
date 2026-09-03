"""Measurement. This file is the spine of the submission.

Runs both strategies over the same payments and reports, without cherry-picking:

  - recovery rate and rupees recovered, Failsafe vs the naive baseline
  - diagnosis accuracy, LLM vs the keyword-lookup baseline, so the model's
    actual contribution is visible rather than assumed
  - wasted actions: money actions spent on payments that were never recoverable.
    This is the false-positive cost and it is reported next to the win, not
    buried under it.
  - compliance violations: actions taken on revoked mandates or suspected fraud,
    which must be zero for Failsafe and are not for the baseline
  - the full exception list: every payment left unrecovered, with the reason

Everything prints to the console and is written to report.md.
"""

import argparse
import sys
from collections import Counter

import agent
import diagnose as diagnosis
import seed as seed_module
from policy import TERMINAL_ARCHETYPES

STOP_REASON_LABELS = {
    "recovered": "recovered",
    "terminal:MANDATE_REVOKED": "stopped: mandate revoked (re-auth needed)",
    "terminal:SUSPECTED_FRAUD": "stopped: suspected fraud (human queue)",
    "attempts_exhausted": "stopped: attempt budget spent",
    "awaiting_human_approval": "held: above the high-value approval gate",
    "human_declined": "stopped: human reviewed and declined",
    "terminal:FAILURE_BURST": "stopped: abnormal failure burst (human queue)",
    "opted_out": "stopped: customer opted out",
    "unknown_archetype": "stopped: diagnosis off-menu, failed closed",
    "scheduled_beyond_horizon": "stopped: right moment falls outside the window",
    "horizon_expired": "stopped: window expired",
}


def is_unrecoverable(payment):
    """True when no action, at any attempt, could ever have worked.

    Read straight from the pre-rolled world, so this is the honest denominator
    for wasted effort rather than an estimate.
    """
    return not any(payment["_world"].values())


def summarise(payments, outcomes, strategy):
    by_id = {p["id"]: p for p in payments}
    recovered = [pid for pid, o in outcomes.items() if o["recovered"]]
    total_actions = sum(o["actions"] for o in outcomes.values())

    wasted = sum(
        o["actions"] for pid, o in outcomes.items() if is_unrecoverable(by_id[pid])
    )
    violations = sum(
        o["actions"]
        for pid, o in outcomes.items()
        if by_id[pid]["_archetype"] in TERMINAL_ARCHETYPES
    )
    money = sum(by_id[pid]["amount"] for pid in recovered)
    at_risk = sum(p["amount"] for p in payments)

    return {
        "strategy": strategy,
        "payments": len(payments),
        "recovered": len(recovered),
        "recovery_rate": len(recovered) / len(payments) if payments else 0.0,
        "money_recovered": money,
        "money_at_risk": at_risk,
        "actions": total_actions,
        "wasted_actions": wasted,
        "compliance_violations": violations,
        "actions_per_recovery": total_actions / len(recovered) if recovered else None,
    }


def diagnosis_accuracy(payments, cache):
    """Score the LLM/cache diagnosis and the keyword baseline on the same rows."""
    llm_right = rules_right = 0
    llm_right_generic = rules_right_generic = generic_total = 0
    confusion = Counter()
    methods = Counter()

    for payment in payments:
        truth = payment["_archetype"]
        model = diagnosis.diagnose(payment, cache)
        rules = diagnosis.rules_diagnose(payment)
        methods[model["method"]] += 1

        if model["archetype"] == truth:
            llm_right += 1
        else:
            confusion[(truth, model["archetype"])] += 1
        if rules["archetype"] == truth:
            rules_right += 1

        if payment["_error_is_generic"]:
            generic_total += 1
            llm_right_generic += model["archetype"] == truth
            rules_right_generic += rules["archetype"] == truth

    total = len(payments)
    return {
        "total": total,
        "model_accuracy": llm_right / total if total else 0.0,
        "rules_accuracy": rules_right / total if total else 0.0,
        "generic_total": generic_total,
        "model_accuracy_generic": llm_right_generic / generic_total if generic_total else 0.0,
        "rules_accuracy_generic": rules_right_generic / generic_total if generic_total else 0.0,
        "confusion": confusion,
        "methods": methods,
    }


def rupees(paise):
    return f"Rs {paise / 100:,.0f}"


def build_report(split, failsafe, naive, accuracy, exceptions, by_archetype, provider):
    lines = []
    add = lines.append

    add(f"# Failsafe - evaluation report ({split} split)\n")
    add(f"Diagnosis source: **{provider}**. "
        f"Outcomes come from the pre-rolled world in `seed.py`; both strategies "
        f"face the identical world, so the comparison is strategy alone.\n")

    add("## Headline\n")
    add("| Metric | Naive retry (baseline) | Failsafe | Delta |")
    add("|---|---:|---:|---:|")
    add(f"| Payments | {naive['payments']} | {failsafe['payments']} | |")
    add(f"| Recovered | {naive['recovered']} | {failsafe['recovered']} | "
        f"{failsafe['recovered'] - naive['recovered']:+d} |")
    add(f"| Recovery rate | {naive['recovery_rate']:.1%} | "
        f"{failsafe['recovery_rate']:.1%} | "
        f"{failsafe['recovery_rate'] - naive['recovery_rate']:+.1%} |")
    add(f"| Money recovered | {rupees(naive['money_recovered'])} | "
        f"{rupees(failsafe['money_recovered'])} | "
        f"{rupees(failsafe['money_recovered'] - naive['money_recovered'])} |")
    add(f"| Actions spent | {naive['actions']} | {failsafe['actions']} | "
        f"{failsafe['actions'] - naive['actions']:+d} |")
    add(f"| **Wasted actions** | {naive['wasted_actions']} | "
        f"{failsafe['wasted_actions']} | "
        f"{failsafe['wasted_actions'] - naive['wasted_actions']:+d} |")
    add(f"| **Compliance violations** | {naive['compliance_violations']} | "
        f"{failsafe['compliance_violations']} | "
        f"{failsafe['compliance_violations'] - naive['compliance_violations']:+d} |")
    add(f"\nMoney at risk in this split: **{rupees(failsafe['money_at_risk'])}**.\n")

    add("Wasted actions are money actions spent on payments that could never "
        "have been recovered. Compliance violations are retries against revoked "
        "mandates or suspected fraud — the baseline commits "
        f"{naive['compliance_violations']}, Failsafe commits "
        f"{failsafe['compliance_violations']} because the policy layer stops "
        "them before any action is taken.\n")

    add("## Diagnosis quality\n")
    add(f"The LLM's only job is naming the archetype. Measured against the "
        f"keyword-lookup baseline on the same {accuracy['total']} payments:\n")
    add("| | Keyword lookup | Model | ")
    add("|---|---:|---:|")
    add(f"| All payments | {accuracy['rules_accuracy']:.1%} | "
        f"{accuracy['model_accuracy']:.1%} |")
    add(f"| Generic error string ({accuracy['generic_total']} payments) | "
        f"{accuracy['rules_accuracy_generic']:.1%} | "
        f"{accuracy['model_accuracy_generic']:.1%} |")
    add("\nThe second row is the one that matters: those payments carry an error "
        "string with no answer in it, so the archetype has to come from context. "
        "That is where a model earns its place, and where a lookup table cannot "
        "follow.\n")

    if accuracy["confusion"]:
        add("Most common misdiagnoses (truth -> predicted):\n")
        for (truth, predicted), count in accuracy["confusion"].most_common(6):
            add(f"- `{truth}` -> `{predicted}` x{count}")
        add("")

    add("## Recovery by archetype\n")
    add("| Archetype | Payments | Recovered | Rate | Actions |")
    add("|---|---:|---:|---:|---:|")
    for archetype, row in sorted(by_archetype.items(), key=lambda kv: -kv[1]["n"]):
        rate = row["recovered"] / row["n"] if row["n"] else 0
        add(f"| {archetype} | {row['n']} | {row['recovered']} | {rate:.0%} | "
            f"{row['actions']} |")
    add("\n`MANDATE_REVOKED` and `SUSPECTED_FRAUD` recover 0% by design. Those "
        "are not failures of the agent — they are the cases where the correct "
        "action is to stop and hand over to a human.\n")

    add("## Exception list\n")
    add(f"Every payment Failsafe did not recover ({len(exceptions)} of "
        f"{failsafe['payments']}), grouped by why it stopped. Nothing here is "
        "hidden or hand-filtered.\n")
    grouped = Counter(e["stop_reason"] for e in exceptions)
    add("| Stop reason | Count | Money left on the table |")
    add("|---|---:|---:|")
    for reason, count in grouped.most_common():
        money = sum(e["amount"] for e in exceptions if e["stop_reason"] == reason)
        add(f"| {STOP_REASON_LABELS.get(reason, reason)} | {count} | {rupees(money)} |")
    add("")
    add("<details><summary>Full unrecovered list</summary>\n")
    add("| Payment | Amount | True archetype | Diagnosed | Stop reason |")
    add("|---|---:|---|---|---|")
    for e in exceptions:
        flag = "" if e["archetype"] == e["truth"] else " (misdiagnosed)"
        add(f"| `{e['id']}` | {rupees(e['amount'])} | {e['truth']} | "
            f"{e['archetype']}{flag} | {STOP_REASON_LABELS.get(e['stop_reason'], e['stop_reason'])} |")
    add("\n</details>")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Measure Failsafe against the baseline.")
    parser.add_argument("--split", choices=["train", "heldout", "all"], default="heldout")
    parser.add_argument("--out", default="report.md")
    args = parser.parse_args()

    payments = seed_module.load()
    if args.split != "all":
        payments = [p for p in payments if p["split"] == args.split]

    provider = diagnosis.llm.active_provider()
    cache = diagnosis.load_cache()
    cached = sum(1 for p in payments if diagnosis._fingerprint(p) in cache)
    if cached == len(payments):
        source = "committed cache (no API calls, fully reproducible)"
    elif provider:
        source = f"live {provider} calls, cached for reproducibility"
    else:
        source = "keyword rules only - NO LLM KEY SET, so the model row below is the baseline"

    failsafe_outcomes = agent.run_batch(payments, "failsafe", f"eval_{args.split}")
    naive_outcomes = agent.run_batch(payments, "naive", f"eval_{args.split}_naive")

    failsafe = summarise(payments, failsafe_outcomes, "failsafe")
    naive = summarise(payments, naive_outcomes, "naive")
    accuracy = diagnosis_accuracy(payments, cache)
    diagnosis.save_cache(cache)

    by_archetype = {}
    exceptions = []
    for payment in payments:
        outcome = failsafe_outcomes[payment["id"]]
        row = by_archetype.setdefault(
            payment["_archetype"], {"n": 0, "recovered": 0, "actions": 0}
        )
        row["n"] += 1
        row["recovered"] += outcome["recovered"]
        row["actions"] += outcome["actions"]
        if not outcome["recovered"]:
            exceptions.append({
                "id": payment["id"],
                "amount": payment["amount"],
                "truth": payment["_archetype"],
                "archetype": outcome["archetype"],
                "stop_reason": outcome["stop_reason"],
            })

    report = build_report(
        args.split, failsafe, naive, accuracy, exceptions, by_archetype, source
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report + "\n")

    # Console summary. Deliberately shows the bad numbers next to the good ones.
    print(f"split          {args.split} ({len(payments)} payments)")
    print(f"diagnosis      {source}\n")
    print(f"{'':<26}{'naive':>12}{'failsafe':>12}")
    print(f"{'recovered':<26}{naive['recovered']:>12}{failsafe['recovered']:>12}")
    print(f"{'recovery rate':<26}{naive['recovery_rate']:>11.1%}{failsafe['recovery_rate']:>12.1%}")
    print(f"{'money recovered':<26}{rupees(naive['money_recovered']):>12}{rupees(failsafe['money_recovered']):>12}")
    print(f"{'actions spent':<26}{naive['actions']:>12}{failsafe['actions']:>12}")
    print(f"{'wasted actions':<26}{naive['wasted_actions']:>12}{failsafe['wasted_actions']:>12}")
    print(f"{'compliance violations':<26}{naive['compliance_violations']:>12}{failsafe['compliance_violations']:>12}")
    print(f"\ndiagnosis accuracy   rules {accuracy['rules_accuracy']:.1%}   "
          f"model {accuracy['model_accuracy']:.1%}")
    print(f"  on generic errors  rules {accuracy['rules_accuracy_generic']:.1%}   "
          f"model {accuracy['model_accuracy_generic']:.1%}   "
          f"({accuracy['generic_total']} payments)")
    print(f"\nunrecovered    {len(exceptions)} payments, see {args.out}")
    print(f"report written to {args.out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
