"""Redaction of secret values from text before it reaches an LLM.

``redact_secrets`` scrubs common secret shapes (PEM keys, cloud/vendor API tokens, and
generic ``key = value`` assignments) out of arbitrary text, replacing each match with a
literal placeholder. It is pure regex over a string — no filesystem or network access —
so it is safe to run on any text (file contents, command output, diffs) on the path
between a local tool result and an outbound LLM call. It never raises: unrecognized or
malformed input simply comes back unchanged with a zero count.
"""

from __future__ import annotations

import re

_PLACEHOLDER = "«redacted»"

# Each pattern redacts its *entire* match unless it has a capture group named to keep a
# prefix (see `_GENERIC_ASSIGNMENT`, which keeps the key name and only redacts the value).
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AWS_ACCESS_KEY_ID = re.compile(r"AKIA[0-9A-Z]{16}")
_GITHUB_TOKEN = re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")
_OPENAI_KEY = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")
_SLACK_TOKEN = re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")
_GOOGLE_API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")

# Generic "key-ish name = long value" assignment. Group 1 is the key name (kept), group 2
# is the secret value (redacted).
_GENERIC_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|access[_-]?key)"
    r"(\s*[:=]\s*)"
    r'["\']?([A-Za-z0-9/+_\-.]{16,})["\']?'
)

# Patterns whose entire match is the secret and gets replaced outright.
_WHOLE_MATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem_private_key", _PEM_PRIVATE_KEY),
    ("aws_access_key_id", _AWS_ACCESS_KEY_ID),
    ("github_token", _GITHUB_TOKEN),
    ("openai_key", _OPENAI_KEY),
    ("slack_token", _SLACK_TOKEN),
    ("google_api_key", _GOOGLE_API_KEY),
    ("bearer_token", _BEARER_TOKEN),
)


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace secret-looking values in ``text`` with a placeholder.

    Runs a fixed sequence of regexes over ``text``: first the "whole match is the
    secret" shapes (PEM blocks, cloud/vendor tokens, bearer tokens), then a generic
    ``key = value`` assignment that keeps the key name and redacts only the value. Each
    match becomes one redaction in the returned count. Never raises; text with no
    matches (including empty input) is returned unchanged with a count of 0.

    Args:
        text: Arbitrary text that may contain secret values (file contents, command
            output, a diff, etc.).

    Returns:
        A tuple of ``(scrubbed_text, number_of_redactions)``.
    """
    if not text:
        return text, 0

    scrubbed = text
    count = 0

    for _name, pattern in _WHOLE_MATCH_PATTERNS:
        scrubbed, n = pattern.subn(_PLACEHOLDER, scrubbed)
        count += n

    def _replace_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{_PLACEHOLDER}"

    scrubbed, n = _GENERIC_ASSIGNMENT.subn(_replace_assignment, scrubbed)
    count += n

    return scrubbed, count
