"""Shared exception class for ssh-guarded lib modules.

Private: scripts and tests should import ``ArtifactError`` from the
public ``lib`` package, not from this module directly.

Lives in its own file so ``sandbox.py`` and ``request_artifact.py`` can
both import it without creating a circular dependency.
"""

from __future__ import annotations


class ArtifactError(Exception):
    """Recoverable, classified failure in an ssh-guarded operation.

    Carries the three fields every script ``main()`` needs to build a
    failure envelope:

    * ``failure_class`` — the project-wide ``data.failure_class`` string
      (see ``.trellis/spec/backend/error-handling.md`` per-script ranges
      and design.md §1 C5 for ssh-guarded's 30-59 region).
    * ``exit_code`` — process exit code; must align with the failure_class.
    * ``message`` — short human-readable stderr line.
    """

    def __init__(self, failure_class: str, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.message = message
