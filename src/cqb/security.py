"""Redaction of everything that leaves the Bridge towards an LLM (design section 10).

Pure functions, no I/O. Redaction is deliberately blunt: a false positive costs a few words of
context, a miss leaks a credential. Replacement markers make the loss visible to the reader.
"""

import re
from typing import Any

REDACTED = "[REDACTED]"
BLOB_MIN = 200  # a run of base64/hex-like characters this long is not prose

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.S,
)
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|cookie|set-cookie)\s*[:=]\s*[^\r\n]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|\d{6,}:[A-Za-z0-9_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|private[_-]?key|credential)[A-Za-z0-9_.-]*)
    (["']?\s*[=:]\s*)
    ("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&"']+)
    """
)
_BLOB = re.compile(r"[A-Za-z0-9+/_=-]{%d,}" % BLOB_MIN)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact(text: str) -> tuple[str, bool]:
    """Return the text with secrets masked and whether anything was changed."""
    out = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}: {REDACTED}", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _TOKEN_SHAPES.sub(REDACTED, out)
    out = _ASSIGNMENT.sub(_mask_assignment, out)
    out = _BLOB.sub(lambda m: f"[BLOB {len(m.group(0))} chars omitted]", out)
    out = _CONTROL.sub("", out)
    return out, out != text


def _mask_assignment(m: re.Match[str]) -> str:
    if m.group(3).isdigit():  # max_tokens=4096 is configuration, not a credential
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{REDACTED}"


def redact_json(value: Any) -> Any:
    """Redact every string of a JSON-like structure (keys are left alone)."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: redact_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_json(v) for v in value]
    return value
