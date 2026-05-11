"""
Zip extraction shape and project discovery.

Scrivener can produce zips where the .scriv is at the root of the archive
or nested inside a wrapper folder. The validator handles both by recursing
and picking the shallowest .scriv found. These tests exercise both shapes
and the discovery logic that walks the local Scrivener folder.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests._helpers import make_fake_scriv, zip_scriv_package, SAMPLE_BOOK
from scrivcheck import Validator


class ZipExtractionTests(unittest.TestCase):
    def test_root_level_scriv_zip(self):
        """Most common shape: zip contains MyBook.scriv/* at top level."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            scriv = make_fake_scriv(tmp / "src", "MyBook", SAMPLE_BOOK)
            zip_path = zip_scriv_package(scriv, tmp / "MyBook.bak.zip", nested=False)

            extract = tmp / "extract"
            extract.mkdir()
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract)

            scrivs = [p for p in extract.rglob("*.scriv") if p.is_dir()]
            self.assertEqual(len(scrivs), 1)
            self.assertEqual(scrivs[0].name, "MyBook.scriv")

    def test_nested_scriv_zip(self):
        """Some Scrivener configs zip with a wrapper folder."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            scriv = make_fake_scriv(tmp / "src", "MyBook", SAMPLE_BOOK)
            zip_path = zip_scriv_package(scriv, tmp / "MyBook.bak.zip", nested=True)

            extract = tmp / "extract"
            extract.mkdir()
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract)

            scrivs = sorted(
                (p for p in extract.rglob("*.scriv") if p.is_dir()),
                key=lambda p: len(p.parts),
            )
            self.assertEqual(len(scrivs), 1)
            self.assertEqual(scrivs[0].name, "MyBook.scriv")

    def test_round_trip_preserves_content_hashes(self):
        """End-to-end: zip a project, extract, and confirm content
        manifests match. This is the actual property the tool relies on."""
        from scrivcheck import compute_manifest, compare_manifests

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            scriv = make_fake_scriv(tmp / "src", "MyBook", SAMPLE_BOOK)
            pre = compute_manifest(scriv)

            zip_path = zip_scriv_package(scriv, tmp / "MyBook.bak.zip")
            extract = tmp / "extract"
            extract.mkdir()
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract)

            restored = next(p for p in extract.rglob("*.scriv") if p.is_dir())
            post = compute_manifest(restored)

            diff = compare_manifests(pre, post)
            self.assertTrue(
                diff["ok"],
                f"zip round-trip should preserve content: {diff}",
            )


class DiscoveryTests(unittest.TestCase):
    def _validator_with(self, local_dir: Path) -> Validator:
        with tempfile.TemporaryDirectory() as run_tmp:
            run_dir = Path(run_tmp) / "run"
            (run_dir / "logs").mkdir(parents=True)
            log = logging.getLogger("test")
            return Validator(
                local_dir=local_dir,
                backup_dir=local_dir,  # not exercised here
                run_dir=run_dir,
                log=log,
                screenshots=False,
                dry_run=True,
            )

    def test_discovers_all_scriv_dirs_with_mode_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            make_fake_scriv(local, "BookOne", SAMPLE_BOOK)
            make_fake_scriv(local, "BookTwo", SAMPLE_BOOK)
            make_fake_scriv(local, "Third Book", SAMPLE_BOOK)
            # Distractor: a non-.scriv file
            (local / "notes.txt").write_text("hi")

            v = self._validator_with(local)
            books = v.discover_books(mode="all")
            names = sorted(b.name for b in books)
            self.assertEqual(names, ["BookOne", "BookTwo", "Third Book"])

    def test_default_mode_returns_only_latest_by_mtime(self):
        """Default behaviour: with multiple books, only the most-recently
        modified one is returned. This is the optimization the user
        requested — proof of the book they're working on, not all of them."""
        import os
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            old = make_fake_scriv(local, "Old", SAMPLE_BOOK)
            new = make_fake_scriv(local, "New", SAMPLE_BOOK)
            # Force mtimes so 'New' is unambiguously newer
            now = _time.time()
            os.utime(old, (now - 3600, now - 3600))
            os.utime(new, (now, now))

            v = self._validator_with(local)
            books = v.discover_books()  # default mode
            self.assertEqual([b.name for b in books], ["New"])

    def test_default_mode_with_single_book_returns_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            make_fake_scriv(local, "OnlyOne", SAMPLE_BOOK)
            v = self._validator_with(local)
            self.assertEqual(
                [b.name for b in v.discover_books()], ["OnlyOne"],
            )

    def test_discover_filtered_by_name_overrides_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            make_fake_scriv(local, "BookOne", SAMPLE_BOOK)
            make_fake_scriv(local, "BookTwo", SAMPLE_BOOK)
            v = self._validator_with(local)
            # `only` should take precedence over the default 'latest' mode
            self.assertEqual(
                [b.name for b in v.discover_books(only="BookOne")],
                ["BookOne"],
            )

    def test_discover_filter_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            make_fake_scriv(local, "BookOne", SAMPLE_BOOK)
            v = self._validator_with(local)
            books = v.discover_books(only="bookone")
            self.assertEqual([b.name for b in books], ["BookOne"])

    def test_missing_local_folder_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self._validator_with(Path(tmp) / "nope")
            with self.assertRaises(FileNotFoundError):
                v.discover_books()


if __name__ == "__main__":
    unittest.main()
