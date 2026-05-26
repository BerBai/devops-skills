# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Unit tests for log-aggregator/scripts/tail_multi.py + lib/* v1.0.

All ``subprocess`` calls are mocked at the module level so this suite is
hermetic: no real ssh, journalctl, docker, kubectl, or tail processes are
spawned. The integration counterpart in
``tests/test_tail_multi_integration.py`` exercises the real subprocess
chain via the ``live_ssh`` marker.

Layout (matches PRD R11 / design D10 buckets):

- ``source_spec`` (~12 tests): brace/glob expansion + 4 scheme parse
  rules + error cases.
- ``time_norm`` (~8 tests): 4 schemes × (normal + fallback).
- ``filters`` (~9 tests): default noise / level / suppress (regular +
  aggressive) / user keep_always / user drop / load_user_overrides.
- ``clock_skew`` (~3 tests): _probe_one math + empty list + all-fail.
- ``fetchers`` (~3 tests): _parse_since_to_epoch + iter_lines dispatch
  + local subprocess streaming.
- ``CLI / main`` (~5 tests): --help, BAD spec → exit 40, file fixture
  round-trip JSON envelope, partial_fetch path (one mocked fetcher
  raises), --json envelope shape (last line has no source key).
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

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
SCRIPT_PATH = SCRIPTS_DIR / "tail_multi.py"

# Several plugins ship a top-level `lib/` directory and load it via
# ``sys.path.insert(0, scripts_dir); from lib import ...``. The first
# test module to do so wins ``sys.modules['lib']``, and subsequent
# modules trip over the cached package. Evict any prior `lib*` before
# importing tail_multi, then restore the previous state so unrelated
# test modules keep working regardless of collection order.
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))

import tail_multi  # noqa: E402, I001

# Grab module/symbol references from tail_multi (which has now imported
# log-aggregator's lib internally). Keeping these as local aliases lets
# the remaining tests reference them without re-touching ``sys.modules``.
source_spec = tail_multi.source_spec
fetchers = tail_multi.fetchers
filters = tail_multi.filters
time_norm = tail_multi.time_norm
clock_skew = tail_multi.clock_skew
FetcherError = tail_multi.FetcherError
RawLine = tail_multi.RawLine
ResolvedSource = tail_multi.ResolvedSource
SpecError = tail_multi.SpecError

