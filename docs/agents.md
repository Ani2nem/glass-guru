# Agents

Four agents were planned; two are built. Triage and the coordinator are the two ends
of the design - narrow extraction and constrained ranking - and intake and comms
follow the same template.

## The model

Amazon Nova Lite on Bedrock, behind an `LLMProvider` interface so the model is a
configuration value. That abstraction is not decoration: Phase 2's eval suite sweeps
several models and answers with numbers whether Nova Lite is good enough per agent,
and that measurement is only affordable if swapping one costs nothing.

Nova Lite is fast and cheap and materially weaker at open-ended reasoning than a
frontier model. The design compensates in three concrete ways rather than hoping.

**Structured output comes from a forced tool call.** The request declares one tool
whose input schema is the shape we want, marks it `strict`, and sets `toolChoice` to
that tool by name. The model has no way to return prose, a fenced code block, or a
cheerful preamble to strip. For a small model this is the difference between an
extraction layer that mostly works and one that does.

**Every output is validated, and repaired with the specific errors.** "That was not
valid, try again" gives a small model nothing to work with. "field `crew_size`: input
should be less than or equal to 2, you sent 4" usually gets a correct answer next
attempt. Attempts are bounded; after that it escalates to a human rather than passing
along something that merely looks right.

**The hard task is constrained.** The coordinator does not author strategy. It reads
candidates the solver has already costed and picks one by name from an enum. Ranking a
short list is within a small model's range; open-ended planning is not.

## What is checked rather than trusted

The roster goes into the prompt, so the model picks ids from a list rather than
inventing them - and every id it returns is still checked against a dictionary. A
hallucinated `van-7` is caught by a lookup, never by the model's confidence.

An event missing a detail its type requires is refused, not guessed. An event is a
fact in an append-only log, and a fabricated one propagates into every plan that
follows.

Whatever the coordinator picks, the deterministic autonomy policy decides whether it
may be applied silently. The model can only ever ask for *more* review, never less. A
model that could authorise a customer-visible change on its own would make the entire
autonomy layer decorative.

## A2A

MCP reaches a tool - a typed function with a schema. A2A reaches an agent, which has
its own judgement and the right to come back and ask a question instead of answering.
That right is why there is a task lifecycle rather than a function call: triage reading
"Dan says the van's making a noise" returns `input-required` and asks which van.

Transports are pluggable. In-process locally and in tests; HTTP between deployed
services. The protocol is real in both, so nothing about an agent changes when it
moves.

## Health signals

The repair rate is the one to watch. A rising count of validation retries means the
model is drifting from what the prompt and schema expect, long before anyone notices a
bad schedule. `provider_failed` is tracked separately, because an unreachable endpoint
is an operator's problem and a malformed answer is a prompt's problem - reporting them
identically sends people to debug the wrong thing.

## Status of the Bedrock path

The Converse request and response shapes were read from botocore's own service model
rather than recalled, but **this code has not been executed against a live Bedrock
endpoint** - there are no AWS credentials in the development environment. Every agent
test runs against a scripted provider. Treat the Bedrock adapter as unverified until
someone runs `glass-guru triage` with credentials present.
