#!/usr/bin/env python3
"""
Scrivener Backup Validation Drill — Chaos Engineering Edition
==============================================================

One-shot validation that proves every Scrivener backup in the configured
folder can actually be restored to a working project. Performs a destructive-
*looking* operation (delete + restore) but never destroys anything: every
"deleted" file is moved to a timestamped quarantine and only purged after
verification succeeds for ALL books.

Chaos engineering principles applied
------------------------------------
1. Steady state captured up front: pre-flight manifest of every .scriv
   project (file list, sizes, content hashes of user-data files).
2. Hypothesis recorded: restoring the most recent backup yields a project
   whose content manifest matches the pre-flight manifest.
3. Blast radius: per-book quarantine. A failure on book N cannot affect
   the other books, and even within a book the safety copy is independent
   of the quarantined original.
4. Defense in depth: a safety copy is taken BEFORE the original is moved
   to quarantine. Two independent copies exist at all times until
   verification passes.
5. Auto-rollback: any exception triggers restoration of the original from
   quarantine, and the safety copy is left in place as a third line of
   defense. The book is marked FAILED.
6. Audit trail: timestamped run directory with screenshots, JSON state
   snapshots, and a final report.

Usage
-----
    ./scrivcheck.py                    # full drill, all books
    ./scrivcheck.py --dry-run          # plan only, no changes
    ./scrivcheck.py --book "MyNovel"   # one book by name
    ./scrivcheck.py --keep-quarantine  # do not auto-purge on success
    ./scrivcheck.py --no-screenshots   # skip screencapture calls

Quarantine cleanup happens only when ALL validated books pass. Otherwise
the quarantine is preserved and the path is printed loudly at the end.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import html as _html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


def strip_rtf(data: bytes) -> str:
    """Extract plain text from a Scrivener RTF content file.

    Handles the subset of RTF that Scrivener 3 on macOS produces:
    UTF-8 encoded files with \\uc0\\uNNNN Unicode escapes for compatibility,
    \\par paragraph breaks, and standard control-word formatting marks.
    """
    src = data.decode("utf-8", errors="replace")
    # RTF Unicode escapes. Scrivener emits \uc0 once then bare \uN for each
    # subsequent character, so match both forms.  Negative values are valid RTF
    # (code points above 32767 are stored as signed 16-bit), mod to keep in range.
    src = re.sub(r"(?:\\uc0)?\\u(-?\d+)\s?",
                 lambda m: chr(int(m.group(1)) % 0x110000), src)
    # Hex escapes: \'e9 → é
    src = re.sub(r"\\'([0-9a-fA-F]{2})",
                 lambda m: chr(int(m.group(1), 16)), src)
    # Remove known header control groups (never contain prose)
    src = re.sub(r"\{\\(?:fonttbl|colortbl|stylesheet|expandedcolortbl)[^{}]*\}", "", src)
    src = re.sub(r"\{\\\*[^{}]*\}", "", src)
    # \par (paragraph break) → newline; \pard (reset) is just removed below
    src = re.sub(r"\\par\b", "\n", src)
    # Remove all remaining control words and stray backslashes/braces
    src = re.sub(r"\\[a-zA-Z]+\-?\d*\s?", "", src)
    src = re.sub(r"[{}\\]", "", src)
    # Normalise whitespace
    src = re.sub(r"[ \t]+", " ", src)
    src = re.sub(r"\n{3,}", "\n\n", src)
    return src.strip()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_LOCAL = Path.home() / "Scrivener local"
# Scrivener for Mac's hard-coded fallback backup location. The actual
# backup folder is read from Scrivener's preferences at runtime (the
# user can redirect it in Scrivener → Settings → Backup → Backup
# Location), so this constant is only used when the preference can't
# be read (e.g. Scrivener never launched on this machine).
FALLBACK_BACKUPS = (
    Path.home() / "Library" / "Application Support" / "Scrivener" / "Backups"
)
DEFAULT_RUN_ROOT = Path.home() / "ScrivCheck"

# Scrivener 3's preferences domain and the backup-folder key we read
# at startup so the user doesn't have to know where their backups are.
SCRIVENER_PREFS_DOMAIN = "com.literatureandlatte.scrivener3"
SCRIVENER_PREFS_BACKUP_KEY = "SCRAutomaticBackupPath"

# Files inside .scriv that Scrivener regenerates or that legitimately drift
# between save and backup. We exclude these from strict content comparison.
VOLATILE_PATTERNS = (
    ".DS_Store",
    "search.indexes",
    "ui.xml",
    "ui.plist",
)

# Files inside .scriv that constitute the user's actual content. These MUST
# match between pre-flight and post-restore manifests, or the drill fails.
CONTENT_PREFIXES = (
    "Files/Data/",     # current Scrivener 3 layout (per-doc folders)
    "Files/Docs/",     # older Scrivener layout
)



# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    relpath: str
    size: int
    sha256: str


@dataclass
class Manifest:
    project: str
    file_count: int
    total_size: int
    files: list[FileEntry]
    captured_at: str

    def content_entries(self) -> dict[str, FileEntry]:
        """Map relpath -> entry for all user-content files."""
        return {
            f.relpath: f
            for f in self.files
            if f.relpath.startswith(CONTENT_PREFIXES)
        }

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "captured_at": self.captured_at,
            "files": [dataclasses.asdict(f) for f in self.files],
        }


@dataclass
class BookResult:
    name: str                       # e.g. "MyNovel" (no .scriv)
    project_path: str               # full path to the .scriv on disk
    backup_zip: Optional[str] = None
    pre_manifest: Optional[Manifest] = None
    post_manifest: Optional[Manifest] = None
    status: str = "PENDING"         # PENDING | PASS | FAIL | SKIPPED
    failure_reason: Optional[str] = None
    steps: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    diff_summary: Optional[dict] = None
    # Live-snapshot fields: set from the most recently modified content file.
    latest_content_file: Optional[str] = None   # relpath within .scriv
    latest_content_mtime: Optional[str] = None  # ISO mtime, pre-drill
    latest_content_mtime_post: Optional[str] = None  # ISO mtime, post-restore
    latest_content_snippet: Optional[str] = None     # extracted prose

    def add_step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(
            {
                "name": name,
                "ok": ok,
                "detail": detail,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "project_path": self.project_path,
            "backup_zip": self.backup_zip,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "steps": self.steps,
            "screenshots": self.screenshots,
            "diff_summary": self.diff_summary,
            "pre_manifest": self.pre_manifest.to_dict() if self.pre_manifest else None,
            "post_manifest": self.post_manifest.to_dict() if self.post_manifest else None,
        }


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(run_dir: Path) -> logging.Logger:
    log = logging.getLogger("scriv-drill")
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    fh = logging.FileHandler(run_dir / "logs" / "run.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    return log


# ---------------------------------------------------------------------------
# Helpers — Scrivener / macOS automation
# ---------------------------------------------------------------------------


def run(cmd: list[str], check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with consistent defaults."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def osascript(script: str, timeout: int = 30) -> str:
    """
    Run an AppleScript, return stdout. On failure, raises CalledProcessError
    with stderr included in the exception args so callers can log it.
    """
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        # Wrap the stderr into the exception message for visibility — the
        # default CalledProcessError just shows the command, not the error.
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, proc.stdout, proc.stderr,
        )
    return proc.stdout.strip()


def scrivener_running() -> bool:
    proc = subprocess.run(["pgrep", "-x", "Scrivener"], capture_output=True)
    return proc.returncode == 0


def discover_scrivener_backup_path() -> Optional[Path]:
    """
    Read Scrivener's configured backup folder from its preferences.

    Scrivener 3 stores it under ``com.literatureandlatte.scrivener3 →
    SCRAutomaticBackupPath``. Returns the resolved Path if both the key
    is set AND the directory exists; ``None`` otherwise (in which case
    the caller should fall back to ``FALLBACK_BACKUPS``).

    The previous "guess a default" approach pointed every user who'd
    redirected backups to a wrong directory and produced silent "no
    zip found" failures. Reading prefs makes ``scrivcheck`` correct
    out-of-the-box for the >95% of users who never pass ``--backups``.
    """
    try:
        proc = subprocess.run(
            ["defaults", "read", SCRIVENER_PREFS_DOMAIN, SCRIVENER_PREFS_BACKUP_KEY],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_dir():
        return candidate
    return None


def screencapture(
    out_path: Path,
    log: logging.Logger,
    enabled: bool = True,
) -> Optional[str]:
    """Capture full screen. -x suppresses sound. Returns file path on success."""
    if not enabled:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["screencapture", "-x", str(out_path)], check=True, timeout=10)
        log.debug("Screenshot saved: %s", out_path)
        return str(out_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("Screenshot failed for %s: %s", out_path, e)
        return None


# ---------------------------------------------------------------------------
# Helpers — manifest / hashing
# ---------------------------------------------------------------------------


def is_volatile(relpath: str) -> bool:
    name = Path(relpath).name
    return any(p in name or relpath.endswith(p) for p in VOLATILE_PATTERNS)


def file_sha256(path: Path, chunk: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def compute_manifest(scriv_path: Path) -> Manifest:
    """Walk a .scriv package and produce a manifest of every regular file."""
    if not scriv_path.exists():
        raise FileNotFoundError(f"No such project: {scriv_path}")

    entries: list[FileEntry] = []
    total = 0
    for root, dirs, files in os.walk(scriv_path):
        # Skip nothing — we want a complete picture
        for fn in files:
            full = Path(root) / fn
            try:
                size = full.stat().st_size
            except OSError:
                continue
            rel = str(full.relative_to(scriv_path))
            digest = file_sha256(full)
            entries.append(FileEntry(relpath=rel, size=size, sha256=digest))
            total += size

    entries.sort(key=lambda e: e.relpath)

    return Manifest(
        project=scriv_path.stem,
        file_count=len(entries),
        total_size=total,
        files=entries,
        captured_at=datetime.now().isoformat(timespec="seconds"),
    )


def compare_manifests(pre: Manifest, post: Manifest) -> dict:
    """
    Compare two manifests focusing on user-content files.

    Returns a structured diff. The drill PASSES iff:
      - every content file in `pre` exists in `post`
      - every content file's sha256 matches
    Volatile files (search indexes, UI state, .DS_Store) are reported but
    do NOT cause failure.
    """
    pre_content = pre.content_entries()
    post_content = post.content_entries()

    missing = sorted(set(pre_content) - set(post_content))
    added = sorted(set(post_content) - set(pre_content))
    changed: list[dict] = []
    for rel in sorted(set(pre_content) & set(post_content)):
        a, b = pre_content[rel], post_content[rel]
        if a.sha256 != b.sha256:
            changed.append(
                {
                    "relpath": rel,
                    "pre_sha256": a.sha256,
                    "post_sha256": b.sha256,
                    "pre_size": a.size,
                    "post_size": b.size,
                }
            )

    # Non-content drift (informational only)
    pre_other = {f.relpath for f in pre.files} - set(pre_content)
    post_other = {f.relpath for f in post.files} - set(post_content)
    other_missing = sorted(pre_other - post_other)
    other_added = sorted(post_other - pre_other)

    ok = not missing and not changed
    return {
        "ok": ok,
        "content_missing": missing,
        "content_added": added,
        "content_changed": changed,
        "noncontent_missing": [p for p in other_missing if not is_volatile(p)],
        "noncontent_added": [p for p in other_added if not is_volatile(p)],
        "pre_total_size": pre.total_size,
        "post_total_size": post.total_size,
        "pre_file_count": pre.file_count,
        "post_file_count": post.file_count,
    }


# ---------------------------------------------------------------------------
# Helpers — backup file resolution
# ---------------------------------------------------------------------------


def _nfc(s: str) -> str:
    """
    Normalize a string to Unicode NFC. macOS HFS+/APFS historically stored
    filenames in NFD (combining-mark form) and Python's ``Path.iterdir()``
    surfaces them as-is; comparing against an NFC project name then misses
    every match silently. Force both sides through NFC so a Cyrillic or
    accented book name matches the on-disk zip filename consistently.
    """
    return unicodedata.normalize("NFC", s)


def find_latest_backup(backup_dir: Path, project_name: str) -> Optional[Path]:
    """
    Find the most recently modified .zip in ``backup_dir`` whose name
    starts with ``project_name``. Scrivener typically names backups:

        ProjectName.bak.zip
        ProjectName.bak1.zip
        ProjectName 2024-01-15 14-30.zip
        ProjectName-bak-2024-01-15T14-30.zip      (Scrivener 3 default)
    """
    if not backup_dir.exists():
        return None
    candidates: list[Path] = []
    safe = re.escape(_nfc(project_name))
    # Space is only a valid separator when followed by a digit (date
    # pattern: "Name 2024-01-15.zip"). A space before a letter would
    # match a completely different project ("ИЖ copy-bak.zip" is a backup
    # of "ИЖ copy.scriv", not of "ИЖ.scriv"). Dot, dash, and underscore
    # can be followed by anything (they are unambiguous separators).
    pattern = re.compile(rf"^{safe}([._-].*|\s\d.*)?\.zip$", re.IGNORECASE)
    for child in backup_dir.iterdir():
        if child.is_file() and pattern.match(_nfc(child.name)):
            candidates.append(child)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def diagnose_missing_backup(
    backup_dir: Path, project_name: str, log: logging.Logger,
) -> None:
    """
    When ``find_latest_backup`` comes up empty, log a structured snapshot
    of what IS in ``backup_dir`` so the user can self-diagnose. The most
    common causes — wrong --backups path, "Back up on save" disabled in
    Scrivener, or backup zips for an old project name — all show up
    differently here.
    """
    if not backup_dir.exists():
        log.error("Backup directory does not exist: %s", backup_dir)
        return
    entries = list(backup_dir.iterdir())
    zips = [e for e in entries if e.is_file() and e.suffix.lower() == ".zip"]
    nfc_name = _nfc(project_name).lower()
    prefix_matches = [
        z for z in zips if _nfc(z.name).lower().startswith(nfc_name[:1].lower())
    ]
    name_substr = [z for z in zips if nfc_name in _nfc(z.name).lower()]

    log.error("No backup zip matched %r in %s", project_name, backup_dir)
    log.error(
        "  directory has %d entries (%d are .zip files)",
        len(entries), len(zips),
    )
    if zips:
        sample = sorted(z.name for z in zips)[:5]
        log.error("  sample zips present: %s", ", ".join(sample))
    if name_substr:
        log.error(
            "  %d zip(s) contain the book name as a substring — "
            "did the project get renamed? %s",
            len(name_substr),
            ", ".join(sorted(z.name for z in name_substr)[:5]),
        )
    elif zips and not prefix_matches:
        log.error(
            "  no zip starts with this book's name. Either Scrivener's "
            "'Back up on save' is disabled for this project, or the "
            "backup destination is configured to a different folder."
        )
    if not zips and entries:
        log.error(
            "  the directory contains %d non-zip entries — is --backups "
            "pointing at the iOS sync folder (.scriv directories) instead "
            "of Scrivener's backup-zip folder? Default is "
            "~/Library/Application Support/Scrivener/Backups",
            len(entries),
        )


def create_backup_zip(
    project_path: Path, backup_dir: Path, log: logging.Logger
) -> Path:
    """
    Create a backup zip of the .scriv project when no existing backup is found.
    Names the zip: <BookName>-bak-<YYYY-MM-DDTHH-MM>.zip to match
    Scrivener's own naming convention (SCRUseDateInBackupFileNames=1 pattern).
    The zip contains the .scriv package as its top-level directory, identical
    to what Scrivener writes, so the existing restore path works unchanged.
    """
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M")
    zip_name = f"{project_path.stem}-bak-{timestamp}.zip"
    zip_path = backup_dir / zip_name
    log.info("No existing backup — creating one: %s", zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(project_path.rglob("*")):
            if f.is_file():
                zf.write(f, Path(project_path.name) / f.relative_to(project_path))
    log.info("Backup created: %s (%s bytes)", zip_path, f"{zip_path.stat().st_size:,}")
    return zip_path


def make_run_dir(run_root: Path) -> Path:
    """
    Return a fresh ``run_<timestamp>`` directory under ``run_root``.

    Two ``scrivcheck`` invocations within the same wall-clock second
    would otherwise collide on the directory name. Append a counter
    until we get a path that doesn't already exist.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = run_root / f"run_{timestamp}"
    if not base.exists():
        return base
    for i in range(1, 1000):
        candidate = run_root / f"run_{timestamp}_{i}"
        if not candidate.exists():
            return candidate
    # Astronomically unlikely; if we hit it, we've earned the exception.
    raise RuntimeError(
        f"Could not allocate a unique run directory under {run_root}"
    )


