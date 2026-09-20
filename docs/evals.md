# Evals

```bash
make eval-offline      # tiers 0 and 3; no model, no credentials, no spend
make eval              # everything; model tiers skip if no model is configured
make eval-baseline     # record the current scores as the regression baseline
```

## Tiers

| Tier | Measures | Gate |
|---|---|---|
| 0 | Solver invariants, and that the checker still catches breaches | 100% |
| 1 | Extraction fidelity, field by field against gold labels | 85% |
| 2 | Action correctness: right events, right targets, offered strategy | 90% |
| 3 | End-to-end scenario outcomes | 95% |
| 4 | Customer messages: grounded first, readable second | 70% |

Tier 0 is absolute because the point of an invariant is that it holds. The others sit
below 1.0 deliberately: a suite that only passes at perfection stops being a gate and
becomes something people disable.

## Tier 0 has two halves, and the second one matters

The obvious half solves each scenario and validates the result. That proves the solver
agrees with the checker.

It says nothing about the checker. Removing a check produces a *missing* violation, not
a detected one — so a checker returning no violations at all scores a hundred percent.
This was not hypothetical: deleting the certification check and re-running the gate,
it passed.

So the second half feeds the checker plans that are deliberately wrong — a crew sent to
work it is not certified for, an arrival two hours before any drive could deliver it, a
two-hour install allotted one minute — and requires the specific violation each should
raise. Removing any of three checks now fails the gate with a message naming what went
unreported.

## Two gates, not one

Thresholds catch something being broken. Baseline comparison catches something
*sliding* while still technically passing, which is how quality usually degrades: not
in one visible step but a series of small ones nobody objected to. Movement under 2% is
ignored, because a gate that fires on sampling noise gets muted, and then it is not a
gate.

## What the datasets are, and are not

The call transcripts and disruption notes in `datasets/` were written by the same person
who wrote the prompts they test. That makes them a **regression suite**, not evidence
the system reads real calls well. They catch drift and they compare models fairly
against each other; they do not tell you how it behaves on a Tuesday morning with a real
customer.

Replacing them with real transcripts, labelled by someone at the business, is the single
highest-value thing that could happen to this suite. The report says so on every run.

## Asserting outcomes, not prose

No scenario passes because wording matched. Each asserts what happened to the schedule —
which jobs were served, which promises held, whether anyone needs a phone call — with
bounds rather than exact numbers, so a legitimate improvement is not a test failure.

Scoring text against text produces a number that moves when a prompt is reworded and
sits still when the system gets worse, which is backwards for a gate.

## Health signals

The report carries mean repair attempts and escalation rate. With a small model, a
rising repair rate means it is drifting from what the prompt and schema expect, long
before a schedule looks wrong. Provider failure is tracked separately from bad output —
an unreachable endpoint is an operator's problem, a malformed answer is a prompt's.

## Measured: Nova Lite, live

The first live run against `us.amazon.nova-lite-v1:0`, and what it changed.

| Tier | Score | Gate |
|---|---|---|
| 0 invariants | 100% (15 cases) | 100% |
| 1 extraction | 94.8% (22 cases) | 85% |
| 2 action | 100% (11 cases) | 90% |
| 3 scenario | 100% (8 cases) | 95% |
| 4 quality | 100% (1 case) | 70% |

Fifty-seven cases in about 42 seconds, escalation rate zero. Nova Lite is comfortably
good enough for every task it is given here - which is the answer the `LLMProvider`
abstraction was built to make cheap to obtain, and it took one command rather than an
argument.

### Four things the live run found

**Nova Lite rejects `strict` on a tool specification.** The field is in the Converse
API shape, which is where I read it from; supporting it is a per-model matter.
`ValidationException: This model doesn't support the strict field.` Availability in the
shape is not support by the model, and only a live call shows the difference. Strict
tools are now an opt-in allow-list, and the validate-and-repair loop carries the weight
instead - which is what it was for.

**Tier 4 had been scoring nothing at all.** No scenario in the library confirmed a
customer window, so nothing was ever customer-visible, so no message was ever drafted.
A tier that never runs cannot fail - the same shape of gap as tier 0 before the
detection cases. There is now a scenario where a promise genuinely cannot be kept.

**A released promise was something the solver could do silently.** Given the chance,
the model moved a confirmed appointment and the checker rejected the result, because
it had no way to know a release had been authorised. Breaking a promise is now off by
default, available only in repair, reported on the candidate, and accepted by the
checker only when explicitly passed in. It cannot be something a solver does quietly
and a checker infers.

**The model's "unnecessary" questions were right.** It kept asking when things
happened, against an instruction not to. An unstated time defaulted to 08:00, so a van
reported off the road at two in the afternoon was recorded as unavailable since
breakfast, retroactively invalidating the work it had already done. The default is now
the moment the note was typed.

Those three or four question cases still fail, and are left failing. Two prompt
revisions did not move them and the extracted events are always correct, so it is a
characteristic of the model rather than a defect to tune away. Continuing to adjust
prompts against twenty-two cases written by the same person who wrote the prompts is
how a suite stops measuring anything.
