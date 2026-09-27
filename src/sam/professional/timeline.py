"""Deterministic date parsing and experience arithmetic.

Pure functions, no I/O, and NO model anywhere: years of experience are computed
by code from resolved dates, never estimated, rounded up, or taken from model
output. Overlapping intervals are merged so a month is never counted twice, and
an interval whose dates conflict (or are incomplete) is EXCLUDED from totals and
reported, never resolved in the owner's favour.

Dates are canonicalized to ``YYYY-MM``, ``YYYY`` (year only) or ``present``.
A year-only date is treated conservatively when a claim needs a lower bound.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

PRESENT = "present"

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_PRESENT_WORDS = frozenset({"present", "current", "now", "ongoing", "today", "to date"})

_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_US = re.compile(r"^(\d{1,2})[-/](\d{4})$")
_NAMED = re.compile(r"^([A-Za-z]{3,9})\.?,?\s+(\d{4})$")
_YEAR = re.compile(r"^(\d{4})$")

MIN_YEAR = 1950
MAX_YEAR = 2100
MIN_GAP_MONTHS = 6


def normalize_date(text: str) -> str | None:
    """Canonical ``YYYY-MM`` / ``YYYY`` / ``present``, or ``None`` if the text is
    not a recognizable date. Never guesses a missing month or year."""

    value = text.strip().strip("()[],;").strip()
    if not value:
        return None
    if value.lower() in _PRESENT_WORDS:
        return PRESENT
    if match := _ISO.match(value):
        return _ym(int(match.group(1)), int(match.group(2)))
    if match := _US.match(value):
        return _ym(int(match.group(2)), int(match.group(1)))
    if match := _NAMED.match(value):
        month = _MONTHS.get(match.group(1).lower())
        return _ym(int(match.group(2)), month) if month else None
    if match := _YEAR.match(value):
        year = int(match.group(1))
        return f"{year:04d}" if MIN_YEAR <= year <= MAX_YEAR else None
    return None


def _ym(year: int, month: int | None) -> str | None:
    if not (MIN_YEAR <= year <= MAX_YEAR) or month is None or not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def dates_agree(a: str, b: str) -> bool:
    """Equal, or the same year at different precision (``2019`` and ``2019-03``).
    ``present`` only agrees with ``present``."""

    if a == b:
        return True
    if PRESENT in (a, b):
        return False
    return a[:4] == b[:4] and len(a) != len(b)


def reconcile_dates(values: Iterable[str]) -> tuple[str | None, bool]:
    """``(value, conflict)``. If every value agrees, the most precise one; if
    any pair disagrees, ``(None, True)``: the conflict is preserved, and no value
    is chosen for the owner."""

    distinct = sorted(set(values))
    if not distinct:
        return None, False
    for i, a in enumerate(distinct):
        for b in distinct[i + 1 :]:
            if not dates_agree(a, b):
                return None, True
    return max(distinct, key=len), False


# --------------------------------------------------------------- intervals


def _index(value: str, *, end: bool, now: date, conservative: bool) -> int:
    """Month index for a canonical date. ``conservative`` picks the bound that
    can only make an interval SHORTER when the month is unknown."""

    if value == PRESENT:
        return now.year * 12 + now.month - 1
    year = int(value[:4])
    if len(value) == 7:
        return year * 12 + int(value[5:7]) - 1
    if end:
        month = 1 if conservative else 12
    else:
        month = 12 if conservative else 1
    return year * 12 + month - 1


@dataclass(frozen=True)
class Interval:
    start: str
    end: str

    def bounds(self, now: date, *, conservative: bool = False) -> tuple[int, int]:
        return (
            _index(self.start, end=False, now=now, conservative=conservative),
            _index(self.end, end=True, now=now, conservative=conservative),
        )

    def is_valid(self, now: date) -> bool:
        low, high = self.bounds(now, conservative=False)
        return high >= low

    def months(self, now: date, *, conservative: bool = False) -> int:
        low, high = self.bounds(now, conservative=conservative)
        return max(0, high - low + 1)

    @property
    def approximate(self) -> bool:
        return len(self.start) == 4 or (len(self.end) == 4)


@dataclass(frozen=True)
class Duration:
    """Whole months, exposed as years and remaining months. Never rounded up."""

    months: int

    @property
    def years(self) -> int:
        return self.months // 12

    @property
    def remainder_months(self) -> int:
        return self.months % 12

    def label(self) -> str:
        return f"{self.years} years {self.remainder_months} months"


def union_months(
    intervals: Sequence[Interval], now: date, *, conservative: bool = False
) -> int:
    """Total months covered by the union of the intervals: overlaps are
    merged, so a month is never counted twice."""

    spans = sorted(
        span
        for interval in intervals
        if interval.is_valid(now)
        for span in [interval.bounds(now, conservative=conservative)]
        if span[1] >= span[0]
    )
    total = 0
    current_start: int | None = None
    current_end = 0
    for low, high in spans:
        if current_start is None:
            current_start, current_end = low, high
        elif low <= current_end:
            current_end = max(current_end, high)
        else:
            total += current_end - current_start + 1
            current_start, current_end = low, high
    if current_start is not None:
        total += current_end - current_start + 1
    return total


def find_gaps(
    intervals: Sequence[Interval], now: date, *, minimum_months: int = MIN_GAP_MONTHS
) -> list[tuple[str, str, int]]:
    """Unexplained gaps between merged intervals of at least ``minimum_months``,
    as ``(from_month, to_month, months)``."""

    spans = sorted(
        interval.bounds(now) for interval in intervals if interval.is_valid(now)
    )
    gaps: list[tuple[str, str, int]] = []
    merged: list[list[int]] = []
    for low, high in spans:
        if merged and low <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    for previous, following in zip(merged, merged[1:], strict=False):
        missing = following[0] - previous[1] - 1
        if missing >= minimum_months:
            gaps.append((_label(previous[1] + 1), _label(following[0] - 1), missing))
    return gaps


def _label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


class IntervalStatus(StrEnum):
    RESOLVED = "resolved"
    CONFLICTED = "conflicted"
    INCOMPLETE = "incomplete"


def today(clock_now: datetime) -> date:
    return clock_now.date()


__all__ = [
    "MIN_GAP_MONTHS",
    "PRESENT",
    "Duration",
    "Interval",
    "IntervalStatus",
    "dates_agree",
    "find_gaps",
    "normalize_date",
    "reconcile_dates",
    "today",
    "union_months",
]
