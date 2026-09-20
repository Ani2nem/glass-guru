# The dispatch board

```bash
make web-install        # once
make web-build          # build the bundle
glass-guru init         # once, if there is no workspace
make api                # http://127.0.0.1:8000
```

For frontend work, `make dev` runs the API on :8000 and Vite on :5173 with hot
reload; Vite proxies `/api` across.

## Layout

Three columns, because a dispatcher is doing three things at once.

**Left - the phone.** Intake takes call notes and fills a job while the customer is
still talking. What it shows in two halves is the point: what was captured, and what
still has to be asked. A silently half-filled form discovered after the call is worse
than no form. Below it, disruptions: type what you heard, review the typed events, then
record them. Nothing an agent extracts is stored until a person agrees to it.

**Middle - the plan.** A Gantt per crew per day, or the same routes on a map. Bar
colour carries commitment state, because "can this move?" is the question asked of
every bar. Each row ends with on-site percentage and idle minutes - the two figures
that make a technically valid but obviously wrong plan look wrong, and neither is
something an invariant check can judge.

**Right - proposals.** Repair candidates, each with how many customers would need
telling and whether the rules allow it to apply without review. Several priced options
rather than one answer, because choosing between "keep every promise and serve less"
and "serve more and make two calls" is a judgement about this business today.

## Things the board deliberately makes hard

The calibration banner is always visible. Every cost on screen rests on parameters
nobody has validated, and that belongs in front of the reader rather than in a config
file they will never open.

A customer-visible repair cannot be applied without an explicit override. The button
says "Approve and apply" rather than "Apply", and the autonomy verdict beside it comes
from deterministic rules, not from the agent that ranked the options.

A customer message that states a time the plan does not support is shown held, in red,
with the offending phrase named. A plausible wrong time in a text is worse than no
text: the customer believes it and arranges their day around it.

## Live updates

The board subscribes to server-sent events, so a change made from the CLI, an agent, or
another tab appears without a refresh. Slow subscribers are dropped rather than allowed
to apply back-pressure - a tab left open on a sleeping laptop must not stall a solve.
