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
import scrivcheck as vsb


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
            mock.patch("scrivcheck.scrivener_running",
                       return_value=False),
            mock.patch("scrivcheck.screencapture",
                       return_value=None),
            mock.patch.object(vsb.Validator, "_scrivener_shot"),
            mock.patch("scrivcheck.ensure_locally_available"),
            mock.patch("scrivcheck.open_in_browser"),
        ]
        return patches

    def _argv(self, *extra):
        # Screenshots are on by default; screencapture is mocked to return None.
        return [
            "scrivcheck.py",
            "--local", str(self.local),
            "--backups", str(self.backups),
            "--run-root", str(self.run_root),
            *extra,
        ]

    def test_non_darwin_returns_two(self):
        with mock.patch.object(vsb.sys, "platform", "linux"), \
             mock.patch.object(sys, "argv", self._argv()):
            self.assertEqual(vsb.main(), 2)

    def test_missing_local_returns_two(self):
        argv = [
            "scrivcheck.py",
            "--local", str(self.root / "nope"),
            "--backups", str(self.backups),
            "--run-root", str(self.run_root),
        ]
        with mock.patch.object(vsb.sys, "platform", "darwin"), \
             mock.patch.object(sys, "argv", argv):
            self.assertEqual(vsb.main(), 2)

    def test_missing_backups_returns_two(self):
        argv = [
            "scrivcheck.py",
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
            with mock.patch("scrivcheck.screencapture",
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
            with mock.patch("scrivcheck.screencapture",
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


class LiveSnapshotProofTests(unittest.TestCase):
    """Cover the LIVE SNAPSHOT section of _emit_proof."""

    def _make_validator(self, tmp):
        run_dir = Path(tmp) / "run"
        (run_dir / "logs").mkdir(parents=True)
        log = logging.getLogger(f"lsp-{id(self)}")
        log.addHandler(logging.NullHandler())
        return vsb.Validator(
            local_dir=Path(tmp) / "local",
            backup_dir=Path(tmp) / "backups",
            run_dir=run_dir,
            log=log,
            screenshots=False,
            dry_run=False,
        )

    def _book(self, **kw):
        b = vsb.BookResult(name="MyBook", project_path="/x/MyBook.scriv")
        b.status = "PASS"
        for k, v in kw.items():
            setattr(b, k, v)
        return b

    def test_no_post_mtime_leaves_verdict_empty(self):
        """latest_content_mtime_post=None → mtime_verdict is empty string."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._make_validator(tmp)
            book = self._book(
                latest_content_file="Files/Data/X/content.rtf",
                latest_content_mtime="2026-05-04T19:00:00",
                latest_content_mtime_post=None,   # <-- branch under test
                latest_content_snippet="some text",
            )
            v._emit_proof(book)
            proof = (v.run_dir / "proof" / "MyBook.txt").read_text()
            self.assertIn("LIVE SNAPSHOT", proof)
            self.assertNotIn("✓ MATCH", proof)

    def test_malformed_mtime_falls_back_to_dash(self):
        """ValueError in fromisoformat → mtime_verdict = '—'."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._make_validator(tmp)
            book = self._book(
                latest_content_file="Files/Data/X/content.rtf",
                latest_content_mtime="not-a-date",
                latest_content_mtime_post="also-not-a-date",
                latest_content_snippet=None,
            )
            v._emit_proof(book)
            proof = (v.run_dir / "proof" / "MyBook.txt").read_text()
            self.assertIn("—", proof)

    def test_long_snippet_gets_ellipsis_prefix(self):
        """Snippets longer than 400 chars are truncated with a leading '…'."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._make_validator(tmp)
            book = self._book(
                latest_content_file="Files/Data/X/content.rtf",
                latest_content_mtime="2026-05-04T19:00:00",
                latest_content_mtime_post="2026-05-04T19:01:00",  # zip newer → match
                latest_content_snippet="x" * 500,
            )
            v._emit_proof(book)
            proof = (v.run_dir / "proof" / "MyBook.txt").read_text()
            self.assertIn("…", proof)

    def test_backup_older_than_last_save_shows_warning(self):
        """If the zip predates the last save, verdict warns about the gap."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._make_validator(tmp)
            book = self._book(
                latest_content_file="Files/Data/X/content.rtf",
                latest_content_mtime="2026-05-04T20:00:00",   # file saved at 20:00
                latest_content_mtime_post="2026-05-04T19:00:00",  # zip from 19:00
                latest_content_snippet=None,
            )
            v._emit_proof(book)
            proof = (v.run_dir / "proof" / "MyBook.txt").read_text()
            self.assertIn("⚠", proof)


class LatestFlagTests(unittest.TestCase):
    """--latest flag restricts validation to the most-recently-modified book."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"
        self.backups = self.root / "backups"
        self.run_root = self.root / "runs"
        self.local.mkdir()
        self.backups.mkdir()
        self.run_root.mkdir()

        import time
        self.scriv_a = make_fake_scriv(self.local, "ABook", SAMPLE_BOOK)
        time.sleep(0.01)
        self.scriv_b = make_fake_scriv(self.local, "ZBook", SAMPLE_BOOK)
        zip_scriv_package(self.scriv_a, self.backups / "ABook.bak.zip")
        zip_scriv_package(self.scriv_b, self.backups / "ZBook.bak.zip")

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *extra):
        return [
            "scrivcheck.py",
            "--local", str(self.local),
            "--backups", str(self.backups),
            "--run-root", str(self.run_root),
            *extra,
        ]

    def test_latest_flag_validates_only_most_recent(self):
        patches = [
            mock.patch.object(vsb.sys, "platform", "darwin"),
            mock.patch("scrivcheck.scrivener_running", return_value=False),
            mock.patch("scrivcheck.screencapture", return_value=None),
            mock.patch.object(vsb.Validator, "_scrivener_shot"),
            mock.patch("scrivcheck.ensure_locally_available"),
            mock.patch("scrivcheck.open_in_browser"),
        ]
        with mock.patch.object(sys, "argv", self._argv("--latest")):
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])
            rc = vsb.main()
        self.assertEqual(rc, 0)
        run_dir = next(self.run_root.iterdir())
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(report["totals"]["books"], 1)
        # ZBook was created last so it's the latest
        self.assertEqual(report["books"][0]["name"], "ZBook")

    def test_default_mode_validates_all_books(self):
        patches = [
            mock.patch.object(vsb.sys, "platform", "darwin"),
            mock.patch("scrivcheck.scrivener_running", return_value=False),
            mock.patch("scrivcheck.screencapture", return_value=None),
            mock.patch.object(vsb.Validator, "_scrivener_shot"),
            mock.patch("scrivcheck.ensure_locally_available"),
            mock.patch("scrivcheck.open_in_browser"),
        ]
        with mock.patch.object(sys, "argv", self._argv()):
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])
            rc = vsb.main()
        self.assertEqual(rc, 0)
        run_dir = next(self.run_root.iterdir())
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(report["totals"]["books"], 2)


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
                mock.patch("scrivcheck.scrivener_running",
                           return_value=False),
                mock.patch("scrivcheck.screencapture",
                           return_value=None),
                mock.patch.object(vsb.Validator, "_scrivener_shot"),
                mock.patch("scrivcheck.ensure_locally_available"),
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

    def test_quarantine_overwrites_wrong_target(self):
        """If a failed restore put wrong content at the target slot but the
        quarantine holds the real original, rollback must remove the wrong
        content and restore from quarantine — not skip because slot is full."""
        import shutil
        # Simulate a slot occupied by wrong content (e.g. wrong project)
        wrong = self.local / "MyBook.scriv"
        wrong.mkdir()
        (wrong / "wrong.txt").write_bytes(b"wrong")
        # Quarantine has the real original
        quarantined = self.v.originals / "MyBook.scriv"
        shutil.copytree(self.scriv, quarantined)

        book = self._new_book()
        self.v._rollback(
            book=book,
            project_path=Path(book.project_path),
            quarantined=quarantined,
            safety_copy=None,
        )
        self.assertEqual(book.steps[-1]["ok"], True)
        self.assertIn("restored from", book.steps[-1]["detail"])
        # The wrong content must be gone; real content is back
        self.assertFalse((wrong / "wrong.txt").exists())

    def test_target_already_present_is_a_noop_when_no_quarantine(self):
        """When no quarantine exists (failure before quarantine step), the
        target still holds the original — no rollback needed."""
        import shutil
        shutil.copytree(self.scriv, self.local / "MyBook.scriv")

        book = self._new_book()
        self.v._rollback(
            book=book,
            project_path=Path(book.project_path),
            quarantined=None,       # original was NEVER quarantined
            safety_copy=None,
        )
        self.assertEqual(book.steps[-1]["detail"], "target already present")

    def test_rollback_swallows_copy_errors(self):
        """Rollback must NEVER raise — it's the last line of defense."""
        import shutil
        quarantined = self.v.originals / "MyBook.scriv"
        shutil.copytree(self.scriv, quarantined)

        book = self._new_book()
        with mock.patch("scrivcheck.shutil.copytree",
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


class EmitProofTests(unittest.TestCase):
    """The verbose proof block is the user-facing evidence that a backup
    is real and restorable. Lock down its shape with tests so a refactor
    can't quietly drop the per-file SHA-256 lines or the attestation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local = root / "local"
        self.backups = root / "backups"
        self.run_dir = root / "run"
        self.local.mkdir()
        self.backups.mkdir()
        (self.run_dir / "logs").mkdir(parents=True)

        self.scriv = make_fake_scriv(self.local, "MyBook", SAMPLE_BOOK)
        self.zip_path = zip_scriv_package(
            self.scriv, self.backups / "MyBook.bak.zip"
        )

        log = logging.getLogger("proof-test")
        log.addHandler(logging.NullHandler())
        self.v = vsb.Validator(
            local_dir=self.local, backup_dir=self.backups,
            run_dir=self.run_dir, log=log,
            screenshots=False, dry_run=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _book_with_full_manifests(self, status="PASS"):
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        book.backup_zip = str(self.zip_path)
        book.pre_manifest = vsb.compute_manifest(self.scriv)
        book.post_manifest = vsb.compute_manifest(self.scriv)
        book.status = status
        return book

    def _proof_text(self, book):
        self.v._emit_proof(book)
        return (self.run_dir / "proof" / f"{book.name}.txt").read_text()

    def test_pass_proof_contains_zip_sha_and_match_lines(self):
        book = self._book_with_full_manifests("PASS")
        text = self._proof_text(book)

        # Zip metadata
        self.assertIn("Backup file", text)
        self.assertIn("sha256", text)
        # Per-file SHA-256 prefix (first 16 hex chars) must appear
        for entry in book.pre_manifest.files:
            if entry.relpath.startswith("Files/Data/"):
                self.assertIn(entry.sha256[:16], text,
                              f"prefix {entry.sha256[:16]} not found for {entry.relpath}")
        # Every content file must show a MATCH
        content_count = len(book.pre_manifest.content_entries())
        self.assertEqual(text.count("✓ MATCH"), content_count,
                         f"expected {content_count} MATCHes, got\n{text}")
        # Attestation
        self.assertIn("ATTESTATION", text)
        self.assertIn("HELD", text)

    def test_fail_proof_marks_mismatch(self):
        book = self._book_with_full_manifests("FAIL")
        # Tamper one post-manifest entry to force a mismatch
        for f in book.post_manifest.files:
            if f.relpath.startswith("Files/Data/"):
                f.sha256 = "0" * 64
                break
        text = self._proof_text(book)
        self.assertIn("✗ MISMATCH", text)
        self.assertIn("REJECTED", text)
        self.assertIn("Mismatch:", text)

    def test_fail_proof_marks_missing(self):
        book = self._book_with_full_manifests("FAIL")
        # Drop one content file from the post-manifest entirely
        target = next(f.relpath for f in book.pre_manifest.files
                      if f.relpath.startswith("Files/Data/"))
        book.post_manifest.files = [
            f for f in book.post_manifest.files if f.relpath != target
        ]
        text = self._proof_text(book)
        self.assertIn("✗ MISSING", text)
        self.assertIn("Missing:", text)

    def test_failure_before_restore_emits_note(self):
        """If we never got to a post-restore manifest, the proof block
        should still be useful — show the zip + pre-flight + a NOTE
        explaining why the drill aborted."""
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        book.backup_zip = str(self.zip_path)
        book.pre_manifest = vsb.compute_manifest(self.scriv)
        book.status = "FAIL"
        book.failure_reason = "No backup zip found"
        text = self._proof_text(book)
        self.assertIn("NOTE", text)
        self.assertIn("No backup zip found", text)
        # And the zip+pre-flight sections should still be present
        self.assertIn("Pre-flight steady state", text)

    def test_proof_skips_zip_section_when_no_backup_known(self):
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        book.pre_manifest = vsb.compute_manifest(self.scriv)
        book.status = "FAIL"
        book.failure_reason = "discovery error"
        text = self._proof_text(book)
        # No backup info → no zip line
        self.assertNotIn("sha256", text.lower().split("attestation")[0])

    def test_proof_skips_zip_section_when_zip_path_missing_on_disk(self):
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        book.backup_zip = str(self.backups / "vanished.zip")
        book.pre_manifest = vsb.compute_manifest(self.scriv)
        book.status = "FAIL"
        text = self._proof_text(book)
        self.assertNotIn("vanished.zip", text)  # no path printed

    def test_dry_run_skipped_book_emits_dry_run_proof(self):
        """Dry-run produces a proof block with the zip metadata + pre-flight
        manifest and a DRY-RUN ATTESTATION footer — but NEVER a post-restore
        section, because nothing was restored."""
        with mock.patch("scrivcheck.screencapture",
                        return_value=None):
            v = vsb.Validator(
                local_dir=self.local, backup_dir=self.backups,
                run_dir=self.run_dir, log=self.v.log,
                screenshots=False, dry_run=True,
            )
            book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
            v.validate_book(book)

        self.assertEqual(book.status, "SKIPPED")
        # Proof artifact still written
        text = (self.run_dir / "proof" / "MyBook.txt").read_text()
        self.assertIn("DRY-RUN ATTESTATION", text)
        self.assertIn("Pre-flight steady state", text)
        self.assertIn("Backup file", text)  # zip section present
        self.assertNotIn("Post-restore", text)  # but no post-restore

    def test_dry_run_when_no_backup_zip_reports_would_create(self):
        """Dry-run with no backup zip: emit proof, note that live run would
        create the backup. Status is SKIPPED, no failure reason about zip."""
        self.zip_path.unlink()

        v = vsb.Validator(
            local_dir=self.local, backup_dir=self.backups,
            run_dir=self.run_dir, log=self.v.log,
            screenshots=False, dry_run=True,
        )
        book = vsb.BookResult(name="MyBook", project_path=str(self.scriv))
        v.validate_book(book)

        self.assertEqual(book.status, "SKIPPED")
        step_names = [s["name"] for s in book.steps]
        self.assertIn("would_create_backup_dryrun", step_names)
        text = (self.run_dir / "proof" / "MyBook.txt").read_text()
        self.assertIn("DRY-RUN ATTESTATION", text)


class WriteHtmlReportTests(unittest.TestCase):
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

    def test_creates_html_file(self):
        vsb.write_html_report(self.run_dir, [self._book()])
        self.assertTrue((self.run_dir / "report.html").exists())

    def test_html_contains_book_name(self):
        out = vsb.write_html_report(self.run_dir, [self._book(name="MyNovel")])
        self.assertIn("MyNovel", out.read_text())

    def test_pass_status_present(self):
        out = vsb.write_html_report(self.run_dir, [self._book(status="PASS")])
        self.assertIn("PASS", out.read_text())

    def test_fail_status_and_reason_present(self):
        out = vsb.write_html_report(
            self.run_dir,
            [self._book(status="FAIL", failure_reason="content drift")],
        )
        text = out.read_text()
        self.assertIn("FAIL", text)
        self.assertIn("content drift", text)

    def test_screenshot_embedded_as_base64(self):
        import base64
        shot_dir = self.run_dir / "screenshots"
        shot_dir.mkdir()
        shot = shot_dir / "001_test.png"
        # A 1×1 white PNG (minimal valid PNG bytes)
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00'
            b'\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18'
            b'\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        shot.write_bytes(png_bytes)
        book = self._book()
        book.screenshots = [str(shot)]
        out = vsb.write_html_report(self.run_dir, [book])
        expected = base64.b64encode(png_bytes).decode()
        self.assertIn(expected, out.read_text())

    def test_empty_books_list_renders_without_crash(self):
        out = vsb.write_html_report(self.run_dir, [])
        self.assertIn("0 pass", out.read_text())

    def test_html_escape_applied_to_book_name(self):
        out = vsb.write_html_report(
            self.run_dir, [self._book(name="<script>alert(1)</script>")]
        )
        self.assertNotIn("<script>", out.read_text())

    def test_screenshot_note_shows_named_books(self):
        out = vsb.write_html_report(
            self.run_dir, [], screenshot_books=("Alpha", "Beta"),
        )
        text = out.read_text()
        self.assertIn("Alpha", text)
        self.assertIn("Beta", text)
        self.assertIn("--screenshot-books", text)

    def test_screenshot_note_shows_all_when_none(self):
        out = vsb.write_html_report(self.run_dir, [], screenshot_books=None)
        self.assertIn("all validated books", out.read_text())

    def test_diff_failure_shows_missing_and_changed_counts(self):
        book = self._book(
            status="FAIL",
            diff_summary={
                "ok": False,
                "content_missing": ["Files/Data/x/content.rtf"],
                "content_changed": [
                    {"relpath": "Files/Data/y/content.rtf",
                     "pre_sha256": "a", "post_sha256": "b",
                     "pre_size": 1, "post_size": 1},
                ],
                "content_added": [],
                "noncontent_missing": [], "noncontent_added": [],
                "pre_total_size": 0, "post_total_size": 0,
                "pre_file_count": 0, "post_file_count": 0,
            },
        )
        out = vsb.write_html_report(self.run_dir, [book])
        text = out.read_text()
        self.assertIn("1 missing", text)
        self.assertIn("1 changed", text)

    def test_long_snippet_gets_ellipsis_prefix(self):
        book = self._book(status="PASS")
        book.latest_content_snippet = "x" * 500
        out = vsb.write_html_report(self.run_dir, [book])
        self.assertIn("…", out.read_text())


class OpenInBrowserTests(unittest.TestCase):
    def test_exception_is_swallowed(self):
        with mock.patch("scrivcheck.subprocess.run",
                        side_effect=OSError("no browser")):
            vsb.open_in_browser(Path("/tmp/report.html"))  # must not raise


class ScreenshotBookArgTests(unittest.TestCase):
    """Cover --screenshot-all-books and --screenshot-books CLI branches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local = root / "local"; self.local.mkdir()
        self.backups = root / "backups"; self.backups.mkdir()
        self.run_root = root / "runs"; self.run_root.mkdir()
        scriv = make_fake_scriv(self.local, "MyBook", SAMPLE_BOOK)
        zip_scriv_package(scriv, self.backups / "MyBook.bak.zip")

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *extra):
        return [
            "scrivcheck",
            "--local", str(self.local),
            "--backups", str(self.backups),
            "--run-root", str(self.run_root),
            *extra,
        ]

    def _patches(self):
        return [
            mock.patch.object(vsb.sys, "platform", "darwin"),
            mock.patch("scrivcheck.scrivener_running", return_value=False),
            mock.patch("scrivcheck.screencapture", return_value=None),
            mock.patch.object(vsb.Validator, "_scrivener_shot"),
            mock.patch("scrivcheck.ensure_locally_available"),
            mock.patch("scrivcheck.open_in_browser"),
        ]

    def test_screenshot_all_books_flag(self):
        with mock.patch.object(sys, "argv", self._argv("--screenshot-all-books")):
            for p in self._patches():
                p.start()
            self.addCleanup(lambda: [p.stop() for p in self._patches()])
            self.assertEqual(vsb.main(), 0)

    def test_screenshot_books_custom_list(self):
        with mock.patch.object(sys, "argv", self._argv("--screenshot-books", "MyBook")):
            for p in self._patches():
                p.start()
            self.addCleanup(lambda: [p.stop() for p in self._patches()])
            self.assertEqual(vsb.main(), 0)


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
