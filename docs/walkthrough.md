# Trying it by hand

Every command here was run once before it was written down.
Where output is shown, that is the real output, trimmed.

## Setup

```bash
make install        # venv and dependencies
make web-install    # the board's dependencies
make web-build      # the board's bundle, served by the API
```

Travel times come from a committed snapshot, so nothing below needs the network or a routing backend.
Only the two agent steps need AWS credentials; everything else runs in a fresh clone.

```bash
export GLASS_GURU_TRAVEL=frozen
export AWS_PROFILE=glass-guru AWS_REGION=us-east-1   # for the agent steps only
```

## In the browser

```bash
.venv/bin/glass-guru init     # seed the sample business, once
make dev                      # board on :5173, API on :8000
```

Then the loop the product exists for.

**Commit a plan.**
The board opens with no head, so the Gantt is empty.
Commit one and five days of routes appear, coloured by commitment state, because "can this move?" is the question asked of every bar.

**Take a call.**
Paste messy notes into the intake panel on the left.
Watch both halves: what was captured, and what still has to be asked.
A silently half-filled form discovered after the call is worse than no form.
Confirm a priced slot and the job appears as `confirmed`.

**Take a second call.**
Check that the new slots are priced against the *updated* plan, and that the first customer's window has not moved.

**Break something.**
Type `Dan called, van 3 won't start, he's stuck at the Henderson site` into the disruption panel.
Review the typed events before recording them; nothing an agent extracts is stored until a person agrees to it.

**Choose a repair.**
Several priced candidates, each with how many customers would need telling and a deterministic autonomy verdict.
A customer-visible change cannot be applied without an explicit override, and the button says "Approve and apply" rather than "Apply".

**Read the drafts.**
A message quoting a time the plan does not support is shown held, in red, with the offending phrase named.

## The same loop, in the terminal

Faster to poke at, and it needs no board build.

```bash
W=/tmp/gg-demo
.venv/bin/glass-guru --workspace $W init
#  seeded /tmp/gg-demo with 20 events

.venv/bin/glass-guru --workspace $W commit
#  committed v001 (aa2732bfd9090d3b), 10 jobs
```

**Price a job while the customer is on the phone.**
This is the headline feature, and the clearest illustration of the split the project rests on: the solver computes the money, the agent runs the conversation.

```bash
.venv/bin/glass-guru --workspace $W slots \
  --service storefront_glass --address "1520 2nd Ave, Seattle, WA" --customer "Nguyen Glass"
```

```
   Wed 23 Sep  07:14-09:14      $16.23   Alex
      already 1 stop nearby that day, +14 min detour

   Fri 25 Sep  07:14-09:14      $29.05   Alex
      a dedicated trip out and back, +26 min driving

  5 option(s) across 5 days.  Booking the cheapest rather than the dearest saves $29.05.
```

**Promise one of them, dispatch a crew, then lose a van.**

```bash
.venv/bin/glass-guru --workspace $W event job-confirmed j-402 \
  --window-start 09:00 --window-end 15:00 --commitment-cost 250
.venv/bin/glass-guru --workspace $W event job-dispatched j-401 --at 06:05
.venv/bin/glass-guru --workspace $W event van-unavailable van-1 --at 10:40 --reason "wont start"
```

The `--at 10:40` matters.
An unstated time used to default to the start of the day, so a van reported off the road at teatime was recorded as unavailable since breakfast, retroactively invalidating the work it had already done that morning.

**Repair, and look at the choices rather than at an answer.**

```bash
.venv/bin/glass-guru --workspace $W repair
```

```
 * least_disruption    9 served    6 change(s)   0 call(s)   [crew_only]
      Keep every promise. Move as little as possible, serve fewer jobs.
           Chen Residence: 09:33 -> 12:40 (same crew)
           Whitfield Tempered: van van-1 -> van-2
      -> applied automatically: 6 internal change(s), no promised window moved
   most_jobs           9 served    6 change(s)   0 call(s)   [crew_only]
```

The autonomy verdict in brackets is deterministic policy, not the agent's opinion.
Add `--apply` to commit a candidate, then `diff` to see exactly what moved.

**The agent path.**

```bash
.venv/bin/glass-guru --workspace $W triage \
  "Dan called, van 3 won't start, he's stuck at the Henderson site"
```

```
  task task-c5f04e8cc7fa   state: input-required
  EVENTS
      van_unavailable        van-3
                             won't start

  NEEDS A DISPATCHER: Is Dan stuck at the Henderson site now, or has he left?
```

It asks rather than assuming, which is the behaviour the eval suite records and deliberately leaves alone: the questions turned out to be right about something the schema was quietly guessing.

## Other things worth running

```bash
make board                     # a day's crew board, rendered
make params                    # every rate, and whether anyone has validated it
make scenarios                 # the named disruption scenarios
make eval-offline              # the tiers that need no model
make scorecard                 # the comment CI posts on a pull request
make image && make image-run   # the container as it is deployed
```

`glass-guru explain j-407` says why a job is scheduled where it is, or why it is not scheduled at all, which is the question a dispatcher actually asks.
