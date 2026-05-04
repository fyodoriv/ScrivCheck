"""
Resilience and edge-case tests.

These cover the failure modes a chaos engineering tool MUST handle
without losing data or producing misleading output:

  * adversarial backup zips (zip slip, symlink escape)
  * Unicode normalization mismatch (NFC/NFD) in book and zip filenames
  * missing-backup diagnostics (the most common real-world UX failure)
  * disk-space starvation before destructive steps
  * run-dir name collisions when scrivcheck is invoked twice in a second
  * Scrivener.app not installed
  * filesystem oddities (permission errors, files vanishing mid-walk)

A regression in any of these silently breaks the safety contract that
every other test suite assumes. Pin them here.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests._helpers import make_fake_scriv, zip_scriv_package, SAMPLE_BOOK
import validate_scrivener_backups as vsb


# ---------------------------------------------------------------------------
# Zip-slip and symlink defenses
# ---------------------------------------------------------------------------


class SafeExtractZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "dest"
        self.dest.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _build_zip(self, members):
        """Build a zip with the given (arcname, data) pairs at known mtime."""
        zip_path = self.root / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for arcname, data in members:
                zf.writestr(arcname, data)
        return zip_path

    def test_normal_zip_extracts_cleanly(self):
        z = self._build_zip([
            ("a/b/c.txt", b"hello"),
            ("a/d.txt", b"world"),
        ])
        vsb.safe_extract_zip(z, self.dest)
        self.assertEqual((self.dest / "a/b/c.txt").read_bytes(), b"hello")
        self.assertEqual((self.dest / "a/d.txt").read_bytes(), b"world")

    def test_traversal_via_dotdot_is_blocked(self):
        z = self._build_zip([
            ("../escaped.txt", b"escape"),
        ])
        with self.assertRaises(RuntimeError) as ctx:
            vsb.safe_extract_zip(z, self.dest)
        self.assertIn("Zip-slip", str(ctx.exception))
        # Critically, no file was written outside the dest
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_absolute_path_entry_blocked(self):
        # Use an unrelated absolute path so the entry resolves outside dest
        z = self._build_zip([
            ("/tmp/scrivcheck-evil-test-marker.txt", b"escape"),
        ])
        with self.assertRaises(RuntimeError):
            vsb.safe_extract_zip(z, self.dest)
        self.assertFalse(Path("/tmp/scrivcheck-evil-test-marker.txt").exists())

    def test_symlink_entry_rejected(self):
        # Build a zip carrying a symlink entry by setting the unix mode.
        zip_path = self.root / "sym.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120000 | 0o777) << 16  # symlink mode
            zf.writestr(info, "/etc/passwd")
        with self.assertRaises(RuntimeError) as ctx:
            vsb.safe_extract_zip(zip_path, self.dest)
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertFalse((self.dest / "link").exists())

    def test_nested_traversal_using_legitimate_prefix(self):
        """Adversaries often hide ``..`` deep in the path."""
        z = self._build_zip([
            ("safe/../../escaped.txt", b"escape"),
        ])
        with self.assertRaises(RuntimeError):
            vsb.safe_extract_zip(z, self.dest)


# ---------------------------------------------------------------------------
# Unicode NFC/NFD matching
# ---------------------------------------------------------------------------


class UnicodeNormalizationTests(unittest.TestCase):
    """macOS sometimes stores filenames in NFD (combining-form). A book
    name typed as NFC must still match its on-disk zip even when the
    representations differ. The previous regex did a literal compare and
    silently missed every match — this is the test that prevents it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nfd_filename_matches_nfc_book_name(self):
        nfc_name = "Café"  # composed
        nfd_zip_name = unicodedata.normalize("NFD", "Café") + ".bak.zip"
        zip_path = self.dir / nfd_zip_name
        zip_path.write_bytes(b"x")
        os.utime(zip_path, (time.time(), time.time()))

        result = vsb.find_latest_backup(self.dir, nfc_name)
        self.assertIsNotNone(result, "NFC name should match NFD on-disk filename")
        self.assertEqual(result, zip_path)

    def test_cyrillic_with_separator(self):
        zip_path = self.dir / "Спускаясь По Спирали-bak-2026-01-04T11-40.zip"
        zip_path.write_bytes(b"x")
        result = vsb.find_latest_backup(self.dir, "Спускаясь По Спирали")
        self.assertEqual(result, zip_path)

    def test_scrivener3_dash_bak_dash_pattern(self):
        """The Scrivener-3 default naming uses ``-bak-<timestamp>``."""
        zip_path = self.dir / "MyBook-bak-2026-01-04T11-40.zip"
        zip_path.write_bytes(b"x")
        result = vsb.find_latest_backup(self.dir, "MyBook")
        self.assertEqual(result, zip_path)


