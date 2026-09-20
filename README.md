# glass-guru

AI dispatch and scheduling for a glass-fitting business: about six fitters, four vans, twenty-five jobs a day, a thirty-mile radius.

Calls arrive all day, so the plan is a living thing rather than a morning artifact.
A dispatcher types while the customer talks, and gets back bookable slots priced by what serving them actually costs:

```
Tue 14:00   already two stops on that street that afternoon, +18 min detour   $22
Wed 09:00   moderate detour                                                   $41
Thu 08:00   a dedicated trip out and back                                     $82
```

Tuesday is not a preference. It is four times cheaper to serve.

## The idea

**The solver owns anything checkable. Agents own everything the solver cannot represent.**

An agent never invents an arrival time or declares a plan feasible.
A solver never decides that "I took the morning off work" should make a slot expensive to move.

The boundary is structural rather than a matter of prompt discipline: the MCP surface has no tool that asserts a schedule, and the intake schema has no duration field, so the model cannot supply one even if it tries.

| | owns |
|---|---|
| CP-SAT | routing, crew formation, van assignment, time windows, certifications, cost |
| Agents | reading a phone call, converting "van 3 won't start" into typed events, ranking candidate repairs, drafting the customer message |
| Neither | the autonomy decision - that is deterministic policy, not a prompt |

Every plan is re-derived from the materialized route by an invariant checker written independently of the solver.
Two implementations of the same calculation agreeing is the whole safety argument, and no plan is committed without it.

## Running it

```bash
make install
make check                 # 373 tests, mypy strict, ruff
make board                 # render a day's schedule
make dev                   # the dispatch board at http://127.0.0.1:5173
```

No credentials and no network are needed for any of that.
Travel times come from a committed OSRM snapshot, so a fresh clone gets real road distances offline and for free.

[docs/walkthrough.md](docs/walkthrough.md) is the by-hand tour: take a call, price it, break a van, repair the day.

For the agents, see [docs/aws-setup.md](docs/aws-setup.md) - Amazon Nova Lite on Bedrock, behind an interface that makes the model a config value.

## How it is kept honest

**Evals gate the merge.** Five tiers, from solver invariants to the tone of a customer message, with thresholds *and* a comparison against a committed baseline - because quality usually degrades as a series of small slips nobody objected to rather than in one visible step. CI posts the scorecard on the pull request. See [docs/evals.md](docs/evals.md) and [docs/cicd.md](docs/cicd.md).

**The judge is judged.** Tone is the only thing scored by a model, and its verdicts are worth nothing until checked, so it is calibrated against labelled messages first. If it cannot match them, or if it answers "good" to everything, tone is reported as unscored rather than as a number.

**The numbers say where they came from.** Every rate and duration carries a provenance tag (`make params`). All thirty-five are currently `estimated`, and the board says so at the top of every board it prints.

**Nothing deploys itself.** CI can replace the running image and nothing else - it holds no credential that can edit infrastructure or delete the event log. The application's own role has no `s3:DeleteObject`, because an append-only log never needs one. See [infra/](infra/).

## Layout

```
src/glass_guru/
  domain/        models, the event log, fold, the invariant checker
  scheduler/     CP-SAT solvers, travel providers, marginal-cost booking
  agents/        intake, triage, coordinator, comms, A2A, LLM providers
  mcp_server/    the tool surface agents see
  api/  cli/     HTTP and terminal front ends
  evals/         tiers, datasets, scoring, the tone judge
infra/           terraform: bootstrap (OIDC, roles) and app (Lambda, S3)
web/             the dispatch board
docs/            design notes and operational guides
```

## Status

The machine works, the evals gate it, and the pipeline deploys it.
The application stack - a container on Lambda behind a function URL, with the event log on S3 - is written and validated but not applied.
About **$3 a month**, nothing when idle; [`infra/app/README.md`](infra/app/README.md) has the apply, the teardown, and why it is not ECS.

Known and deliberate: every business parameter is still an estimate; the tone labels are mine rather than the business's, which calibrates the judge against clear-cut cases but not against real taste; and Nova Lite asks a clarifying question on a handful of triage notes, which is left failing because a gate adjusted until it passes measures nothing.
