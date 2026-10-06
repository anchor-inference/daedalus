"""Expanding a repeat rule into occurrences, for events and planner tasks alike.

A series is expanded in its own IANA zone, never in UTC: "every Monday at 10:00 in Berlin" stays at
10:00 on both sides of a daylight-saving change, which a rule expanded in UTC would move by an hour.
All-day series are expanded over plain dates. python-dateutil arrives with icalendar, which needs it
for its own parsing, so no dependency is added for this.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rrulestr

MAX_OCCURRENCES = 2000
"""Occurrences one read may return. A week view needs a few dozen; the cap is what keeps a careless
range over a daily series from building a list the app cannot draw anyway."""
MAX_RANGE_DAYS = 400
"""The longest range one read may expand: a year view and its margins."""
SERIES_END_SCAN = 20000
"""How many occurrences are walked to find where a bounded series ends. A COUNT beyond it is treated
as endless, which only costs the read a little pruning."""
_FREQUENCIES = {"YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY"}
"""MINUTELY and SECONDLY are refused: no calendar offers them, and a range over one is a million rows."""
_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("use an IANA time zone, such as Europe/London") from exc


def parts(rule: str) -> dict[str, str]:
    text = rule.strip().removeprefix("RRULE:")
    found: dict[str, str] = {}
    for piece in text.split(";"):
        if not piece:
            continue
        key, _, value = piece.partition("=")
        if not value:
            raise ValueError(f"the repeat rule has a part without a value: {piece}")
        found[key.strip().upper()] = value.strip().upper()
    return found


def normalize(rule: str) -> str:
    """One ``RRULE:`` line, checked; an empty rule stays empty.

    Only the rule itself is kept: exceptions live in their own column, and a client that sends a
    whole recurrence block gets its RRULE line taken and the rest refused, rather than silently kept
    in a field nothing reads.
    """
    text = (rule or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rules = [line for line in lines if line.upper().startswith("RRULE:") or line.upper().startswith("FREQ=")]
    if len(rules) != 1 or len(lines) != 1:
        raise ValueError("give exactly one repeat rule, such as FREQ=WEEKLY;BYDAY=MO")
    found = parts(rules[0])
    if found.get("FREQ") not in _FREQUENCIES:
        raise ValueError("the repeat rule needs FREQ=DAILY, WEEKLY, MONTHLY, YEARLY or HOURLY")
    if "COUNT" in found and "UNTIL" in found:
        raise ValueError("a repeat rule takes COUNT or UNTIL, not both")
    normalized = "RRULE:" + ";".join(f"{key}={value}" for key, value in found.items())
    if len(normalized) > 500:
        raise ValueError("the repeat rule is too long")
    # Parse once against a fixed start, so a rule dateutil cannot read is refused at the door
    # instead of failing every later read of the calendar.
    build(normalized, datetime(2026, 1, 1, 9, tzinfo=UTC))
    return normalized


def _until(value: str, start: datetime) -> str:
    """UNTIL rewritten to match the start's kind, which dateutil insists on: aware with an aware start
    (RFC 5545 says UTC), plain with a floating or all-day start."""
    aware = start.tzinfo is not None
    if len(value) == 8:
        moment = datetime.combine(date(int(value[:4]), int(value[4:6]), int(value[6:8])), time(23, 59, 59))
        moment = moment.replace(tzinfo=start.tzinfo) if aware else moment
    elif value.endswith("Z"):
        moment = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if not aware:
            moment = moment.replace(tzinfo=None)
    else:
        moment = datetime.strptime(value, "%Y%m%dT%H%M%S")
        moment = moment.replace(tzinfo=start.tzinfo) if aware else moment
    if aware:
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return moment.strftime("%Y%m%dT%H%M%S")


def build(rule: str, start: datetime) -> rrule:
    found = parts(rule)
    if "UNTIL" in found:
        found["UNTIL"] = _until(found["UNTIL"], start)
    built = rrulestr("RRULE:" + ";".join(f"{key}={value}" for key, value in found.items()), dtstart=start)
    assert isinstance(built, rrule)
    return built


def series_start(start_at: str, timezone: str, all_day: bool) -> datetime:
    """Where a series begins, in the frame it repeats in: a plain date-time at midnight for an all-day
    series, the wall-clock time in the event's zone otherwise."""
    moment = datetime.fromisoformat(start_at)
    if all_day:
        return datetime.combine(moment.date(), time())
    return moment.astimezone(zone(timezone))