# ---------------------------------------------------------------------------
# Missing-backup diagnostics
# ---------------------------------------------------------------------------


class DiagnoseMissingBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = logging.getLogger(f"diag-{id(self)}")
        self.log.handlers.clear()
        self.records: list[str] = []
        h = logging.Handler()
        # logging.LogRecord.getMessage() already performs %-formatting, so
        # don't double-format. Just append the formatted message.
        h.emit = lambda r: self.records.append(r.getMessage())
        self.log.addHandler(h)
        self.log.setLevel(logging.DEBUG)

    def tearDown(self):
        self.tmp.cleanup()

    def _all(self):
        return "\n".join(self.records)

    def test_directory_does_not_exist_logs_clear_message(self):
        vsb.diagnose_missing_backup(
            self.dir / "nope", "MyBook", self.log,
        )
        self.assertIn("does not exist", self._all())

    def test_directory_with_only_scriv_dirs_hints_at_sync_folder_misconfig(self):
        (self.dir / "Foo.scriv").mkdir()
        (self.dir / "Bar.scriv").mkdir()
        vsb.diagnose_missing_backup(self.dir, "MyBook", self.log)
        text = self._all()
        self.assertIn("non-zip entries", text)
        self.assertIn("iOS sync folder", text)

    def test_directory_with_unrelated_zips_hints_at_back_up_on_save(self):
        (self.dir / "OtherBook-bak-2026-01-01.zip").write_bytes(b"x")
        (self.dir / "ThirdBook-bak-2026-01-02.zip").write_bytes(b"x")
        vsb.diagnose_missing_backup(self.dir, "MyBook", self.log)
        text = self._all()
        self.assertIn("Back up on save", text)

    def test_directory_with_substring_match_hints_at_rename(self):
        (self.dir / "OldNameMyBook-bak.zip").write_bytes(b"x")
        vsb.diagnose_missing_backup(self.dir, "MyBook", self.log)
        text = self._all()
        self.assertIn("substring", text)
        self.assertIn("renamed", text)


# ---------------------------------------------------------------------------
# Run-dir collision protection
# ---------------------------------------------------------------------------


class MakeRunDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_unused_path(self):
        d = vsb.make_run_dir(self.root)
        self.assertFalse(d.exists())  # caller is responsible for mkdir
        self.assertTrue(d.name.startswith("run_"))

    def test_collision_appends_counter(self):
        # Pre-create the base path; make_run_dir must invent a fresh one
        with mock.patch("validate_scrivener_backups.datetime") as mdt:
            mdt.now.return_value.strftime.return_value = "2026-05-03_22-03-40"
            (self.root / "run_2026-05-03_22-03-40").mkdir()
            d1 = vsb.make_run_dir(self.root)
            self.assertEqual(d1.name, "run_2026-05-03_22-03-40_1")
            d1.mkdir()
            d2 = vsb.make_run_dir(self.root)
            self.assertEqual(d2.name, "run_2026-05-03_22-03-40_2")

    def test_exhausted_counter_raises(self):
        with mock.patch("validate_scrivener_backups.datetime") as mdt:
            mdt.now.return_value.strftime.return_value = "T"
            # Fake every candidate as existing
            with mock.patch.object(Path, "exists", return_value=True):
                with self.assertRaises(RuntimeError):
                    vsb.make_run_dir(self.root)


