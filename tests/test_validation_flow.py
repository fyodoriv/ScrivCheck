"""
Integration tests for the per-book validation flow.

The macOS-specific calls (AppleScript, screencapture, Scrivener open/quit)
are mocked. What we're testing is the *flow*: that a happy path produces
PASS, that simulated failures route through the rollback code, and that
no permutation of failure leaves the user without their original file.

The chaos engineering invariant under test:

    For every book at every point in time during a run, at least one of
    the following must be true:
      - the original .scriv is at its real location, OR
      - the original .scriv is in quarantine/originals/, OR
      - a copy of the original is in quarantine/safety-copies/.

If we ever observe a state where NONE of the three holds, the test fails.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests._helpers import make_fake_scriv, zip_scriv_package, SAMPLE_BOOK
from scrivcheck import (
    Validator,
    BookResult,
    compute_manifest,
)


class FlowTestCase(unittest.TestCase):
    """Sets up a realistic on-disk layout: a local folder with a .scriv,
    a backup folder with a matching zip, and a run directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local = root / "local"
        self.backups = root / "backups"
        self.run_dir = root / "run"
        self.local.mkdir()
        self.backups.mkdir()
        (self.run_dir / "logs").mkdir(parents=True)

        # Create a real .scriv in the local folder
        self.scriv = make_fake_scriv(self.local, "MyBook", SAMPLE_BOOK)
        # Create a matching backup zip
        self.zip_path = zip_scriv_package(
            self.scriv, self.backups / "MyBook.bak.zip"
        )

        self.log = logging.getLogger("flow-test")
        self.log.addHandler(logging.NullHandler())

        # Patch all macOS-specific calls used by validate_book
        patches = [
            mock.patch("scrivcheck.scrivener_running",
                       return_value=False),
            mock.patch("scrivcheck.screencapture",
                       return_value=None),
            mock.patch(
                "scrivcheck.ensure_locally_available"
            ),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

        self.validator = Validator(
            local_dir=self.local,
            backup_dir=self.backups,
            run_dir=self.run_dir,
            log=self.log,
            screenshots=False,
            dry_run=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    # --- Invariant assertions ---

    def assert_data_safety_invariant(self, book: BookResult):
        """For chaos engineering this is the only assertion that really
        matters: at no observable point should the original be lost."""
        target = self.local / Path(book.project_path).name
        quar_orig = self.validator.originals / Path(book.project_path).name
        safety = self.validator.safety / Path(book.project_path).name

        present = []
        if target.exists():
            present.append("local")
        if quar_orig.exists():
            present.append("quarantine/originals")
        if safety.exists():
            present.append("quarantine/safety-copies")

        self.assertTrue(
            present,
            f"DATA LOSS: {book.name} not present in any of "
            "[local, quarantine/originals, quarantine/safety-copies]",
        )


class ScrivenerRunningWarningTests(FlowTestCase):
    """If Scrivener is running when the live drill starts, the pre-flight
    manifest could catch files mid-write. validate_book emits a clear
    warning step but does NOT auto-quit (auto-quit could lose the user's
    in-progress edits). The drill proceeds best-effort."""

    def test_warning_step_recorded_when_scrivener_running(self):
        with mock.patch("scrivcheck.scrivener_running",
                        return_value=True):
            book = BookResult(name="MyBook", project_path=str(self.scriv))
            self.validator.validate_book(book)
        step_names = [s["name"] for s in book.steps]
        self.assertIn("scrivener_running_warning", step_names)
        # Drill still proceeds and passes
        self.assertEqual(book.status, "PASS",
                         f"steps={book.steps} reason={book.failure_reason}")


class HappyPathTests(FlowTestCase):
    def test_full_flow_produces_pass(self):
        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)

        self.assertEqual(book.status, "PASS", f"steps={book.steps} reason={book.failure_reason}")
        self.assertIsNotNone(book.pre_manifest)
        self.assertIsNotNone(book.post_manifest)
        self.assertTrue(book.diff_summary["ok"])
        self.assert_data_safety_invariant(book)

    def test_pass_run_leaves_book_at_real_location(self):
        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)
        self.assertTrue(self.scriv.exists(), "book must be back at real location after PASS")

    def test_pass_steps_recorded_in_order(self):
        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)
        names = [s["name"] for s in book.steps]
        # Don't pin exact sequence, but assert the critical waypoints occur
        for required in ("safety_copy", "quarantine_original",
                         "unzip_backup", "restore_to_local",
                         "verify_manifest"):
            self.assertIn(required, names, f"missing step: {required}")

    def test_safety_copy_exists_after_quarantine_step(self):
        """Defense-in-depth: between safety_copy and successful end of run,
        BOTH the safety copy and the quarantined original must exist."""
        original_move = shutil.move
        observed = {"both_present_at_some_point": False}

        def spy_move(src, dst, *a, **kw):
            result = original_move(src, dst, *a, **kw)
            # After a move into the originals quarantine, check both copies
            dst_path = Path(dst)
            if dst_path.parent == self.validator.originals:
                safety = self.validator.safety / dst_path.name
                if safety.exists() and dst_path.exists():
                    observed["both_present_at_some_point"] = True
            return result

        with mock.patch("scrivcheck.shutil.move", side_effect=spy_move):
            book = BookResult(name="MyBook", project_path=str(self.scriv))
            self.validator.validate_book(book)

        self.assertTrue(observed["both_present_at_some_point"],
                        "defense-in-depth invariant violated")