def as_utc(moment: datetime) -> datetime:
    # An all-day occurrence is a plain date, stored the way every all-day boundary is: midnight UTC.
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def key(moment: datetime) -> str:
    """The identity of an occurrence: its original start as stored, a UTC ISO instant."""
    return as_utc(moment).isoformat()


def occurrences(rule: str, start_at: str, end_at: str, timezone: str, all_day: bool, first: datetime, last: datetime, limit: int = MAX_OCCURRENCES) -> list[tuple[datetime, datetime]]:
    """The (start, end) pairs, in UTC, of a series' occurrences that overlap ``[first, last)``."""
    start = series_start(start_at, timezone, all_day)
    length = datetime.fromisoformat(end_at) - datetime.fromisoformat(start_at)
    built = build(rule, start)
    # Walk from the earliest start that could still overlap the window; dateutil wants that bound in
    # the same kind (aware or plain) as the series start.
    after = first - length
    if all_day:
        after = after.astimezone(UTC).replace(tzinfo=None)
    found: list[tuple[datetime, datetime]] = []
    for moment in built.xafter(after, count=limit, inc=True):
        begins = as_utc(moment)
        if begins >= last:
            break
        ends = begins + length
        if ends > first or (length == timedelta(0) and begins >= first):
            found.append((begins, ends))
    return found


def series_end(rule: str, start_at: str, end_at: str, timezone: str, all_day: bool) -> str | None:
    """When the last occurrence ends, or ``None`` for a series without an end: lets a read skip series
    that finished before its range without expanding them."""
    if not rule:
        return None
    found = parts(rule)
    if "COUNT" not in found and "UNTIL" not in found:
        return None
    built = build(rule, series_start(start_at, timezone, all_day))
    walked = list(itertools.islice(built, SERIES_END_SCAN + 1))
    if not walked:
        return end_at
    if len(walked) > SERIES_END_SCAN:
        return None
    return (as_utc(walked[-1]) + (datetime.fromisoformat(end_at) - datetime.fromisoformat(start_at))).isoformat()


def next_after(rule: str, start: datetime, after: datetime) -> datetime | None:
    """The first occurrence strictly after ``after``, in the series' own frame."""
    return build(rule, start).after(after, inc=False)


# Microsoft Graph keeps a series as a structured pattern rather than an RRULE. These two convert the
# rules people actually make (daily, weekly on days, monthly on a date or the nth weekday, yearly) and
# refuse anything else, so a rule Outlook cannot hold is turned down when it is saved, not lost later.

_GRAPH_DAYS = dict(zip(_DAYS, ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), strict=True))
_GRAPH_INDEX = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}


def _nth(value: str) -> tuple[int, str]:
    sign = -1 if value.startswith("-") else 1
    digits = value.lstrip("+-")
    number = "".join(itertools.takewhile(str.isdigit, digits))
    return sign * int(number or 0), digits[len(number):]