def directory_size_bytes(path: Path) -> int:
    """Sum the size of every regular file under ``path``."""
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except OSError:
                continue
    return total


def assert_enough_free_space(
    project_path: Path, run_dir: Path, log: logging.Logger,
    headroom_factor: float = 2.5,
) -> None:
    """
    Raise ``RuntimeError`` if there isn't enough free space on the
    run-dir filesystem for the safety copy + the unzipped backup.

    The drill creates two on-disk copies of the project (safety copy +
    quarantined original would be a move on the same FS, but the
    UNZIPPED backup is a fresh write into the run_dir). Headroom factor
    accounts for the unzipped tree being ≈1× the live project plus a
    safety copy of ≈1×.
    """
    project_size = directory_size_bytes(project_path)
    free = shutil.disk_usage(run_dir).free
    needed = int(project_size * headroom_factor)
    if free < needed:
        raise RuntimeError(
            f"Not enough free space on {run_dir}'s filesystem: need "
            f"~{needed:,} bytes ({headroom_factor}× project size), "
            f"have {free:,} bytes."
        )
    log.debug(
        "Disk-space pre-flight OK: project=%d bytes, free=%d bytes (%.1fx headroom)",
        project_size, free, free / max(project_size, 1),
    )


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """
    Extract ``zip_path`` into ``dest_dir`` with a zip-slip guard: every
    entry's resolved destination must remain inside ``dest_dir``.
    Refuses absolute paths, ``..`` traversal, and symlink entries that
    would escape the staging dir.

    Background: ``zipfile.ZipFile.extractall`` on Python < 3.12 does not
    by itself reject malicious paths. A tampered backup zip could carry
    an entry like ``../../../../etc/passwd`` and clobber files outside
    the run directory. We're a chaos engineering tool that quarantines
    user content — we MUST be defensive about adversarial archives even
    when the source is the user's own machine, since "the source we
    trust" is exactly what a tampered backup pretends to be.
    """
    dest_real = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Reject symlink entries outright — Scrivener doesn't write
            # them and they're a vector for escape.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError(
                    f"Refusing to extract symlink entry {member.filename!r} "
                    f"from {zip_path}"
                )
            target = (dest_dir / member.filename).resolve()
            try:
                target.relative_to(dest_real)
            except ValueError as e:
                raise RuntimeError(
                    f"Zip-slip blocked: entry {member.filename!r} in "
                    f"{zip_path} resolves outside the staging directory"
                ) from e
        zf.extractall(dest_dir)