# ---------------------------------------------------------------------------
# Disk-space pre-flight
# ---------------------------------------------------------------------------


class DiskSpacePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scriv = make_fake_scriv(self.root, "Book", SAMPLE_BOOK)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.log = logging.getLogger(f"disk-{id(self)}")
        self.log.addHandler(logging.NullHandler())

    def tearDown(self):
        self.tmp.cleanup()

    def test_passes_when_disk_has_room(self):
        # Real run on a normal dev box — should always have headroom.
        vsb.assert_enough_free_space(self.scriv, self.run_dir, self.log)

    def test_raises_when_free_space_below_threshold(self):
        fake_usage = mock.MagicMock(free=10)  # 10 bytes free
        with mock.patch("validate_scrivener_backups.shutil.disk_usage",
                        return_value=fake_usage):
            with self.assertRaises(RuntimeError) as ctx:
                vsb.assert_enough_free_space(self.scriv, self.run_dir, self.log)
        self.assertIn("Not enough free space", str(ctx.exception))


class DirectorySizeTests(unittest.TestCase):
    def test_sums_all_file_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a").write_bytes(b"x" * 100)
            (d / "sub").mkdir()
            (d / "sub" / "b").write_bytes(b"y" * 50)
            self.assertEqual(vsb.directory_size_bytes(d), 150)

    def test_ignores_files_that_vanish_mid_walk(self):
        """Race condition: file disappears between os.walk and stat()."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a").write_bytes(b"x" * 10)
            ghost = d / "ghost"
            ghost.write_bytes(b"y" * 5)

            real_stat = Path.stat
            def flaky_stat(self, *a, **kw):
                if self == ghost:
                    raise OSError("vanished")
                return real_stat(self, *a, **kw)
            with mock.patch.object(Path, "stat", flaky_stat):
                # The ghost file's size is skipped silently.
                self.assertEqual(vsb.directory_size_bytes(d), 10)


# ---------------------------------------------------------------------------
# Scrivener backup-path discovery
# ---------------------------------------------------------------------------


class DiscoverScrivenerBackupPathTests(unittest.TestCase):
    """Reading SCRAutomaticBackupPath from Scrivener's prefs is the
    single biggest UX win — the previous "guess a default" approach
    pointed at the wrong directory for any user who'd redirected
    backups (which is most users with Dropbox/iCloud setups)."""

    def test_returns_none_when_defaults_command_missing(self):
        with mock.patch(
            "validate_scrivener_backups.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            self.assertIsNone(vsb.discover_scrivener_backup_path())

    def test_returns_none_on_timeout(self):
        import subprocess as _sp
        with mock.patch(
            "validate_scrivener_backups.subprocess.run",
            side_effect=_sp.TimeoutExpired("defaults", 5),
        ):
            self.assertIsNone(vsb.discover_scrivener_backup_path())

    def test_returns_none_when_pref_unset(self):
        with mock.patch(
            "validate_scrivener_backups.subprocess.run",
            return_value=mock.MagicMock(returncode=1, stdout="", stderr=""),
        ):
            self.assertIsNone(vsb.discover_scrivener_backup_path())

    def test_returns_none_when_pref_empty_string(self):
        with mock.patch(
            "validate_scrivener_backups.subprocess.run",
            return_value=mock.MagicMock(returncode=0, stdout="\n"),
        ):
            self.assertIsNone(vsb.discover_scrivener_backup_path())

    def test_returns_path_when_pref_set_and_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "validate_scrivener_backups.subprocess.run",
                return_value=mock.MagicMock(returncode=0, stdout=tmp + "\n"),
            ):
                self.assertEqual(vsb.discover_scrivener_backup_path(), Path(tmp))

    def test_returns_none_when_pref_path_does_not_exist(self):
        """A configured-but-deleted path mustn't get returned silently;
        the fallback path is more useful in that case."""
        with mock.patch(
            "validate_scrivener_backups.subprocess.run",
            return_value=mock.MagicMock(
                returncode=0, stdout="/Users/nope/totally-not-real/x\n",
            ),
        ):
            self.assertIsNone(vsb.discover_scrivener_backup_path())


class MainBackupPathDiscoveryTests(unittest.TestCase):
    """When --backups is omitted, main() should pick up Scrivener's
    configured path. When provided, main() should respect it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"; self.local.mkdir()
        self.run_root = self.root / "runs"; self.run_root.mkdir()
        self.discovered = self.root / "discovered_backups"
        self.discovered.mkdir()
        make_fake_scriv(self.local, "MyBook", SAMPLE_BOOK)
        zip_scriv_package(
            self.local / "MyBook.scriv",
            self.discovered / "MyBook.bak.zip",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_uses_discovered_path_when_backups_flag_absent(self):
        argv = [
            "scrivcheck",
            "--local", str(self.local),
            "--run-root", str(self.run_root),
            "--dry-run",
        ]
        with mock.patch.object(vsb.sys, "platform", "darwin"), \
             mock.patch.object(sys, "argv", argv), \
             mock.patch(
                 "validate_scrivener_backups.discover_scrivener_backup_path",
                 return_value=self.discovered,
             ):
            self.assertEqual(vsb.main(), 0)
            run_dir = next(self.run_root.iterdir())
            log_text = (run_dir / "logs" / "run.log").read_text()
            self.assertIn(str(self.discovered), log_text)
            self.assertIn("auto-discovered", log_text)

    def test_main_falls_back_when_discovery_returns_none_and_fallback_exists(self):
        argv = [
            "scrivcheck",
            "--local", str(self.local),
            "--run-root", str(self.run_root),
            "--dry-run",
        ]
        with mock.patch.object(vsb.sys, "platform", "darwin"), \
             mock.patch.object(sys, "argv", argv), \
             mock.patch(
                 "validate_scrivener_backups.discover_scrivener_backup_path",
                 return_value=None,
             ), \
             mock.patch(
                 "validate_scrivener_backups.FALLBACK_BACKUPS",
                 self.discovered,
             ):
            self.assertEqual(vsb.main(), 0)
            run_dir = next(self.run_root.iterdir())
            log_text = (run_dir / "logs" / "run.log").read_text()
            self.assertIn("fallback default", log_text)


# ---------------------------------------------------------------------------
# validate_book full-flow guards (integration with new helpers)
# ---------------------------------------------------------------------------


class ValidateBookGuardsTests(unittest.TestCase):
    """End-to-end checks that the new defenses fire from inside
    validate_book and route into the standard FAIL + safety-copy flow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local = root / "local"; self.local.mkdir()
        self.backups = root / "backups"; self.backups.mkdir()
        self.run_dir = root / "run"; (self.run_dir / "logs").mkdir(parents=True)

        self.scriv = make_fake_scriv(self.local, "MyBook", SAMPLE_BOOK)
        self.zip_path = zip_scriv_package(
            self.scriv, self.backups / "MyBook.bak.zip",
        )

        log = logging.getLogger(f"vbg-{id(self)}")
        log.addHandler(logging.NullHandler())

        patches = [
            mock.patch("validate_scrivener_backups.scrivener_running",
                       return_value=False),
            mock.patch("validate_scrivener_backups.screencapture",
                       return_value=None),
            mock.patch("validate_scrivener_backups.ensure_locally_available"),
        ]
        for p in patches:
            p.start()
        for p in patches:
            self.addCleanup(p.stop)

        self.v = vsb.Validator(
            local_dir=self.local, backup_dir=self.backups,
            run_dir=self.run_dir, log=log,
            screenshots=False, dry_run=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_slip_in_backup_triggers_fail_with_intact_original(self):
        # Replace the legitimate backup with one that escapes the staging
        self.zip_path.unlink()
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("../../escaped.txt", "evil")
            zf.writestr("MyBook.scriv/Files/Data/UUID-1/content.rtf", "ignored")

        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        self.v.validate_book(book)

        self.assertEqual(book.status, "FAIL")
        self.assertIn("Zip-slip", book.failure_reason)
        # Original must be back at its real location after rollback
        self.assertTrue(self.scriv.exists())

    def test_disk_space_starvation_aborts_before_quarantine(self):
        """If we don't have room, NEVER move the original — the rollback
        path could fail too. Aborting before quarantine_original keeps
        the live file untouched."""
        with mock.patch(
            "validate_scrivener_backups.assert_enough_free_space",
            side_effect=RuntimeError("Not enough free space"),
        ):
            book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
            self.v.validate_book(book)
        self.assertEqual(book.status, "FAIL")
        # Original is still there, untouched, and nothing got quarantined
        self.assertTrue(self.scriv.exists())
        self.assertEqual(list(self.v.originals.iterdir()), [])

    def test_missing_backup_creates_backup_and_passes(self):
        """When no backup zip exists, the tool creates one and proceeds to PASS."""
        self.zip_path.unlink()
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        self.v.validate_book(book)
        self.assertEqual(book.status, "PASS",
                         f"steps={book.steps} reason={book.failure_reason}")
        step_names = [s["name"] for s in book.steps]
        self.assertIn("create_backup", step_names)
        # Original must be back at real location after PASS
        self.assertTrue(self.scriv.exists())


# ---------------------------------------------------------------------------
# create_backup_zip
# ---------------------------------------------------------------------------


class CreateBackupZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scriv = make_fake_scriv(self.root / "local", "MyBook", SAMPLE_BOOK)
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        self.log = logging.getLogger(f"cbz-{id(self)}")
        self.log.addHandler(logging.NullHandler())

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_zip_with_scrivener_naming_convention(self):
        zip_path = vsb.create_backup_zip(self.scriv, self.backup_dir, self.log)
        self.assertTrue(zip_path.exists())
        self.assertRegex(
            zip_path.name,
            r"^MyBook-bak-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}\.zip$",
        )

    def test_created_zip_is_findable_by_find_latest_backup(self):
        vsb.create_backup_zip(self.scriv, self.backup_dir, self.log)
        found = vsb.find_latest_backup(self.backup_dir, "MyBook")
        self.assertIsNotNone(found)

    def test_created_zip_contains_scriv_package(self):
        zip_path = vsb.create_backup_zip(self.scriv, self.backup_dir, self.log)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        # Top-level directory in the zip must be the .scriv package
        self.assertTrue(any(n.startswith("MyBook.scriv/") for n in names))

    def test_created_zip_content_matches_original(self):
        zip_path = vsb.create_backup_zip(self.scriv, self.backup_dir, self.log)
        # Extract and verify a known file
        extract_dir = self.root / "extracted"
        extract_dir.mkdir()
        vsb.safe_extract_zip(zip_path, extract_dir)
        restored = extract_dir / "MyBook.scriv"
        self.assertTrue(restored.is_dir())
        content = (restored / "Files/Data/UUID-1/content.rtf").read_bytes()
        self.assertEqual(content, SAMPLE_BOOK["Files/Data/UUID-1/content.rtf"])

    def test_dry_run_no_backup_reports_would_create_without_creating(self):
        """In dry-run mode, when no backup exists, no zip is created."""
        run_dir = self.root / "run"
        (run_dir / "logs").mkdir(parents=True)
        v = vsb.Validator(
            local_dir=self.root / "local",
            backup_dir=self.backup_dir,
            run_dir=run_dir,
            log=self.log,
            screenshots=False,
            dry_run=True,
        )
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        v.validate_book(book)

        # Status is SKIPPED (dry-run), not FAIL
        self.assertEqual(book.status, "SKIPPED")
        step_names = [s["name"] for s in book.steps]
        self.assertIn("would_create_backup_dryrun", step_names)
        # No zip was created
        self.assertEqual(list(self.backup_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
