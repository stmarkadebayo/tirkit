"""PII redaction with typed placeholders (SPEC FR-3, FR-8, NFR-2).

Replaces emails, phone numbers, payment-card numbers (Luhn-checked), API keys and secrets, IPv4
addresses and IBANs with ``[EMAIL]``, ``[PHONE]``, ``[CARD]``, ``[API_KEY]``, ``[IP]``, ``[IBAN]``.
Extra patterns can be passed per call (e.g. customer IDs).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from tirkit.media import is_media_ref

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Well-known secret formats first, then generic "key-ish" tokens.
API_KEYS = [
    re.compile(r"\bsk-(?:proj-|ant-(?:api\d{2}-)?)?[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),  # Stripe
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\",;]{8,})"
    ),
]
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,4}){2,4}(?!\w)"
)
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def _phone(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    # 7-15 digits (E.164 max); avoid years, versions and plain numbers.
    return "[PHONE]" if 7 <= len(digits) <= 15 else m.group(0)


def redact(text: str, extra: Iterable[tuple[str | re.Pattern[str], str]] = ()) -> str:
    """Return ``text`` with PII replaced by placeholders."""
    if not text:
        return text
    out = text
    for pattern, label in extra:
        out = re.sub(pattern, label, out)
    out = EMAIL.sub("[EMAIL]", out)
    for pat in API_KEYS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(0).replace(m.group(1), "[API_KEY]"), out)
        else:
            out = pat.sub("[API_KEY]", out)
    out = IBAN.sub("[IBAN]", out)
    out = CARD.sub(_card, out)
    out = IPV4.sub("[IP]", out)
    out = PHONE.sub(_phone, out)
    return out


def redact_obj(obj: Any, extra: Iterable[tuple[str | re.Pattern[str], str]] = ()) -> Any:
    """Redact every string inside a JSON-like tree (media references are left intact)."""
    extra = list(extra)
    if isinstance(obj, str):
        return redact(obj, extra)
    if is_media_ref(obj):
        return obj
    if isinstance(obj, dict):
        return {k: redact_obj(v, extra) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_obj(v, extra) for v in obj]
    return obj