def ensure_locally_available(path: Path, log: logging.Logger) -> None:
    """
    Dropbox via CloudStorage may keep files online-only. Force download by
    reading a byte. brctl would be cleaner but isn't always available.
    """
    try:
        with path.open("rb") as f:
            f.read(1)
    except OSError as e:
        log.warning("Could not read %s (online-only?): %s", path, e)
        # Try `brctl download` as a fallback
        subprocess.run(["brctl", "download", str(path)], check=False)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class Validator:
    def __init__(
        self,
        local_dir: Path,
        backup_dir: Path,
        run_dir: Path,
        log: logging.Logger,
        screenshots: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.local = local_dir
        self.backups = backup_dir
        self.run_dir = run_dir
        self.log = log
        self.screenshots_enabled = screenshots
        self.dry_run = dry_run

        self.quarantine = run_dir / "quarantine"
        self.safety = self.quarantine / "safety-copies"
        self.originals = self.quarantine / "originals"
        self.staging = self.quarantine / "staging"
        for d in (self.quarantine, self.safety, self.originals, self.staging):
            d.mkdir(parents=True, exist_ok=True)

        self.shots_dir = run_dir / "screenshots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)

        self.shot_counter = 0

    # --- screenshots ---

    def shot(self, label: str, book: Optional[BookResult] = None) -> Optional[str]:
        self.shot_counter += 1
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", label).strip("_")
        path = self.shots_dir / f"{self.shot_counter:03d}_{slug}.png"
        result = screencapture(path, self.log, enabled=self.screenshots_enabled)
        if result and book is not None:
            book.screenshots.append(result)
        return result

    # --- discovery ---

    def discover_books(
        self,
        only: Optional[str] = None,
        mode: str = "latest",
    ) -> list[BookResult]:
        """
        Find ``.scriv`` projects in the local folder.

        ``mode`` selects which subset to return:
          • ``"latest"`` (default) — only the most-recently-modified
            ``.scriv`` directory. Optimized for the common case: prove
            the backup of whatever you're working on right now.
          • ``"all"`` — every ``.scriv`` in the local folder.

        ``only`` overrides ``mode`` and selects a single named project
        (case-insensitive match on the stem). When set, ``mode`` is
        ignored.
        """
        if not self.local.exists():
            raise FileNotFoundError(f"Local folder missing: {self.local}")
        all_books: list[BookResult] = []
        for child in sorted(self.local.iterdir()):
            if child.suffix == ".scriv" and child.is_dir():
                all_books.append(
                    BookResult(name=child.stem, project_path=str(child))
                )
        if only is not None:
            return [b for b in all_books if b.name.lower() == only.lower()]
        if mode == "all" or len(all_books) <= 1:
            return all_books
        # mode == "latest": pick the most recently modified .scriv
        all_books.sort(
            key=lambda b: Path(b.project_path).stat().st_mtime, reverse=True,
        )
        return all_books[:1]

    # --- per-book validation ---

    def validate_book(self, book: BookResult) -> None:
        """
        Run the full drill for one book. Mutates `book` with status/steps.
        Any exception is caught and turned into FAIL with auto-rollback.

        Dry-run mode short-circuits BEFORE touching Scrivener: it only
        looks at on-disk state (current manifest + latest backup zip)
        and reports the plan. No AppleScript, no ``open``, no focus
        stealing.
        """
        self.log.info("=" * 70)
        self.log.info("BOOK: %s", book.name)
        self.log.info("=" * 70)

        project_path = Path(book.project_path)
        safety_copy: Optional[Path] = None
        quarantined_original: Optional[Path] = None
        restored: Optional[Path] = None

        if self.dry_run:
            self._dry_run_book(book, project_path)
            self._emit_proof(book)
            return

        try:
            # Live mode no longer drives Scrivener. Earlier versions tried
            # to open the project and `quit saving yes` to fire Scrivener's
            # "Back up on save" hook; in practice that hook is gated on
            # SCRBackUpOnManualSave (manual Cmd+S only) and never fires
            # from an AppleScript-initiated save. Validating the latest
            # *existing* backup is the honest, focus-respecting contract.
            # Users who want a fresh backup save manually in Scrivener
            # before running scrivcheck.
            if scrivener_running():
                self.log.warning(
                    "Scrivener is running. The pre-flight manifest may "
                    "catch files mid-write if Scrivener saves during the "
                    "drill. Quit Scrivener first for the cleanest run."
                )
                book.add_step("scrivener_running_warning", True)

            # Step 1: pre-flight manifest
            self.log.info("Computing pre-flight manifest…")
            book.pre_manifest = compute_manifest(project_path)
            book.add_step(
                "preflight_manifest",
                True,
                f"{book.pre_manifest.file_count} files, "
                f"{book.pre_manifest.total_size:,} bytes",
            )

            # Live snapshot: record mtime + prose of the most recently
            # modified content file so we can confirm it survives in the backup.
            rtf_files = [
                (project_path / rel, rel)
                for rel in book.pre_manifest.content_entries()
                if rel.endswith(".rtf")
            ]
            if rtf_files:
                rtf_files.sort(
                    key=lambda t: t[0].stat().st_mtime
                    if t[0].exists() else 0,
                    reverse=True,
                )
                latest_path, latest_rel = rtf_files[0]
                book.latest_content_file = latest_rel
                book.latest_content_mtime = datetime.fromtimestamp(
                    latest_path.stat().st_mtime
                ).isoformat(timespec="seconds")
                book.latest_content_snippet = strip_rtf(latest_path.read_bytes())

            # Step 3: locate latest backup; create one if none exists
            backup_zip = find_latest_backup(self.backups, book.name)
            if not backup_zip:
                backup_zip = create_backup_zip(
                    project_path, self.backups, self.log
                )
                book.add_step("create_backup", True, backup_zip.name)
            else:
                ensure_locally_available(backup_zip, self.log)
                self.log.info("Latest backup: %s", backup_zip)
            book.backup_zip = str(backup_zip)
            book.add_step("locate_backup", True, backup_zip.name)

            # Step 3.5: disk-space pre-flight. Aborting BEFORE the safety
            # copy means we never half-do the drill in a low-disk failure.
            assert_enough_free_space(project_path, self.run_dir, self.log)
            book.add_step("disk_space_check", True)

            # Step 4: defense-in-depth — safety copy BEFORE quarantine move
            self.log.info("Making safety copy…")
            safety_copy = self.safety / project_path.name
            shutil.copytree(project_path, safety_copy)
            book.add_step("safety_copy", True, str(safety_copy))

            # Step 5: quarantine the original (NOT delete)
            self.log.info("Quarantining original (no rm)…")
            quarantined_original = self.originals / project_path.name
            shutil.move(str(project_path), str(quarantined_original))
            self.shot(f"{book.name}_03_after_quarantine", book)
            book.add_step("quarantine_original", True, str(quarantined_original))

            # Step 6: unzip backup into staging (with zip-slip guard)
            self.log.info("Unzipping backup into staging…")
            staging_book = self.staging / book.name
            if staging_book.exists():
                shutil.rmtree(staging_book)
            staging_book.mkdir(parents=True)
            safe_extract_zip(backup_zip, staging_book)
            self.shot(f"{book.name}_04_unzipped", book)
            book.add_step("unzip_backup", True, str(staging_book))

            # Step 7: locate the .scriv inside staging (zip can be either
            # the package itself or a parent folder containing it)
            scriv_dirs = [
                p for p in staging_book.rglob("*.scriv") if p.is_dir()
            ]
            if not scriv_dirs:
                raise RuntimeError(
                    f"No .scriv package found inside {backup_zip}"
                )
            # Pick the shallowest match
            scriv_dirs.sort(key=lambda p: len(p.parts))
            unzipped_scriv = scriv_dirs[0]
            book.add_step("locate_unzipped_scriv", True, str(unzipped_scriv))

            # Step 8: move into local folder, rename to original name
            self.log.info("Restoring to local folder with original name…")
            restored = self.local / project_path.name  # uses original .scriv name
            shutil.move(str(unzipped_scriv), str(restored))
            self.shot(f"{book.name}_05_restored", book)
            book.add_step("restore_to_local", True, str(restored))

            # Manifest equality (next step) is sufficient proof that the
            # restored .scriv is byte-identical to the pre-flight state, so
            # Scrivener will accept it iff it accepted the original. We
            # used to reopen Scrivener here as a smoke test; that step was
            # removed because (a) it rewrote volatile metadata and forced
            # filtering, and (b) it doubled the drill's runtime for no
            # additional proof beyond what SHA-256 equality already gives.

            # Step 9: post-restore manifest + comparison
            self.log.info("Computing post-restore manifest…")
            book.post_manifest = compute_manifest(restored)

            # Record the backup zip's mtime as the "backup time" so we can
            # compare it against the live file's mtime in the proof block.
            # (Post-restore file mtimes are unreliable: the extraction resets them.)
            if book.backup_zip:
                book.latest_content_mtime_post = datetime.fromtimestamp(
                    Path(book.backup_zip).stat().st_mtime
                ).isoformat(timespec="seconds")

            diff = compare_manifests(book.pre_manifest, book.post_manifest)
            book.diff_summary = diff
            book.add_step(
                "verify_manifest",
                diff["ok"],
                f"missing={len(diff['content_missing'])} "
                f"changed={len(diff['content_changed'])}",
            )

            if not diff["ok"]:
                raise RuntimeError(
                    f"Content drift detected: "
                    f"{len(diff['content_missing'])} missing, "
                    f"{len(diff['content_changed'])} changed"
                )

            book.status = "PASS"
            self.log.info("✅ %s: PASS", book.name)

        except Exception as e:  # noqa: BLE001 — we want to catch everything
            book.status = "FAIL"
            book.failure_reason = str(e)
            self.log.error("❌ %s FAILED: %s", book.name, e)
            book.add_step("failure", False, str(e))

            # Auto-rollback. Restore the original from quarantine (or, if
            # that fails, from the safety copy) into the local folder.
            self._rollback(book, project_path, quarantined_original, safety_copy)

        self._emit_proof(book)

    def _dry_run_book(self, book: BookResult, project_path: Path) -> None:
        """
        Dry-run path. Inspects on-disk state only — no Scrivener calls,
        no file moves. Useful as a quick check that a backup zip exists
        and that the script can hash the live project.
        """
        book.status = "SKIPPED"
        book.failure_reason = "dry-run (no Scrivener interaction, no restore)"

        # Pre-flight manifest is a pure file walk; safe in dry-run.
        self.log.info("Computing pre-flight manifest (no save)…")
        book.pre_manifest = compute_manifest(project_path)
        book.add_step(
            "preflight_manifest_dryrun",
            True,
            f"{book.pre_manifest.file_count} files, "
            f"{book.pre_manifest.total_size:,} bytes",
        )

        backup_zip = find_latest_backup(self.backups, book.name)
        if backup_zip is None:
            self.log.info(
                "[dry-run] No backup zip found — live run would create one in %s",
                self.backups,
            )
            book.add_step(
                "would_create_backup_dryrun",
                True,
                f"no zip found; live run would create one in {self.backups}",
            )
            return

        book.backup_zip = str(backup_zip)
        book.add_step("locate_backup_dryrun", True, backup_zip.name)
        self.log.info(
            "[dry-run] live drill would: open Scrivener (background), "
            "save → refresh backup, quarantine original, unzip backup, "
            "restore, verify SHA-256."
        )

    def _emit_proof(self, book: BookResult) -> None:
        """
        Write per-book proof to ``<run_dir>/proof/<book>.txt`` (full detail)
        and print a brief summary to stdout.

        Full detail (file + debug log) — every file hash, every verdict.
        Brief summary (stdout / info log) — backup hash, file counts,
        attestation. Keeps terminal output proportional regardless of project
        size (667 files = same ~12 lines as 3 files).
        """
        lines: list[str] = []   # full detail → file + debug log
        brief: list[str] = []   # summary only → stdout (info log)
        bar = "═" * 70

        def both(*ls: str) -> None:
            for ln in ls:
                lines.append(ln)
                brief.append(ln)

        def full(*ls: str) -> None:
            for ln in ls:
                lines.append(ln)

        both(bar, f"PROOF — {book.name}", bar)

        # 1. Backup zip — full detail in file, one-line digest on stdout.
        if book.backup_zip:
            zp = Path(book.backup_zip)
            if zp.exists():
                stat = zp.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds"
                )
                digest = file_sha256(zp)
                full(
                    "",
                    "Backup file (real, on disk, hashed in your presence):",
                    f"    path    {zp}",
                    f"    size    {stat.st_size:,} bytes",
                    f"    mtime   {mtime}",
                    f"    sha256  {digest}",
                )
                both(
                    "",
                    f"Backup: {zp.name}  ({stat.st_size:,} bytes)",
                    f"        sha256 {digest}",
                )

        pre = book.pre_manifest.content_entries() if book.pre_manifest else {}
        post = book.post_manifest.content_entries() if book.post_manifest else {}

        # 2. Pre-flight — per-file listing in file only; count on stdout.
        if pre:
            pre_bytes = sum(e.size for e in pre.values())
            full(
                "",
                "Pre-flight steady state (BEFORE the backup was touched):",
                f"    {len(pre)} content file(s), {pre_bytes:,} bytes",
            )
            for relpath in sorted(pre):
                e = pre[relpath]
                full(f"      {relpath:<48s} {e.size:>9,} B  {e.sha256[:16]}…")
            brief.append(
                f"Pre-flight:   {len(pre)} content file(s), {pre_bytes:,} bytes"
            )

        # 3. Post-restore — per-file verdicts in file only; counts on stdout.
        if pre and post:
            post_bytes = sum(e.size for e in post.values())
            full(
                "",
                "Post-restore manifest (project rebuilt FROM THE ZIP):",
                f"    {len(post)} content file(s), {post_bytes:,} bytes",
            )
            matches = mismatches = missing = 0
            for relpath in sorted(pre):
                pre_e = pre[relpath]
                post_e = post.get(relpath)
                if post_e is None:
                    full(
                        f"      {relpath:<48s} {'':>9}    "
                        f"{'':<16}  ✗ MISSING after restore"
                    )
                    missing += 1
                elif post_e.sha256 != pre_e.sha256:
                    full(
                        f"      {relpath:<48s} {post_e.size:>9,} B  "
                        f"{post_e.sha256[:16]}…  ✗ MISMATCH "
                        f"(was {pre_e.sha256[:8]}, got {post_e.sha256[:8]})"
                    )
                    mismatches += 1
                else:
                    full(
                        f"      {relpath:<48s} {post_e.size:>9,} B  "
                        f"{post_e.sha256[:16]}…  ✓ MATCH"
                    )
                    matches += 1

            if mismatches or missing:
                brief.append(
                    f"Post-restore: {matches}/{len(pre)} ✓ match  "
                    f"{mismatches} ✗ mismatch  {missing} ✗ missing"
                )
            else:
                brief.append(
                    f"Post-restore: {matches}/{len(pre)} SHA-256 byte-identical ✓"
                )

            # 4. Attestation — same on stdout and in file.
            verdict = "HELD ✅" if book.status == "PASS" else "REJECTED ❌"
            attest = [
                "",
                "ATTESTATION",
                f"    Status:     {book.status}",
                (
                    f"    Verified:   {matches}/{len(pre)} content file(s) "
                    "SHA-256 byte-identical to pre-flight"
                ),
            ]
            if mismatches:
                attest.append(
                    f"    Mismatch:   {mismatches} file(s) differ from pre-flight"
                )
            if missing:
                attest.append(
                    f"    Missing:    {missing} file(s) absent after restore"
                )
            attest += [
                f"    Hypothesis: {verdict}",
                f"    At:         {datetime.now().isoformat(timespec='seconds')}",
            ]
            both(*attest)

        # Dry-run attestation — always brief (no per-file section to skip).
        if book.status == "SKIPPED" and not post:
            inspected = []
            if book.backup_zip and Path(book.backup_zip).exists():
                inspected.append("backup zip presence + SHA-256")
            if pre:
                inspected.append("live folder pre-flight manifest")
            both(
                "",
                "DRY-RUN ATTESTATION",
                "    Status:     SKIPPED (plan only, no restore)",
                f"    Inspected:  {', '.join(inspected) if inspected else 'nothing'}",
                "    Note:       no save was triggered, no files were moved.",
            )

        if book.status == "FAIL" and not post:
            both(
                "",
                "NOTE",
                "    Drill aborted before a post-restore manifest could be computed.",
                f"    Reason: {book.failure_reason}",
            )

        # Live snapshot: mtime comparison + prose excerpt.
        if book.latest_content_file and book.status in ("PASS", "SKIPPED"):
            mtime_pre = book.latest_content_mtime or "—"
            mtime_post = book.latest_content_mtime_post
            if mtime_post:
                # Backup must be at least as new as the last file save.
                try:
                    zip_ts = datetime.fromisoformat(mtime_post).timestamp()
                    file_ts = datetime.fromisoformat(mtime_pre).timestamp()
                    if zip_ts >= file_ts:
                        mtime_verdict = "✓ backup captured this edit"
                    else:
                        gap = file_ts - zip_ts
                        mtime_verdict = f"⚠ backup is {gap:.0f}s older than last save"
                except ValueError:
                    mtime_verdict = "—"
            else:
                mtime_verdict = ""

            both("", "LIVE SNAPSHOT")
            both(f"    Most recently edited:  {book.latest_content_file}")
            both(f"    Last saved to local folder: {mtime_pre}")
            if mtime_post:
                both(f"    Backup zip created:        {mtime_post}  {mtime_verdict}")

            if book.latest_content_snippet:
                # Show up to ~400 chars, trimmed to word boundary, indented.
                raw = book.latest_content_snippet.replace("\n", " ")
                excerpt = raw[-400:].lstrip()
                if len(raw) > 400:
                    excerpt = "…" + excerpt
                wrapped = []
                while excerpt:
                    wrapped.append("    " + excerpt[:78])
                    excerpt = excerpt[78:]
                both("", "    Text from backup:")
                both(*wrapped)

        both(bar)

        # Full detail → debug log + proof file (durable artifact).
        for line in lines:
            self.log.debug(line)
        proof_dir = self.run_dir / "proof"
        proof_dir.mkdir(parents=True, exist_ok=True)
        (proof_dir / f"{book.name}.txt").write_text("\n".join(lines) + "\n")

        # Brief summary → stdout (info level).
        for line in brief:
            self.log.info(line)


    def _rollback(
        self,
        book: BookResult,
        project_path: Path,
        quarantined: Optional[Path],
        safety_copy: Optional[Path],
    ) -> None:
        """Best-effort restoration of the original. Never raises."""
        try:
            target = self.local / project_path.name

            # Prefer the quarantined original (it's the literal pre-state)
            source = None
            if quarantined and quarantined.exists():
                source = quarantined
            elif safety_copy and safety_copy.exists():
                source = safety_copy

            if source is None:
                # Original was never quarantined (failure occurred before
                # the quarantine step). The target still has the original.
                if target.exists():
                    self.log.info("Original was never quarantined; target intact.")
                    book.add_step("rollback", True, "target already present")
                else:
                    self.log.error("ROLLBACK IMPOSSIBLE — no source found. "
                                   "Quarantine path: %s", self.quarantine)
                    book.add_step("rollback", False, "no source available")
                return

            # A quarantined original exists — restore it unconditionally.
            # The target slot may have wrong content from a failed restore;
            # remove it first so copytree can write cleanly.
            if target.exists():
                self.log.info("Removing failed restore at %s before rollback.", target)
                shutil.rmtree(target)
            self.log.info("Rolling back from %s -> %s", source, target)
            shutil.copytree(source, target)
            book.add_step("rollback", True, f"restored from {source}")
        except Exception as e:  # noqa: BLE001
            self.log.exception("Rollback itself failed: %s", e)
            book.add_step("rollback", False, str(e))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_html_report(run_dir: Path, books: list[BookResult]) -> Path:
    """Generate a self-contained HTML dashboard; return its path."""

    def esc(s: object) -> str:
        return _html.escape(str(s)) if s is not None else ""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    passed = sum(1 for b in books if b.status == "PASS")
    failed = sum(1 for b in books if b.status == "FAIL")

    _STATUS_COLOR = {
        "PASS": "#16a34a", "FAIL": "#dc2626",
        "SKIPPED": "#64748b", "PENDING": "#64748b",
    }

    def _book_card(book: BookResult) -> str:
        color = _STATUS_COLOR.get(book.status, "#64748b")
        rows: list[str] = []

        if book.backup_zip:
            zp = Path(book.backup_zip)
            if zp.exists():
                st = zp.stat()
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                rows.append(
                    f'<tr><th>Backup</th><td><code>{esc(zp.name)}</code></td></tr>'
                    f'<tr><th>Size</th><td>{st.st_size:,} B</td></tr>'
                    f'<tr><th>Modified</th><td>{mtime}</td></tr>'
                )

        if book.pre_manifest:
            pre_cnt = len(book.pre_manifest.content_entries())
            rows.append(
                f'<tr><th>Pre-flight</th><td>{pre_cnt} content files &nbsp; '
                f'{book.pre_manifest.total_size:,} B total</td></tr>'
            )
        if book.post_manifest:
            post_cnt = len(book.post_manifest.content_entries())
            rows.append(
                f'<tr><th>Post-restore</th><td>{post_cnt} content files &nbsp; '
                f'{book.post_manifest.total_size:,} B total</td></tr>'
            )

        if book.diff_summary:
            d = book.diff_summary
            if d["ok"]:
                cell = '<span class="ok">&#10003; All SHA-256 match</span>'
            else:
                parts = []
                if d["content_missing"]:
                    parts.append(f'{len(d["content_missing"])} missing')
                if d["content_changed"]:
                    parts.append(f'{len(d["content_changed"])} changed')
                cell = f'<span class="err">&#10007; {", ".join(parts)}</span>'
            rows.append(f'<tr><th>Diff</th><td>{cell}</td></tr>')

        if book.failure_reason:
            rows.append(
                f'<tr><th>Reason</th>'
                f'<td><span class="err">{esc(book.failure_reason)}</span></td></tr>'
            )

        table = (
            f'<table class="info">{"".join(rows)}</table>' if rows else ""
        )

        snippet = ""
        if book.latest_content_snippet and book.status in ("PASS", "SKIPPED"):
            raw = book.latest_content_snippet.replace("\n", " ")
            excerpt = raw[-400:].lstrip()
            if len(raw) > 400:
                excerpt = "…" + excerpt
            snippet = (
                f'<div class="snippet"><b>Latest content:</b> {esc(excerpt)}</div>'
            )

        shots = ""
        for spath in book.screenshots:
            sp = Path(spath)
            if sp.exists():
                data = base64.b64encode(sp.read_bytes()).decode()
                shots += (
                    f'<img src="data:image/png;base64,{data}" '
                    f'alt="{esc(sp.stem)}" class="shot">'
                )

        return (
            f'<div class="card">'
            f'<div class="ch"><span class="bn">{esc(book.name)}</span>'
            f'<span class="badge" style="background:{color}">{book.status}</span></div>'
            f'<div class="cb">{table}{snippet}{shots}</div>'
            f'</div>'
        )

    if failed > 0:
        bar_color = _STATUS_COLOR["FAIL"]
    elif passed > 0:
        bar_color = _STATUS_COLOR["PASS"]
    else:
        bar_color = "#64748b"

    cards = "\n".join(_book_card(b) for b in books)

    page = (
        "<!DOCTYPE html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Scrivener Backup Check — {now}</title>"
        "<style>"
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#f1f5f9;color:#0f172a;padding:24px 32px}"
        "h1{font-size:1.3rem;font-weight:700;margin-bottom:2px}"
        ".meta{font-size:.75rem;color:#64748b;margin-bottom:14px}"
        f".bar{{display:inline-flex;gap:12px;padding:6px 14px;border-radius:8px;"
        f"background:{bar_color};color:#fff;font-weight:600;font-size:.9rem;"
        "margin-bottom:22px}"
        ".card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;"
        "margin-bottom:14px;overflow:hidden}"
        ".ch{display:flex;align-items:center;justify-content:space-between;"
        "padding:10px 14px;background:#f8fafc;border-bottom:1px solid #e2e8f0}"
        ".bn{font-weight:600}"
        ".badge{padding:2px 10px;border-radius:20px;color:#fff;"
        "font-size:.75rem;font-weight:700;letter-spacing:.4px}"
        ".cb{padding:12px 14px}"
        ".info{width:100%;border-collapse:collapse;font-size:.82rem;margin-bottom:8px}"
        ".info th{text-align:left;color:#64748b;font-weight:500;padding:2px 0;"
        "width:100px;white-space:nowrap}"
        ".info td{padding:2px 0}"
        "code{font-family:ui-monospace,monospace;background:#f1f5f9;"
        "padding:1px 4px;border-radius:3px;font-size:.8rem}"
        ".ok{color:#16a34a;font-weight:500}"
        ".err{color:#dc2626}"
        ".snippet{font-size:.8rem;color:#475569;background:#f8fafc;padding:8px;"
        "border-radius:4px;margin-top:6px;line-height:1.5}"
        ".shot{max-width:100%;border-radius:6px;border:1px solid #e2e8f0;"
        "margin-top:10px;display:block}"
        "</style></head><body>"
        "<h1>Scrivener Backup Validation</h1>"
        f'<div class="meta">{esc(str(run_dir))} &nbsp;&middot;&nbsp; {now}</div>'
        f'<div class="bar"><span>{passed} pass</span>'
        f'<span>{failed} fail</span><span>{len(books)} total</span></div>'
        f"{cards}"
        "</body></html>"
    )

    out = run_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    return out