def to_graph(rule: str, start: datetime, timezone: str, all_day: bool) -> dict[str, Any]:
    found = parts(rule)
    interval = int(found.get("INTERVAL", "1"))
    frequency = found["FREQ"]
    days = [day for day in found.get("BYDAY", "").split(",") if day]
    pattern: dict[str, Any] = {"interval": interval}
    unsupported = set(found) - {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "COUNT", "UNTIL", "WKST"}
    if unsupported or frequency == "HOURLY":
        raise ValueError("Outlook cannot store this repeat rule; use daily, weekly, monthly or yearly")
    if frequency == "DAILY" and not days:
        pattern["type"] = "daily"
    elif frequency == "WEEKLY" or (frequency == "DAILY" and days):
        if any(_nth(day)[0] for day in days):
            raise ValueError("Outlook cannot store this repeat rule")
        pattern.update(type="weekly", daysOfWeek=[_GRAPH_DAYS[day] for day in days] or [_GRAPH_DAYS[_DAYS[start.weekday()]]], firstDayOfWeek=_GRAPH_DAYS[found.get("WKST", "MO")])
    elif frequency in ("MONTHLY", "YEARLY"):
        yearly = frequency == "YEARLY"
        if yearly:
            pattern["month"] = int(found.get("BYMONTH", str(start.month)))
        if days:
            index, setpos = _nth(days[0])[0], int(found.get("BYSETPOS", "0"))
            index = index or setpos
            if index not in _GRAPH_INDEX or any(_nth(day)[0] not in (0, index) for day in days):
                raise ValueError("Outlook cannot store this repeat rule")
            pattern.update(type="relativeYearly" if yearly else "relativeMonthly", daysOfWeek=[_GRAPH_DAYS[_nth(day)[1]] for day in days], index=_GRAPH_INDEX[index])
        else:
            pattern.update(type="absoluteYearly" if yearly else "absoluteMonthly", dayOfMonth=int(found.get("BYMONTHDAY", str(start.day))))
    else:
        raise ValueError("Outlook cannot store this repeat rule")
    span: dict[str, Any] = {"type": "noEnd", "startDate": start.date().isoformat(), "recurrenceTimeZone": timezone if not all_day else "UTC"}
    if "COUNT" in found:
        span.update(type="numbered", numberOfOccurrences=int(found["COUNT"]))
    elif "UNTIL" in found:
        until = _until(found["UNTIL"], start)
        moment = datetime.strptime(until, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).astimezone(start.tzinfo) if until.endswith("Z") else datetime.strptime(until, "%Y%m%dT%H%M%S")
        span.update(type="endDate", endDate=moment.date().isoformat())
    return {"pattern": pattern, "range": span}


def from_graph(recurrence: dict[str, Any]) -> str:
    pattern, span = recurrence.get("pattern") or {}, recurrence.get("range") or {}
    kind = pattern.get("type")
    reverse = {name: code for code, name in _GRAPH_DAYS.items()}
    days = [reverse[day.lower()] for day in pattern.get("daysOfWeek") or [] if day.lower() in reverse]
    found: dict[str, str] = {}
    if kind == "daily":
        found["FREQ"] = "DAILY"
    elif kind == "weekly":
        found.update(FREQ="WEEKLY", BYDAY=",".join(days))
        if pattern.get("firstDayOfWeek"):
            found["WKST"] = reverse.get(str(pattern["firstDayOfWeek"]).lower(), "MO")
    elif kind in ("absoluteMonthly", "absoluteYearly"):
        found["FREQ"] = "MONTHLY" if kind == "absoluteMonthly" else "YEARLY"
        if kind == "absoluteYearly":
            found["BYMONTH"] = str(pattern.get("month") or 1)
        found["BYMONTHDAY"] = str(pattern.get("dayOfMonth") or 1)
    elif kind in ("relativeMonthly", "relativeYearly"):
        found["FREQ"] = "MONTHLY" if kind == "relativeMonthly" else "YEARLY"
        if kind == "relativeYearly":
            found["BYMONTH"] = str(pattern.get("month") or 1)
        index = {name: number for number, name in _GRAPH_INDEX.items()}.get(str(pattern.get("index") or "first"), 1)
        found["BYDAY"] = ",".join(days)
        found["BYSETPOS"] = str(index)
    else:
        raise ValueError(f"unknown Outlook repeat pattern {kind!r}")
    if int(pattern.get("interval") or 1) != 1:
        found["INTERVAL"] = str(int(pattern["interval"]))
    if span.get("type") == "numbered":
        found["COUNT"] = str(int(span.get("numberOfOccurrences") or 1))
    elif span.get("type") == "endDate" and span.get("endDate"):
        found["UNTIL"] = str(span["endDate"]).replace("-", "")
    return "RRULE:" + ";".join(f"{name}={value}" for name, value in found.items())


__all__ = ["MAX_OCCURRENCES", "MAX_RANGE_DAYS", "as_utc", "build", "from_graph", "key", "next_after", "normalize", "occurrences", "parts", "series_end", "series_start", "to_graph", "zone"]
