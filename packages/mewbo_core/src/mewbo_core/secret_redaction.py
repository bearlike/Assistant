#!/usr/bin/env python3
"""Redact secret values out of every log record at the single sink chokepoint.

``SecretRedactor`` is a pure, I/O-free atomic class. One instance is installed
as loguru's global patcher (``logger.configure(patcher=...)``) so every record —
the interpolated message text and the bound ``extra`` mapping — is scrubbed
before any handler writes it. The same instance is usable directly at a
deliberate-dump call site (``redact_mapping``) to scrub a payload BEFORE it is
interpolated into a message, which is where key-name heuristics still apply: by
patcher time the arguments have already been formatted into the message string
and only the value-SHAPE heuristics can still recognise a secret.

``install_stdlib_log_bridge`` routes the standard ``logging`` module's records
into loguru so third-party libraries (litellm, langfuse, httpx, ldap3, pymongo,
werkzeug) are scrubbed by the SAME patcher — the bridge only reroutes, it never
redacts on its own.

Two heuristics, deliberately conservative:

* **Key-name** — a dict key / env-var name whose (camelCase-split) form contains
  ``token``/``secret``/``password``/``passwd``/``bearer``/``authorization``/
  ``credential``/``apikey``, or has ``key`` as a whole token, marks its value as
  secret. This is the strong heuristic — it catches an arbitrary-shaped value —
  but it only works on structured data (``extra`` and explicit ``redact_mapping``
  call sites), not on a value already flattened into a message string.
* **Value-shape** — a small, documented set of vendor-published token prefixes
  (``ghp_``/``github_pat_``/``sk-``/``xox?-``/``AKIA``/``AIza``/JWT), a
  ``Bearer <token>`` scheme, and ``secret_key=value`` pairs in a stringified
  mapping. This is the net that still fires once a secret has landed in free
  text, where the key name is gone.

Redaction output is always ``<redacted len=N>``, revealing at most the length.

Fail-safe: any exception inside the redactor degrades the record to a
``[redaction-error]`` marker with its payload dropped — an unscrubbable record
must never ship raw.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from functools import lru_cache
from types import FrameType
from typing import Any

REDACTION_ERROR_MARKER = "[redaction-error]"


def _redacted(length: int) -> str:
    """Render the length-only redaction placeholder."""
    return f"<redacted len={length}>"


# --- Key-name heuristic --------------------------------------------------

# Words that mark a value secret, matched case-insensitively as a SUBSTRING of
# the camelCase-split key ("access_token"/"clientSecret"/"apiKey" all hit).
# Bare "key" is intentionally NOT here — it would fire on "monkey"/"keyboard" —
# and is handled as a whole-token match instead.
_SECRET_KEY_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "bearer",
    "authorization",
    "credential",
    "apikey",
    "cookie",  # Cookie / Set-Cookie carry session credentials
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@lru_cache(maxsize=4096)
def _classify_key(key: str) -> bool:
    """Return True when a key name marks its value as secret.

    Pure and cached: the same handful of key names recur on every record, so the
    classification runs once per distinct name for the process lifetime — the
    hot-path optimisation the patcher relies on.
    """
    normalized = _CAMEL_BOUNDARY.sub("_", key).lower()
    if any(word in normalized for word in _SECRET_KEY_SUBSTRINGS):
        return True
    # Whole-token "key" only: "api_key"/"secret_key" hit, "monkey"/"turkey" do not.
    return "key" in _KEY_TOKEN_SPLIT.split(normalized)


# --- Value-shape heuristics ----------------------------------------------

# Vendor-published token SHAPES (prefix + length). Each is specific enough that
# a benign identifier is very unlikely to collide. This is the net for a secret
# already interpolated into a message, where the key name is gone.
_TOKEN_SHAPE_RE = re.compile(
    "|".join(
        (
            r"ghp_[A-Za-z0-9]{20,}",  # GitHub PAT (classic)
            r"gh[ousr]_[A-Za-z0-9]{20,}",  # GitHub oauth/user/server/refresh
            r"github_pat_[A-Za-z0-9_]{20,}",  # GitHub PAT (fine-grained)
            r"sk-[A-Za-z0-9_-]{20,}",  # OpenAI / Anthropic-style API key
            r"xox[abprs]-[A-Za-z0-9-]{10,}",  # Slack token
            r"AKIA[0-9A-Z]{16}",  # AWS access key id
            r"AIza[0-9A-Za-z_-]{35}",  # Google API key
            # JWT: three base64url segments (header starts with the "eyJ" of {")
            r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}",
        )
    )
)

# "Bearer <token>" in free text — keep the scheme word, redact the credential.
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")

# Auth SCHEME labels that are values, not secrets — the KV net must not redact
# the word itself (the credential beside it is handled by the shape/bearer nets).
_SCHEME_WORDS = frozenset({"bearer", "basic", "digest", "negotiate"})

# key=value / "key": "value" in a stringified mapping (env/header/config dump)
# where the KEY names a secret — catches arbitrary-shaped values the shape net
# cannot recognise. Bare "key" is omitted (free-text false positives such as
# "sort key=name"); it is covered on the structured path via _classify_key. The
# quote group uses ``(?P<q>['"])?`` (quantifier OUTSIDE the group) so an absent
# quote leaves the group non-participating — an ``['"]?`` INSIDE the group would
# "participate" by matching empty and force the conditional's quoted branch,
# which greedily eats across spaces.
_KV_RE = re.compile(
    r"""(?ix)
    (?P<key>
        ['"]? [\w.\-]*
        (?: token | secret | password | passwd | bearer | authorization
          | credential | api[_-]?key | access[_-]?key | private[_-]?key )
        [\w.\-]* ['"]?
    )
    (?P<sep> \s* [:=] \s* )
    (?P<q> ['"] )?
    (?P<val> (?(q) [^'"]{1,4096} | [^\s'",}\]]{1,4096} ) )
    (?(q) (?P=q) )
    """
)


class SecretRedactor:
    """Pure, I/O-free redactor: install as loguru's patcher or call directly."""

    # --- text ------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        """Scrub token shapes, ``Bearer`` credentials and secret ``key=value``.

        Returns the input unchanged when nothing matches (a normal log line
        passes through byte-for-byte).
        """
        if not text:
            return text
        out = _TOKEN_SHAPE_RE.sub(self._shape_sub, text)
        out = _BEARER_RE.sub(self._bearer_sub, out)
        # The key=value net is the costliest pass; skip it unless a separator is
        # even present (the vast majority of log lines have neither).
        if ":" in out or "=" in out:
            out = _KV_RE.sub(self._kv_sub, out)
        return out

    @staticmethod
    def _shape_sub(match: re.Match[str]) -> str:
        return _redacted(len(match.group(0)))

    @staticmethod
    def _bearer_sub(match: re.Match[str]) -> str:
        return f"{match.group(1)}{_redacted(len(match.group(2)))}"

    @staticmethod
    def _kv_sub(match: re.Match[str]) -> str:
        value = match.group("val")
        stripped = value.strip("'\"")
        # Leave a scheme label (Bearer/Basic/…) and an already-redacted value
        # untouched — the credential beside a scheme word is handled by the
        # shape/bearer nets, and re-redacting a placeholder only reports its
        # own length.
        if stripped.lower() in _SCHEME_WORDS or stripped.startswith("<redacted"):
            return match.group(0)
        quote = match.group("q") or ""
        return f"{match.group('key')}{match.group('sep')}{quote}{_redacted(len(value))}{quote}"

    # --- values / mappings ----------------------------------------------

    @staticmethod
    def _redact_value(value: Any) -> Any:
        """Replace a secret value with its length; leave ``None`` untouched."""
        if value is None:
            return None
        length = len(value) if isinstance(value, (str, bytes)) else len(str(value))
        return _redacted(length)

    def redact_mapping(self, data: Mapping[Any, Any]) -> dict[Any, Any]:
        """Return a NEW dict with secret-keyed values redacted, recursively.

        Call this at a deliberate-dump site to scrub a payload BEFORE logging it,
        so the key-name heuristic applies to arbitrary-shaped values.
        """
        result: dict[Any, Any] = {}
        for key, value in data.items():
            if _classify_key(str(key)):
                result[key] = self._redact_value(value)
            else:
                result[key] = self._redact_element(value)
        return result

    def _redact_element(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self.redact_mapping(value)
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, (list, tuple)):
            return type(value)(self._redact_element(item) for item in value)
        return value

    def _redact_extra_inplace(self, extra: dict[str, Any]) -> None:
        for key in list(extra.keys()):
            value = extra[key]
            if _classify_key(key):
                extra[key] = self._redact_value(value)
            elif isinstance(value, str):
                extra[key] = self.redact_text(value)
            elif isinstance(value, (Mapping, list, tuple)):
                extra[key] = self._redact_element(value)

    # --- loguru patcher --------------------------------------------------

    def patch(self, record: Any) -> None:
        """Loguru global patcher: scrub message + ``extra`` in place, fail-safe.

        ``record`` is loguru's record dict (typed ``Any`` — it is the library
        interop boundary). Runs once per record before every sink formats it.
        """
        try:
            message = record.get("message")
            if message:
                record["message"] = self.redact_text(message)
            extra = record.get("extra")
            if extra:
                self._redact_extra_inplace(extra)
        except Exception:
            self._degrade(record)

    @staticmethod
    def _degrade(record: Any) -> None:
        """Drop an unscrubbable record's payload — never ship it raw.

        Keeps only the loguru-bound ``name`` so the sink format still renders;
        the original message and every other ``extra`` value are discarded.
        """
        try:
            record["message"] = REDACTION_ERROR_MARKER
            extra = record.get("extra")
            if isinstance(extra, dict):
                name = extra.get("name")
                extra.clear()
                extra["name"] = name if name is not None else "redacted"
        except Exception:
            pass


_REDACTOR = SecretRedactor()


def get_secret_redactor() -> SecretRedactor:
    """Return the process-wide redactor singleton."""
    return _REDACTOR


def install_log_redaction(logger: Any) -> None:
    """Install the redactor as loguru's global patcher (single sink chokepoint).

    ``configure(patcher=...)`` applies the callback to every record across all
    sinks before formatting and leaves already-added handlers intact, so it can
    be called before or after the sinks are added.
    """
    logger.configure(patcher=_REDACTOR.patch)


class _LoguruBridgeHandler(logging.Handler):
    """Forward stdlib ``logging`` records into loguru.

    Redaction stays the ONE global patcher — this handler only reroutes so a
    third-party library's record flows through loguru (and thus the redactor)
    instead of the stdlib last-resort stderr handler. It never redacts itself.
    """

    def __init__(self, logger: Any) -> None:
        super().__init__()
        self._logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            try:
                level: str | int = self._logger.level(record.levelname).name
            except (ValueError, AttributeError):
                level = record.levelno
            # Walk out of the logging module so loguru attributes the right origin.
            frame: FrameType | None = logging.currentframe()
            depth = 2
            while frame is not None and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            # bind(name=…) keeps the ``[{extra[name]}]`` sink format renderable;
            # exceptions render under the sink's diagnose=False, like native logs.
            self._logger.bind(name=record.name).opt(
                depth=depth, exception=record.exc_info
            ).log(level, record.getMessage())
        except Exception:
            # A malformed third-party record must never crash the emitting thread.
            self.handleError(record)


def install_stdlib_log_bridge(logger: Any) -> None:
    """Route stdlib ``logging`` → loguru on the root logger (idempotent).

    Adds the bridge handler so records from third-party libraries reach the
    global redactor patcher. Deliberately does NOT change the root level (no
    volume change — the existing per-logger suppress-list still governs what
    emits) and preserves any OTHER root handlers (e.g. pytest's log capture);
    only a previously-installed bridge is replaced.
    """
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, _LoguruBridgeHandler)]
    root.addHandler(_LoguruBridgeHandler(logger))


# Thin call-site aliases (data/ecosystem convention, not logic) for scrubbing a
# payload before it is interpolated into a log message.
redact_mapping = _REDACTOR.redact_mapping
redact_text = _REDACTOR.redact_text