# Restore whatever `lib*` modules existed before our import dance so the
# next test module (e.g. test_ssh_upload_unit.py) sees the namespace it
# would have seen had we never run.
for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):  # pragma: no cover
    sys.path.remove(str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk_source(
    scheme: str = "journal",
    host: str = "local",
    payload: str = "sshd",
    query: dict[str, str] | None = None,
) -> ResolvedSource:
    return ResolvedSource(
        scheme=scheme,
        host=host,
        payload=payload,
        query=query or {},
        original_spec=f"{scheme}://{host}/{payload}",
        source_tag=f"{host}/{payload}" if payload else f"{host}/system",
    )


def _mk_raw(
    body: str,
    *,
    scheme: str = "journal",
    raw_ts: str | None = None,
    fields: dict[str, str] | None = None,
    fetched_at: float | None = None,
    query: dict[str, str] | None = None,
) -> RawLine:
    return RawLine(
        source=_mk_source(scheme=scheme, query=query),
        raw_body=body,
        raw_ts=raw_ts,
        fields=fields or {},
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


# ---------------------------------------------------------------------------
# source_spec
# ---------------------------------------------------------------------------


def test_spec_journal_basic() -> None:
    [r] = source_spec.resolve(["journal://web-1/nginx"])
    assert r.scheme == "journal"
    assert r.host == "web-1"
    assert r.payload == "nginx"
    assert r.source_tag == "web-1/nginx"
    assert r.query == {}


def test_spec_journal_no_unit_defaults_to_system_tag() -> None:
    [r] = source_spec.resolve(["journal://pai/"])
    assert r.payload == ""
    assert r.source_tag == "pai/system"


def test_spec_docker_requires_container() -> None:
    with pytest.raises(SpecError, match="container"):
        source_spec.resolve(["docker://host/"])


def test_spec_kube_requires_ns_pod_or_label() -> None:
    # ns-only with no label → error
    with pytest.raises(SpecError, match="kube"):
        source_spec.resolve(["kube://local/just-ns"])


def test_spec_kube_with_pod_glob_keeps_pattern_until_fetch() -> None:
    [r] = source_spec.resolve(["kube://local/ns1/payment-svc-*"])
    assert r.scheme == "kube"
    assert r.payload == "ns1/payment-svc-*"
    # source_tag is human-shaped; ns-pod joined by `-`
    assert "payment-svc-" in r.source_tag


def test_spec_kube_label_query() -> None:
    [r] = source_spec.resolve(["kube://local/ns1?label=app=foo"])
    assert r.scheme == "kube"
    assert r.query == {"label": "app=foo"}


def test_spec_file_requires_path_and_adds_leading_slash() -> None:
    [r] = source_spec.resolve(["file://local/var/log/syslog"])
    assert r.scheme == "file"
    assert r.payload == "/var/log/syslog"
    assert r.source_tag == "local/var/log/syslog"


def test_spec_file_with_ts_query() -> None:
    [r] = source_spec.resolve(
        ["file://local/tmp/x.log?ts_regex=^\\d+&ts_format=%s"]
    )
    assert r.query["ts_regex"] == "^\\d+"
    assert r.query["ts_format"] == "%s"


def test_spec_unsupported_scheme() -> None:
    with pytest.raises(SpecError, match="unsupported scheme"):
        source_spec.resolve(["nfs://host/share"])


def test_spec_invalid_no_scheme() -> None:
    with pytest.raises(SpecError, match="scheme://"):
        source_spec.resolve(["just-a-host"])


def test_spec_brace_expand_single_level() -> None:
    rs = source_spec.resolve(["journal://web-{1,2,3}/nginx"])
    assert len(rs) == 3
    hosts = sorted(r.host for r in rs)
    assert hosts == ["web-1", "web-2", "web-3"]


def test_spec_brace_expand_nested_cartesian() -> None:
    rs = source_spec.resolve(["journal://{a,b}-{1,2}/svc"])
    # 2 × 2 = 4 unique hosts
    hosts = sorted(r.host for r in rs)
    assert hosts == ["a-1", "a-2", "b-1", "b-2"]


def test_spec_dedupe_keeps_first_occurrence() -> None:
    rs = source_spec.resolve(
        ["journal://web-1/nginx", "journal://web-1/nginx"]
    )
    assert len(rs) == 1


def test_spec_malformed_query_raises() -> None:
    with pytest.raises(SpecError, match="k=v"):
        source_spec.resolve(["file://local/tmp/x.log?notakvpair"])


def test_spec_missing_host_raises() -> None:
    with pytest.raises(SpecError, match="missing host"):
        source_spec.resolve(["journal:///nginx"])


# ---------------------------------------------------------------------------
# time_norm
# ---------------------------------------------------------------------------


def test_norm_journal_microsecond_epoch() -> None:
    # 2024-05-24T...Z; 1716580921000000 μs
    rl = _mk_raw("hello", scheme="journal", raw_ts="1716580921000000")
    norm = time_norm.normalize(rl)
    assert norm is not None
    dt, body = norm
    assert body == "hello"
    assert dt.tzinfo is timezone.utc
    assert dt.year == 2024


def test_norm_journal_missing_ts_returns_none() -> None:
    rl = _mk_raw("hello", scheme="journal", raw_ts=None)
    assert time_norm.normalize(rl) is None


def test_norm_docker_rfc3339_z() -> None:
    rl = _mk_raw("ok", scheme="docker", raw_ts="2026-05-25T14:02:01.123Z")
    norm = time_norm.normalize(rl)
    assert norm is not None
    dt, _ = norm
    assert dt.year == 2026
    assert dt.tzinfo is timezone.utc


def test_norm_docker_offset_converted_to_utc() -> None:
    rl = _mk_raw("ok", scheme="docker", raw_ts="2026-05-25T22:02:01+08:00")
    dt, _ = time_norm.normalize(rl)  # type: ignore[misc]
    assert dt.hour == 14  # 22-08 = 14 UTC


def test_norm_docker_bad_ts_returns_none() -> None:
    rl = _mk_raw("ok", scheme="docker", raw_ts="not-a-date")
    assert time_norm.normalize(rl) is None


def test_norm_kube_uses_docker_logic() -> None:
    rl = _mk_raw("ok", scheme="kube", raw_ts="2026-05-25T14:02:01Z")
    norm = time_norm.normalize(rl)
    assert norm is not None
    assert norm[0].year == 2026


def test_norm_file_uses_query_regex_and_format() -> None:
    rl = _mk_raw(
        "2026-05-25 14:02:01 hello",
        scheme="file",
        query={
            "ts_regex": r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
            "ts_format": "%Y-%m-%d %H:%M:%S",
        },
    )
    norm = time_norm.normalize(rl)
    assert norm is not None
    dt, _ = norm
    assert dt.year == 2026
    assert dt.hour == 14


def test_norm_file_without_query_returns_none() -> None:
    rl = _mk_raw("hello world", scheme="file")
    assert time_norm.normalize(rl) is None


# ---------------------------------------------------------------------------
# filters — level inference + pipeline
# ---------------------------------------------------------------------------


class _Stub:
    """Minimal duck-typed line for FilterPipeline.should_drop()."""

    def __init__(self, body: str, level: str = "info") -> None:
        self.body = body
        self.level = level


def test_filters_default_noise_drops_healthz() -> None:
    fp = filters.build_pipeline(
        no_default=False, min_level="trace", suppress=False,
        aggressive=False, user_overrides=([], []),
    )
    assert fp.should_drop(_Stub("GET /healthz 200 1ms")) is True


def test_filters_no_default_keeps_healthz() -> None:
    fp = filters.build_pipeline(
        no_default=True, min_level="trace", suppress=False,
        aggressive=False, user_overrides=([], []),
    )
    assert fp.should_drop(_Stub("GET /healthz 200 1ms")) is False


def test_filters_min_level_drops_info_when_warn() -> None:
    fp = filters.build_pipeline(
        no_default=True, min_level="warn", suppress=False,
        aggressive=False, user_overrides=([], []),
    )
    assert fp.should_drop(_Stub("normal log", level="info")) is True
    assert fp.should_drop(_Stub("uh oh", level="warn")) is False


def test_filters_suppress_after_5_repeats_within_1s() -> None:
    fp = filters.build_pipeline(
        no_default=True, min_level="trace", suppress=True,
        aggressive=False, user_overrides=([], []),
    )
    # First 5 occurrences kept, 6th dropped (entry.count > 5 → drop).
    dropped = [fp.should_drop(_Stub("repeated line")) for _ in range(10)]
    # at least the first one is kept; the tail is dropped
    assert dropped[0] is False
    assert any(dropped)  # some dropped


def test_filters_aggressive_suppress_normalizes_digits() -> None:
    fp = filters.build_pipeline(
        no_default=True, min_level="trace", suppress=True,
        aggressive=True, user_overrides=([], []),
    )
    # Different IDs but aggressive normalization collapses to one key.
    for i in range(10):
        fp.should_drop(_Stub(f"request id={i} done"))
    # Internal map should have collapsed all 10 into a single key.
    assert len(fp._suppress_state) == 1


def test_filters_keep_always_overrides_noise() -> None:
    keep = [re.compile(r"healthz")]
    fp = filters.build_pipeline(
        no_default=False, min_level="trace", suppress=False,
        aggressive=False, user_overrides=([], keep),
    )
    assert fp.should_drop(_Stub("GET /healthz 200 1ms")) is False


def test_filters_user_drop_appends_to_noise() -> None:
    drop = [re.compile(r"my-noise")]
    fp = filters.build_pipeline(
        no_default=True, min_level="trace", suppress=False,
        aggressive=False, user_overrides=(drop, []),
    )
    assert fp.should_drop(_Stub("hit my-noise here")) is True


def test_filters_infer_level_journal_priority() -> None:
    # priority 3 = err → "error"
    assert filters.infer_level("journal", {"PRIORITY": "3"}, "") == "error"
    # priority 7 = debug
    assert filters.infer_level("journal", {"PRIORITY": "7"}, "") == "debug"


def test_filters_infer_level_inline_warn_token() -> None:
    assert filters.infer_level("file", {}, "[WARN] foo") == "warn"
    assert filters.infer_level("file", {}, "ERROR: panic") == "error"


def test_filters_load_user_overrides_missing_file(tmp_path: Path) -> None:
    drop, keep = filters.load_user_overrides(tmp_path / "nope.json")
    assert (drop, keep) == ([], [])


def test_filters_load_user_overrides_compiles(tmp_path: Path) -> None:
    p = tmp_path / "log_filters.json"
    p.write_text(
        json.dumps({"drop": ["foo"], "keep_always": ["bar", "[invalid"]})
    )
    drop, keep = filters.load_user_overrides(p)
    assert len(drop) == 1
    # bad regex silently skipped → only "bar" survives
    assert len(keep) == 1


# ---------------------------------------------------------------------------
# clock_skew
# ---------------------------------------------------------------------------


def test_clock_skew_empty_hosts_returns_empty() -> None:
    assert clock_skew.probe([]) == {}


def test_clock_skew_local_filtered_out() -> None:
    # 'local' is filtered before any subprocess; we patch subprocess to
    # blow up if it's ever called.
    with patch.object(subprocess, "run", side_effect=AssertionError("nope")):
        assert clock_skew.probe(["local"]) == {}


def test_clock_skew_probe_one_computes_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub ssh_execute envelope: remote echoes a unix timestamp ~5s ahead.
    future_ts = time.time() + 5.0
    envelope = {
        "success": True, "exit_code": 0,
        "stdout": f"{future_ts:.6f}\n", "stderr": "",
        "data": {},
    }

    def fake_run(*_a: Any, **_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope),
                               stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    skew = clock_skew._probe_one("remote-host", timeout=5.0)
    # ±50 ms tolerance for RTT noise (timing of test)
    assert 4500 <= skew <= 5500


def test_clock_skew_all_fail_returns_none_per_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: Any, **_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=255,
            stdout=json.dumps({"success": False, "exit_code": 255}),
            stderr="ssh: connect failed",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = clock_skew.probe(["bad-host-a", "bad-host-b"], timeout=2.0)
    assert set(out.keys()) == {"bad-host-a", "bad-host-b"}
    assert all(v is None for v in out.values())


# ---------------------------------------------------------------------------
# fetchers — _parse_since_to_epoch + dispatch
# ---------------------------------------------------------------------------


def test_parse_since_duration_min() -> None:
    now = time.time()
    epoch = fetchers._parse_since_to_epoch("5min")
    # ~5 minutes ago, ±5s tolerance
    assert now - 305 <= epoch <= now - 295


def test_parse_since_bare_epoch() -> None:
    assert fetchers._parse_since_to_epoch("1716580921") == 1716580921


def test_parse_since_iso() -> None:
    epoch = fetchers._parse_since_to_epoch("2026-05-25T14:02:01Z")
    # 2026-05-25T14:02:01Z → 1779026521
    dt = datetime(2026, 5, 25, 14, 2, 1, tzinfo=timezone.utc)
    assert epoch == int(dt.timestamp())


def test_parse_since_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    assert fetchers._parse_since_to_epoch("blarg") == 1_000_000 - 300


def test_iter_lines_dispatch_unknown_scheme_raises() -> None:
    src = ResolvedSource(
        scheme="unknown", host="local", payload="x",
        query={}, original_spec="unknown://local/x", source_tag="local/x",
    )
    with pytest.raises(FetcherError):
        list(fetchers.iter_lines(src, since="5min", follow=False, timeout=5))


def test_file_fetcher_local_streams_lines(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("line a\nline b\nline c\n")
    src = source_spec.resolve([f"file://local{log}"])[0]
    out = list(fetchers.iter_lines(src, since="5min", follow=False, timeout=5))
    assert [r.raw_body for r in out] == ["line a", "line b", "line c"]


# ---------------------------------------------------------------------------
# ReorderBuffer
# ---------------------------------------------------------------------------


def test_reorder_buffer_max_pending_flush() -> None:
    # All timestamps placed *in the future* so the flush_after_s threshold
    # never triggers; this isolates the max_pending behavior.
    rb = tail_multi.ReorderBuffer(max_pending=3, flush_after_s=2.0)
    base = datetime.now(tz=timezone.utc).replace(microsecond=0)
    base = base.replace(year=base.year + 1)
    for i in range(5):
        line = tail_multi.FetchedLine(
            raw=_mk_raw(f"l{i}"),
            norm_time=base.replace(second=i % 60),
            body=f"l{i}",
            level="info",
        )
        rb.push(line)
    ready = rb.pop_ready()
    assert [item.body for item in ready] == ["l0", "l1"]
    rest = rb.drain()
    assert [item.body for item in rest] == ["l2", "l3", "l4"]


def test_reorder_buffer_flush_threshold_aged_out() -> None:
    rb = tail_multi.ReorderBuffer(max_pending=999, flush_after_s=0.0)
    # An aged-out entry — older than threshold (now - 0 = now) → pops.
    past = datetime.now(tz=timezone.utc).replace(year=2020)
    rb.push(tail_multi.FetchedLine(
        raw=_mk_raw("old"), norm_time=past, body="old", level="info",
    ))
    ready = rb.pop_ready()
    assert [item.body for item in ready] == ["old"]


# ---------------------------------------------------------------------------
# CLI / main()
# ---------------------------------------------------------------------------


def test_cli_help_lists_flags(capsys: pytest.CaptureFixture[str]) -> None:
    parser = tail_multi.build_parser()
    help_txt = parser.format_help()
    for flag in (
        "--sources", "--follow", "--since", "--min-level", "--suppress",
        "--aggressive-suppress", "--max-concurrent",
        "--no-default-filters", "--json",
    ):
        assert flag in help_txt


def test_cli_bad_spec_exits_40(capsys: pytest.CaptureFixture[str]) -> None:
    rc = tail_multi.main(["--sources", "BAD-SPEC", "--json"])
    assert rc == 40
    out = capsys.readouterr().out
    env = json.loads(out.strip())
    assert env["success"] is False
    assert env["exit_code"] == 40
    assert env["data"]["failure_class"] == "source_spec_invalid"


def test_cli_file_fixture_round_trip_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "fixture.log"
    log.write_text("alpha\nbeta\ngamma\n")
    rc = tail_multi.main([
        "--sources", f"file://local{log}",
        "--no-default-filters",
        "--json",
    ])
    assert rc == 0
    out_lines = [
        ln for ln in capsys.readouterr().out.strip().split("\n") if ln
    ]
    # Last line is envelope (no `source` key); preceding are events.
    *events, summary_line = out_lines
    summary = json.loads(summary_line)
    assert summary["success"] is True
    assert summary["exit_code"] == 0
    assert summary["data"]["events_emitted"] == 3
    assert summary["data"]["failure_class"] is None
    # Each event has the required keys.
    for ev_line in events:
        ev = json.loads(ev_line)
        assert ev["source"].startswith("local")
        assert ev["raw"] in {"alpha", "beta", "gamma"}


def test_cli_partial_fetch_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One mocked fetcher raises; other succeeds → exit 0 + partial_fetch."""
    log = tmp_path / "ok.log"
    log.write_text("good\n")

    real_iter_lines = fetchers.iter_lines

    def fake_iter(src: ResolvedSource, **kw: Any):
        if src.scheme == "journal":
            raise FetcherError(
                host=src.host, argv=["journalctl"], returncode=255,
                stderr_tail="ssh: connect failed",
            )
        # fall through to real file fetcher for the file source
        yield from real_iter_lines(src, **kw)

    monkeypatch.setattr(tail_multi.fetchers, "iter_lines", fake_iter)

    rc = tail_multi.main([
        "--sources", f"file://local{log}", "journal://unreachable-host/sshd",
        "--no-default-filters", "--json",
    ])
    out_lines = [
        ln for ln in capsys.readouterr().out.strip().split("\n") if ln
    ]
    summary = json.loads(out_lines[-1])
    assert rc == 0
    assert summary["success"] is True
    assert summary["data"]["failure_class"] == "partial_fetch"
    assert len(summary["data"]["failed_fetchers"]) == 1
    assert "unreachable-host" in summary["data"]["failed_fetchers"][0]["source"]


def test_cli_all_fetchers_fail_exits_41(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_iter(src: ResolvedSource, **_kw: Any):
        raise FetcherError(host=src.host, argv=[], returncode=255,
                           stderr_tail="boom")
        yield  # pragma: no cover

    monkeypatch.setattr(tail_multi.fetchers, "iter_lines", fake_iter)
    rc = tail_multi.main([
        "--sources", "journal://a/x", "journal://b/x",
        "--json",
    ])
    out_lines = [
        ln for ln in capsys.readouterr().out.strip().split("\n") if ln
    ]
    summary = json.loads(out_lines[-1])
    assert rc == 41
    assert summary["data"]["failure_class"] == "fetcher_failed"


def test_cli_json_summary_has_no_source_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Callers distinguish events from the summary by 'source' key absence."""
    log = tmp_path / "x.log"
    log.write_text("a\nb\n")
    rc = tail_multi.main([
        "--sources", f"file://local{log}",
        "--no-default-filters", "--json",
    ])
    assert rc == 0
    lines = [
        json.loads(ln)
        for ln in capsys.readouterr().out.strip().split("\n") if ln
    ]
    assert "source" not in lines[-1]
    assert all("source" in ln for ln in lines[:-1])
