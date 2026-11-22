"""Detection and masking of secrets before a repository pack is produced."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class SecretFinding:
    """A suspected secret, with a redacted description only."""

    name: str
    line: int
    redacted: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.name} ({self.redacted})"


# A credential-like key, also inside longer names such as DB_PASSWORD or
# OPENAI_API_KEY. Lookarounds replace \b, which fails next to "_".
_KEY = (
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*"
    r"(?:api[_-]?key|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?key|"
    r"(?:access|auth|refresh|bearer|api)[_-]?token|token|password|passwd|pwd)"
    r"(?![A-Za-z0-9])"
)

_PATTERN_DEFS: tuple[tuple[str, str], ...] = (
    ("private key", r"(?P<value>-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----)"),
    ("AWS access key", r"\b(?P<value>(?:AKIA|ASIA)[0-9A-Z]{16})\b"),
    ("GitHub token", r"\b(?P<value>(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}))"),
    ("Slack token", r"\b(?P<value>xox[abposr]-[A-Za-z0-9-]{10,})\b"),
    ("Google API key", r"\b(?P<value>AIza[0-9A-Za-z_-]{35})"),
    ("OpenAI/Anthropic key", r"(?<![A-Za-z0-9_-])(?P<value>sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{20,})"),
    ("Stripe key", r"\b(?P<value>[sr]k_(?:live|test)_[A-Za-z0-9]{16,})"),
    ("bearer token", r"\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})"),
    (
        "credential assignment",
        r"(?i)" + _KEY + r"['\"]?\s*[:=]\s*(?P<quote>['\"])(?P<value>[^'\"\n]{4,})(?P=quote)",
    ),
    (
        "credential in URL",
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:@/]+:(?P<value>[^\s@/]{4,})@",
    ),
)

# Unquoted values (YAML, INI, TOML-like, .env). Only used for config files,
# because in Python "password = get_password()" is not a secret.
_UNQUOTED_DEF = (
    "credential assignment",
    r"(?im)^[ \t]*(?:export[ \t]+)?" + _KEY + r"[ \t]*[:=][ \t]*"
    r"(?P<value>(?![\$%{<'\"\[]|null\b|none\b|true\b|false\b)[^\s#;,'\"]{4,})",
)

PATTERNS = tuple((name, re.compile(pattern)) for name, pattern in _PATTERN_DEFS)
CONFIG_PATTERNS = PATTERNS + ((_UNQUOTED_DEF[0], re.compile(_UNQUOTED_DEF[1])),)
MASK = "***MASKED***"

# File names that are refused outright, whatever their content.
_SENSITIVE_NAMES = {
    ".netrc", ".pypirc", ".pgpass", ".htpasswd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SENSITIVE_STEMS = {"credentials", "credential", "secrets", "secret", "service-account", "service_account"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}


def is_sensitive_file(path: str) -> bool:
    """True for files that usually hold credentials (.env, credentials.json, keys)."""
    name = PurePosixPath(path).name.lower()
    if name == ".env" or name.startswith(".env."):
        return name not in {".env.example", ".env.sample", ".env.template"}
    if name in _SENSITIVE_NAMES or PurePosixPath(name).suffix in _SENSITIVE_SUFFIXES:
        return True
    stem = name.split(".", 1)[0]
    return stem in _SENSITIVE_STEMS or stem.startswith(("service-account", "service_account"))


def _value_span(match: re.Match[str]) -> tuple[int, int]:
    try:
        return match.span("value")
    except IndexError:
        return match.span()


def _patterns(config: bool) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return CONFIG_PATTERNS if config else PATTERNS


def scan_text(text: str, *, config: bool = False) -> list[SecretFinding]:
    """Find suspected secrets without returning their values.

    config=True also flags unquoted values, as found in YAML, INI or .env files.
    """
    found: dict[tuple[int, int], SecretFinding] = {}
    lines = text.splitlines()
    for name, pattern in _patterns(config):
        for match in pattern.finditer(text):
            start, end = _value_span(match)
            if (start, end) in found:
                continue
            line_no = text.count("\n", 0, start) + 1
            original = lines[line_no - 1] if line_no <= len(lines) else ""
            redacted = original[: start - (text.rfind("\n", 0, start) + 1)] + MASK
            redacted = redacted[:80] + ("..." if len(redacted) > 80 else "")
            found[(start, end)] = SecretFinding(name, line_no, redacted)
    return sorted(found.values(), key=lambda item: (item.line, item.name))


def mask_text(text: str, *, config: bool = False) -> str:
    """Replace suspected secret values with a fixed marker."""
    spans = [
        _value_span(match) for _name, pattern in _patterns(config) for match in pattern.finditer(text)
    ]
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
    return mask_text(message, config=True)
