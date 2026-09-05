# 5-minute pitch video — script and shot list

You have `demo-walkthrough.webm` — 1:45 of the dashboard, recorded automatically.
It has **no audio**. Narrate over it.

Total target: **4:45–5:00**. Three segments.

---

## Before you record

1. Start the dashboard: `uvicorn app:app --reload` → open `http://127.0.0.1:8078`
2. Open a terminal in the project folder, font size up (Ctrl+Shift+`+`), window
   maximised.
3. Close Slack, WhatsApp, email. A notification popup mid-recording means a retake.
4. **Read `policy.py` once.** You have to be able to answer "why doesn't the LLM
   pick the action" without hesitating.

**Recording tool:** Windows has one built in — `Win + Alt + R` (Xbox Game Bar).
Free alternative with better control: OBS Studio. Record at 1080p.

**Do a 30-second test recording first** and play it back. Check your mic actually
captured. More pitch videos are ruined by silent audio than by anything else.

---

## Segment 1 — the problem (0:00–0:45)

**Screen:** you, or a static title card. Not the dashboard yet.

> "When a payment fails on Razorpay, the merchant loses the sale and nobody
> tells them. The money just doesn't arrive.
>
> Most merchants do one of two things: nothing, or retry everything tomorrow.
> Both are wrong, because payments don't fail for one reason — they fail for
> six, and each one needs a different response at a different time.
>
> If the customer had no money, retrying tomorrow is pointless — you want the
> 1st, when salary lands. If their card expired, retrying is pointless forever.
> And if their subscription mandate was revoked, retrying isn't just useless —
> it's a compliance violation.
>
> That last one is why a smarter retry loop isn't enough. Some payments must
> never be retried, and a system that can't tell the difference is dangerous,
> not just ineffective.
>
> So I built Failsafe."

---

## Segment 2 — the demo (0:45–3:15)

**Screen:** play `demo-walkthrough.webm`. Timings below are from the start of
the video file, so add 45s for video position.

| Video time | What's on screen | Say this |
|---|---|---|
| 0:00 | Hero — "It knows when not to retry" | "Failsafe diagnoses why each payment failed, picks a bounded recovery action, and stops when stopping is the right answer." |
| 0:06 | Four stat tiles | "Measured on 60 held-out payments I never looked at while building, against a naive-retry baseline. 35 recovered against 10. Four lakh seventy-two thousand recovered against one fifty-five. And it did that in **fewer** actions — 102 against 163." |
| 0:18 | Still on tiles | "The number I care about most is the last one. Zero compliance violations. The baseline commits 21." |
| 0:25 | Six-cause table | "This is why. Six causes, six different correct answers. The bottom two in red are the ones where the right move is to do nothing at all." |
| 0:45 | Cost panel | "And I report what it costs. 15 wasted actions — money actions spent on payments nothing could have saved. That's the false-positive cost, and it sits next to the win, not under it." |
| 0:58 | Recovery tab | "Recovery per cause, against the baseline, over an identical pre-rolled world — every outcome was decided before either strategy ran, so neither can get lucky." |
| 1:10 | Terminal archetypes | "Mandate revoked and suspected fraud recover zero percent **by design**. Those aren't failures. Stopping is the correct answer." |
| 1:22 | Diagnosis tab | "The LLM has exactly one job: name the cause. So I measured whether it beats a keyword lookup on the error string." |
| 1:30 | The two bars | "80 percent against 75 overall. But look at the second row — on the payments whose error just says *'Payment failed, please try again'*, the lookup gets 16.7 percent and the model gets 33.3. It doubles it, because it reads the context: how many others failed at that bank in the last hour, the customer's history. It's still only a third, and I'd rather say that than round it up." |
| 1:48 | Audit tab, FS0038 opens | "Every money action is explainable. This payment failed on the 13th of August — insufficient funds." |
| 2:00 | The timestamps | "Watch the timestamps. The rule `retry_at_salary_cycle` fires, and the agent waits **nineteen days** for the 1st of September. Then attempt one fails, attempt two fails, attempt three captures six thousand rupees. A blind retry loop would have burned all three attempts in August against an empty account." |
| 2:18 | Mandate revoked payment | "And here's the opposite. Mandate revoked — diagnosed, terminal rule fires, escalated to a human. One step. No retry, ever." |
| 2:30 | How It Works tab | "Here's the design decision the whole project rests on." |

---

## Segment 3 — the argument and what broke (3:15–4:45)

**Screen:** stay on the How It Works tab, or switch to `policy.py` in your editor.

> "**The LLM classifies. It never chooses the money action.**
>
> It picks one of six causes. A fixed table maps that cause to an action, and
> then every action has to clear seven deterministic gates — three attempts
> maximum, no messaging between 9pm and 9am, anything over fifty thousand
> rupees waits for a human.
>
> The consequence is the point: a misdiagnosis costs a wasted retry. It can
> never cost a compliance breach or a double charge, because those are bounded
> by rules the model doesn't reach.
>
> And I learned the harder version of that the hard way."

**[This next part is what the form asks for. Don't skip it.]**

> "My first measured run showed **nine compliance violations** — even though the
> policy refuses to retry revoked mandates. The policy was fine. The problem was
> that a fraud case misdiagnosed as insufficient funds arrives at the policy
> layer already wearing the wrong label, so the terminal check had nothing to
> fire on.
>
> So safety couldn't sit downstream of the model. Two guards now run without
> consulting the diagnosis at all: an abnormal burst of failures stops a payment
> whatever was diagnosed, and a recurring payment has its mandate **looked up**
> rather than guessed — because mandate status is a fact you can check, and
> guessing at a fact you can check is indefensible. Nine to zero.
>
> The other one I'll admit: at one point my agent recovered *less money* than
> the dumb baseline. Three times the recovery rate, a third of the money. I'd
> built the fifty-thousand-rupee approval gate as a terminal stop, so every
> large payment got escalated to a human and then just abandoned. The gate was
> right; treating escalation as a bin was wrong.
>
> I only caught it because the report prints the baseline next to my own number.
> If I'd only measured myself, I'd have shipped 'three times the recovery rate'
> and never noticed I was losing money."

**Optional, if you're under time — the honest limitation. It scores well.**

> "One thing I'll say plainly: the payments are synthetic, and I wrote the
> simulator that decides whether a recovery works. So the absolute rupee figure
> isn't real. What is real: both strategies face the identical pre-rolled world,
> so the comparison is fair — and the safety results don't depend on the
> simulation at all. A revoked mandate is never retried regardless of any
> probability I chose."

**Close:**

> "Everything reproduces with zero API keys — the diagnoses are cached and
> committed, so you can clone it and get my exact numbers. Twenty-four tests
> cover the stopping rules. Thanks for watching."

---

## What to cut if you run long

1. The second "what broke" story (the money one) — keep the compliance one
2. The synthetic-data limitation paragraph
3. Slow down less on the diagnosis tab

**Never cut:** zero compliance violations, the nineteen-day wait, and the
first "what broke" story. Those three are the submission.

---

## Common mistakes

- **Don't read this script word for word.** Know the beats, say it your way. A
  read-aloud script is audible and it reads as someone else's work.
- Don't apologise for the numbers. 33.3% is a finding, not a failure.
- Don't say "we" if it's just you.
- If you fluff a line, pause two seconds and say it again — easy to cut, and
  much faster than restarting the take.
