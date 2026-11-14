"""Presentation feed: mirrors the employee's pre-submission dialogue to the Workflow Monitor during a demonstration.

The feedback check is deliberately not recorded (PR-30), so the audit trail cannot show it. For an audience, the demo
script (and the review app's presentation mode) write a separate, non-authoritative feed of what the employee saw and
did: the check, edits, responses, and the submission. It is off by default, holds only synthetic demo text, is never
read by any workflow, and is wiped with the environment. A deployment leaves it off.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FEED_FILE = "presentation_feed.jsonl"
RESPONSE_LABELS = {"accurate": "Accurate", "not_intended": "Not what I intended",
                   "confirm_new": "Yes, new work: flag for TTO lead review", "not_new": "Not new: existing task"}


def enabled_by_env() -> bool:
    return os.environ.get("STRATACASTER_PRESENTATION", "").lower() in ("1", "true", "yes", "on")


class PresentationFeed:
    def __init__(self, path: Path, enabled: bool = False):
        self.path, self.enabled = Path(path), enabled

    def emit(self, kind: str, actor: str, clock_ts: str = "", **data) -> None:
        if not self.enabled:
            return
        row = {"kind": kind, "actor": actor, "ts": clock_ts, "wall": time.time(), **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def acting(self, actor: str, ts: str, role: str) -> None:
        """A person was selected in the review app to act in a role (the People Monitor shows them at once)."""
        self.emit("acting", actor, ts, role=role)

    # ---------------------------------------------------------------- event builders (what the employee saw)

    def check(self, actor: str, ts: str, month: str, data: dict, registry, draft: int = 1) -> None:
        rows = []
        for r in data["statements"]:
            rows.append({"index": r["index"], "text": r["text"], "tag": r["tag"],
                         "aligned": [f"{a['req_task_id']} {a['title']}" for a in r["aligned"]],
                         "suggested_task": (r.get("suggested_task") or {}).get("title"),
                         "similar_elsewhere": [f"{e['req_task_id']} {e['title']} ({e['tto_id']})"
                                               for e in r.get("similar_elsewhere", [])]})
        self.emit("check", actor, ts, month=month, draft=draft, engine=data.get("engine"),
                  role_req_id=data.get("role_req_id"), statements=rows)

    def edit(self, actor: str, ts: str, index: int, before: str, after: str) -> None:
        self.emit("edit", actor, ts, index=index, before=before, after=after)

    def responses(self, actor: str, ts: str, responses: dict) -> None:
        self.emit("responses", actor, ts, responses=[
            {"index": int(i), "response": RESPONSE_LABELS.get(r["response"], r["response"]),
             "intended": r.get("intended_req_task_id"), "note": r.get("note") or ""} for i, r in responses.items()])

    def submitted(self, actor: str, ts: str, outcome) -> None:
        if not outcome.ok:
            self.emit("submit_denied", actor, ts, reason=outcome.reason)
            return
        a = (outcome.data.get("assessment") or {}).get("assessment") or {}
        fb = a.get("employee_feedback") or {}
        self.emit("submitted", actor, ts, input_id=outcome.data["input_id"], input_tag=a.get("input_tag"),
                  aligned_req_task_ids=a.get("aligned_req_task_ids", []),
                  recommended=[s["suggested_task"]["title"] for s in a.get("statements", [])
                               if s.get("suggested_task")],
                  differed=fb.get("check_differed", 0), needs_review=fb.get("needs_higher_review", False))


def read_feed(path: Path, after: int) -> tuple[list[dict], int]:
    """New feed rows after line `after`, and the current line count (smaller than `after` after a reset)."""
    if not path.exists():
        return [], 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], after
    rows = []
    for i, line in enumerate(lines[after:], after + 1):
        try:
            rows.append(json.loads(line) | {"line": i})
        except json.JSONDecodeError:
            continue
    return rows, len(lines)
