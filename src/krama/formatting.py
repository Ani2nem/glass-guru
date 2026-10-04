"""How times are written where a person will read them.

Twelve-hour, because that is what this business says out loud. A dispatcher reading
"Alex 8 to 17" has to translate before they can use it, and translating is exactly
what a display is supposed to have already done.

Deliberately not used for anything the machine compares - event times, cache keys,
snapshots and the grounding checker all stay on unambiguous forms. This is the last
step before a string reaches a screen.
"""

from __future__ import annotations

from datetime import datetime, time


def clock(value: time | datetime) -> str:
    """``8:00 AM``, ``12:30 PM``, ``5:05 PM``.

    No leading zero on the hour: "08:00 AM" reads like a machine wrote it.
    """
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {meridiem}"


def clock_range(start: time | datetime, end: time | datetime) -> str:
    """``8:00 AM - 5:00 PM``, and ``9:00 - 11:30 AM`` when both sides agree.

    Repeating the meridiem inside one range is noise; dropping it when it differs
    would be a lie. Checking is one comparison.
    """
    same_half = (start.hour < 12) == (end.hour < 12)
    left = f"{start.hour % 12 or 12}:{start.minute:02d}" if same_half else clock(start)
    return f"{left} - {clock(end)}"
