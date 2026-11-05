"""Scrub personal data and secrets before anything leaves the machine.

Deliberately conservative about what it touches: memory addresses (0x...),
lock ids and line numbers must survive, or triage quality drops."""
from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <TOKEN>"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "<JWT>"),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "<API_KEY>"),
    ("secret_key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|ant|proj)?[-_]?[A-Za-z0-9_-]{16,}"), "<API_KEY>"),
    ("kv_secret", re.compile(r"(?i)\b(token|api_?key|secret|password|passwd|auth)=([^&\s\"']+)"), r"\1=<REDACTED>"),
    ("url_query", re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']+"), r"\1?<QUERY>"),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    ("phone", re.compile(r"(?<![\w.:])\+\d{10,14}\b"), "<PHONE>"),
    # Device ids only when labelled: bare 16-hex strings are also pc values and
    # registers in tombstones, which triage needs.
    ("device_id", re.compile(r"(?i)\b(android_?id|device_?id|gaid|adid|install_?id)(\s*[=:]\s*)[0-9a-f-]{16,36}"), r"\1\2<ID>"),
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<ID>"),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pattern, repl in PATTERNS:
        text, n = pattern.subn(repl, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts
