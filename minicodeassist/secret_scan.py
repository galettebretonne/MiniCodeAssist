"""Detection and masking of secrets before a repository pack is produced."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    """A suspected secret, with a redacted description only."""

    name: str
    line: int
    redacted: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.name} ({self.redacted})"


_PATTERN_DEFS: tuple[tuple[str, str], ...] = (
    ("private key", r"(?P<value>-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----)"),
    ("AWS access key", r"\b(?P<value>AKIA[0-9A-Z]{16})\b"),
    ("GitHub token", r"\b(?P<value>gh[pousr]_[A-Za-z0-9]{20,})\b"),
    ("Slack token", r"\b(?P<value>xox[abposr]-[A-Za-z0-9-]{10,})\b"),
    ("Google API key", r"\b(?P<value>AIza[0-9A-Za-z_-]{35})\b"),
    ("bearer token", r"\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})"),
    (
        "credential assignment",
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
        r"password|passwd|pwd)\b['\"]?\s*[:=]\s*(?P<quote>['\"])(?P<value>[^'\"]{4,})(?P=quote)",
    ),
    (
        "credential in URL",
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:@/]+:(?P<value>[^\s@/]{4,})@",
    ),
)

PATTERNS = tuple((name, re.compile(pattern)) for name, pattern in _PATTERN_DEFS)
MASK = "***MASKED***"


def _value_span(match: re.Match[str]) -> tuple[int, int]:
    try:
        return match.span("value")
    except IndexError:
        return match.span()


def scan_text(text: str) -> list[SecretFinding]:
    """Find suspected secrets without returning their values."""
    findings: list[SecretFinding] = []
    lines = text.splitlines()
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = _value_span(match)
            line_no = text.count("\n", 0, start) + 1
            original = lines[line_no - 1] if line_no <= len(lines) else ""
            redacted = original[: start - (text.rfind("\n", 0, start) + 1)] + MASK
            redacted = redacted[:80] + ("..." if len(redacted) > 80 else "")
            findings.append(SecretFinding(name, line_no, redacted))
    return sorted(findings, key=lambda item: (item.line, item.name))


def mask_text(text: str) -> str:
    """Replace suspected secret values with a fixed marker."""
    spans: list[tuple[int, int]] = []
    for _name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            spans.append(_value_span(match))

    if not spans:
        return text

    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(MASK)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def redact_message(message: str) -> str:
    """Mask secrets in arbitrary messages, such as subprocess stderr."""
    return mask_text(message)
