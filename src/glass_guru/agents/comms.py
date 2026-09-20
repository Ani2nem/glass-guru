"""Comms: telling a customer what changed, without inventing anything.

The agent drafts; the draft is then checked against the facts it was given. That
second step is the whole design. A message is the one artefact here that reaches a
customer directly, and a plausible wrong time in a text message is worse than no
message at all - the customer believes it, arranges their day around it, and the
business discovers the error when a crew arrives to an empty house.

So every time, date and day-name in a draft is extracted and matched against the plan
diff it was written from. Anything the diff does not support is flagged and the draft
is held. The model cannot talk its way past this: the check reads the text it produced,
not its intentions.

Tone is the part a model genuinely adds. "We've had a van break down and need to move
you to Thursday morning, sorry about that" is a better sentence than any template, and
templates cannot tell that a customer who took a day off deserves a different opening
line from one who said any time was fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from glass_guru.agents.llm.base import LLMProvider
from glass_guru.agents.structured import Example, Extraction, extract
from glass_guru.domain.diff import ChangeKind, JobChange, PlanDiff
from glass_guru.obs.tracing import record, span

#: Anything that looks like a promise about when. Deliberately greedy: a false
#: positive costs a dispatcher three seconds, a false negative reaches a customer.
_TIME_PATTERNS = (
    re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.IGNORECASE),
    # The lookbehind matters: without it "3:10pm" also matches as "10pm", and a
    # correctly grounded message gets held for a phrase it never contained.
    re.compile(r"(?<![:\d])\b\d{1,2}\s*(?:am|pm)\b", re.IGNORECASE),
    re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:january|february|march|april|may|june|july|august|september|october|"
        r"november|december)\s+\d{1,2}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:today|tomorrow)\b", re.IGNORECASE),
)


class DraftMessage(BaseModel):
    """One message to one customer."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(description="The job this concerns, exactly as given.")
    channel: Literal["sms", "email", "call"] = Field(
        description="sms for short notice, call when the news is bad or complicated."
    )
    body: str = Field(
        description=(
            "The message itself. Plain, warm, under 300 characters for sms. State only "
            "times and dates you were given. Never apologise twice."
        )
    )


class CommsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[DraftMessage] = Field(default_factory=list)


SYSTEM = """\
You write short messages to customers of a glass-fitting business when their
appointment changes.

Rules:
- Use only the times, dates and facts listed. Never state a time you were not given.
  If you are unsure when the crew will arrive, say you will confirm.
- One message per customer listed. No message for anyone not listed.
- Warm and brief. No corporate padding, no "we sincerely apologise for any
  inconvenience caused".
- Acknowledge what the customer arranged when you are told they arranged something.
- Choose `call` rather than `sms` when the change is significant or needs a decision.
"""


