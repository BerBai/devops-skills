"""Shared helpers for log-aggregator scripts.

Stable surface (consumed by tail_multi.py, correlate.py, grep_across_sources.py):

    source_spec.resolve(specs: list[str]) -> list[ResolvedSource]
    fetchers.iter_lines(source, *, since, follow, timeout) -> Iterator[RawLine]
    time_norm.normalize(line: RawLine) -> tuple[datetime, str] | None
    clock_skew.probe(remote_hosts: list[str], timeout: float) -> dict[str, int | None]
    filters.build_pipeline(...) -> FilterPipeline
    filters.load_user_overrides() -> tuple[list[Pattern], list[Pattern]]

These five modules are the foundation lib for the log-aggregator plugin.
Introduced by `tail_multi` v1.0 (see
`.trellis/tasks/05-25-log-aggregator-tail-multi/`).
"""