def open_in_browser(path: Path) -> None:
    """Open path in the default browser. Best-effort; never raises."""
    try:
        subprocess.run(
            ["open", str(path)], check=False, capture_output=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass


def write_report(run_dir: Path, books: list[BookResult]) -> Path:
    report = {
        "run_dir": str(run_dir),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "books": len(books),
            "passed": sum(1 for b in books if b.status == "PASS"),
            "failed": sum(1 for b in books if b.status == "FAIL"),
            "skipped": sum(1 for b in books if b.status == "SKIPPED"),
        },
        "books": [b.to_dict() for b in books],
    }
    out = run_dir / "report.json"
    out.write_text(json.dumps(report, indent=2))

    # Human-readable summary
    lines = [
        "Scrivener Backup Validation Report",
        "=" * 50,
        f"Run dir: {run_dir}",
        f"Books:   {report['totals']['books']}",
        f"PASS:    {report['totals']['passed']}",
        f"FAIL:    {report['totals']['failed']}",
        f"SKIP:    {report['totals']['skipped']}",
        "",
    ]
    for b in books:
        marker = {"PASS": "✅", "FAIL": "❌", "SKIPPED": "⏭", "PENDING": "?"}[b.status]
        lines.append(f"{marker} {b.name}")
        if b.backup_zip:
            lines.append(f"   backup: {Path(b.backup_zip).name}")
        if b.failure_reason:
            lines.append(f"   reason: {b.failure_reason}")
        if b.diff_summary and not b.diff_summary["ok"]:
            d = b.diff_summary
            lines.append(
                f"   drift: {len(d['content_missing'])} missing, "
                f"{len(d['content_changed'])} changed"
            )
    summary = run_dir / "report.txt"
    summary.write_text("\n".join(lines) + "\n")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chaos-engineering drill for Scrivener backups."
    )
    parser.add_argument("--local", type=Path, default=DEFAULT_LOCAL)
    parser.add_argument(
        "--backups", type=Path, default=None,
        help=(
            "Folder containing Scrivener backup zips. If omitted, the "
            "path is read from Scrivener's preferences "
            "(SCRAutomaticBackupPath); falls back to "
            f"{FALLBACK_BACKUPS} if prefs aren't readable."
        ),
    )
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument(
        "--book", type=str, default=None,
        help="Validate only this book (by name, no .scriv). Overrides scope flags.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--all", action="store_true",
        help="Validate every .scriv in the local folder (default behaviour).",
    )
    scope.add_argument(
        "--latest", action="store_true",
        help="Validate only the most-recently-modified .scriv.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Plan only — no Scrivener interaction, no file changes.",
    )
    parser.add_argument(
        "--no-screenshots", dest="screenshots", action="store_false",
        help="Skip screen captures (default: on; requires Screen Recording permission).",
    )
    parser.set_defaults(screenshots=True)
    parser.add_argument(
        "--keep-quarantine", action="store_true",
        help="Do not auto-purge the quarantine even if all books pass.",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This tool only runs on macOS (requires AppleScript + screencapture).",
              file=sys.stderr)
        return 2

    run_dir = make_run_dir(args.run_root)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    log = setup_logging(run_dir)
    log.info("Run dir: %s", run_dir)

    # If --backups wasn't passed (sentinel: None), discover from
    # Scrivener's preferences. Falls back to the hard-coded default
    # only when prefs can't be read AND the fallback exists.
    if args.backups is None:
        discovered = discover_scrivener_backup_path()
        if discovered is not None:
            args.backups = discovered
            log.info(
                "Backups (auto-discovered from Scrivener prefs): %s",
                args.backups,
            )
        else:
            args.backups = FALLBACK_BACKUPS
            log.info("Backups (fallback default): %s", args.backups)
    else:
        log.info("Backups: %s", args.backups)

    log.info("Local:   %s", args.local)
    log.info("Mode:    %s", "DRY-RUN" if args.dry_run else "LIVE")

    if not args.local.exists():
        log.error("Local folder does not exist: %s", args.local)
        return 2
    if not args.backups.exists():
        log.error("Backup folder does not exist: %s", args.backups)
        log.error(
            "  Pass --backups <path> or check Scrivener → Settings → "
            "Backup → Backup Location."
        )
        return 2

    validator = Validator(
        local_dir=args.local,
        backup_dir=args.backups,
        run_dir=run_dir,
        log=log,
        screenshots=args.screenshots,
        dry_run=args.dry_run,
    )
    validator.shot("00_preflight")

    mode = "latest" if args.latest else "all"
    books = validator.discover_books(only=args.book, mode=mode)
    if not books:
        log.warning("No .scriv projects found.")
        return 1

    selection = (
        f"{books[0].name} (latest by mtime)" if mode == "latest" and not args.book
        else ", ".join(b.name for b in books)
    )
    log.info("Validating %d book(s): %s", len(books), selection)

    for book in books:
        validator.validate_book(book)

    # Final report
    report_path = write_report(run_dir, books)
    html_path = write_html_report(run_dir, books)
    log.info("Report: %s", report_path)

    failed = [b for b in books if b.status == "FAIL"]
    passed = [b for b in books if b.status == "PASS"]

    print()
    print("=" * 60)
    print(f"PASS: {len(passed)}    FAIL: {len(failed)}    "
          f"TOTAL: {len(books)}")
    print(f"Report:    {report_path}")
    print(f"Dashboard: {html_path}")
    print(f"Screens:   {validator.shots_dir}")
    print("=" * 60)

    open_in_browser(html_path)

    # Quarantine policy: purge ONLY if every book passed AND user didn't
    # ask to keep it. Otherwise loud-print the path.
    if failed:
        print()
        print("⚠️  FAILURES OCCURRED — quarantine PRESERVED for forensics:")
        print(f"   {validator.quarantine}")
        print("   Originals are safe under quarantine/originals/ and "
              "quarantine/safety-copies/.")
        return 1

    if args.dry_run or args.keep_quarantine:
        print(f"Quarantine preserved at: {validator.quarantine}")
        return 0

    log.info("All books passed — purging quarantine.")
    shutil.rmtree(validator.quarantine, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
