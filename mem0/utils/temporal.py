"""Timezone-aware observation-time helpers for memory extraction.

Shared by the additive extraction prompt builder and the v3 context service.
The contract:

- ``None`` means *system local timezone* (``datetime.now().astimezone()``).
- Accepts IANA zone names (``Asia/Shanghai``), fixed UTC offsets
  (``+08:00`` / ``-0530`` / ``+8``), and ``UTC``/``GMT`` aliases.
- Timestamps are parsed once and converted into the requested timezone so the
  prompt can display a date, weekday, and UTC offset that are always mutually
  consistent.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TimeZoneSpec = Optional[Union[str, tzinfo]]

_LOCAL_ALIASES = frozenset({"local", "system", "sys", "auto", "default"})
_UTC_ALIASES = frozenset({"utc", "gmt", "z"})
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")
# Covers LOCOMO's ``"1:56 pm on 8 May, 2023"`` session timestamp format.
_NAMED_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})\b")

_CHINESE_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_ENGLISH_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def get_local_timezone() -> tzinfo:
    """Return the system local timezone, falling back to UTC when unknown."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _offset_to_text(offset: Optional[timedelta]) -> str:
    if offset is None:
        return "+00:00"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def utc_offset_text(tz: tzinfo, at: Optional[datetime] = None) -> str:
    """Format a timezone's UTC offset at ``at`` (defaults to now) as ±HH:MM."""
    dt = at if at is not None else datetime.now(tz)
    return _offset_to_text(dt.utcoffset())


def timezone_label(tz: tzinfo, at: Optional[datetime] = None) -> str:
    """Human-readable timezone label, e.g. ``Asia/Shanghai (UTC+08:00)``."""
    key = getattr(tz, "key", None)
    if key:
        return f"{key} ({utc_offset_text(tz, at)})"
    return f"UTC{utc_offset_text(tz, at)}"


def resolve_timezone(timezone_spec: TimeZoneSpec = None) -> tzinfo:
    """Resolve ``timezone_spec`` to a concrete :class:`tzinfo`.

    ``None`` and the aliases ``local/system/sys/auto/default`` resolve to the
    system local timezone. Invalid explicit values raise ``ValueError`` so an
    API caller gets a 4xx instead of silently falling back to a wrong zone.
    """
    if timezone_spec is None:
        return get_local_timezone()

    if isinstance(timezone_spec, tzinfo):
        return timezone_spec

    if not isinstance(timezone_spec, str):
        raise ValueError(
            f"timezone must be an IANA name, UTC offset, or None, got {type(timezone_spec).__name__}"
        )

    text = timezone_spec.strip()
    if not text:
        raise ValueError("timezone must not be empty")

    lowered = text.lower()
    if lowered in _LOCAL_ALIASES:
        return get_local_timezone()
    if lowered in _UTC_ALIASES:
        return timezone.utc

    match = _OFFSET_RE.fullmatch(text)
    if match:
        sign_text, hours_text, minutes_text = match.groups()
        hours = int(hours_text)
        minutes = int(minutes_text or 0)
        if hours > 23 or minutes > 59:
            raise ValueError(f"Invalid UTC offset: {text!r}")
        offset = timedelta(hours=hours, minutes=minutes)
        if sign_text == "-":
            offset = -offset
        return timezone(offset)

    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {text!r}") from exc


def _epoch_to_datetime(value: Union[int, float], tz: tzinfo) -> datetime:
    if abs(value) >= 10_000_000_000:  # milliseconds (epoch ms ≈ 1.7e12)
        value = value / 1000.0
    return datetime.fromtimestamp(value, tz=tz)


def coerce_observation_datetime(value: Any, tz: tzinfo) -> datetime:
    """Normalize an observation timestamp to an aware datetime in ``tz``.

    Accepts ISO-8601 strings (date or datetime), Unix epoch seconds or
    milliseconds, and named English dates such as ``"1:56 pm on 8 May, 2023"``.
    Naive ISO strings are interpreted *in the requested timezone* — never in
    the server's system zone. Raises ``ValueError`` for unparsable input.
    """
    if isinstance(value, bool):
        raise ValueError(f"timestamp must be an ISO-8601 string or epoch seconds, got {type(value).__name__}")

    if isinstance(value, (int, float)):
        try:
            return _epoch_to_datetime(value, tz)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"timestamp epoch seconds could not be parsed: {value!r}") from exc

    if not isinstance(value, str):
        raise ValueError(f"timestamp must be an ISO-8601 string or epoch seconds, got {type(value).__name__}")

    text = value.strip()
    if not text:
        raise ValueError("timestamp must not be empty")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"timestamp date could not be parsed: {value!r}") from exc
        return parsed.replace(tzinfo=tz)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except ValueError:
        pass

    match = _NAMED_DATE_RE.search(text)
    if match:
        day, month, year = match.groups()
        for date_format in ("%d %B %Y", "%d %b %Y"):
            try:
                parsed_date = datetime.strptime(f"{day} {month} {year}", date_format)
                return datetime.combine(parsed_date.date(), time.min, tzinfo=tz)
            except ValueError:
                continue

    try:
        return _epoch_to_datetime(float(text), tz)
    except (OverflowError, OSError, ValueError):
        pass

    raise ValueError(f"timestamp could not be parsed as ISO-8601, epoch seconds, or named date: {value!r}")


def observation_date_text(value: Any, tz: tzinfo) -> str:
    """Return the ``YYYY-MM-DD`` calendar date of ``value`` in ``tz``."""
    return coerce_observation_datetime(value, tz).date().isoformat()


def weekday_name(day: date, language: str = "en") -> str:
    """Weekday name for ``day`` in English or Chinese."""
    names = _CHINESE_WEEKDAYS if language == "zh" else _ENGLISH_WEEKDAYS
    return names[day.weekday()]


def has_cjk(text: str) -> bool:
    """True when ``text`` contains a CJK unified ideograph or kana."""
    if not isinstance(text, str):
        return False
    return any(
        "\u3400" <= ch <= "\u4dbf"
        or "\u4e00" <= ch <= "\u9fff"
        or "\uf900" <= ch <= "\ufaff"
        or "\u3040" <= ch <= "\u30ff"
        for ch in text
    )


def messages_contain_cjk(messages: Any) -> bool:
    """Best-effort CJK detection over new messages for prompt conditioning."""
    if isinstance(messages, str):
        return has_cjk(messages)
    if isinstance(messages, dict):
        return has_cjk(str(messages.get("content") or ""))
    if isinstance(messages, (list, tuple)):
        for item in messages:
            if messages_contain_cjk(item):
                return True
    return False
