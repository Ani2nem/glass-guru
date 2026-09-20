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
