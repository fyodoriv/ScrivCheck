"""
End-to-end tests for the CLI entry point and the report writer.

`main()` is the actual user-facing surface — exit codes, quarantine
purge policy, screenshot wiring, the platform check — and it stitches
together everything else. We exercise it with the macOS automation
layer mocked so the whole pipeline can be driven on Linux CI.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import make_fake_scriv, zip_scriv_package, SAMPLE_BOOK
import validate_scrivener_backups as vsb


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


class WriteReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _book(self, name="Book", status="PASS", **kw):
        b = vsb.BookResult(name=name, project_path=f"/local/{name}.scriv")
        b.status = status
        for k, v in kw.items():
            setattr(b, k, v)
        return b

    def test_pass_book_in_report(self):
        out = vsb.write_report(self.run_dir, [self._book(status="PASS")])
        data = json.loads(out.read_text())
        self.assertEqual(data["totals"]["passed"], 1)
        self.assertEqual(data["totals"]["failed"], 0)
        self.assertIn("Book", (self.run_dir / "report.txt").read_text())

    def test_failure_with_diff_summary_renders(self):
        b = self._book(
            status="FAIL",
            failure_reason="content drift",
            backup_zip="/x/Book.bak.zip",
            diff_summary={
                "ok": False,
                "content_missing": ["Files/Data/UUID-2/content.rtf"],
                "content_added": [],
                "content_changed": [
                    {"relpath": "Files/Data/UUID-1/content.rtf",
                     "pre_sha256": "a", "post_sha256": "b",
                     "pre_size": 1, "post_size": 1}
                ],
                "noncontent_missing": [],
                "noncontent_added": [],
                "pre_total_size": 0, "post_total_size": 0,
                "pre_file_count": 0, "post_file_count": 0,
            },
        )
        vsb.write_report(self.run_dir, [b])
        text = (self.run_dir / "report.txt").read_text()
        self.assertIn("FAIL", text)
        self.assertIn("Book.bak.zip", text)
        self.assertIn("drift:", text)
        self.assertIn("content drift", text)

    def test_skipped_status_marker_present(self):
        b = self._book(status="SKIPPED")
        vsb.write_report(self.run_dir, [b])
        text = (self.run_dir / "report.txt").read_text()
        # SKIPPED lines exist for the book
        self.assertIn("Book", text)

    def test_pending_status_marker(self):
        # Book that never ran (PENDING) must still appear without crashing
        b = vsb.BookResult(name="Never", project_path="/x/Never.scriv")
        vsb.write_report(self.run_dir, [b])
        self.assertIn("Never", (self.run_dir / "report.txt").read_text())

    def test_empty_books_writes_zero_totals(self):
        out = vsb.write_report(self.run_dir, [])
        data = json.loads(out.read_text())
        self.assertEqual(data["totals"], {"books": 0, "passed": 0, "failed": 0, "skipped": 0})


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"
        self.backups = self.root / "backups"
        self.run_root = self.root / "runs"
        self.local.mkdir()
        self.backups.mkdir()
        self.run_root.mkdir()

        self.scriv = make_fake_scriv(self.local, "MyBook", SAMPLE_BOOK)
        self.zip_path = zip_scriv_package(
            self.scriv, self.backups / "MyBook.bak.zip"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _common_patches(self):
        """Patch everything that talks to macOS. Returns the ExitStack."""
        patches = [
            mock.patch.object(vsb.sys, "platform", "darwin"),
            mock.patch("validate_scrivener_backups.scrivener_open"),
            mock.patch("validate_scrivener_backups.scrivener_save_active"),
            mock.patch("validate_scrivener_backups.scrivener_quit"),
            mock.patch("validate_scrivener_backups.scrivener_running",
                       return_value=False),
            mock.patch("validate_scrivener_backups.screencapture",
                       return_value=None),
            mock.patch("validate_scrivener_backups.ensure_locally_available"),
        ]
        return patches

    def _argv(self, *extra):
        return [
            "validate_scrivener_backups.py",
            "--local", str(self.local),
            "--backups", str(self.backups),
            "--run-root", str(self.run_root),
            "--no-screenshots",
            *extra,
        ]

    def test_non_darwin_returns_two(self):
        with mock.patch.object(vsb.sys, "platform", "linux"), \
             mock.patch.object(sys, "argv", self._argv()):
            self.assertEqual(vsb.main(), 2)

    def test_missing_local_returns_two(self):
        argv = [
            "validate_scrivener_backups.py",
            "--local", str(self.root / "nope"),
            "--backups", str(self.backups),
            "--run-root", str(self.run_root),
        ]
        with mock.patch.object(vsb.sys, "platform", "darwin"), \
             mock.patch.object(sys, "argv", argv):
            self.assertEqual(vsb.main(), 2)

    def test_missing_backups_returns_two(self):
        argv = [
            "validate_scrivener_backups.py",
            "--local", str(self.local),
            "--backups", str(self.root / "nope"),
            "--run-root", str(self.run_root),
        ]
        with mock.patch.object(vsb.sys, "platform", "darwin"), \
             mock.patch.object(sys, "argv", argv):
            self.assertEqual(vsb.main(), 2)

    def test_no_books_returns_one(self):
        # Drop the only book so discovery finds nothing
        import shutil
        shutil.rmtree(self.scriv)

        with mock.patch.object(sys, "argv", self._argv()):
            patches = self._common_patches()
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])
            self.assertEqual(vsb.main(), 1)

    def test_happy_path_purges_quarantine(self):
        with mock.patch.object(sys, "argv", self._argv()):
            patches = self._common_patches()
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            rc = vsb.main()
            self.assertEqual(rc, 0)
            # Run dir created
            run_dirs = list(self.run_root.iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            # Report present
            report = json.loads((run_dir / "report.json").read_text())
            self.assertEqual(report["totals"]["passed"], 1)
            self.assertEqual(report["totals"]["failed"], 0)
            # Quarantine purged on success
            self.assertFalse((run_dir / "quarantine").exists())

    def test_keep_quarantine_flag_preserves_dir(self):
        with mock.patch.object(sys, "argv", self._argv("--keep-quarantine")):
            patches = self._common_patches()
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            rc = vsb.main()
            self.assertEqual(rc, 0)
            run_dir = next(self.run_root.iterdir())
            self.assertTrue((run_dir / "quarantine").exists())

    def test_dry_run_returns_zero_and_keeps_quarantine(self):
        with mock.patch.object(sys, "argv", self._argv("--dry-run")):
            patches = self._common_patches()
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            rc = vsb.main()
            self.assertEqual(rc, 0)
            run_dir = next(self.run_root.iterdir())
            self.assertTrue((run_dir / "quarantine").exists())
            report = json.loads((run_dir / "report.json").read_text())
            self.assertEqual(report["totals"]["skipped"], 1)

    def test_failure_returns_one_and_preserves_quarantine(self):
        # Corrupt the backup so validation fails
        self.zip_path.write_bytes(b"not a zip file")

        with mock.patch.object(sys, "argv", self._argv()):
            patches = self._common_patches()
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            rc = vsb.main()
            self.assertEqual(rc, 1)
            run_dir = next(self.run_root.iterdir())
            # Quarantine MUST be preserved — that's where the safety copies live
            self.assertTrue((run_dir / "quarantine").exists())

    def test_book_filter_runs_only_matching_book(self):
        # Add a second book; --book should restrict validation to one
        make_fake_scriv(self.local, "OtherBook", SAMPLE_BOOK)
        zip_scriv_package(
            self.local / "OtherBook.scriv",
            self.backups / "OtherBook.bak.zip",
        )

        with mock.patch.object(sys, "argv", self._argv("--book", "MyBook")):
            patches = self._common_patches()
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            rc = vsb.main()
            self.assertEqual(rc, 0)
            run_dir = next(self.run_root.iterdir())
            report = json.loads((run_dir / "report.json").read_text())
            self.assertEqual(report["totals"]["books"], 1)
            self.assertEqual(report["books"][0]["name"], "MyBook")


# ---------------------------------------------------------------------------
# Validator internals not covered elsewhere
# ---------------------------------------------------------------------------


class ShotTests(unittest.TestCase):
    """Cover the screenshot-list bookkeeping when screencapture succeeds."""

    def test_shot_with_book_appends_to_book_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "logs").mkdir(parents=True)
            log = logging.getLogger("shot-int")
            log.addHandler(logging.NullHandler())

            fake_path = "/tmp/fake-screenshot.png"
            with mock.patch("validate_scrivener_backups.screencapture",
                            return_value=fake_path):
                v = vsb.Validator(
                    local_dir=Path(tmp),
                    backup_dir=Path(tmp),
                    run_dir=run_dir,
                    log=log,
                    screenshots=True,
                    dry_run=False,
                )
                book = vsb.BookResult(name="X", project_path="/x.scriv")
                result = v.shot("preflight", book)
                self.assertEqual(result, fake_path)
                self.assertEqual(book.screenshots, [fake_path])

    def test_shot_without_book_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "logs").mkdir(parents=True)
            log = logging.getLogger("shot-no-book")
            log.addHandler(logging.NullHandler())
            with mock.patch("validate_scrivener_backups.screencapture",
                            return_value="/tmp/x.png"):
                v = vsb.Validator(
                    local_dir=Path(tmp),
                    backup_dir=Path(tmp),
                    run_dir=run_dir,
                    log=log,
                    screenshots=True,
                    dry_run=False,
                )
                # No book → must not crash, must return path
                self.assertEqual(v.shot("preflight"), "/tmp/x.png")


class StagingDirReuseTests(unittest.TestCase):
    """If a previous run left a staging dir behind, validate_book should
    nuke it before extracting — otherwise the rglob would pick up stale
    .scriv folders."""

    def test_pre_existing_staging_book_is_removed_then_extracted(self):
        from tests._helpers import make_fake_scriv, zip_scriv_package
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            backups = root / "backups"
            run_dir = root / "run"
            local.mkdir()
            backups.mkdir()
            (run_dir / "logs").mkdir(parents=True)

            scriv = make_fake_scriv(local, "MyBook", SAMPLE_BOOK)
            zip_path = zip_scriv_package(scriv, backups / "MyBook.bak.zip")

            log = logging.getLogger("staging-test")
            log.addHandler(logging.NullHandler())

            patches = [
                mock.patch("validate_scrivener_backups.scrivener_open"),
                mock.patch("validate_scrivener_backups.scrivener_save_active"),
                mock.patch("validate_scrivener_backups.scrivener_quit"),
                mock.patch("validate_scrivener_backups.scrivener_running",
                           return_value=False),
                mock.patch("validate_scrivener_backups.screencapture",
                           return_value=None),
                mock.patch("validate_scrivener_backups.ensure_locally_available"),
            ]
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            v = vsb.Validator(
                local_dir=local, backup_dir=backups, run_dir=run_dir,
                log=log, screenshots=False, dry_run=False,
            )
            # Pre-create a stale staging dir with junk inside
            stale = v.staging / "MyBook"
            stale.mkdir(parents=True)
            (stale / "leftover.txt").write_text("garbage from earlier run")

            book = vsb.BookResult(name="MyBook", project_path=str(scriv))
            v.validate_book(book)

            self.assertEqual(book.status, "PASS",
                             f"steps={book.steps} reason={book.failure_reason}")


class RollbackFallbackTests(unittest.TestCase):
    """Drive the _rollback method directly to exercise its branches:
    quarantined-only, safety-only, neither-source, and exception handler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local = root / "local"
        self.local.mkdir()
        run_dir = root / "run"
        (run_dir / "logs").mkdir(parents=True)
        self.run_dir = run_dir

        log = logging.getLogger("rb-test")
        log.addHandler(logging.NullHandler())
        self.v = vsb.Validator(
            local_dir=self.local, backup_dir=self.local, run_dir=run_dir,
            log=log, screenshots=False, dry_run=False,
        )
        # Build a stand-in original we can use as a quarantine source
        self.scriv = make_fake_scriv(root / "src", "MyBook", SAMPLE_BOOK)

    def tearDown(self):
        self.tmp.cleanup()

    def _new_book(self):
        return vsb.BookResult(
            name="MyBook",
            project_path=str(self.local / "MyBook.scriv"),
        )

    def test_falls_back_to_safety_copy_when_quarantined_missing(self):
        # Place a safety copy but no quarantined original
        import shutil
        safety = self.v.safety / "MyBook.scriv"
        shutil.copytree(self.scriv, safety)

        book = self._new_book()
        self.v._rollback(
            book=book,
            project_path=Path(book.project_path),
            quarantined=None,
            safety_copy=safety,
        )
        self.assertTrue((self.local / "MyBook.scriv").exists(),
                        "safety copy should have been used")
        self.assertEqual(book.steps[-1]["name"], "rollback")
        self.assertTrue(book.steps[-1]["ok"])

    def test_no_source_available_records_failed_rollback(self):
        book = self._new_book()
        self.v._rollback(
            book=book,
            project_path=Path(book.project_path),
            quarantined=None,
            safety_copy=None,
        )
        self.assertFalse((self.local / "MyBook.scriv").exists())
        self.assertEqual(book.steps[-1]["name"], "rollback")
        self.assertFalse(book.steps[-1]["ok"])
        self.assertIn("no source available", book.steps[-1]["detail"])

    def test_target_already_present_is_a_noop(self):
        # Pretend the original is back at its real location already
        import shutil
        shutil.copytree(self.scriv, self.local / "MyBook.scriv")
        # Also have a quarantined copy that we'd normally restore from
        quarantined = self.v.originals / "MyBook.scriv"
        shutil.copytree(self.scriv, quarantined)

        book = self._new_book()
        self.v._rollback(
            book=book,
            project_path=Path(book.project_path),
            quarantined=quarantined,
            safety_copy=None,
        )
        self.assertEqual(book.steps[-1]["detail"], "target already present")

    def test_rollback_swallows_copy_errors(self):
        """Rollback must NEVER raise — it's the last line of defense."""
        import shutil
        quarantined = self.v.originals / "MyBook.scriv"
        shutil.copytree(self.scriv, quarantined)

        book = self._new_book()
        with mock.patch("validate_scrivener_backups.shutil.copytree",
                        side_effect=OSError("disk full")):
            self.v._rollback(
                book=book,
                project_path=Path(book.project_path),
                quarantined=quarantined,
                safety_copy=None,
            )
        self.assertEqual(book.steps[-1]["name"], "rollback")
        self.assertFalse(book.steps[-1]["ok"])


# ---------------------------------------------------------------------------
# Module-as-script
# ---------------------------------------------------------------------------


class ScriptEntryTests(unittest.TestCase):
    def test_running_module_as_script_invokes_main(self):
        """Cover the `if __name__ == '__main__'` guard."""
        import runpy
        path = Path(vsb.__file__)
        # Patch sys.exit so the test process stays alive, and force the
        # platform check to fail so main() exits quickly without touching
        # the filesystem.
        with mock.patch.object(vsb.sys, "platform", "linux"), \
             mock.patch("sys.exit") as msx, \
             mock.patch.object(sys, "argv", [str(path)]):
            runpy.run_path(str(path), run_name="__main__")
            msx.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
