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

**Top left - one box.** Type what you just heard, whoever called and whatever it was about. A classifier decides whether it is a booking or a disruption and hands it to the right agent, showing which it chose and why; one click overrides it. There were two boxes here and the first person to use the board typed a broken van into the one that books appointments.

Under it, **Fix the day**: the priced ways out of whatever just went wrong. Two or three options rather than one answer, each with how many customers would need telling and whether the rules allow it to apply without review.

**Top right - crews.** Who is available, and underneath, how hard the week is working each of them. On-site percentage and idle minutes are the two figures that make a technically valid but obviously wrong plan look wrong, and neither is something an invariant check can judge.

**Below, the full width of the window - the week.** Days across, time down, work as blocks, on a light surface so the one thing with real information density is the bright object on the screen. Colour carries commitment state, because "can this move?" is the question asked of every block. Click one for the detail the grid cannot show at a glance.

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
