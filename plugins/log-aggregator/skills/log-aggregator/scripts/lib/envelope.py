"""envelope.py — the one JSON envelope rule every log-aggregator CLI follows.

``tail_multi``, ``grep_across_sources`` and ``correlate`` each used to build
their own envelope and their own exit-code table. They drifted: the same
"one source could not be read" outcome was reported three incompatible ways
(DEVOPS-BUG-012), and ``correlate`` even masked it as a total success
(DEVOPS-BUG-011). This module is the single place the rule lives now.

The contract, applied through :func:`apply_fetch_failures`::

    Any source failed to fetch
        -> success = false
        -> exit_code = 41
        -> data.failure_class = "partial_fetch"
        -> data.failed_fetchers = [{"source": ..., "error": ...}, ...]

    ...and the data that *was* fetched still ships in ``data``, so a caller can
    use the partial result while knowing it is partial.

``fetcher_failed`` was the ``tail_multi`` v1.0 spelling of the same outcome.
It is accepted as an alias (:func:`canonical_failure_class`) but never emitted.

A run that fails outright for a *different*, more specific reason (bad source
spec, unparseable pattern, ``correlate`` anchor not found) keeps that class;
``failed_fetchers`` is still populated so the missing source is not hidden.

Exit codes live here for the same reason: the three CLIs are one plugin and
their hundred-blocks are allocated together. 40-49 are the shared fetch/spec
classes, 90-92 belong to ``correlate``, 180 to ``grep_across_sources``.

    =====  ==========================================
    0      success
    40     source_spec_invalid
    41     partial_fetch  (alias: fetcher_failed)
    42     clock_skew_failed (reserved; warns only today)
    43     normalize_failed
    44     no_sources_resolved
    90     anchor_not_found          (correlate)
    91     window_invalid            (correlate)
    92     pair_unmatched            (correlate)
    124    ssh idle/connect timeout  (propagated)
    180    pattern_invalid           (grep_across_sources)
    =====  ==========================================
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .fetchers import FetcherError

#: failure_class emitted whenever at least one source could not be fetched.
PARTIAL_FETCH = "partial_fetch"

#: The v1.0 tail_multi spelling. Accepted as an input alias, never emitted.
FETCHER_FAILED = "fetcher_failed"

#: Exit code that always accompanies :data:`PARTIAL_FETCH` (all three CLIs).
PARTIAL_FETCH_EXIT_CODE = 41

#: failure_class -> exit_code. Shared by every log-aggregator CLI so the same
#: outcome cannot map to two numbers again.
EXIT_CODES: dict[str | None, int] = {
    None: 0,
    "source_spec_invalid": 40,
    PARTIAL_FETCH: PARTIAL_FETCH_EXIT_CODE,
    FETCHER_FAILED: PARTIAL_FETCH_EXIT_CODE,  # compatibility alias
    "clock_skew_failed": 42,
    "normalize_failed": 43,
    "no_sources_resolved": 44,
    "anchor_not_found": 90,
    "window_invalid": 91,
    "pair_unmatched": 92,
    "pattern_invalid": 180,
    "_generic": 1,
    "_timeout": 124,
}

#: Legacy spelling -> today's spelling. Only listed entries are rewritten.
_ALIASES: dict[str, str] = {
    FETCHER_FAILED: PARTIAL_FETCH,
}


def canonical_failure_class(failure_class: str | None) -> str | None:
    """Return the current spelling for ``failure_class`` (``None`` stays ``None``)."""
    if failure_class is None:
        return None
    return _ALIASES.get(failure_class, failure_class)


def exit_code_for(failure_class: str | None) -> int:
    """Return the exit code for ``failure_class``; unknown names fall back to 1."""
    if failure_class is None:
        return 0
    return EXIT_CODES.get(
        canonical_failure_class(failure_class), EXIT_CODES["_generic"]
    )


def format_fetch_error(exc: BaseException) -> str:
    """Render a fetcher exception for ``data.failed_fetchers[].error``.

    Prefers the last line of the process's stderr — which
    :class:`~lib.fetchers.FetcherError` has already stripped of the sshd login
    banner — over the exception repr.
    """
    if isinstance(exc, FetcherError):
        tail = exc.stderr_tail.strip().splitlines()[-1] if exc.stderr_tail else ""
        return f"rc={exc.returncode} {tail}".strip()
    return f"{type(exc).__name__}: {exc}".strip()


def failure_pairs(failures: Iterable[Any]) -> list[tuple[str, str]]:
    """Normalise the two shapes the CLIs carry into ``(source_tag, error)``.

    ``tail_multi`` keeps ``(tag, message)`` tuples; ``grep_across_sources`` and
    ``correlate`` keep ``FetcherFailure(source_tag=..., error=...)`` records.
    """
    pairs: list[tuple[str, str]] = []
    for item in failures:
        if isinstance(item, tuple) and len(item) == 2:
            source_tag, error = item
        else:
            source_tag = getattr(item, "source_tag", repr(item))
            error = getattr(item, "error", "")
        pairs.append((str(source_tag), str(error)))
    return pairs


def failed_fetcher_records(failures: Iterable[Any]) -> list[dict[str, str]]:
    """The ``data.failed_fetchers`` shape shared by all three envelopes."""
    return [
        {"source": source_tag, "error": error}
        for source_tag, error in failure_pairs(failures)
    ]


def apply_fetch_failures(
    envelope: dict[str, Any], failures: Iterable[Any]
) -> dict[str, Any]:
    """Stamp the shared partial-fetch rule onto an already-built envelope.

    Sets ``success`` / ``exit_code`` / ``data.failure_class`` /
    ``data.failed_fetchers`` from ``failures`` and returns the envelope. The
    rest of ``data`` is left untouched: successfully fetched data still ships.
    """
    records = failed_fetcher_records(failures)
    failure_class = PARTIAL_FETCH if records else None
    data = envelope.setdefault("data", {})
    data["failure_class"] = failure_class
    data["failed_fetchers"] = records
    envelope["success"] = failure_class is None
    envelope["exit_code"] = exit_code_for(failure_class)
    return envelope


__all__ = [
    "EXIT_CODES",
    "FETCHER_FAILED",
    "PARTIAL_FETCH",
    "PARTIAL_FETCH_EXIT_CODE",
    "apply_fetch_failures",
    "canonical_failure_class",
    "exit_code_for",
    "failed_fetcher_records",
    "failure_pairs",
    "format_fetch_error",
]
