"""Bounded retry with exponential backoff for fleet operations (DV-17).

Broadcasting a command across a fleet hits a lot of transient noise:
``Connection refused`` while an sshd restarts, ``Connection timed out`` over a
flaky link, a proxy that drops the first packet. Re-running the whole
broadcast by hand is the usual answer today, which is exactly the kind of
manual step that turns into "run it again, but only on the three that
failed".

The retry policy here is deliberately narrow, because a retry that fires on
the wrong failure class is worse than no retry at all:

* **Only** ``network`` and ``timeout`` are retried.
* ``auth``, ``host_key``, ``kex``, ``permission``, ``invalid_arguments``,
  ``not_found`` … are never retried. Repeating a rejected credential is how
  you lock an account; repeating a host-key mismatch is how you train people
  to ignore the warning.
* The decision is made from ``data.failure_class``, never from an exit code,
  because the same exit code can mean "the remote command exited 42" and "ssh
  could not connect".

Usage::

    from lib.retry import RetryPolicy, retrying

    policy = RetryPolicy(attempts=3, delay=2.0, backoff=2.0, max_delay=30.0)
    result = retrying(lambda: run_once(), policy)

``retrying`` returns the last result and stamps ``data.attempts`` (1-based) on
it, plus ``data.retry_history`` with one entry per abandoned attempt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

#: Failure classes that are safe — and useful — to repeat.
RETRYABLE_CLASSES: frozenset[str] = frozenset({"network", "timeout"})

#: Upper bound on the sum of all sleeps, so ``--retry 10`` cannot silently
#: turn a 2-minute command into an hour-long one.
DEFAULT_MAX_DELAY = 30.0

#: Overall guard: never sleep more than this in one call, whatever the flags.
MAX_TOTAL_SLEEP = 300.0


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, and how long to wait between attempts.

    ``attempts`` counts the **total** number of tries, so ``attempts=3`` means
    "one initial call plus up to two retries" — the same convention as
    ``curl --retry`` and Ansible's ``retries`` + 1.
    """
    attempts: int = 1
    delay: float = 2.0
    backoff: float = 2.0
    max_delay: float = DEFAULT_MAX_DELAY

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")
        if self.delay < 0:
            raise ValueError("delay must be >= 0")
        if self.backoff < 1:
            raise ValueError("backoff must be >= 1.0 (use 1.0 for a fixed delay)")

    @property
    def enabled(self) -> bool:
        return self.attempts > 1

    def sleep_for(self, failed_attempt: int) -> float:
        """Seconds to wait after ``failed_attempt`` (1-based) failed."""
        raw = self.delay * (self.backoff ** max(0, failed_attempt - 1))
        return float(min(raw, self.max_delay))

    def describe(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "delay_s": self.delay,
            "backoff": self.backoff,
            "max_delay_s": self.max_delay,
        }


def failure_class(result: dict[str, Any] | None) -> str | None:
    """Read the failure class out of an envelope, tolerating both shapes.

    Two envelopes travel through this skill: the standard one
    (``data.failure_class``) and the per-host cluster envelope, which carries
    ``failure_class`` at the top level. Accepting either keeps the retry
    decision identical for a single host and for a fleet member.
    """
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        value = data.get("failure_class")
        if isinstance(value, str) and value:
            return value
    top = result.get("failure_class")
    return top if isinstance(top, str) and top else None


def is_retryable(result: dict[str, Any] | None) -> bool:
    """True when this envelope looks like transient transport trouble."""
    if not isinstance(result, dict) or result.get("success"):
        return False
    return failure_class(result) in RETRYABLE_CLASSES


def retrying(
    call: Callable[[int], dict[str, Any]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Call ``call(attempt)`` until it succeeds, is unretryable, or runs out.

    ``call`` receives the 1-based attempt number (so a caller can vary the
    command, e.g. add a longer connect timeout on later attempts) and must
    return an envelope. The returned envelope always carries:

    ``data.attempts``
        how many attempts were actually made (1 when nothing was retried).
    ``data.retry_history``
        one ``{"attempt", "failure_class", "exit_code", "waited_s"}`` record
        per abandoned attempt (absent when ``attempts == 1``).
    ``data.retry_policy``
        the policy, echoed, but only when ``attempts > 1`` was requested.

    Anything unexpected (an exception, a ``None`` return) is treated as a
    terminal result: this helper must never turn a bug into a retry storm.
    """

    result: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    slept_total = 0.0
    total = max(1, policy.attempts)

    for attempt in range(1, total + 1):
        try:
            result = call(attempt) or {}
        except Exception as exc:  # noqa: BLE001 — a raising call is terminal
            result = {
                "success": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "data": {"failure_class": "internal"},
            }
        if result.get("success") or attempt >= total or not is_retryable(result):
            break
        wait = policy.sleep_for(attempt)
        if slept_total + wait > MAX_TOTAL_SLEEP:
            # Refuse to keep sleeping: report the last failure as final.
            history.append(
                {
                    "attempt": attempt,
                    "failure_class": failure_class(result),
                    "exit_code": result.get("exit_code"),
                    "waited_s": 0.0,
                    "retry_skipped": "total sleep budget exhausted",
                }
            )
            break
        history.append(
            {
                "attempt": attempt,
                "failure_class": failure_class(result),
                "exit_code": result.get("exit_code"),
                "waited_s": round(wait, 3),
            }
        )
        sleep(wait)
        slept_total += wait

    data = result.setdefault("data", {})
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    data["attempts"] = len(history) + 1
    if history:
        data["retry_history"] = history
        data["retry_policy"] = policy.describe()
        data["retry_slept_s"] = round(slept_total, 3)
    return result


def cli_flags(parser: Any, *, enabled_by_default: bool = False) -> None:
    """Add ``--retry`` / ``--retry-delay`` / ``--retry-backoff`` to a parser."""
    parser.add_argument(
        "--retry",
        type=int,
        default=1 if not enabled_by_default else 3,
        metavar="N",
        help=(
            "Total attempts for transient failures (default: 1 = no retry). "
            "Only failure_class network/timeout are repeated; auth and "
            "host-key failures never are."
        ),
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Wait before the second attempt (default: 2.0)",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=2.0,
        metavar="FACTOR",
        help=(
            "Multiplier applied to the delay after each failed attempt "
            "(default: 2.0; use 1.0 for a fixed delay)"
        ),
    )


def policy_from_args(args: Any) -> RetryPolicy:
    """Build a :class:`RetryPolicy` from parsed CLI args, clamping nonsense."""
    attempts = int(getattr(args, "retry", 1) or 1)
    if attempts < 1:
        attempts = 1
    delay = float(getattr(args, "retry_delay", 2.0) or 0.0)
    backoff = float(getattr(args, "retry_backoff", 2.0) or 1.0)
    return RetryPolicy(
        attempts=attempts,
        delay=max(0.0, delay),
        backoff=max(1.0, backoff),
    )


__all__ = [
    "DEFAULT_MAX_DELAY",
    "MAX_TOTAL_SLEEP",
    "RETRYABLE_CLASSES",
    "RetryPolicy",
    "cli_flags",
    "failure_class",
    "is_retryable",
    "policy_from_args",
    "retrying",
]
