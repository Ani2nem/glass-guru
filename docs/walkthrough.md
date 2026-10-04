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
export AWS_PROFILE=glass-guru AWS_REGION=us-east-1   # for the agent steps only
export KRAMA_TRAVEL=warm                        # see below
docker compose up -d osrm                            # the routing backend warm mode uses
```

`warm` rather than `frozen` matters the moment you type a real address.
The committed snapshot covers the fixture's geography and refuses to invent a leg it does not have, which is right for tests and wrong mid-call: quoting a customer the system has never driven to fails with a cache miss.
`warm` answers from the snapshot and asks OSRM for anything new.
`frozen` is still the default everywhere else, and still what CI and the evals use.

## In the browser

```bash
.venv/bin/krama init     # seed the sample business
make dev                      # board on :5173, API on :8000
```

`init` is safe to run again: if the workspace is already there it says so and does
nothing. `init --force` throws it away and seeds a fresh one.

Then the loop the product exists for.

**Commit a plan.**
The board opens with no head, so the Gantt is empty.
Commit one and five days of routes appear, coloured by commitment state, because "can this move?" is the question asked of every bar.

**Take a call.**
Paste messy notes into the box on the left - the only one.
A classifier reads the note first and decides whether it is a booking or a disruption, says why, and hands it to the right agent; one click overrides it if it gets it wrong.
Watch both halves of what comes back: what was captured, and what still has to be asked.
A silently half-filled form discovered after the call is worse than no form.
Confirm a priced slot and the job appears as `confirmed`.

**Take a second call.**
Check that the new slots are priced against the *updated* plan, and that the first customer's window has not moved.

**Break something.**
Type `Dan called, van 3 won't start, he's stuck at the Henderson site` into the same box.
It should come back tagged as a disruption rather than as a customer named Dan, which is what it used to do when there were two boxes.
Review the typed events before recording them; nothing an agent extracts is stored until a person agrees to it.

**Reroute around it.**
The plan banner goes stale the moment the outage is recorded.
Press "Rebuild around it" in the red banner: the solver rebuilds the week around the problem, keeping every confirmed window it can and reassigning crews where it must.
If someone is out for a whole day rather than from a phone call, click their day in the crew rota instead - same event, one click.
The richer repair flow (several priced candidates, blast radius, an autonomy verdict per option) still exists in the terminal as `krama repair`; it came off the board because one well-explained button beats three unexplained ones.

## The same loop, in the terminal

Faster to poke at, and it needs no board build.

```bash
W=/tmp/gg-demo
.venv/bin/krama --workspace $W init
#  seeded /tmp/gg-demo with 20 events

.venv/bin/krama --workspace $W commit
#  committed v001 (aa2732bfd9090d3b), 10 jobs
```

**Price a job while the customer is on the phone.**
This is the headline feature, and the clearest illustration of the split the project rests on: the solver computes the money, the agent runs the conversation.

```bash
.venv/bin/krama --workspace $W slots \
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
.venv/bin/krama --workspace $W event job-confirmed j-402 \
  --window-start 09:00 --window-end 15:00 --commitment-cost 250
.venv/bin/krama --workspace $W event job-dispatched j-401 --at 06:05
.venv/bin/krama --workspace $W event van-unavailable van-1 --at 10:40 --reason "wont start"
```

The `--at 10:40` matters.
An unstated time used to default to the start of the day, so a van reported off the road at teatime was recorded as unavailable since breakfast, retroactively invalidating the work it had already done that morning.

**Repair, and look at the choices rather than at an answer.**

```bash
.venv/bin/krama --workspace $W repair
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
.venv/bin/krama --workspace $W triage \
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

`krama explain j-407` says why a job is scheduled where it is, or why it is not scheduled at all, which is the question a dispatcher actually asks.
