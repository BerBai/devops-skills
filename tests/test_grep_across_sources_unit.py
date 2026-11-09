# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Unit tests for log-aggregator/scripts/grep_across_sources.py v1.0."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "log-aggregator"
    / "skills"
    / "log-aggregator"
    / "scripts"
)

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import grep_across_sources as grepper  # noqa: E402, I001

RawLine = grepper.RawLine
ResolvedSource = grepper.ResolvedSource
FetcherError = grepper.FetcherError

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):  # pragma: no cover
    sys.path.remove(str(SCRIPTS_DIR))


def _src(
    scheme: str = "file",
    host: str = "local",
    payload: str = "/tmp/app.log",
    tag: str | None = None,
    query: dict[str, str] | None = None,
) -> ResolvedSource:
    return ResolvedSource(
        scheme=scheme,
        host=host,
        payload=payload,
        query=query or {},
        original_spec=f"{scheme}://{host}{payload}",
        source_tag=tag or f"{host}{payload}",
    )


def _raw(
    body: str,
    *,
    source: ResolvedSource | None = None,
    raw_ts: str | None = None,
    fields: dict[str, str] | None = None,
    fetched_at: float | None = None,
) -> RawLine:
    return RawLine(
        source=source or _src(),
        raw_body=body,
        raw_ts=raw_ts,
        fields=fields or {},
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


def _group(
    source_tag: str,
    hits: int,
    samples: list[tuple[str, str]] | None = None,
    first_seen: str | None = None,
    events: list[tuple[str, str]] | None = None,
) -> grepper.HitGroup:
    sample_rows = samples or []
    return grepper.HitGroup(
        source_tag=source_tag,
        hits=hits,
        events=events if events is not None else list(sample_rows),
        samples=sample_rows,
        first_seen=first_seen,
    )


def test_validate_pattern_empty_and_none_are_heatmap() -> None:
    assert grepper._validate_pattern(None) is None
    assert grepper._validate_pattern("") is None


def test_validate_pattern_compiles_regex() -> None:
    pattern = grepper._validate_pattern(r"ERROR|WARN")
    assert pattern is not None
    assert pattern.search("WARN disk")


def test_validate_pattern_invalid_raises() -> None:
    with pytest.raises(grepper.PatternError):
        grepper._validate_pattern("[bad regex")


def test_run_grep_counts_matches_and_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tag="local/app")

    def fake_iter(*_a: Any, **_kw: Any):
        yield _raw("ERROR one", source=src, raw_ts="2026-05-25T14:00:01Z")
        yield _raw("OK", source=src, raw_ts="2026-05-25T14:00:02Z")
        yield _raw("ERROR two", source=src, raw_ts="2026-05-25T14:00:03Z")

    monkeypatch.setattr(grepper.fetchers, "iter_lines", fake_iter)
    group = grepper._run_grep_per_source(src, grepper._validate_pattern("ERROR"), "1h", 1)
    assert group.hits == 2
    assert group.first_seen == "2026-05-25T14:00:01Z"
    assert group.events == [
        ("2026-05-25T14:00:01Z", "ERROR one"),
        ("2026-05-25T14:00:03Z", "ERROR two"),
    ]
    assert group.samples == [("2026-05-25T14:00:01Z", "ERROR one")]


def test_run_grep_heatmap_counts_all_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tag="local/app")

    def fake_iter(*_a: Any, **_kw: Any):
        yield _raw("a", source=src, raw_ts="ts1")
        yield _raw("b", source=src, raw_ts="ts2")

    monkeypatch.setattr(grepper.fetchers, "iter_lines", fake_iter)
    group = grepper._run_grep_per_source(src, None, "1h", 5)
    assert group.hits == 2
    assert group.first_seen == "ts1"
    assert len(group.samples) == 2


def test_collect_groups_records_partial_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _src(tag="good/app")
    bad = _src(host="bad", tag="bad/app")

    def fake_iter(src: ResolvedSource, *_a: Any, **_kw: Any):
        if src.host == "bad":
            raise FetcherError("bad", ["tail"], 255, stderr_tail="no route")
        yield _raw("ERROR", source=src, raw_ts="ts1")

    monkeypatch.setattr(grepper.fetchers, "iter_lines", fake_iter)
    groups, failures = grepper._collect_groups(
        [good, bad],
        grepper._validate_pattern("ERROR"),
        "1h",
        5,
    )
    assert {group.source_tag: group.hits for group in groups} == {"good/app": 1, "bad/app": 0}
    assert failures[0].source_tag == "bad/app"
    assert "no route" in failures[0].error


