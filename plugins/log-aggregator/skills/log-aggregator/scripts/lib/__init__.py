"""Shared helpers for log-aggregator scripts.

Stable surface (consumed by tail_multi.py, correlate.py, grep_across_sources.py):

    source_spec.resolve(specs: list[str]) -> list[ResolvedSource]
    fetchers.iter_lines(source, *, since, follow, timeout, lines) -> Iterator[RawLine]
    time_norm.normalize(line: RawLine) -> tuple[datetime, str] | None
    clock_skew.probe(remote_hosts: list[str], timeout: float) -> dict[str, int | None]
    filters.build_pipeline(...) -> FilterPipeline
    filters.load_user_overrides() -> tuple[list[Pattern], list[Pattern]]
    envelope.EXIT_CODES / apply_fetch_failures(...) / exit_code_for(...)

``envelope`` is the single implementation of the plugin's partial-fetch rule:
any source that failed to fetch makes the whole run `success=false`,
`exit_code=41`, `data.failure_class="partial_fetch"`, with the successfully
fetched data still in `data`. All three CLIs build their envelope through it so
the rule cannot drift per script (DEVOPS-BUG-011 / DEVOPS-BUG-012).

These modules are the foundation lib for the log-aggregator plugin.
Introduced by `tail_multi` v1.0 (see
`.trellis/tasks/05-25-log-aggregator-tail-multi/`).
"""
