"""Small shared types for workflow outcomes."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Outcome:
    ok: bool
    reason: str
    data: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # allows `if outcome:` in scripts
        return self.ok


def deny(reason: str, **data) -> Outcome:
    return Outcome(False, reason, data)


def allow(reason: str = "ok", **data) -> Outcome:
    return Outcome(True, reason, data)
