# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Unit tests for log-aggregator/scripts/correlate.py v1.0."""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
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
import correlate  # noqa: E402, I001

RawLine = correlate.RawLine
ResolvedSource = correlate.ResolvedSource
FetcherError = correlate.FetcherError

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


def _line(
    body: str,
    seconds: int,
    *,
    source_tag: str = "local/app",
    level: str = "info",
) -> correlate.NormalizedLine:
    dt = datetime(2026, 5, 25, 14, 2, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return correlate.NormalizedLine(
        raw=_raw(body, source=_src(tag=source_tag)),
        norm_time=dt,
        body=body,
        level=level,
    )


def test_parse_window_seconds_minutes_hours_days() -> None:
    assert correlate._parse_window("30s") == 30
    assert correlate._parse_window("2m") == 120
    assert correlate._parse_window("1h") == 3600
    assert correlate._parse_window("1d") == 86400


def test_parse_window_decimal_seconds() -> None:
    assert correlate._parse_window("0.5s") == 0.5


@pytest.mark.parametrize("value", ["garbage", "-1s", "0s", "30", "1w"])
def test_parse_window_invalid(value: str) -> None:
    with pytest.raises(correlate.WindowError):
        correlate._parse_window(value)


def test_find_anchor_earliest_global_match() -> None:
    lines = [
        _line("later MATCH", 10, source_tag="b"),
        _line("earliest MATCH", 1, source_tag="a"),
    ]
    anchor = correlate._find_anchor(lines, re.compile("MATCH"), pair_mode=False)
    assert isinstance(anchor, correlate.Anchor)
    assert anchor.source_tag == "a"
    assert anchor.body == "earliest MATCH"


def test_find_anchor_none() -> None:
    assert correlate._find_anchor([_line("nope", 0)], re.compile("MATCH"), pair_mode=False) is None


def test_find_anchor_pair_mode_first_and_last() -> None:
    lines = [_line("span_id=s7 start", 0), _line("middle", 5), _line("span_id=s7 end", 12)]
    pair = correlate._find_anchor(lines, re.compile("span_id=s7"), pair_mode=True)
    assert isinstance(pair, tuple)
    assert pair[0].body.endswith("start")
    assert pair[1].body.endswith("end")


def test_find_anchor_pair_mode_single_match_raises() -> None:
    with pytest.raises(correlate.PairUnmatchedError):
        correlate._find_anchor([_line("span_id=s7 once", 0)], re.compile("s7"), pair_mode=True)


def test_find_anchor_regex_special_chars() -> None:
    anchor = correlate._find_anchor(
        [_line("GET /api/charge?x=1 500", 0)],
        re.compile(r"GET /api/charge\?x=1 .*500"),
        pair_mode=False,
    )
    assert isinstance(anchor, correlate.Anchor)


def test_filter_window_inclusive_edges() -> None:
    lines = [_line("left edge", -5), _line("anchor", 0), _line("right edge", 5), _line("out", 6)]
    anchor = correlate.Anchor("local/app", lines[1].norm_time, "anchor", "info", 1)
    assert [line.body for line in correlate._filter_window(lines, anchor, 5)] == [
        "left edge", "anchor", "right edge",
    ]


def test_filter_window_pair_mode_expands_whole_span() -> None:
    lines = [_line("pre", -2), _line("start", 0), _line("mid", 4), _line("end", 8), _line("post", 10)]
    pair = (
        correlate.Anchor("local/app", lines[1].norm_time, "start", "info", 1),
        correlate.Anchor("local/app", lines[3].norm_time, "end", "info", 3),
    )
    assert [line.body for line in correlate._filter_window(lines, pair, 2)] == [
        "pre", "start", "mid", "end", "post",
    ]


def test_compute_hint_earliest_critical() -> None:
    hint = correlate._compute_hint([
        _line("ERROR later", 5, level="error"),
        _line("FATAL first", 2, level="fatal"),
    ])
    assert hint is not None
    assert hint["body"] == "FATAL first"
    assert hint["level"] == "fatal"


def test_compute_hint_none_when_no_critical() -> None:
    assert correlate._compute_hint([_line("INFO ok", 0, level="info")]) is None


def test_compute_hint_canonicalizes_critical() -> None:
    hint = correlate._compute_hint([_line("CRITICAL bad", 0, level="critical")])
    assert hint is not None
    assert hint["level"] == "fatal"


def test_emit_human_three_sections(capsys: pytest.CaptureFixture[str]) -> None:
    lines = [_line("ERROR root", -1, level="error"), _line("GET /api 500", 0, level="error")]
    anchor = correlate.Anchor(lines[1].source_tag, lines[1].norm_time, lines[1].body, "error", 1)
    summary = {
        "sources_with_hits": 1,
        "anchor_severity": "error",
        "likely_root_cause_hint": correlate._compute_hint(lines),
    }
    correlate._emit_three_sections_human(
        anchor=anchor, window_lines=lines, window_s=30, summary=summary
    )
    out = capsys.readouterr().out
    assert "=== Anchor ===" in out
    assert "=== Within window (30s) ===" in out
    assert "=== Summary ===" in out
    assert "likely_root_cause_hint:" in out
    assert "← anchor" in out


def test_build_success_json_envelope_pair_duration() -> None:
    lines = [_line("span start", 0), _line("span end", 2)]
    pair = (
        correlate.Anchor(lines[0].source_tag, lines[0].norm_time, lines[0].body, "info", 0),
        correlate.Anchor(lines[1].source_tag, lines[1].norm_time, lines[1].body, "info", 1),
    )
    env = correlate._build_success_envelope(
        anchor=pair,
        window_lines=lines,
        window_s=1,
        sources=[_src(tag="local/app")],
        skew={},
        failed_fetchers=[],
        elapsed_s=0.1,
    )
    assert env["success"] is True
    assert env["data"]["summary"]["duration_ms"] == 2000
    assert env["data"]["anchor"]["first"]["body"] == "span start"


def test_collect_all_lines_normalizes_and_infers_level(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(scheme="journal", tag="local/sshd")
    raw = _raw("boom", source=src, raw_ts="1779717721000000", fields={"PRIORITY": "3"})

    def fake_iter(*_a: Any, **_kw: Any):
        yield raw

    monkeypatch.setattr(correlate.fetchers, "iter_lines", fake_iter)
    lines, failures = correlate._collect_all_lines([src], "1h")
    assert failures == []
    assert len(lines) == 1
    assert lines[0].level == "error"
    assert lines[0].body == "boom"


def test_collect_all_lines_records_fetcher_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(scheme="journal", host="bad", tag="bad/sshd")

    def fake_iter(*_a: Any, **_kw: Any):
        raise FetcherError(host="bad", argv=["journalctl"], returncode=255, stderr_tail="no route")
        yield  # pragma: no cover

    monkeypatch.setattr(correlate.fetchers, "iter_lines", fake_iter)
    lines, failures = correlate._collect_all_lines([src], "1h")
    assert lines == []
    assert failures[0].source_tag == "bad/sshd"
    assert "no route" in failures[0].error


def test_cli_help_lists_locked_flags() -> None:
    help_txt = correlate.build_parser().format_help()
    for flag in ("--sources", "--anchor", "--window", "--pair-mode", "--since", "--json"):
        assert flag in help_txt


def test_cli_window_invalid_exits_91(capsys: pytest.CaptureFixture[str]) -> None:
    rc = correlate.main([
        "--sources", "file://local/tmp/x.log",
        "--anchor", "X",
        "--window", "garbage",
        "--json",
    ])
    env = json.loads(capsys.readouterr().out)
    assert rc == 91
    assert env["data"]["failure_class"] == "window_invalid"


def test_cli_bad_spec_exits_40(capsys: pytest.CaptureFixture[str]) -> None:
    rc = correlate.main(["--sources", "BAD-SPEC", "--anchor", "X", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 40
    assert env["data"]["failure_class"] == "source_spec_invalid"


def test_cli_anchor_not_found_exits_90(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(correlate.clock_skew, "probe", lambda *_a, **_kw: {})
    monkeypatch.setattr(correlate, "_collect_all_lines", lambda *_a, **_kw: ([ _line("nope", 0) ], []))
    rc = correlate.main(["--sources", "file://local/tmp/x.log", "--anchor", "MATCH", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 90
    assert env["data"]["failure_class"] == "anchor_not_found"


def test_cli_pair_unmatched_exits_92(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(correlate.clock_skew, "probe", lambda *_a, **_kw: {})
    monkeypatch.setattr(correlate, "_collect_all_lines", lambda *_a, **_kw: ([ _line("MATCH once", 0) ], []))
    rc = correlate.main([
        "--sources", "file://local/tmp/x.log",
        "--anchor", "MATCH",
        "--pair-mode",
        "--json",
    ])
    env = json.loads(capsys.readouterr().out)
    assert rc == 92
    assert env["data"]["failure_class"] == "pair_unmatched"


def test_cli_success_json_envelope(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(correlate.clock_skew, "probe", lambda *_a, **_kw: {})
    lines = [_line("ERROR root", -1, level="error"), _line("GET /api 500", 0, level="error")]
    monkeypatch.setattr(correlate, "_collect_all_lines", lambda *_a, **_kw: (lines, []))
    rc = correlate.main([
        "--sources", "file://local/tmp/x.log",
        "--anchor", r"GET /api.*500",
        "--window", "5s",
        "--json",
    ])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["anchor"]["body"] == "GET /api 500"
    assert env["data"]["summary"]["likely_root_cause_hint"]["body"] == "ERROR root"


def test_cli_partial_fetch_still_succeeds_when_anchor_found(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(correlate.clock_skew, "probe", lambda *_a, **_kw: {})
    failure = correlate.FetcherFailure("bad/source", "rc=255 boom")
    monkeypatch.setattr(
        correlate,
        "_collect_all_lines",
        lambda *_a, **_kw: ([ _line("MATCH survives", 0) ], [failure]),
    )
    rc = correlate.main(["--sources", "file://local/tmp/x.log", "--anchor", "MATCH", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"]["partial_fetch"] is True
    assert env["data"]["failed_fetchers"][0]["source"] == "bad/source"
