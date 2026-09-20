# Where this stops coping

```bash
make load       # the sweep these numbers come from
```

## The day model has a cliff, not a slope

Measured with synthetic travel at a ten-second budget, jobs in one day:

Measured at an eight-second budget, jobs in one day, with the thresholds as shipped:

| jobs/day | pruning | result |
|---|---|---|
| 25 | off (at the threshold) | FEASIBLE, 23 served |
| 26 | k=6 | FEASIBLE, 21 served |
| 28 | k=6 | FEASIBLE, 19 served |
| 32 | k=6 | FEASIBLE, 23 served |
| 40 | k=6 | UNKNOWN at 8s, FEASIBLE 18 served at 10s |
| 50 | k=6 | **UNKNOWN, nothing served** |

Without pruning the full model does not degrade gracefully: it answers at 25 and
returns nothing at 28. That is worth knowing because the usual intuition - "it will just get a bit slower"
- is wrong here, and a business growing from 25 jobs a day to 28 would have gone from
a working scheduler to one that returns an empty plan.

Growing the crew makes it worse rather than better. The model has one boolean per crew
per ordered pair of stops, so arcs grow as crews times n squared; more vans means more
crews means a larger model.

## k-nearest pruning

Each stop keeps arcs only to its nearest few neighbours. Every depot arc is kept - a
crew must be able to start and finish anywhere, and dropping those is how pruning turns
a feasible day infeasible - and the neighbour relation is symmetrised, because A can be
among B's nearest without B being among A's and keeping only mutual pairs strands the
outlying stop.

`k=6` is aggressive on purpose. Pruning applies only above 25 jobs, and in that range
the honest comparison is not "pruned versus unpruned" but **"a plan versus none"**:
at 35 jobs k=12 returns UNKNOWN and k=6 serves 27. There is no quality being traded,
because there is no alternative answer to trade it against.

The threshold sits at exactly 25 - the last size the full model answers. An earlier
value of 28 left a hole: a 28-job day was above what the full model could handle and
below what triggered pruning, so it returned nothing while a 32-job day planned fine.

## The horizon is the real scaling mechanism

| | solve | served |
|---|---|---|
| 25 jobs over 5 days | 0.8s | 96% |
| 60 jobs over 5 days | 39s | 95% |

Twelve jobs a day is comfortably inside the day model's range, which is why splitting
the work across days works where solving one enormous day does not. A business at four
times this volume needs either more days in the horizon or a second decomposition
within the day - geographic sharding is the obvious one, and the sector machinery for
it already exists in `assign_days`.

## The hot path has its own budget

A booking quote is two solves with a customer on the phone. It used to inherit the
batch ceiling, which at 25 jobs a day made a quote take **twenty seconds** - not a
feature anybody would use.

Two fixes, measured at 25 jobs a day:

| | first quote | subsequent |
|---|---|---|
| batch budget, no cache | 20s | 20s |
| quote budget (1.5s/solve) | 3.1s | 3.1s |
| **plus baseline cache** | **3.1s** | **1.6s** |

Half of every quote was re-solving the day as it already stands, and that answer does
not change between one caller and the next. The cache is keyed on the exact set of jobs
in the day, so a booking, a cancellation or a disruption invalidates it by construction
rather than by somebody remembering to.

Past about three seconds a dispatcher starts apologising for the pause, so the first
quote after a change is at the limit and the rest are comfortable.

## What is not measured here

Memory, concurrent solves, and the cost of a cold travel cache against live OSRM. The
frozen snapshot makes travel free in these numbers; a genuinely new service area pays
for its geography once, which `make freeze-travel` does deliberately rather than
discovering under load.
