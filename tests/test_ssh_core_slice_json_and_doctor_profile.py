"""BUG-005 and OPT-001: the shipped content envelope and the capability probe.

Two field reports from the 2026-11-05 wave:

* **BUG-005** — ``ssh_slice --json`` forced ``stdout`` empty and never put the
  sliced text in ``data``, so every documented ``--json`` example returned only
  metadata.
* **OPT-001** — ``doctor.py`` only probed ssh-core's own 14 scripts, so the
  backends of the other seven plugins in this marketplace had no pre-flight at
  all; the only way to know was a manual ``command -v`` per binary per host.

Everything here is offline: the transport is stubbed at the seam
(``ssh_slice._run_ssh_execute`` / ``doctor._run_worker``), so the suite still
runs on an air-gapped machine.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.compat import temp_dir  # noqa: E402
from .ssh_core.helpers import SCRIPTS, load_script  # noqa: E402

slice_mod = load_script("ssh_slice")
doctor = load_script("doctor")


def _transport_payload(stdout: str, *, success: bool = True,
                       data: dict | None = None, exit_code: int = 0,
                       stderr: str = "") -> tuple[dict, int]:
    """The shape ssh_execute --json returns, with an injectable body."""
    return (
        {
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "data": {"failure_class": None, **(data or {})},
        },
        42,
    )


class SliceJsonContent(unittest.TestCase):
    """BUG-005: the body must reach a JSON caller."""

    def setUp(self) -> None:
        self._original = slice_mod._run_ssh_execute

    def tearDown(self) -> None:
        slice_mod._run_ssh_execute = self._original

    def _run(self, body: str, *argv: str, data: dict | None = None,
             **kwargs) -> tuple[int, dict, str]:
        slice_mod._run_ssh_execute = lambda *a, **k: _transport_payload(
            body, data=data, **kwargs
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = slice_mod.main(
                ["lab-240", "/var/log/app.log", *argv, "--json"]
            )
        return code, json.loads(buffer.getvalue()), buffer.getvalue()

    def test_content_is_in_the_envelope(self) -> None:
        code, payload, _raw = self._run(
            "alpha\nbeta\ngamma\n", "--lines", "1-3"
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["content"], "alpha\nbeta\ngamma\n")
        self.assertEqual(payload["data"]["content_lines"], 3)
        self.assertEqual(payload["data"]["content_bytes"], len(b"alpha\nbeta\ngamma\n"))
        self.assertFalse(payload["data"]["content_truncated"])

    def test_stdout_carries_the_body_like_ssh_execute(self) -> None:
        _code, payload, _raw = self._run("alpha\nbeta\n", "--lines", "1-2")
        self.assertEqual(payload["stdout"], payload["data"]["content"])
        self.assertNotEqual(payload["stdout"], "")

    def test_sha256_matches_the_content(self) -> None:
        body = "root:x:0:0:root:/root:/bin/bash\n"
        _code, payload, _raw = self._run(body, "--lines", "1")
        expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.assertEqual(payload["data"]["content_sha256"], expected)

    def test_output_file_write_failure_keeps_the_content(self) -> None:
        """The transport succeeded: losing the disk write must not lose the body."""
        body = "line one\n"
        with temp_dir() as tmp:
            # A directory as the target makes the write fail on every platform.
            target = Path(tmp) / "as-dir"
            target.mkdir()
            code, payload, _raw = self._run(
                body, "--lines", "1", "--output-file", str(target)
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["data"]["failure_class"], "invalid_arguments")
        self.assertEqual(payload["data"]["content"], body)

    def test_counts_agree_with_data_range(self) -> None:
        body = "one\ntwo\nthree\nfour\n"
        _code, payload, _raw = self._run(body, "--lines", "1-4")
        self.assertEqual(payload["data"]["content_bytes"], payload["data"]["range"]["bytes"])
        self.assertEqual(payload["data"]["content_lines"], payload["data"]["range"]["lines"])

    def test_json_content_equals_the_output_file(self) -> None:
        """The issue's acceptance test: sha256-verifiable parity."""
        body = "".join(f"line-{i}\n" for i in range(1, 6))
        with temp_dir() as tmp:
            target = Path(tmp) / "window.txt"
            _code, payload, _raw = self._run(
                body, "--lines", "1-5", "--output-file", str(target)
            )
            written = target.read_bytes()
        self.assertEqual(written, payload["data"]["content"].encode("utf-8"))
        self.assertEqual(
            hashlib.sha256(written).hexdigest(),
            payload["data"]["content_sha256"],
        )
        self.assertEqual(
            payload["data"]["output_file"]["sha256"],
            payload["data"]["content_sha256"],
        )
        self.assertEqual(payload["data"]["output_file"]["file_bytes"], len(written))

    def test_grep_line_number_prefixes_survive(self) -> None:
        """``grep -n -E`` prefixes are part of the answer, not decoration."""
        body = (
            "41-2026-11-05 ERROR boom\n"
            "42:2026-11-05 ERROR bang\n"
            "43-2026-11-05 ERROR biff\n"
        )
        _code, payload, _raw = self._run(
            body, "--grep", "ERROR", "--context", "1"
        )
        self.assertEqual(payload["data"]["content"], body)
        self.assertEqual(payload["stdout"], body)
        first = payload["data"]["content"].splitlines()[0]
        self.assertRegex(first, r"^\d+[:-]")

    def test_grep_prefixes_survive_into_the_output_file_too(self) -> None:
        body = "7:hit one\n9:hit two\n"
        with temp_dir() as tmp:
            target = Path(tmp) / "hits.txt"
            _code, payload, _raw = self._run(
                body, "--grep", "hit", "--output-file", str(target)
            )
            written = target.read_text(encoding="utf-8")
        self.assertEqual(written, payload["data"]["content"])
        self.assertTrue(payload["data"]["content"].startswith("7:hit one"))

    def test_stat_mode_content_is_the_wc_line(self) -> None:
        _code, payload, _raw = self._run("  46 2470\n", "--stat")
        self.assertEqual(payload["data"]["content"], "  46 2470\n")
        self.assertEqual(payload["data"]["stat"], {"lines": 46, "bytes": 2470})
        # Two numbers cannot be cut by a byte budget, so no advice is due.
        self.assertIsNone(payload["data"]["content_hint"])
        self.assertFalse(payload["data"]["content_truncated"])

    def test_truncated_window_says_so_and_points_at_output_file(self) -> None:
        body = "x" * 100
        _code, payload, _raw = self._run(
            body, "--lines", "1-40", "--max-bytes", "100"
        )
        data = payload["data"]
        self.assertTrue(data["content_truncated"])
        self.assertIsNotNone(data["content_hint"])
        self.assertIn("--output-file", data["content_hint"])
        self.assertIn("next_offset", data["content_hint"])

    def test_transport_elision_marks_the_content_truncated(self) -> None:
        body = "visible\n"
        _code, payload, _raw = self._run(
            body, "--lines", "1-5000",
            data={"truncated": True, "bytes_elided": 2048},
        )
        self.assertTrue(payload["data"]["content_truncated"])
        self.assertEqual(payload["data"]["transport_bytes_elided"], 2048)

    def test_oversized_max_bytes_warns_before_it_bites(self) -> None:
        _code, payload, _raw = self._run(
            "short\n", "--lines", "1", "--max-bytes", "12000000"
        )
        self.assertIsNotNone(payload["data"]["content_hint"])
        self.assertIn("--output-file", payload["data"]["content_hint"])

    def test_failure_still_has_the_field_set(self) -> None:
        slice_mod._run_ssh_execute = lambda *a, **k: _transport_payload(
            "", success=False, exit_code=102, stderr="connection refused",
            data={"failure_class": "network"},
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = slice_mod.main(
                ["lab-240", "/var/log/app.log", "--lines", "1-5", "--json"]
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 102)
        self.assertEqual(payload["data"]["failure_class"], "network")
        self.assertEqual(payload["data"]["content"], "")
        self.assertEqual(payload["stdout"], "")


class SliceHumanMode(unittest.TestCase):
    """Human mode must stay byte-identical: body on stdout, advice on stderr."""

    def setUp(self) -> None:
        self._original = slice_mod._run_ssh_execute

    def tearDown(self) -> None:
        slice_mod._run_ssh_execute = self._original

    def test_stdout_is_only_the_body(self) -> None:
        slice_mod._run_ssh_execute = lambda *a, **k: _transport_payload(
            "line one\nline two\n"
        )
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = slice_mod.main(["lab-240", "/var/log/app.log", "--lines", "1-2"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "line one\nline two\n")
        self.assertEqual(err.getvalue(), "")

    def test_hint_goes_to_stderr_not_into_the_body(self) -> None:
        slice_mod._run_ssh_execute = lambda *a, **k: _transport_payload(
            "x" * 100
        )
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            slice_mod.main(
                ["lab-240", "/var/log/app.log", "--lines", "1-40",
                 "--max-bytes", "100"]
            )
        self.assertEqual(out.getvalue(), "x" * 100)
        self.assertIn("--output-file", err.getvalue())


class DoctorProfileParser(unittest.TestCase):
    """``--profile`` is a closed set and ``transport`` is the default."""

    def test_default_is_transport(self) -> None:
        args = doctor.build_parser().parse_args([])
        self.assertEqual(args.profile, "transport")

    def test_full_is_accepted(self) -> None:
        args = doctor.build_parser().parse_args(["--profile", "full"])
        self.assertEqual(args.profile, "full")

    def test_unknown_profile_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            doctor.build_parser().parse_args(["--profile", "everything"])

    def test_help_mentions_the_profile(self) -> None:
        text = doctor.build_parser().format_help()
        self.assertIn("--profile", text)
        self.assertIn("full", text)


class DoctorRemoteBatch(unittest.TestCase):
    """OPT-001: N binaries must cost ONE ssh round trip."""

    def test_only_one_worker_call_for_every_backend(self) -> None:
        calls: list[list[str]] = []
        original = doctor._run_worker

        def fake_worker(argv, timeout):
            calls.append(list(argv))
            return (
                {
                    "success": True,
                    "exit_code": 0,
                    "stdout": (
                        "__SSHCORE_BACKEND__ kubectl|/usr/bin/kubectl|"
                        "Client Version: v1.31.0\n"
                        "__SSHCORE_BACKEND__ helm|/usr/bin/helm|v3.14.0\n"
                    ),
                    "stderr": "",
                    "data": {"failure_class": None},
                },
                12,
            )

        doctor._run_worker = fake_worker
        try:
            args = doctor.build_parser().parse_args(
                ["--profile", "full", "--host", "lab-240"]
            )
            args.modes = ["key"]
            statuses = doctor.probe_remote_backends(
                "lab-240", args, ("kubectl", "helm", "docker")
            )
        finally:
            doctor._run_worker = original
        self.assertEqual(len(calls), 1, calls)
        command = calls[0][2]
        # every requested binary appears in the same remote command
        for name in ("kubectl", "helm", "docker"):
            self.assertIn(f"command -v {name}", command)
        self.assertTrue(statuses["kubectl"].present)
        self.assertEqual(statuses["kubectl"].version, "1.31.0")
        self.assertTrue(statuses["helm"].present)
        self.assertFalse(statuses["docker"].present)

    def test_parse_handles_a_missing_binary_without_a_version(self) -> None:
        statuses = doctor._parse_remote_backends(
            "__SSHCORE_BACKEND__ docker|/usr/bin/docker|\n"
            "__SSHCORE_BACKEND__ podman|\n",
            ("docker", "podman"),
        )
        self.assertTrue(statuses["docker"].present)
        self.assertIsNone(statuses["docker"].version)
        self.assertFalse(statuses["podman"].present)

    def test_parse_ignores_a_login_banner(self) -> None:
        statuses = doctor._parse_remote_backends(
            "Authorized users only.\n"
            "__SSHCORE_BACKEND__ tail|/usr/bin/tail|tail (GNU coreutils) 8.32\n",
            ("tail",),
        )
        self.assertTrue(statuses["tail"].present)
        self.assertEqual(statuses["tail"].version, "8.32")

    def test_failed_transport_degrades_every_backend_honestly(self) -> None:
        original = doctor._run_worker

        def fake_worker(argv, timeout):
            return (
                {
                    "success": False,
                    "exit_code": 102,
                    "stdout": "",
                    "stderr": "ssh: connect to host lab-240 port 22: Network is unreachable",
                    "data": {"failure_class": "network"},
                },
                10,
            )

        doctor._run_worker = fake_worker
        try:
            args = doctor.build_parser().parse_args(["--host", "lab-240"])
            args.modes = ["key"]
            statuses = doctor.probe_remote_backends("lab-240", args, ("helm",))
        finally:
            doctor._run_worker = original
        self.assertFalse(statuses["helm"].present)
        self.assertIn("network", statuses["helm"].detail)
        self.assertEqual(statuses["helm"].probe_error, "network")

    def test_an_unreachable_target_is_unknown_not_a_false_green(self) -> None:
        """A probe that never ran must not read as "this plugin is fine"."""
        def status(name: str, present: bool) -> doctor.BackendStatus:
            return doctor.BackendStatus(
                name=name, kind="binary", present=present,
                path=f"/usr/bin/{name}" if present else None,
            )

        local = {
            "ssh": status("ssh", True),
            "nc": status("nc", False),
            "tail": status("tail", False),
            "journalctl": status("journalctl", False),
        }
        remote = {
            name: doctor.BackendStatus(
                name=name, kind="binary", scope="remote", present=False,
                detail="remote probe failed (network): unreachable",
                probe_error="network",
            )
            for name in ("ssh", "nc", "tail", "journalctl")
        }
        rows = {row.plugin: row for row in doctor.assess_plugins(local, remote)}
        self.assertEqual(rows["remote-debug"].status, "unknown")
        self.assertEqual(rows["remote-debug"].probe_error, "network")
        self.assertFalse(rows["remote-debug"].as_dict()["ready"])


class DoctorVersionBanners(unittest.TestCase):
    """``version`` is a nicety, but a banner must not be misread as "missing"."""

    def test_kubectl_style_banner(self) -> None:
        self.assertEqual(
            doctor._short_version("Client Version: v1.31.0"), "1.31.0"
        )

    def test_docker_style_banner(self) -> None:
        self.assertEqual(
            doctor._short_version("Docker version 24.0.7, build afdd53b"),
            "24.0.7",
        )

    def test_helm_build_metadata_is_stripped(self) -> None:
        self.assertEqual(doctor._short_version("v3.14.0+gd3f6a8b"), "3.14.0")

    def test_coreutils_banner(self) -> None:
        self.assertEqual(
            doctor._short_version("tail (GNU coreutils) 8.32"), "8.32"
        )

    def test_a_banner_without_a_version_is_none(self) -> None:
        self.assertIsNone(doctor._short_version("OpenSSH_9.5p1"))
        self.assertIsNone(doctor._short_version("nc"))


class DoctorReadiness(unittest.TestCase):
    """Per-plugin readiness is computed from statuses, not from PATH guesses."""

    def _status(self, name: str, present: bool, version: str | None = None,
                scope: str = "local") -> doctor.BackendStatus:
        return doctor.BackendStatus(
            name=name, kind="binary", scope=scope, present=present,
            version=version, path=f"/usr/bin/{name}" if present else None,
        )

    def test_any_of_group_is_satisfied_by_one_member(self) -> None:
        local = {
            "docker": self._status("docker", False),
            "podman": self._status("podman", True, "4.9.3"),
            "ssh": self._status("ssh", True),
            "yaml": doctor.BackendStatus("yaml", "python", present=False),
            "hcl2": doctor.BackendStatus("hcl2", "python", present=False),
        }
        rows = {row.plugin: row for row in doctor.assess_plugins(local)}
        self.assertEqual(rows["docker-quick"].status, "ok")
        self.assertTrue(any("podman 4.9.3" in item
                            for item in rows["docker-quick"].present))

    def test_missing_required_backend_is_degraded_and_named(self) -> None:
        local = {
            "ssh": self._status("ssh", True),
            "kubectl": self._status("kubectl", False),
            "helm": self._status("helm", False),
        }
        rows = {row.plugin: row for row in doctor.assess_plugins(local)}
        self.assertEqual(rows["k8s-debug"].status, "degraded")
        joined = ", ".join(rows["k8s-debug"].missing)
        self.assertIn("kubectl", joined)
        self.assertIn("helm", joined)

    def test_python_module_is_required_where_it_is_used(self) -> None:
        local = {
            "ssh": self._status("ssh", True),
            "gh": self._status("gh", True, "2.40.1"),
            "yaml": doctor.BackendStatus("yaml", "python", present=False),
        }
        rows = {row.plugin: row for row in doctor.assess_plugins(local)}
        self.assertEqual(rows["cicd-debug"].status, "degraded")
        self.assertIn("python:yaml", rows["cicd-debug"].missing)

    def test_optional_backends_never_degrade_a_plugin(self) -> None:
        local = {
            "ssh": self._status("ssh", True),
            "journalctl": self._status("journalctl", False),
            "tail": self._status("tail", False),
            "nc": self._status("nc", False),
        }
        rows = {row.plugin: row for row in doctor.assess_plugins(local)}
        self.assertEqual(rows["remote-debug"].status, "ok")
        self.assertIn("nc", rows["remote-debug"].optional_missing)

    def test_every_plugin_in_the_marketplace_has_a_row(self) -> None:
        rows = doctor.assess_plugins({})
        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {row.plugin for row in rows},
            {
                "ssh-core", "ssh-guarded", "remote-debug", "k8s-debug",
                "docker-quick", "iac-state", "cicd-debug", "log-aggregator",
            },
        )

    def test_capabilities_entries_are_machine_readable(self) -> None:
        local = {
            "ssh": self._status("ssh", True),
            "kubectl": self._status("kubectl", True, "1.31.0"),
            "helm": self._status("helm", False),
            "hcl2": doctor.BackendStatus(
                "hcl2", "python", present=True, version="8.1.2"
            ),
        }
        rows = doctor.assess_plugins(local)
        entries = doctor.capability_entries(rows, local, None)
        by_key = {(e["plugin"], e["requirement"]): e for e in entries}
        helm = by_key[("k8s-debug", "helm")]
        for field in ("plugin", "requirement", "kind", "scope", "required",
                      "present", "version"):
            self.assertIn(field, helm)
        self.assertFalse(helm["present"])
        self.assertTrue(by_key[("k8s-debug", "kubectl")]["present"])
        self.assertEqual(by_key[("iac-state", "hcl2")]["version"], "8.1.2")
        self.assertEqual(by_key[("iac-state", "hcl2")]["kind"], "python")

    def test_remote_status_wins_over_a_local_one(self) -> None:
        local = {"ssh": self._status("ssh", True),
                 "kubectl": self._status("kubectl", False)}
        remote = {"ssh": self._status("ssh", True, scope="remote"),
                  "kubectl": self._status("kubectl", True, "1.31.0", scope="remote")}
        rows = {row.plugin: row for row in doctor.assess_plugins(local, remote)}
        self.assertEqual(rows["k8s-debug"].status, "degraded")  # helm still absent
        self.assertIn("kubectl 1.31.0", rows["k8s-debug"].present)


class DoctorFullProfileEndToEnd(unittest.TestCase):
    """The shipped command, run for real (local probes only, no ssh)."""

    def _run(self, *args: str) -> tuple[int, dict, str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "doctor.py"), *args, "--json"],
            capture_output=True, timeout=300, check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        stdout = proc.stdout.decode("utf-8", "replace")
        self.assertTrue(
            stdout.strip(),
            f"doctor.py {args} produced no stdout (rc={proc.returncode}); "
            f"stderr:\n{stderr}",
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic
            self.fail(
                f"doctor.py {args} did not return an envelope ({exc}); "
                f"stdout={stdout[:400]!r} stderr={stderr[:400]!r}"
            )
        return proc.returncode, payload, stderr

    def _unready_plugin(self) -> str:
        """Pick a plugin this machine really cannot run, or skip.

        The end-to-end test must not assume ``kubectl`` is absent: CI images
        and developer boxes differ. The contract under test is "a plugin that
        is not ready makes --require exit 9", not "this box lacks k8s".
        """
        _code, payload, _err = self._run("--profile", "full")
        rows = payload["data"]["plugin_readiness"].get("local", [])
        for row in rows:
            if not row.get("ready"):
                return str(row["plugin"])
        self.skipTest("all eight plugin backends are ready on this host")

    def test_full_profile_reports_capabilities(self) -> None:
        code, payload, _err = self._run("--profile", "full")
        self.assertIn(code, (0, 9))
        data = payload["data"]
        self.assertEqual(data["profile"], "full")
        entries = data["capabilities"]
        self.assertTrue(entries)
        plugins = {entry["plugin"] for entry in entries}
        self.assertIn("k8s-debug", plugins)
        self.assertIn("docker-quick", plugins)
        self.assertIn("iac-state", plugins)
        for entry in entries:
            self.assertIn("present", entry)
            self.assertIn("requirement", entry)
        readiness = data["plugin_readiness"]["local"]
        self.assertEqual(len(readiness), 8)

    def test_transport_profile_is_unchanged(self) -> None:
        """Byte compatibility: no new keys, no new rows (OPT-001 acceptance)."""
        code, payload, _err = self._run()
        self.assertIn(code, (0, 9))
        data = payload["data"]
        self.assertNotIn("capabilities", data)
        self.assertNotIn("plugin_readiness", data)
        self.assertEqual(
            sorted(data),
            [
                "does_not_work", "executable", "failure_class",
                "missing_requirements", "modes", "probes", "python",
                "required", "targets", "version", "works",
            ],
        )

    def test_transport_is_the_default_profile(self) -> None:
        _code, payload, _err = self._run("--profile", "transport")
        self.assertNotIn("profile", payload["data"])

    def test_require_a_ready_plugin_passes(self) -> None:
        code, payload, _err = self._run("--profile", "full", "--require", "ssh-core")
        self.assertEqual(code, 0, payload["data"]["missing_requirements"])
        self.assertEqual(payload["data"]["missing_requirements"], [])

    def test_require_a_degraded_plugin_exits_9(self) -> None:
        plugin = self._unready_plugin()
        code, payload, _err = self._run("--profile", "full", "--require", plugin)
        self.assertEqual(code, 9)
        self.assertIn(plugin, payload["data"]["missing_requirements"])

    def test_require_a_plugin_prefix_matches_the_family(self) -> None:
        """``--require k8s`` gates on ``k8s-debug``, like ``--require exec``."""
        plugin = self._unready_plugin()
        prefix = plugin.split("-", 1)[0]
        code, payload, _err = self._run("--profile", "full", "--require", prefix)
        self.assertEqual(code, 9)
        self.assertIn(prefix, payload["data"]["missing_requirements"])

    def test_require_an_unknown_name_is_refused(self) -> None:
        code, payload, _err = self._run("--profile", "full", "--require", "nope")
        self.assertEqual(code, 2)
        self.assertEqual(payload["data"]["failure_class"], "invalid_arguments")


class SelfcheckProfilePointer(unittest.TestCase):
    """selfcheck stays local; it names the command that answers the rest."""

    def test_human_report_points_at_doctor_full(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "selfcheck.py")],
            capture_output=True, timeout=180, check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        text = proc.stdout.decode("utf-8", "replace")
        self.assertIn("doctor.py --profile full", text)

    def test_json_carries_the_pointer(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "selfcheck.py"), "--json"],
            capture_output=True, timeout=180, check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        self.assertIn("doctor.py --profile full", payload["data"]["profile_hint"])


if __name__ == "__main__":
    unittest.main()