def test_emit_summary_sorts_by_hits_and_shows_sample() -> None:
    buf = io.StringIO()
    grepper._emit_summary(
        [
            _group("b", 1, [("2026-05-25T14:00:01Z", "WARN")]),
            _group("a", 3, [("2026-05-25T14:00:02Z", "ERROR")]),
        ],
        False,
        buf,
    )
    out = buf.getvalue()
    assert "Source" in out
    assert "Hits" in out
    assert out.index("a") < out.index("b")
    assert "Total: 2 sources, 4 hits" in out


def test_emit_summary_truncates_sample() -> None:
    buf = io.StringIO()
    grepper._emit_summary(
        [_group("a", 1, [("ts", "x" * 120)])],
        False,
        buf,
    )
    assert "..." in buf.getvalue()


def test_emit_summary_heatmap_labels_lines() -> None:
    buf = io.StringIO()
    grepper._emit_summary([_group("a", 2, first_seen="ts1")], True, buf)
    out = buf.getvalue()
    assert "Lines" in out
    assert "First seen" in out
    assert "Total: 1 sources, 2 lines" in out


def test_emit_summary_zero_hits() -> None:
    buf = io.StringIO()
    grepper._emit_summary([_group("a", 0)], False, buf)
    assert "Total: 1 sources, 0 hits" in buf.getvalue()


def test_emit_raw_writes_samples_in_sorted_group_order() -> None:
    buf = io.StringIO()
    grepper._emit_raw(
        [
            _group("b", 2, [("ts2", "line b")]),
            _group("a", 5, [("ts1", "line a")]),
        ],
        buf,
    )
    out = buf.getvalue().splitlines()
    assert out == ["[a ts1] line a", "[b ts2] line b"]


def test_emit_raw_not_limited_by_samples() -> None:
    buf = io.StringIO()
    grepper._emit_raw(
        [_group("a", 3, samples=[("ts1", "line 1")], events=[
            ("ts1", "line 1"),
            ("ts2", "line 2"),
            ("ts3", "line 3"),
        ])],
        buf,
    )
    assert buf.getvalue().splitlines() == [
        "[a ts1] line 1",
        "[a ts2] line 2",
        "[a ts3] line 3",
    ]


def test_emit_raw_empty_hits_writes_nothing() -> None:
    buf = io.StringIO()
    grepper._emit_raw([_group("a", 0)], buf)
    assert buf.getvalue() == ""


def test_emit_json_events_and_total_hits() -> None:
    buf = io.StringIO()
    grepper._emit_json(
        [_group("a", 2, [("ts1", "ERROR one")], events=[
            ("ts1", "ERROR one"),
            ("ts2", "ERROR two"),
        ])],
        [],
        False,
        buf,
    )
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert lines[0]["raw"] == "ERROR one"
    assert lines[-1]["success"] is True
    assert lines[-1]["data"]["total_hits"] == 2
    assert lines[-1]["data"]["failure_class"] is None


def test_emit_json_heatmap_uses_total_lines() -> None:
    buf = io.StringIO()
    grepper._emit_json([_group("a", 2, [("ts1", "a")])], [], True, buf)
    env = json.loads(buf.getvalue().splitlines()[-1])
    assert env["data"]["total_lines"] == 2
    assert "total_hits" not in env["data"]


def test_emit_json_partial_fetch_marker() -> None:
    """A failed source makes the envelope a failure, not a labelled success.

    DEVOPS-BUG-012: this used to assert ``success: true`` / ``exit_code: 0``
    with only ``data.failure_class`` carrying the bad news. A caller reading
    ``success`` (or the process exit code) saw a clean run whose counts were
    silently computed from a subset of the requested sources.
    """
    buf = io.StringIO()
    envelope = grepper._emit_json(
        [_group("a", 0)],
        [grepper.FetcherFailure("a", "boom")],
        False,
        buf,
    )
    emitted = json.loads(buf.getvalue().splitlines()[-1])
    assert emitted == envelope
    assert envelope["success"] is False
    assert envelope["exit_code"] == 41
    assert envelope["data"]["failure_class"] == "partial_fetch"
    assert envelope["data"]["failed_fetchers"] == [{"source": "a", "error": "boom"}]
    # The data that was collected still ships.
    assert envelope["data"]["total_hits"] == 0
    assert envelope["data"]["per_source"] == {"a": 0}


