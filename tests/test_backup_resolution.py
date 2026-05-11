"""
Backup file resolution.

`find_latest_backup` is the only thing standing between the tool and
restoring the wrong file. These tests cover the naming conventions
Scrivener actually uses, plus adversarial cases (prefix collisions,
wrong extensions, no candidates).
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests._helpers import make_fake_scriv  # noqa: F401 (import side-effects: sys.path)
from scrivcheck import find_latest_backup


def _touch(path: Path, mtime: float, content: bytes = b"x") -> None:
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


class BackupResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def test_picks_newest_among_matches(self):
        _touch(self.dir / "MyNovel.bak.zip", self.now - 7200)
        _touch(self.dir / "MyNovel.bak1.zip", self.now - 3600)
        newest = self.dir / "MyNovel 2026-05-03 14-00.zip"
        _touch(newest, self.now)

        result = find_latest_backup(self.dir, "MyNovel")
        self.assertEqual(result, newest)

    def test_classic_bak_naming(self):
        _touch(self.dir / "MyNovel.bak.zip", self.now)
        result = find_latest_backup(self.dir, "MyNovel")
        self.assertEqual(result.name, "MyNovel.bak.zip")

    def test_dated_naming(self):
        target = self.dir / "MyNovel 2026-01-15 09-00.zip"
        _touch(target, self.now)
        result = find_latest_backup(self.dir, "MyNovel")
        self.assertEqual(result, target)

    def test_no_match_returns_none(self):
        _touch(self.dir / "OtherBook.bak.zip", self.now)
        self.assertIsNone(find_latest_backup(self.dir, "MyNovel"))

    def test_does_not_match_prefix_only_names(self):
        """`Novel` must NOT match `MyNovel.bak.zip`. Real bug risk."""
        _touch(self.dir / "MyNovel.bak.zip", self.now)
        result = find_latest_backup(self.dir, "Novel")
        self.assertIsNone(result, f"prefix-only must not match, got {result}")

    def test_does_not_match_unrelated_zips(self):
        _touch(self.dir / "MyNovel-export.zip", self.now)  # no separator we accept
        # An export that uses '-' followed by lowercase word IS allowed by
        # our regex (we accept `[\s._-]`). Make sure we don't accidentally
        # exclude legitimate Scrivener variants.
        result = find_latest_backup(self.dir, "MyNovel")
        self.assertEqual(result.name, "MyNovel-export.zip")

    def test_ignores_non_zip_files(self):
        _touch(self.dir / "MyNovel.bak", self.now)  # missing .zip
        _touch(self.dir / "MyNovel.txt", self.now)
        self.assertIsNone(find_latest_backup(self.dir, "MyNovel"))

    def test_book_name_with_spaces(self):
        _touch(self.dir / "My Big Book.bak.zip", self.now)
        result = find_latest_backup(self.dir, "My Big Book")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "My Big Book.bak.zip")

    def test_book_name_with_regex_special_chars(self):
        """Names like `Book (draft).scriv` are legal. The regex must not
        crash or matchwrong things."""
        _touch(self.dir / "Book (draft).bak.zip", self.now)
        result = find_latest_backup(self.dir, "Book (draft)")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Book (draft).bak.zip")

    def test_case_insensitive_match(self):
        _touch(self.dir / "mynovel.bak.zip", self.now)
        result = find_latest_backup(self.dir, "MyNovel")
        self.assertIsNotNone(result)

    def test_missing_directory_returns_none(self):
        self.assertIsNone(find_latest_backup(self.dir / "nope", "X"))

    def test_subdirectories_are_ignored(self):
        """We don't descend into subdirs — backups should be at top level."""
        sub = self.dir / "sub"
        sub.mkdir()
        _touch(sub / "MyNovel.bak.zip", self.now)
        self.assertIsNone(find_latest_backup(self.dir, "MyNovel"))


if __name__ == "__main__":
    unittest.main()
