"""Injectable clock so freshness and expiration are reproducible in tests and demos."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not _UTC_RE.match(value):
        raise ValueError(f"timestamp must be ISO 8601 UTC with a Z suffix: {value!r}")
    dt = datetime.fromisoformat(value[:-1] + "+00:00")
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    """Fixed or system clock. A fixed clock only moves when advanced explicitly."""

    def __init__(self, fixed: str | None = None):
        self._fixed = parse_utc(fixed) if fixed else None

    @property
    def is_fixed(self) -> bool:
        return self._fixed is not None

    def now(self) -> datetime:
        return self._fixed if self._fixed else datetime.now(timezone.utc)

    def now_str(self) -> str:
        return format_utc(self.now())

    def set(self, value: str) -> None:
        self._fixed = parse_utc(value)

    def advance(self, **kwargs) -> None:
        self._fixed = self.now() + timedelta(**kwargs)
