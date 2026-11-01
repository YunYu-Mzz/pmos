"""Statistical periods: validation, expansion and request-template variables.

A period value is always a string so it can key tasks, archives and receipts:
day ``2026-10-11``, month ``2026-10``, year ``2026``, week_start_monday
``2026-10-26`` (a report asked by the Monday of its week). ``snapshot`` datasets
have no statistical period; their task is keyed by the sampling date.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")
KINDS = ("day", "month", "year", "snapshot", "week_start_monday")


class PeriodError(ValueError):
    pass


def today() -> date:
    return datetime.now(CHINA).date()


def parse(kind: str, value: str) -> date:
    """Return the first day covered by ``value``; reject non-canonical text."""
    try:
        if kind in ("day", "snapshot"):
            parsed = date.fromisoformat(value)
            canonical = parsed.isoformat()
        elif kind == "week_start_monday":
            parsed = date.fromisoformat(value)
            canonical = parsed.isoformat() if parsed.weekday() == 0 else None
        elif kind == "month":
            parsed = date.fromisoformat(value + "-01")
            canonical = parsed.strftime("%Y-%m")
        elif kind == "year":
            parsed = date(int(value), 1, 1)
            canonical = f"{parsed.year:04d}"
        else:
            raise PeriodError("period_kind_unknown")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PeriodError):
            raise
        raise PeriodError("period_invalid") from None
    if canonical != value:
        raise PeriodError("period_invalid")
    return parsed


def last_day(kind: str, value: str) -> date:
    first = parse(kind, value)
    if kind == "month":
        return first.replace(day=calendar.monthrange(first.year, first.month)[1])
    if kind == "year":
        return first.replace(month=12, day=31)
    if kind == "week_start_monday":
        return first + timedelta(days=6)
    return first


def validate(kind: str, value: str, *, on: date | None = None) -> str:
    """A period may be requested once it has started; future periods are refused."""
    if parse(kind, value) > (on or today()):
        raise PeriodError("period_in_future")
    return value


def value_of(kind: str, day: date) -> str:
    if kind in ("day", "snapshot"):
        return day.isoformat()
    if kind == "week_start_monday":
        return (day - timedelta(days=day.weekday())).isoformat()
    if kind == "month":
        return day.strftime("%Y-%m")
    if kind == "year":
        return f"{day.year:04d}"
    raise PeriodError("period_kind_unknown")


def previous(kind: str, value: str) -> str:
    first = parse(kind, value)
    if kind in ("day", "snapshot"):
        return value_of(kind, first - timedelta(days=1))
    if kind == "week_start_monday":
        return value_of(kind, first - timedelta(days=7))
    if kind == "month":
        return value_of(kind, (first - timedelta(days=1)).replace(day=1))
    return value_of(kind, date(first.year - 1, 1, 1))


def expand(kind: str, start: str, end: str, *, on: date | None = None, lag_days: int = 0,
           complete_only: bool = False) -> list[str]:
    """All periods from ``start`` to ``end`` (a period value or ``today``), newest first.

    ``lag_days`` holds back periods the site publishes late (a day report that
    appears the next morning uses 1). ``complete_only`` keeps a month or year
    out until its last day has passed the lag.
    """
    if kind == "snapshot":
        raise PeriodError("snapshot_has_no_range")
    limit = (on or today()) - timedelta(days=lag_days)
    first = parse(kind, start)
    final = limit if end == "today" else min(last_day(kind, end), limit)
    if final < first:
        return []
    cursor = value_of(kind, final)
    if complete_only and last_day(kind, cursor) > limit:
        cursor = previous(kind, cursor)
    values: list[str] = []
    while parse(kind, cursor) >= first:
        values.append(cursor)
        cursor = previous(kind, cursor)
    return values


def variables(kind: str, value: str) -> dict[str, str]:
    """Placeholders available to request templates for one period."""
    first, last = parse(kind, value), last_day(kind, value)
    return {
        "period": value,
        "date": first.isoformat(), "date_compact": first.strftime("%Y%m%d"),
        "start": first.isoformat(), "end": last.isoformat(),
        "start_compact": first.strftime("%Y%m%d"), "end_compact": last.strftime("%Y%m%d"),
        "month": first.strftime("%Y-%m"), "month_compact": first.strftime("%Y%m"),
        "year": f"{first.year:04d}",
    }
