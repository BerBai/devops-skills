"""filters — noise / level / suppress pipeline shared by tail_multi siblings.

Public surface:
    LEVEL_ORDER: dict[str, int]
    infer_level(scheme, fields, body) -> str
    build_pipeline(no_default, min_level, suppress, aggressive, user_overrides)
        -> FilterPipeline
    FilterPipeline.should_drop(line: FetchedLine) -> bool
    load_user_overrides() -> tuple[list[Pattern], list[Pattern]]

Order of operations (per PRD R6):
    1. keep_always (user)   — highest priority: NEVER drop a keep-always hit
    2. noise (built-in)     — toggle with --no-default-filters
    3. drop (user)          — appended to noise
    4. level (--min-level)  — discard below threshold
    5. suppress (--suppress / --aggressive-suppress) — rate-limit identical
       normalized lines
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# ---------------------------------------------------------------------------
# Level ordering
# ---------------------------------------------------------------------------

LEVEL_ORDER: dict[str, int] = {
    "trace": 0,
    "debug": 1,
    "info": 2,
    "warn": 3,
    "warning": 3,
    "error": 4,
    "err": 4,
    "fatal": 5,
    "critical": 5,
    "crit": 5,
    "alert": 5,
    "panic": 5,
}

# Map syslog priority (0..7) to canonical level strings.
# syslog: 0=emerg, 1=alert, 2=crit, 3=err, 4=warn, 5=notice, 6=info, 7=debug
_SYSLOG_PRIORITY_MAP: dict[int, str] = {
    0: "fatal", 1: "fatal", 2: "fatal", 3: "error",
    4: "warn", 5: "info", 6: "info", 7: "debug",
}

# Embedded level token like ``WARN:`` / ``[ERROR]`` / ``level=warning``.
_LEVEL_INLINE_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|PANIC)\b",
    re.IGNORECASE,
)
# nginx-style status code 5xx → error (PRD/noise_reduction.md L96).
_NGINX_5XX_RE = re.compile(r"\b[A-Z]+ \S+ HTTP/[\d.]+\D+5\d\d\b|\b\" 5\d\d\b")

# Repeat threshold for syslog-style suppression (per PRD R6).
_SUPPRESS_THRESHOLD = 5


def infer_level(scheme: str, fields: dict[str, str], body: str) -> str:  # noqa: PLR0911 — scheme-by-scheme heuristic
    """Best-effort level inference per scheme.

    Returns canonical "trace"|"debug"|"info"|"warn"|"error"|"fatal".
    Unknown -> "info" so ``--min-level warn`` strips it (per PRD R6 doc).
    """
    if scheme == "journal":
        prio = fields.get("PRIORITY")
        if prio is not None:
            try:
                return _SYSLOG_PRIORITY_MAP.get(int(prio), "info")
            except (ValueError, TypeError):
                pass

    if _NGINX_5XX_RE.search(body):
        return "error"

    m = _LEVEL_INLINE_RE.search(body)
    if m:
        raw = m.group(1).lower()
        if raw == "warning":
            return "warn"
        if raw == "err":
            return "error"
        if raw in {"critical", "panic"}:
            return "fatal"
        return raw

    return "info"


# ---------------------------------------------------------------------------
# Default noise deny-list — copied from references/noise_reduction.md
# ---------------------------------------------------------------------------

_DEFAULT_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Healthcheck / liveness / readiness (noise_reduction.md "默认丢什么")
    re.compile(r"\bGET /healthz?\b"),
    re.compile(r"\bGET /ready\b"),
    re.compile(r"\bGET /ping\b"),
    re.compile(r"\bkube-probe/[\d.]+"),
    re.compile(r"\bELB-HealthChecker/"),
    # Static assets
    re.compile(r"\bGET /favicon\.ico"),
    re.compile(r"\bGET /robots\.txt"),
    # CORS preflight
    re.compile(r"\bOPTIONS \b.*\b 204\b"),
    # k8s heartbeat / leader-elect
    re.compile(r"\bleader-elect\b"),
    re.compile(r"\belectionRunner\b"),
    # systemd unit start/stop
    re.compile(r"\bStarted \w+\.service\b"),
    re.compile(r"\bStopped \w+\.service\b"),
)


# ---------------------------------------------------------------------------
# Suppress (syslog-style rate limit)
# ---------------------------------------------------------------------------


@dataclass
class _SuppressEntry:
    first_seen: float
    count: int


# Lines normalized so that high-cardinality fields don't defeat rate limiting.
_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_PID_RE = re.compile(r"\[\d+\]")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_TRACE_ID_RE = re.compile(r"\b(?:trace_id|request_id|req_id)=\S+", re.IGNORECASE)
_HEX_RE = re.compile(r"\b[a-fA-F0-9]{8,}\b")
_DIGIT_RE = re.compile(r"\d+")


def _normalize_key(body: str, *, aggressive: bool) -> str:
    """Strip high-cardinality fields so that the same logical line dedupes."""
    s = body
    s = _TS_RE.sub("<TS>", s)
    s = _PID_RE.sub("[<PID>]", s)
    s = _IPV4_RE.sub("<IP>", s)
    s = _TRACE_ID_RE.sub("<TRACE>", s)
    if aggressive:
        s = _HEX_RE.sub("<HEX>", s)
        s = _DIGIT_RE.sub("<N>", s)
    return s.strip()


# ---------------------------------------------------------------------------
# FilterPipeline
# ---------------------------------------------------------------------------


class _LineLike(Protocol):
    body: str
    level: str


@dataclass
class FilterPipeline:
    """Stateful three-stage filter (noise -> level -> suppress)."""

    noise_patterns: list[re.Pattern[str]]
    user_drop: list[re.Pattern[str]]
    user_keep_always: list[re.Pattern[str]]
    min_level: str
    suppress: bool
    aggressive: bool
    _suppress_state: dict[str, _SuppressEntry] = field(default_factory=dict)
    _suppressed_summary: dict[str, int] = field(default_factory=dict)

    def should_drop(self, line: _LineLike) -> bool:  # noqa: PLR0911 — distinct rule branches
        body = line.body
        # 1. keep_always wins
        if any(p.search(body) for p in self.user_keep_always):
            return False

        # 2 + 3. noise + user drop
        for p in self.noise_patterns:
            if p.search(body):
                return True
        for p in self.user_drop:
            if p.search(body):
                return True

        # 4. level
        level = (line.level or "info").lower()
        min_rank = LEVEL_ORDER.get(self.min_level, 0)
        line_rank = LEVEL_ORDER.get(level, LEVEL_ORDER["info"])
        if line_rank < min_rank:
            return True

        # 5. rate suppress
        if self.suppress:
            key = _normalize_key(body, aggressive=self.aggressive)
            now = time.monotonic()
            entry = self._suppress_state.get(key)
            if entry is None or now - entry.first_seen >= 1.0:
                self._suppress_state[key] = _SuppressEntry(now, 1)
                return False
            entry.count += 1
            if entry.count > _SUPPRESS_THRESHOLD:
                self._suppressed_summary[key] = (
                    self._suppressed_summary.get(key, 0) + 1
                )
                return True

        return False

    def drain_suppressed(self) -> dict[str, int]:
        """Return-and-clear the suppressed-count map for envelope summary."""
        out = dict(self._suppressed_summary)
        self._suppressed_summary.clear()
        return out


def build_pipeline(
    *,
    no_default: bool,
    min_level: str,
    suppress: bool,
    aggressive: bool,
    user_overrides: tuple[list[re.Pattern[str]], list[re.Pattern[str]]],
) -> FilterPipeline:
    """Construct a FilterPipeline. ``user_overrides = (drop, keep_always)``."""
    drop, keep_always = user_overrides
    noise = [] if no_default else list(_DEFAULT_NOISE_PATTERNS)
    return FilterPipeline(
        noise_patterns=noise,
        user_drop=list(drop),
        user_keep_always=list(keep_always),
        min_level=min_level,
        suppress=suppress,
        aggressive=aggressive,
    )


# ---------------------------------------------------------------------------
# User overrides — ~/.devops/log_filters.json
# ---------------------------------------------------------------------------


_USER_OVERRIDES_PATH = Path.home() / ".devops" / "log_filters.json"


def load_user_overrides(
    path: Path | None = None,
) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    """Load (drop_patterns, keep_always_patterns) from JSON config.

    Missing file -> ``([], [])``. Malformed JSON / non-compilable regex are
    silently skipped so a typo in the user config can't crash the script.
    """
    p = path if path is not None else _USER_OVERRIDES_PATH
    if not p.exists():
        return [], []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    drop_raw = data.get("drop", []) if isinstance(data, dict) else []
    keep_raw = data.get("keep_always", []) if isinstance(data, dict) else []
    drop: list[re.Pattern[str]] = []
    keep: list[re.Pattern[str]] = []
    for pat in drop_raw:
        if not isinstance(pat, str):
            continue
        try:
            drop.append(re.compile(pat))
        except re.error:
            continue
    for pat in keep_raw:
        if not isinstance(pat, str):
            continue
        try:
            keep.append(re.compile(pat))
        except re.error:
            continue
    return drop, keep


__all__ = [
    "LEVEL_ORDER",
    "infer_level",
    "FilterPipeline",
    "build_pipeline",
    "load_user_overrides",
]
