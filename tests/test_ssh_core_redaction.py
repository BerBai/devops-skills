"""SSH-07: host identifiers must not leak into the envelope."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib.redact as redact_module  # noqa: E402
from lib import emit  # noqa: E402
from lib.redact import HostFacts, Redactor, redact_identity_path  # noqa: E402


class TextRedaction(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor(
            facts=HostFacts(
                alias="prod-db",
                user="root",
                hostname="192.168.30.240",
                identity_files=["C:/Users/me/.ssh/id_ed25519"],
            )
        )

    def test_user_at_host_is_replaced(self) -> None:
        text = "root@192.168.30.240: Permission denied (publickey)."
        out = self.redactor.text(text)
        self.assertNotIn("192.168.30.240", out)
        self.assertNotIn("root@", out)
        self.assertIn("<alias>", out)

    def test_hostname_alone_is_replaced(self) -> None:
        out = self.redactor.text("Connecting to 192.168.30.240 port 22")
        self.assertIn("<host>", out)
        self.assertNotIn("192.168.30.240", out)

    def test_alias_is_kept(self) -> None:
        self.assertEqual(self.redactor.text("prod-db unreachable"), "prod-db unreachable")

    def test_argv_identity_flag_is_hidden(self) -> None:
        argv = [
            "ssh", "-i", "C:/Users/me/.ssh/id_ed25519",
            "-o", "IdentityFile=C:/Users/me/.ssh/id_ed25519",
            "root@192.168.30.240", "true",
        ]
        out = self.redactor.argv(argv)
        self.assertNotIn("id_ed25519", " ".join(out))
        self.assertIn("<alias>", out)

    def test_literal_user_at_host_port(self) -> None:
        facts = HostFacts(alias="root@10.0.0.5:2222", literal_user="root",
                          literal_host="10.0.0.5", literal_port="2222")
        out = Redactor(facts=facts).text("root@10.0.0.5: Permission denied")
        self.assertNotIn("10.0.0.5", out)

    def test_success_path_argv_is_still_redacted(self) -> None:
        result = {
            "success": True,
            "exit_code": 0,
            "stdout": "ok from 192.168.30.240\n",
            "stderr": "",
            "data": {"ssh_argv": ["ssh", "192.168.30.240", "true"],
                     "raw_stderr": "root@192.168.30.240 ok"},
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            emit(result, as_json=True, redactor=self.redactor)
        payload = buffer.getvalue()
        self.assertNotIn("192.168.30.240", payload)
        self.assertIn("<alias>", payload)


class IdentityPath(unittest.TestCase):
    def test_standard_key_name_is_kept(self) -> None:
        self.assertEqual(
            redact_identity_path("C:/Users/me/.ssh/id_ed25519"), "~/.ssh/id_ed25519"
        )

    def test_custom_key_hides_the_directory(self) -> None:
        self.assertEqual(
            redact_identity_path("C:/keys/prod_ed25519"), "<key-dir>/prod_ed25519"
        )


class SshGFacts(unittest.TestCase):
    def test_facts_come_from_ssh_g(self) -> None:
        sample = "user deploy\nhostname 10.0.1.11\nport 2222\nidentityfile ~/.ssh/id_x\n"
        parsed = dict(line.split(None, 1) for line in sample.strip().splitlines())
        with mock.patch.object(redact_module, "_ssh_g", return_value=parsed):
            redact_module._CACHE.clear()
            r = redact_module.redactor_for("prod-web")
        self.assertEqual(r.facts.user, "deploy")
        self.assertEqual(r.facts.hostname, "10.0.1.11")
        self.assertIn("~/.ssh/id_x", r.facts.identity_files)


if __name__ == "__main__":
    unittest.main()