EXAMPLES: tuple[Example, ...] = (
    Example(
        text=(
            "Chen Residence (j-402): was Monday 09:33, now Monday 12:40. "
            "Customer took the morning off work. Reason: a van broke down."
        ),
        output=CommsOutput(
            messages=[
                DraftMessage(
                    job_id="j-402",
                    channel="call",
                    body=(
                        "Hi Sarah - one of our vans has broken down this morning, so we "
                        "need to move your fitting to Monday 12:40. I know you'd taken "
                        "the morning off, so do call if that no longer works and we'll "
                        "find something better."
                    ),
                )
            ]
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class GroundingIssue:
    """A claim in a draft that the facts do not support."""

    job_id: str
    phrase: str
    detail: str


@dataclass(frozen=True, slots=True)
class CommsResult:
    drafts: tuple[DraftMessage, ...]
    issues: tuple[GroundingIssue, ...]
    extraction: Extraction[CommsOutput]

    @property
    def safe_to_send(self) -> bool:
        """Only when every claim is supported. A held draft is a dispatcher's problem;
        an ungrounded one that went out is a customer's."""
        return bool(self.drafts) and not self.issues

    def for_job(self, job_id: str) -> DraftMessage | None:
        return next((d for d in self.drafts if d.job_id == job_id), None)


def _facts_for(change: JobChange, tz: tzinfo) -> tuple[str, set[str]]:
    """The brief for one customer, and the phrases a draft may legitimately contain."""
    allowed: set[str] = set()

    def note(moment: datetime) -> str:
        """Record a moment, and every *exact* way of writing it.

        Only exact forms. An earlier version also allowed the bare hour, so "3pm"
        passed for a 15:10 slot - and would have passed for 15:55, waving through a
        message nearly an hour wrong. Rounding is the model's to justify, not the
        checker's to assume.
        """
        local = moment.astimezone(tz)
        allowed.add(f"{local:%H:%M}")
        allowed.add(f"{local:%A}".lower())
        allowed.add(f"{local:%-I:%M}{local:%p}".lower())
        if local.minute == 0:
            allowed.add(f"{local:%-I}{local:%p}".lower())
        return f"{local:%A} {local:%H:%M}"

    name = change.customer_name or change.job_id
    if change.kind is ChangeKind.DROPPED and change.before:
        was = note(change.before.arrival)
        line = f"{name} ({change.job_id}): was {was}, cannot be done that day."
    elif change.before and change.after:
        was, now = note(change.before.arrival), note(change.after.arrival)
        line = f"{name} ({change.job_id}): was {was}, now {now}."
    elif change.after:
        line = f"{name} ({change.job_id}): now booked for {note(change.after.arrival)}."
    else:
        line = f"{name} ({change.job_id}): appointment changed."

    if change.promised_window:
        # The customer was given a window, so a message may legitimately restate it.
        note(change.promised_window.start)
        note(change.promised_window.end)
        line += " Customer was given a window and may have arranged their day around it."
    return line, allowed


def verify_grounding(draft: DraftMessage, allowed: set[str]) -> tuple[GroundingIssue, ...]:
    """Check every time-like phrase in a draft against what the facts support.

    Reads the produced text rather than the model's stated intent, which is the only
    kind of check a model cannot argue with.
    """
    issues: list[GroundingIssue] = []
    seen: set[str] = set()
    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(draft.body):
            phrase = match.group(0).strip().lower()
            normalised = phrase.replace(" ", "")
            if normalised in seen:
                continue
            seen.add(normalised)
            if normalised not in {a.replace(" ", "").lower() for a in allowed}:
                issues.append(
                    GroundingIssue(
                        job_id=draft.job_id,
                        phrase=match.group(0).strip(),
                        detail="not supported by the plan change this message describes",
                    )
                )
    return tuple(issues)


def draft_customer_messages(
    provider: LLMProvider,
    diff: PlanDiff,
    *,
    tz: tzinfo,
    reason: str = "",
    max_attempts: int = 3,
) -> CommsResult:
    """Draft a message per affected customer, then check every claim in it."""
    changes = diff.customer_visible_changes
    if not changes:
        return CommsResult(drafts=(), issues=(), extraction=Extraction(value=None))

    with span("agent.comms", customers=len(changes)) as active:
        briefs: list[str] = []
        allowed_by_job: dict[str, set[str]] = {}
        for change in changes:
            line, allowed = _facts_for(change, tz)
            briefs.append(line)
            allowed_by_job[change.job_id] = allowed

        body = "\n".join(briefs)
        if reason:
            body += f"\n\nReason to give: {reason}"

        extraction = extract(
            provider,
            CommsOutput,
            system=SYSTEM,
            text=body,
            examples=EXAMPLES,
            max_attempts=max_attempts,
            schema_description="One message per affected customer.",
        )

        if extraction.value is None:
            record(escalated=True, drafts=0)
            active.set_attribute("outcome", "escalated")
            return CommsResult(drafts=(), issues=(), extraction=extraction)

        drafts = tuple(extraction.value.messages)
        issues: list[GroundingIssue] = []
        expected = set(allowed_by_job)

        for draft in drafts:
            if draft.job_id not in expected:
                # A message to someone who is not affected. Worse than a wrong time:
                # nothing changed for them, and a message says otherwise.
                issues.append(
                    GroundingIssue(
                        job_id=draft.job_id,
                        phrase=draft.job_id,
                        detail="message written for a customer whose appointment did not change",
                    )
                )
                continue
            issues.extend(verify_grounding(draft, allowed_by_job[draft.job_id]))

        for job_id in expected - {d.job_id for d in drafts}:
            issues.append(
                GroundingIssue(
                    job_id=job_id,
                    phrase="",
                    detail="no message drafted for a customer whose appointment changed",
                )
            )

        record(drafts=len(drafts), issues=len(issues), repairs=extraction.repairs)
        active.set_attribute("outcome", "clean" if not issues else "held")
        return CommsResult(drafts=drafts, issues=tuple(issues), extraction=extraction)
