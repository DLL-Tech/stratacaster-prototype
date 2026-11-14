"""Deterministic content checks. Model output never decides whether content may cross the boundary.

check_summary() runs inside Delivery with access to the source narrative, so it can detect verbatim
copying, not just forbidden field names. scan_source() flags restricted or instruction-like content
in the source itself. check_explanation() screens Solutions output for prohibited personnel language.
"""
from __future__ import annotations

import re

FILTER_VERSION = "prototype-content-filter/1.0"

_PII = {
    "email-address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone-number": re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    "ssn-pattern": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}
_RESTRICTED = re.compile(r"\b(confidential|proprietary|secret|customer extract|classified)\b|delivery-local:", re.I)
_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|system prompt|you are now|"
    r"\bpmo approved\b|mark (this|the) record|set audience|publish (immediately|to all|to everyone)|"
    r"include the raw narrative|disregard (the )?(rules|policy)", re.I)
_PERSONNEL = re.compile(
    r"\b(rank(ed|ing|s)?|top performer|best (employee|engineer|candidate|person)|worst|"
    r"should be (hired|promoted|fired|terminated|reassigned)|performance (rating|score)|"
    r"suitab(le|ility) for (hire|promotion|employment)|recommend (hiring|promoting|firing|staffing))\b", re.I)


def _shingles(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def scan_source(narrative: str) -> list[str]:
    """Blocking exceptions raised by the source content itself (Delivery-side)."""
    out = []
    for code, rx in _PII.items():
        if rx.search(narrative):
            out.append(f"BLOCKING:source-{code}")
    if _RESTRICTED.search(narrative):
        out.append("BLOCKING:source-restricted-content")
    if _INJECTION.search(narrative):
        out.append("BLOCKING:possible-prompt-injection")
    return out


def check_summary(summary: str, narrative: str) -> list[str]:
    """Return blocking exceptions for a proposed cross-boundary evidence summary."""
    out = []
    if not summary.strip():
        out.append("BLOCKING:empty-summary")
    if summary.strip().lower() == narrative.strip().lower() or (_shingles(summary) & _shingles(narrative)):
        out.append("BLOCKING:verbatim-narrative-in-summary")
    for code, rx in _PII.items():
        if rx.search(summary):
            out.append(f"BLOCKING:summary-{code}")
    if _RESTRICTED.search(summary):
        out.append("BLOCKING:summary-restricted-content")
    if _INJECTION.search(summary):
        out.append("BLOCKING:summary-instruction-content")
    if _PERSONNEL.search(summary):
        out.append("BLOCKING:summary-personnel-judgment")
    if len(summary) > 300:
        out.append("BLOCKING:summary-not-minimized")
    return out


def check_explanation(text: str) -> list[str]:
    out = []
    if _PERSONNEL.search(text):
        out.append("personnel-judgment-language")
    for code, rx in _PII.items():
        if rx.search(text):
            out.append(code)
    return out


def blocking(exceptions: list[str]) -> list[str]:
    return [e for e in exceptions if e.startswith("BLOCKING:")]
