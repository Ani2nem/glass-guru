# Connecting an MCP client

The server exposes the scheduling engine over stdio.

```bash
glass-guru init            # once, to create a workspace
glass-guru-mcp             # runs the server
```

Claude Desktop or any MCP client:

```json
{
  "mcpServers": {
    "glass-guru": {
      "command": "glass-guru-mcp",
      "env": {
        "GLASS_GURU_WORKSPACE": "/absolute/path/to/.glass-guru",
        "GLASS_GURU_TRAVEL": "warm"
      }
    }
  }
}
```

`GLASS_GURU_TRAVEL` picks the travel source. `frozen` is offline and free but only
covers addresses already on the books; `warm` uses the frozen snapshot and computes
the few missing legs from a running OSRM, which is what a live quote for a new address
needs.

## Tools

| Tool | Answers |
|---|---|
| `get_world_state` | Who is working, which vans run, what is outstanding |
| `suggest_booking_slots` | When can we come out, and what does each option cost |
| `plan_week` | Plan the horizon; optionally commit it |
| `repair_plan` | Priced trade-offs after a disruption, with autonomy verdicts |
| `record_event` | Log a breakdown, a sick worker, a confirmed window |
| `get_plan_diff` | What changed, and whether a customer would notice |

## What the boundary deliberately excludes

There is no tool to set an arrival time, override feasibility, or force a plan past
its invariants. An agent can ask the engine for a plan and record what a caller said;
it cannot assert a schedule. That is the whole point of putting MCP here rather than
giving an agent direct access to the solver.

Every tool returns a small closed schema with the explanation already computed. Why a
job is unserved is arithmetic, and arithmetic stays on the deterministic side - an
agent left to infer it would eventually infer it wrong and say so confidently.

Failures come back as `{"error", "detail", "remedy"}` rather than exceptions, because
a model handed a stack trace paraphrases it into something plausible and wrong.
