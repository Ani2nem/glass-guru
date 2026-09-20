# Agents

All four are built: triage (free text to typed events), intake (a call to a priced
job), coordinator (choosing between costed options) and comms (telling a customer,
without inventing anything).

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

## The division of labour, agent by agent

| Agent | The model does | Code does |
|---|---|---|
| Triage | Reads a sentence, names event kinds and ids | Checks every id exists; refuses an event missing a required detail |
| Intake | Classifies the service, hears what the caller arranged | Supplies duration, certifications, crew, parts from the catalogue; prices the promise |
| Coordinator | Ranks already-costed options | Decides autonomy; the model may only ask for more review |
| Comms | Writes the sentence | Checks every time and date in it against the plan diff |

The pattern repeats: the model handles interpretation and phrasing, code handles
anything where being plausibly wrong is expensive.

Intake is the sharpest case. The `CallExtraction` schema has **no field for duration**,
so the model cannot supply one even if asked. A schedule built on an invented duration
is wrong in a way no invariant check can catch, because every arrival time is
internally consistent with the fiction. The catalogue answers from the service type,
and once there are completed jobs those figures become measured percentiles of actuals
with nothing above them changing.

Commitment cost works the same way. "I'd have to take the morning off" is the phrase
the whole field exists for - no dropdown captures it and the solver cannot infer it -
but the model only identifies *which* of a fixed list of arrangements was expressed.
The dollar figure is a business parameter, capped, because asking a language model to
price goodwill produces a confident number with nothing behind it.

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

## Grounding customer messages

Comms is the one artefact that reaches a customer directly, so a draft is checked
rather than trusted. Every time, date and day-name in the text is extracted and matched
against the plan diff it was written from; anything unsupported holds the draft.

A plausible wrong time in a text message is worse than no message: the customer
believes it, arranges their day around it, and the business finds out when a crew
arrives to an empty house.

The check reads the produced text, not the model's stated intent - the only kind of
check a model cannot argue with. Two bugs in the checker itself were found by running
it: the bare hour "3pm" was initially accepted for a 15:10 slot (and would have been
for 15:55), and "3:10pm" was flagged as containing "10pm" because a colon creates a
word boundary. Both now have tests.

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
