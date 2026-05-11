"""
Manifest computation and comparison.

These tests exercise the steady-state-capture and verification logic that
sits at the heart of the chaos engineering drill. If these pass, the tool
can correctly tell "the backup faithfully reproduces user content" from
"the backup is silently incomplete or corrupted".
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests._helpers import make_fake_scriv, SAMPLE_BOOK
from scrivcheck import (
    compare_manifests,
    compute_manifest,
    is_volatile,
)


class ComputeManifestTests(unittest.TestCase):
    def test_walks_all_files_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            scriv = make_fake_scriv(Path(tmp), "Book", SAMPLE_BOOK)
            m = compute_manifest(scriv)

            self.assertEqual(m.project, "Book")
            self.assertEqual(m.file_count, len(SAMPLE_BOOK))
            self.assertEqual(m.total_size, sum(len(v) for v in SAMPLE_BOOK.values()))

            # Every file should have a sha256
            for entry in m.files:
                self.assertEqual(len(entry.sha256), 64)
                self.assertTrue(all(c in "0123456789abcdef" for c in entry.sha256))

    def test_relpath_uses_forward_slash_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            scriv = make_fake_scriv(Path(tmp), "Book", SAMPLE_BOOK)
            m = compute_manifest(scriv)
            paths = {f.relpath for f in m.files}
            # Some specific paths we expect
            self.assertIn("project.scrivx", paths)
            self.assertIn("Files/Data/UUID-1/content.rtf", paths)

    def test_missing_project_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                compute_manifest(Path(tmp) / "does-not-exist.scriv")

    def test_empty_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = make_fake_scriv(Path(tmp), "Empty", {})
            m = compute_manifest(empty)
            self.assertEqual(m.file_count, 0)
            self.assertEqual(m.total_size, 0)

    def test_files_sorted_deterministically(self):
        """Two identical projects must produce manifests in the same order
        so naive serialization comparison wouldn't false-positive on
        ordering noise."""
        with tempfile.TemporaryDirectory() as tmp:
            a = make_fake_scriv(Path(tmp) / "a", "Book", SAMPLE_BOOK)
            b = make_fake_scriv(Path(tmp) / "b", "Book", SAMPLE_BOOK)
            ma = compute_manifest(a)
            mb = compute_manifest(b)
            self.assertEqual(
                [f.relpath for f in ma.files],
                [f.relpath for f in mb.files],
            )


class CompareManifestTests(unittest.TestCase):
    """The PASS/FAIL decision lives here. These cases enumerate the
    real-world reasons a restored backup might differ from the original."""

    def _two_scrivs(self, content_a, content_b):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        a = make_fake_scriv(Path(tmp) / "a", "Book", content_a)
        b = make_fake_scriv(Path(tmp) / "b", "Book", content_b)
        return compute_manifest(a), compute_manifest(b)

    def test_byte_identical_passes(self):
        ma, mb = self._two_scrivs(SAMPLE_BOOK, SAMPLE_BOOK)
        diff = compare_manifests(ma, mb)
        self.assertTrue(diff["ok"])
        self.assertEqual(diff["content_missing"], [])
        self.assertEqual(diff["content_changed"], [])

    def test_user_content_modified_fails(self):
        modified = dict(SAMPLE_BOOK)
        modified["Files/Data/UUID-1/content.rtf"] = b"TAMPERED"
        ma, mb = self._two_scrivs(SAMPLE_BOOK, modified)
        diff = compare_manifests(ma, mb)
        self.assertFalse(diff["ok"])
        self.assertEqual(len(diff["content_changed"]), 1)
        self.assertEqual(
            diff["content_changed"][0]["relpath"],
            "Files/Data/UUID-1/content.rtf",
        )

    def test_user_content_missing_fails(self):
        truncated = {k: v for k, v in SAMPLE_BOOK.items()
                     if k != "Files/Data/UUID-2/content.rtf"}
        ma, mb = self._two_scrivs(SAMPLE_BOOK, truncated)
        diff = compare_manifests(ma, mb)
        self.assertFalse(diff["ok"])
        self.assertIn("Files/Data/UUID-2/content.rtf", diff["content_missing"])

    def test_user_content_added_passes(self):
        """Restored project having extra user files is unusual but shouldn't
        fail the drill — the original content is intact."""
        with_extra = dict(SAMPLE_BOOK)
        with_extra["Files/Data/UUID-4/content.rtf"] = b"surprise"
        ma, mb = self._two_scrivs(SAMPLE_BOOK, with_extra)
        diff = compare_manifests(ma, mb)
        self.assertTrue(diff["ok"])
        self.assertIn("Files/Data/UUID-4/content.rtf", diff["content_added"])

    def test_volatile_drift_only_passes(self):
        """search.indexes and ui.plist drift between save and restore is
        normal — Scrivener regenerates them. Must not fail the drill."""
        with_volatile_drift = dict(SAMPLE_BOOK)
        with_volatile_drift["Files/search.indexes"] = b"<different-search-data>"
        with_volatile_drift["Settings/ui.plist"] = b"<different-ui>"
        ma, mb = self._two_scrivs(SAMPLE_BOOK, with_volatile_drift)
        diff = compare_manifests(ma, mb)
        self.assertTrue(diff["ok"], f"volatile-only drift should pass: {diff}")

    def test_volatile_missing_does_not_fail_drill(self):
        no_volatile = {k: v for k, v in SAMPLE_BOOK.items()
                       if not is_volatile(k)}
        ma, mb = self._two_scrivs(SAMPLE_BOOK, no_volatile)
        diff = compare_manifests(ma, mb)
        # OK because content files are intact, even though some non-content
        # files went missing.
        self.assertTrue(diff["ok"])
        # Volatile-only drift is reported as informational, not failure
        self.assertEqual(diff["noncontent_missing"], [])

    def test_simultaneous_drift_modes_all_reported(self):
        """If multiple things go wrong at once, the diff should expose all
        of them (so we can debug without a second run)."""
        broken = {
            "project.scrivx": b"<?xml/>",  # minor change, non-content
            "Files/Data/UUID-1/content.rtf": b"chapter one body",  # same
            # UUID-2 missing
            "Files/Data/UUID-3/content.rtf": b"DIFFERENT epilogue",
            "Files/Data/UUID-4/content.rtf": b"unexpected",  # added
            "Files/search.indexes": b"new",  # volatile, ignored
        }
        ma, mb = self._two_scrivs(SAMPLE_BOOK, broken)
        diff = compare_manifests(ma, mb)
        self.assertFalse(diff["ok"])
        self.assertIn("Files/Data/UUID-2/content.rtf", diff["content_missing"])
        self.assertIn("Files/Data/UUID-4/content.rtf", diff["content_added"])
        changed_paths = {c["relpath"] for c in diff["content_changed"]}
        self.assertIn("Files/Data/UUID-3/content.rtf", changed_paths)


class IsVolatileTests(unittest.TestCase):
    def test_known_volatile_files(self):
        self.assertTrue(is_volatile("Files/search.indexes"))
        self.assertTrue(is_volatile("Settings/ui.plist"))
        self.assertTrue(is_volatile(".DS_Store"))

    def test_user_content_not_volatile(self):
        self.assertFalse(is_volatile("Files/Data/UUID/content.rtf"))
        self.assertFalse(is_volatile("project.scrivx"))


if __name__ == "__main__":
    unittest.main()
