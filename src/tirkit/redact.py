"""PII redaction with typed placeholders (SPEC FR-3, FR-8, NFR-2).

Replaces emails, phone numbers, payment-card numbers (Luhn-checked), API keys and secrets, IPv4
addresses and IBANs with ``[EMAIL]``, ``[PHONE]``, ``[CARD]``, ``[API_KEY]``, ``[IP]``, ``[IBAN]``.
Extra patterns can be passed per call (e.g. customer IDs).

Three modes (:data:`MODES`): ``off``, ``secrets`` (keys, tokens, cards, IBANs, passwords; the
default for new projects) and ``strict`` (also emails, phone numbers and IP addresses). See
:class:`Redactor`.
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
# One cheap scan that must match whenever any pattern in API_KEYS can: ordinary text (the common
# case) then skips all nine. When you add an API_KEYS pattern, add its marker here (a test checks).
API_KEY_TRIGGER = re.compile(
    r"sk-|AKIA|ASIA|gh[pousr]_|github_pat_|xox[abprs]-|AIza|_live_|_test_|eyJ"
    r"|(?i:api[_-]?key|secret|token|password|passwd|pwd)"
)
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
PHONE = re.compile(
    r"(?<![\w+])(?<!\w[-./])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,4}){2,4}(?!\w)"
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


_DATE_PREFIX = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?!\d)"
)
_SEPARATOR = re.compile(r"[\s.-]")


def _is_phone(text: str) -> bool:
    """7-15 digits (E.164) written like a phone number.

    To avoid mangling IDs, timestamps and numbers, a bare digit run is not a phone number: it needs
    a leading ``+``, an area code in parentheses, or at least two separators (``415-555-0134``,
    ``020 7946 0958``). Dates, decimals and lists of equal-sized groups are skipped.
    """
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return False
    if _DATE_PREFIX.match(text):
        return False
    if text.startswith(("+", "(")) or "(" in text:
        return True
    groups = re.findall(r"\d+", text)
    if len(groups) >= 3 and len(set(map(len, groups))) == 1 and len(groups[0]) >= 3:
        return False  # "100 200 300", "1024 2048 4096": number lists, not phone numbers
    return len(_SEPARATOR.findall(text)) >= 2


MODES = ("off", "secrets", "strict")
DEFAULT_MODE = "secrets"
# Span attribute where the SDK records how many values it redacted before sending.
SDK_REDACTIONS_ATTR = "tirkit.redactions"


def mode_from_settings(settings: dict[str, Any] | None) -> str:
    """A project's redaction mode. Projects saved before modes existed have only the on/off
    ``redact`` flag: on means ``strict`` (what it did then), off or missing means ``off``."""
    settings = settings or {}
    mode = settings.get("redact_mode")
    if mode in MODES:
        return str(mode)
    return "strict" if settings.get("redact") else "off"


Pattern = str | re.Pattern[str]


class Redactor:
    """Redacts strings and JSON-like trees in one mode and counts what it replaced.

    - ``off``: nothing.
    - ``secrets``: API keys and tokens, JWTs, ``password=``-style assignments, Luhn-valid card
      numbers and IBANs. High precision; the default for new projects.
    - ``strict``: ``secrets`` plus emails, phone numbers and IPv4 addresses.

    Extra ``(pattern, label)`` pairs (for example customer IDs) run first in both active modes.
    """

    def __init__(self, mode: str = "strict", extra: Iterable[tuple[Pattern, str]] = ()) -> None:
        if mode not in MODES:
            raise ValueError(f"redaction mode must be one of {MODES}")
        self.mode = mode
        self.extra = [(re.compile(p) if isinstance(p, str) else p, label) for p, label in extra]
        self.count = 0

    def _sub(self, pat: re.Pattern[str], repl: Any, text: str) -> str:
        if callable(repl):
            hits = 0

            def wrapped(m: re.Match[str]) -> str:
                nonlocal hits
                out = str(repl(m))
                if out != m.group(0):
                    hits += 1
                return out

            text = pat.sub(wrapped, text)
            self.count += hits
            return text
        text, n = pat.subn(repl, text)
        self.count += n
        return text

    def text(self, text: str) -> str:
        if not text or self.mode == "off":
            return text
        out = text
        for pattern, label in self.extra:
            out = self._sub(pattern, label, out)
        strict = self.mode == "strict"
        if strict:
            out = self._sub(EMAIL, "[EMAIL]", out)
        if API_KEY_TRIGGER.search(out):
            for pat in API_KEYS:
                if pat.groups:
                    out = self._sub(pat, lambda m: m.group(0).replace(m.group(1), "[API_KEY]"), out)
                else:
                    out = self._sub(pat, "[API_KEY]", out)
        out = self._sub(IBAN, "[IBAN]", out)
        out = self._sub(CARD, _card, out)
        if strict:
            out = self._sub(IPV4, "[IP]", out)
            out = self._sub(PHONE, _phone, out)
        return out

    def obj(self, obj: Any) -> Any:
        """Redact every string inside a JSON-like tree (media references are left intact)."""
        if self.mode == "off":
            return obj
        if isinstance(obj, str):
            return self.text(obj)
        if is_media_ref(obj):
            return obj
        if isinstance(obj, dict):
            return {k: self.obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.obj(v) for v in obj]
        return obj


def _phone(m: re.Match[str]) -> str:
    return "[PHONE]" if _is_phone(m.group(0)) else m.group(0)


def redact(text: str, extra: Iterable[tuple[Pattern, str]] = (), mode: str = "strict") -> str:
    """Return ``text`` with PII replaced by placeholders (``strict`` unless ``mode`` says)."""
    return Redactor(mode, extra).text(text)


def redact_obj(obj: Any, extra: Iterable[tuple[Pattern, str]] = (), mode: str = "strict") -> Any:
    """Redact every string inside a JSON-like tree (media references are left intact)."""
    return Redactor(mode, extra).obj(obj)
