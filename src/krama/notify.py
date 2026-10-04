"""Texting the crew.

One job: deliver a short message to a fitter's phone. Everything that decides WHO
gets a message and WHAT it says lives with the overtime flow in the API layer, where
it is deterministic and tested; this module only carries the envelope.

Off by default, and deliberately so. Twilio is the obvious carrier, but a Twilio
phone number rents for roughly a dollar a month whether it sends anything or not -
exactly the idle cost this deployment is built to avoid - so nothing here signs up
for anything. The default sink writes each message to the application log, which
makes the flow fully testable end to end; pointing it at a real phone is a
configuration change:

    KRAMA_SMS=twilio
    TWILIO_ACCOUNT_SID=...   TWILIO_AUTH_TOKEN=...   TWILIO_FROM=+1...

NOT verified against the live service. The request shape below follows Twilio's
published REST API; no account or key was available to run it, which is the same
honesty note the TomTom client carries.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from krama.obs.tracing import span

log = logging.getLogger("krama.notify")


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened to one message - for the caller to report, not to retry."""

    to: str
    accepted: bool
    detail: str = ""


class Notifier(Protocol):
    def send(self, to: str, body: str) -> Delivery: ...


class LogNotifier:
    """The default. Prints the text it would have sent, so a developer watching the
    server log sees exactly what a fitter would have received."""

    def send(self, to: str, body: str) -> Delivery:
        # warning, not info: uvicorn's default config swallows INFO from library
        # loggers, and a message that silently went nowhere is exactly the thing a
        # developer watching the log needs to see.
        log.warning("SMS not sent (log mode) to %s: %s", to or "<no number>", body)
        return Delivery(to=to, accepted=bool(to), detail="logged, not sent")


class RecordingNotifier:
    """For tests: keeps every message, delivers none."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> Delivery:
        self.sent.append((to, body))
        return Delivery(to=to, accepted=bool(to))


class TwilioNotifier:
    """Twilio's Messages endpoint, via plain urllib - no SDK dependency."""

    ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    def __init__(self, sid: str, token: str, sender: str, timeout: float = 10.0) -> None:
        self.sid = sid
        self.token = token
        self.sender = sender
        self.timeout = timeout

    def send(self, to: str, body: str) -> Delivery:
        if not to:
            return Delivery(to=to, accepted=False, detail="no phone number on file")
        payload = urllib.parse.urlencode({"To": to, "From": self.sender, "Body": body}).encode()
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        request = urllib.request.Request(
            self.ENDPOINT.format(sid=self.sid),
            data=payload,
            headers={"Authorization": f"Basic {auth}", "User-Agent": "krama/0.1"},
        )
        with span("notify.twilio"):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    answer = json.load(response)
                return Delivery(to=to, accepted=True, detail=str(answer.get("sid", "")))
            except OSError as exc:
                # A failed text must never fail the booking that triggered it.
                log.warning("SMS to %s failed: %s", to, exc)
                return Delivery(to=to, accepted=False, detail=str(exc))


def build_notifier(name: str | None = None) -> Notifier:
    """``log`` (default) or ``twilio``, from ``KRAMA_SMS``."""
    resolved = (name or os.environ.get("KRAMA_SMS", "log")).lower()
    if resolved in {"", "log", "off", "none"}:
        return LogNotifier()
    if resolved == "twilio":
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        sender = os.environ.get("TWILIO_FROM", "")
        if not (sid and token and sender):
            raise ValueError(
                "KRAMA_SMS=twilio needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN "
                "and TWILIO_FROM. Unset KRAMA_SMS to log messages instead."
            )
        return TwilioNotifier(sid, token, sender)
    raise ValueError(f"unknown SMS provider {resolved!r}; expected 'log' or 'twilio'")
