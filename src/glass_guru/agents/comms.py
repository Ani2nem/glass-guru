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

#: A bare hour range - "9-11", "between 2 and 4". Checked separately because each
#: endpoint must be verified on its own: a model restating a 09:00-11:30 window as
#: "9-11" gets the opening right and the closing wrong, and quoting a customer a
#: window narrower than the one they were given is its own kind of broken promise.
_HOUR_RANGE = re.compile(
    # Both dash characters are deliberate: a model that types the longer one is
    # making the same claim, and only one of them would be checked otherwise.
    r"(?<![:\d])\b(\d{1,2})\s*(?:-|–|to|and)\s*(\d{1,2})\b(?!\d)",  # noqa: RUF001
    re.IGNORECASE,
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


def _facts_for(change: JobChange, tz: tzinfo, now: datetime | None = None) -> tuple[str, set[str]]:
    """The brief for one customer, and the phrases a draft may legitimately contain.

    ``now`` resolves "today" and "tomorrow". Without it those words cannot be checked
    at all: they are true or false only relative to when the message is sent, and a
    checker that flags a correct "today" trains a dispatcher to ignore it.
    """
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
            # A bare hour is only an exact statement on the hour. Adding it for 15:10
            # would wave through "3pm", and for 15:55 a message nearly an hour wrong.
            allowed.add(f"{local:%-I}{local:%p}".lower())
            allowed.add(f"{local:%-I}")
        if now is not None:
            days = (local.date() - now.astimezone(tz).date()).days
            if days == 0:
                allowed.add("today")
            elif days == 1:
                allowed.add("tomorrow")
        return f"{local:%A} {local:%H:%M}"

    name = change.customer_name or change.job_id
    if change.kind is ChangeKind.DROPPED and change.before:
        was = note(change.before.arrival)
        line = f"{name} ({change.job_id}): was {was}, cannot be done that day."
    elif change.before and change.after:
        was, becomes = note(change.before.arrival), note(change.after.arrival)
        line = f"{name} ({change.job_id}): was {was}, now {becomes}."
    elif change.after:
        line = f"{name} ({change.job_id}): now booked for {note(change.after.arrival)}."
    else:
        line = f"{name} ({change.job_id}): appointment changed."

    if change.promised_window:
        # State the window rather than merely mentioning that one exists. Without the
        # times, a model restates them from the old arrival - 09:33 became "a 9-11
        # window" against a real 09:00-11:30 - and quoting a customer a narrower
        # window than they were given is its own kind of broken promise.
        opens = change.promised_window.start.astimezone(tz)
        closes = change.promised_window.end.astimezone(tz)
        note(change.promised_window.start)
        note(change.promised_window.end)
        line += (
            f" They were promised {opens:%H:%M}-{closes:%H:%M} and may have arranged"
            " their day around it."
        )
    return line, allowed


def verify_grounding(draft: DraftMessage, allowed: set[str]) -> tuple[GroundingIssue, ...]:
    """Check every time-like phrase in a draft against what the facts support.

    Reads the produced text rather than the model's stated intent, which is the only
    kind of check a model cannot argue with.
    """
    issues: list[GroundingIssue] = []
    seen: set[str] = set()
    flat = {a.replace(" ", "").lower() for a in allowed}

    for match in _HOUR_RANGE.finditer(draft.body):
        for hour in match.groups():
            if hour.lstrip("0") not in flat and f"{hour}:00" not in flat:
                issues.append(
                    GroundingIssue(
                        job_id=draft.job_id,
                        phrase=match.group(0).strip(),
                        detail=(
                            f"the window quoted here does not match the one on file "
                            f"({hour} is not a time this change supports)"
                        ),
                    )
                )
                break

    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(draft.body):
            phrase = match.group(0).strip().lower()
            normalised = phrase.replace(" ", "")
            if normalised in seen:
                continue
            seen.add(normalised)
            if normalised not in flat:
                issues.append(
                    GroundingIssue(
                        job_id=draft.job_id,
                        phrase=match.group(0).strip(),
                        detail="not supported by the plan change this message describes",
                    )
                )
    return tuple(issues)


#: House style, expressed as things that are simply wrong rather than matters of taste.
#:
#: These are checked rather than judged on purpose. A rubric handed to a model should
#: only carry the questions that genuinely need reading comprehension; asking it
#: whether a message contains "j-407" wastes a call, costs money, and gives a less
#: reliable answer than a regular expression. What is left for the judge is the part
#: that actually requires judgement.
_STYLE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "internal identifier",
        re.compile(r"\b(?:j-\d+|van-\d+|crew-van-\d+|w-[a-z]+)\b", re.IGNORECASE),
        "job, van and worker ids mean nothing to a customer and leak how we work",
    ),
    (
        "internal vocabulary",
        re.compile(
            r"\b(?:provisional|dispatched|commitment cost|solver|invariant|horizon"
            r"|plan version|re-?optimi[sz]e[ds]?)\b",
            re.IGNORECASE,
        ),
        "scheduling vocabulary the customer has no reason to know",
    ),
    (
        "unauthorised compensation",
        re.compile(
            r"\b(?:discount|refund|voucher|compensat\w*|free of charge|no charge|on us)\b",
            re.IGNORECASE,
        ),
        "money was promised that nobody approved, and the customer will hold us to it",
    ),
)

#: One apology is warmth. Two is grovelling, and it reads as though something worse
#: happened than actually did.
_APOLOGY = re.compile(r"\b(?:sorry|apolog\w+)\b", re.IGNORECASE)

#: Beyond this an SMS is split by the carrier, arrives out of order, and costs twice.
_SMS_LIMIT = 320


def verify_house_style(draft: DraftMessage) -> tuple[GroundingIssue, ...]:
    """Check a draft against the rules that need no taste to apply.

    Deliberately separate from tone. Whether a message is *warm enough* is a judgement
    call; whether it quotes an internal job id, promises a refund nobody authorised, or
    apologises three times is not.
    """
    issues: list[GroundingIssue] = []

    for label, pattern, why in _STYLE_RULES:
        match = pattern.search(draft.body)
        if match:
            issues.append(
                GroundingIssue(job_id=draft.job_id, phrase=match.group(0), detail=f"{label}: {why}")
            )

    apologies = len(_APOLOGY.findall(draft.body))
    if apologies > 1:
        issues.append(
            GroundingIssue(
                job_id=draft.job_id,
                phrase=f"{apologies} apologies",
                detail="apologising more than once reads as though something worse happened",
            )
        )

    if draft.channel == "sms" and len(draft.body) > _SMS_LIMIT:
        issues.append(
            GroundingIssue(
                job_id=draft.job_id,
                phrase=f"{len(draft.body)} characters",
                detail=f"over {_SMS_LIMIT}, so the carrier splits it and it may arrive jumbled",
            )
        )

    return tuple(issues)


def draft_customer_messages(
    provider: LLMProvider,
    diff: PlanDiff,
    *,
    tz: tzinfo,
    reason: str = "",
    now: datetime | None = None,
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
            line, allowed = _facts_for(change, tz, now)
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