class FailureRollbackTests(FlowTestCase):
    def test_no_backup_zip_creates_backup_and_passes(self):
        # Remove the backup — the tool should create one and proceed.
        self.zip_path.unlink()

        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)

        self.assertEqual(book.status, "PASS",
                         f"steps={book.steps} reason={book.failure_reason}")
        step_names = [s["name"] for s in book.steps]
        self.assertIn("create_backup", step_names)
        # Original must be back at its real location after PASS.
        self.assertTrue(self.scriv.exists())
        self.assert_data_safety_invariant(book)

    def test_backup_creation_failure_marks_fail_without_touching_original(self):
        """If create_backup_zip itself fails (e.g. permission error), the
        original is never quarantined and the book is marked FAIL."""
        self.zip_path.unlink()

        import scrivcheck as vsb_module
        with mock.patch.object(vsb_module, "create_backup_zip",
                               side_effect=OSError("disk full")):
            book = BookResult(name="MyBook", project_path=str(self.scriv))
            self.validator.validate_book(book)

        self.assertEqual(book.status, "FAIL")
        # Original must still be at its real location, untouched.
        self.assertTrue(self.scriv.exists())
        # Nothing should have been quarantined (failure before safety copy).
        self.assertFalse(any(self.validator.originals.iterdir()))
        self.assertFalse(any(self.validator.safety.iterdir()))

    def test_corrupted_zip_triggers_rollback(self):
        # Replace the legitimate zip with garbage
        self.zip_path.write_bytes(b"not a zip file")

        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)

        self.assertEqual(book.status, "FAIL")
        # The user MUST still have their book somewhere
        self.assert_data_safety_invariant(book)

    def test_zip_without_scriv_inside_triggers_rollback(self):
        # Re-write zip to contain only stray files (no .scriv package)
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("readme.txt", "this isn't a Scrivener project")

        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)

        self.assertEqual(book.status, "FAIL")
        self.assert_data_safety_invariant(book)
        # And specifically, it should be back at the real location
        # (rollback restores from quarantine/originals)
        self.assertTrue(self.scriv.exists())

    def test_content_drift_in_backup_fails_with_intact_originals(self):
        # Build a backup whose content differs from the live project
        tampered_content = dict(SAMPLE_BOOK)
        tampered_content["Files/Data/UUID-1/content.rtf"] = b"BACKUP IS STALE"
        tmp = Path(self.tmp.name) / "tampered"
        tampered_scriv = make_fake_scriv(tmp, "MyBook", tampered_content)
        # Overwrite the legitimate backup
        self.zip_path.unlink()
        zip_scriv_package(tampered_scriv, self.zip_path)

        book = BookResult(name="MyBook", project_path=str(self.scriv))
        self.validator.validate_book(book)

        self.assertEqual(book.status, "FAIL")
        self.assertIn("drift", book.failure_reason.lower())
        # Diff details should be preserved for forensics
        self.assertIsNotNone(book.diff_summary)
        self.assertEqual(len(book.diff_summary["content_changed"]), 1)
        self.assert_data_safety_invariant(book)


class DryRunTests(FlowTestCase):
    def test_dry_run_does_not_modify_filesystem(self):
        v = Validator(
            local_dir=self.local,
            backup_dir=self.backups,
            run_dir=self.run_dir,
            log=self.log,
            screenshots=False,
            dry_run=True,
        )
        # Snapshot the local folder contents before
        before = compute_manifest(self.scriv)

        book = BookResult(name="MyBook", project_path=str(self.scriv))
        v.validate_book(book)

        self.assertEqual(book.status, "SKIPPED")
        # Nothing should have been moved or copied
        self.assertTrue(self.scriv.exists())
        after = compute_manifest(self.scriv)
        self.assertEqual(before.total_size, after.total_size)
        self.assertEqual(before.file_count, after.file_count)
        # Quarantine subdirs exist (they're created up front) but must be empty
        self.assertEqual(list(v.originals.iterdir()), [])
        self.assertEqual(list(v.safety.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