def test_main_partial_fetch_exits_41_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: one dead source -> rc 41, the live source's hits survive."""
    good = _src(tag="good/app")
    bad = _src(host="bad", tag="bad/app")

    def fake_iter(src: ResolvedSource, *_a: Any, **_kw: Any):
        if src.host == "bad":
            raise FetcherError("bad", ["tail"], 255, stderr_tail="no route")
        yield _raw("ERROR", source=src, raw_ts="ts1")

    monkeypatch.setattr(grepper.source_spec, "resolve", lambda _specs: [good, bad])
    monkeypatch.setattr(grepper.fetchers, "iter_lines", fake_iter)

    rc = grepper.main(["--sources", "x", "--pattern", "ERROR", "--json"])
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln]
    assert rc == 41
    assert lines[-1]["success"] is False
    assert lines[-1]["exit_code"] == 41
    assert lines[-1]["data"]["failure_class"] == "partial_fetch"
    assert lines[-1]["data"]["per_source"] == {"good/app": 1, "bad/app": 0}
    assert [event["raw"] for event in lines[:-1]] == ["ERROR"]


def test_main_partial_fetch_exit_code_in_human_formats(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """summary/raw print no envelope, so only the exit code can carry it."""
    src = _src(tag="local/app")

    def fake_iter(*_a: Any, **_kw: Any):
        raise FetcherError("local", ["tail"], 255, stderr_tail="no route")
        yield  # pragma: no cover

    monkeypatch.setattr(grepper.source_spec, "resolve", lambda _specs: [src])
    monkeypatch.setattr(grepper.fetchers, "iter_lines", fake_iter)

    assert grepper.main(["--sources", "x", "--pattern", "ERROR"]) == 41
    assert "failed_fetcher" in capsys.readouterr().err
    assert grepper.main(["--sources", "x", "--format", "raw"]) == 41


def test_main_pattern_invalid_returns_180_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = grepper.main(["--sources", "file://local/tmp/a.log", "--pattern", "[bad", "--json"])
    out = capsys.readouterr().out
    env = json.loads(out)
    assert rc == 180
    assert env["exit_code"] == 180
    assert env["data"]["failure_class"] == "pattern_invalid"


def test_main_source_spec_invalid_returns_40(capsys: pytest.CaptureFixture[str]) -> None:
    rc = grepper.main(["--sources", "not-a-spec", "--pattern", "ERROR", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 40
    assert env["data"]["failure_class"] == "source_spec_invalid"


def test_main_json_alias_equals_format_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _src(tag="local/app")

    def fake_resolve(_specs: list[str]) -> list[ResolvedSource]:
        return [src]

    def fake_iter(*_a: Any, **_kw: Any):
        yield _raw("ERROR", source=src, raw_ts="ts1")

    monkeypatch.setattr(grepper.source_spec, "resolve", fake_resolve)
    monkeypatch.setattr(grepper.fetchers, "iter_lines", fake_iter)

    rc_json = grepper.main(["--sources", "file://local/tmp/a.log", "--pattern", "ERROR", "--json"])
    out_json = capsys.readouterr().out
    rc_format = grepper.main(
        ["--sources", "file://local/tmp/a.log", "--pattern", "ERROR", "--format", "json"]
    )
    out_format = capsys.readouterr().out

    assert rc_json == rc_format == 0

    def _without_timing(text: str) -> list[dict]:
        """The envelopes are equal apart from wall-clock ``elapsed_s``.

        Comparing the raw stdout made this test flaky: two back-to-back runs
        can round ``elapsed_s`` differently (0.0 vs 0.016) with no behavioural
        difference. Every other field must still match exactly.
        """
        rows = [json.loads(line) for line in text.splitlines() if line]
        for row in rows:
            row.get("data", {}).pop("elapsed_s", None)
        return rows

    assert _without_timing(out_json) == _without_timing(out_format)


def test_main_summary_zero_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _src(tag="local/app")
    monkeypatch.setattr(grepper.source_spec, "resolve", lambda _specs: [src])
    monkeypatch.setattr(
        grepper.fetchers,
        "iter_lines",
        lambda *_a, **_kw: iter([_raw("OK", source=src, raw_ts="ts1")]),
    )
    rc = grepper.main(["--sources", "file://local/tmp/a.log", "--pattern", "ERROR"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Total: 1 sources, 0 hits" in out
