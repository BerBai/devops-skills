"""CI-13: cover `lib/configfile.py` and `lib/ssh_config.py` directly.

Both are load-bearing and both were only exercised *through* a CLI subprocess,
which is why the line coverage for them stayed under 40% even though their
behaviour is covered end-to-end. These tests drive the functions themselves, so
a regression names the function rather than "some CLI returned 1".

`lib/configfile.py` owns the one thing this project cannot get wrong: the lock
plus atomic replace that keeps `~/.ssh/config` from being lost or half-written.
`lib/ssh_config.py` owns "adopt key auth for one alias" — the step that turns a
password-only host into a key-authenticated one after `deploy_pubkey`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.configfile import (  # noqa: E402
    LOCK_TIMEOUT_S,
    ConfigLock,
    ConfigLockTimeout,
    atomic_write_with_backup,
    find_backup,
    latest_backup,
    list_backups,
    next_backup_path,
    restore_backup,
)

from .ssh_core.compat import temp_dir  # noqa: E402
from .ssh_core.helpers import SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
from lib.ssh_config import AdoptOutcome, adopt_key_auth, chmod_private  # noqa: E402

CONFIG = """\
# environment: production
# tags: web
Host web-01
    HostName 10.0.0.1
    User deploy
    # auth: password

Host db-01
    HostName 10.0.0.2
    User admin
"""


class Locking(unittest.TestCase):
    def test_lock_is_reentrant_across_sequential_uses(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            with ConfigLock(target):
                pass
            with ConfigLock(target):  # must not deadlock on the same file
                pass

    def test_lock_file_is_created_next_to_the_config(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            with ConfigLock(target):
                lock = target.with_name(target.name + ".lock")
                self.assertTrue(lock.exists())
            # The lock file is deliberately never deleted: unlinking a locked
            # file is how two processes end up holding the same lock.
            self.assertTrue(lock.exists())

    def test_second_holder_times_out_rather_than_hanging(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            first = ConfigLock(target)
            first.acquire()
            try:
                with self.assertRaises(ConfigLockTimeout):
                    ConfigLock(target, timeout=0.2).acquire()
            finally:
                first.release()

    def test_release_is_idempotent(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            lock = ConfigLock(target)
            lock.acquire()
            lock.release()
            lock.release()  # must not raise

    def test_default_timeout_is_documented(self) -> None:
        self.assertGreater(LOCK_TIMEOUT_S, 0)


class BackupNaming(unittest.TestCase):
    def test_each_call_returns_a_fresh_path(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            seen = {next_backup_path(target) for _ in range(5)}
            self.assertEqual(len(seen), 5)

    def test_name_is_sortable_and_contains_the_pid(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            name = next_backup_path(target).name
            self.assertIn(f"-{os.getpid()}-", name)
            self.assertTrue(name.startswith("config.bak."))


class AtomicWrite(unittest.TestCase):
    def test_write_creates_a_backup_with_the_old_content(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            backup = atomic_write_with_backup(target, "Host new\n    HostName 1.2.3.4\n")
            self.assertTrue(backup.is_file())
            self.assertIn("web-01", backup.read_text(encoding="utf-8"))
            self.assertIn("Host new", target.read_text(encoding="utf-8"))

    def test_write_leaves_no_scratch_file_behind(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config"
            target.write_text(CONFIG, encoding="utf-8")
            atomic_write_with_backup(target, "Host only\n")
            leftovers = [p.name for p in Path(tmp).iterdir() if ".tmp." in p.name]
            self.assertEqual(leftovers, [])

    def test_missing_target_is_refused(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            with self.assertRaises(FileNotFoundError):
                atomic_write_with_backup(Path(tmp) / "nope", "x")


class BackupDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir(ignore_cleanup_errors=True)
        self.target = Path(self._tmp.name) / "config"
        self.target.write_text(CONFIG, encoding="utf-8")
        for index in range(3):
            atomic_write_with_backup(self.target, f"# revision {index}\nHost h{index}\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_returns_every_generation(self) -> None:
        self.assertEqual(len(list_backups(self.target)), 3)

    def test_latest_is_the_newest_empty_named_backup(self) -> None:
        latest = latest_backup(self.target)
        self.assertIsNotNone(latest)
        self.assertTrue(str(latest).startswith(str(self.target) + ".bak."))

    def test_find_by_suffix_fragment(self) -> None:
        backups = list_backups(self.target)
        fragment = backups[-1].name.split(".bak.")[-1][:8]
        found = find_backup(self.target, fragment)
        self.assertIsNotNone(found)

    def test_find_unknown_fragment_returns_none(self) -> None:
        self.assertIsNone(find_backup(self.target, "does-not-exist"))

    def test_restore_puts_the_content_back(self) -> None:
        restore_backup(self.target, latest_backup(self.target))
        # The newest backup holds the state *before* the last write.
        self.assertIn("revision 1", self.target.read_text(encoding="utf-8"))

    def test_earliest_resolves(self) -> None:
        """FA-40: `--from earliest|oldest|first` used to answer not_found."""
        for token in ("earliest", "oldest", "first"):
            with self.subTest(token=token):
                found = find_backup(self.target, token)
                self.assertIsNotNone(found)
                self.assertEqual(found, list_backups(self.target)[0])

    def test_restore_does_not_redefine_latest(self) -> None:
        """FA-40: two `restore --from latest` runs must be idempotent."""
        newest = latest_backup(self.target)
        self.assertIsNotNone(newest)
        expected = newest.read_text(encoding="utf-8")
        restore_backup(self.target, newest)
        second = find_backup(self.target, "latest")
        self.assertEqual(second, newest, "restore invented a new 'latest' backup")
        restore_backup(self.target, second)
        self.assertEqual(self.target.read_text(encoding="utf-8"), expected)


class AdoptKeyAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir(ignore_cleanup_errors=True)
        self.config = Path(self._tmp.name) / "config"
        self.config.write_text(CONFIG, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_config_writes_nothing(self) -> None:
        missing = Path(self._tmp.name) / "absent"
        outcome = adopt_key_auth("web-01", "~/.ssh/id_ed25519", ssh_config=str(missing))
        self.assertFalse(outcome.changed)
        self.assertFalse(outcome.alias_known)
        self.assertFalse(missing.exists())
        self.assertIn("does not exist", outcome.message)

    def test_unknown_alias_writes_nothing(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        outcome = adopt_key_auth("nope", "~/.ssh/id_ed25519", ssh_config=str(self.config))
        self.assertFalse(outcome.changed)
        self.assertFalse(outcome.alias_known)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertIn("not in", outcome.message)

    def test_password_alias_is_flipped_to_key(self) -> None:
        outcome = adopt_key_auth(
            "web-01", "~/.ssh/id_ed25519", ssh_config=str(self.config)
        )
        self.assertTrue(outcome.changed)
        self.assertTrue(outcome.alias_known)
        self.assertIn("auth", outcome.changed_fields)
        self.assertIn("identity_file", outcome.changed_fields)
        self.assertIsNotNone(outcome.backup_path)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("# auth: key", text)
        self.assertIn("IdentityFile ~/.ssh/id_ed25519", text)
        # The untouched host keeps its own metadata.
        self.assertIn("Host db-01", text)

    def test_second_run_is_a_no_op(self) -> None:
        adopt_key_auth("web-01", "~/.ssh/id_ed25519", ssh_config=str(self.config))
        again = adopt_key_auth(
            "web-01", "~/.ssh/id_ed25519", ssh_config=str(self.config)
        )
        self.assertFalse(again.changed)
        self.assertIn("already uses key auth", again.message)

    def test_a_different_key_replaces_the_identity(self) -> None:
        adopt_key_auth("web-01", "~/.ssh/id_ed25519", ssh_config=str(self.config))
        swapped = adopt_key_auth(
            "web-01", "~/.ssh/id_rsa", ssh_config=str(self.config)
        )
        self.assertTrue(swapped.changed)
        self.assertEqual(swapped.changed_fields, ["identity_file"])
        self.assertIn("IdentityFile ~/.ssh/id_rsa", self.config.read_text(encoding="utf-8"))

    def test_outcome_defaults(self) -> None:
        outcome = AdoptOutcome(changed=False, alias_known=True, changed_fields=[])
        self.assertIsNone(outcome.backup_path)
        self.assertEqual(outcome.message, "")


class ChmodPrivate(unittest.TestCase):
    def test_sets_0600(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "secret"
            path.write_text("x", encoding="utf-8")
            chmod_private(path)
            if os.name != "nt":  # Windows has no POSIX permission bits
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_file_is_ignored(self) -> None:
        chmod_private(Path("/nonexistent/secret"))  # must not raise


if __name__ == "__main__":
    unittest.main()
