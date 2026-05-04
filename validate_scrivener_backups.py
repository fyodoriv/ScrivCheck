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
    ./validate_scrivener_backups.py                    # full drill, all books
    ./validate_scrivener_backups.py --dry-run          # plan only, no changes
    ./validate_scrivener_backups.py --book "MyNovel"   # one book by name
    ./validate_scrivener_backups.py --keep-quarantine  # do not auto-purge on success
    ./validate_scrivener_backups.py --no-screenshots   # skip screencapture calls

Quarantine cleanup happens only when ALL validated books pass. Otherwise
the quarantine is preserved and the path is printed loudly at the end.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_LOCAL = Path.home() / "Scrivener local"
DEFAULT_BACKUPS = (
    Path.home() / "Library" / "CloudStorage" / "Dropbox" / "Apps" / "Scrivener"
)
DEFAULT_RUN_ROOT = Path.home() / "scrivener-validation"

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

BACKUP_SETTLE_SECONDS = 8     # how long to wait after Save for backup zip
SCRIVENER_QUIT_TIMEOUT = 30   # seconds


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


def scrivener_quit(log: logging.Logger) -> None:
    """Quit Scrivener gracefully. Save any open documents first."""
    if not scrivener_running():
        return
    log.info("Quitting Scrivener (saving open documents)…")
    try:
        osascript(
            'tell application "Scrivener"\n'
            '  if it is running then\n'
            '    try\n'
            '      save every document\n'
            '    end try\n'
            '    quit\n'
            '  end if\n'
            'end tell'
        )
    except subprocess.CalledProcessError as e:
        log.warning("Graceful quit failed: %s", e.stderr)
    # Wait for process to actually exit
    deadline = time.time() + SCRIVENER_QUIT_TIMEOUT
    while time.time() < deadline:
        if not scrivener_running():
            return
        time.sleep(0.5)
    raise RuntimeError("Scrivener did not quit within timeout")


def scrivener_open(project_path: Path, log: logging.Logger) -> None:
    """
    Open a .scriv project in Scrivener WITHOUT bringing the app to the
    foreground. The `-g` flag tells `open` to launch in background so we
    don't steal the user's window focus during the drill.
    """
    log.info("Opening in Scrivener (background): %s", project_path)
    run(["open", "-g", "-a", "Scrivener", str(project_path)], check=True)
    # Give Scrivener a moment to load the project
    time.sleep(5)


def scrivener_save_active(log: logging.Logger) -> None:
    """
    Save every open document via AppleScript. ``save every document`` is
    chosen over ``save front document`` because the latter requires
    Scrivener to be the frontmost app (not the case when we open in the
    background) and silently fails when no front window exists.
    """
    log.info("Saving open documents…")
    try:
        osascript('tell application "Scrivener" to save every document')
    except subprocess.CalledProcessError as e:
        # Surface the underlying AppleScript error so the user can debug
        # — the most common cause is missing Automation permission for
        # Terminal → Scrivener (System Settings → Privacy & Security →
        # Automation).
        msg = (e.stderr or "").strip() or "no stderr captured"
        log.error("Save via AppleScript failed: %s", msg)
        raise
    time.sleep(2)


def screencapture(out_path: Path, log: logging.Logger, enabled: bool = True) -> Optional[str]:
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


def find_latest_backup(backup_dir: Path, project_name: str) -> Optional[Path]:
    """
    Find the most recently modified .zip in `backup_dir` whose name starts
    with `project_name`. Scrivener typically names backups:
        ProjectName.bak.zip
        ProjectName.bak1.zip
        ProjectName 2024-01-15 14-30.zip
    """
    if not backup_dir.exists():
        return None
    candidates: list[Path] = []
    safe = re.escape(project_name)
    pattern = re.compile(rf"^{safe}([\s._-].*)?\.zip$", re.IGNORECASE)
    for child in backup_dir.iterdir():
        if child.is_file() and pattern.match(child.name):
            candidates.append(child)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


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
            # Step 1: open in Scrivener and save (triggers configured backup)
            scrivener_open(project_path, self.log)
            self.shot(f"{book.name}_01_opened", book)
            book.add_step("open_in_scrivener", True)

            scrivener_save_active(self.log)
            self.shot(f"{book.name}_02_saved", book)
            book.add_step("save_to_trigger_backup", True)

            scrivener_quit(self.log)
            book.add_step("quit_scrivener", True)

            # Step 2: pre-flight manifest
            self.log.info("Computing pre-flight manifest…")
            book.pre_manifest = compute_manifest(project_path)
            book.add_step(
                "preflight_manifest",
                True,
                f"{book.pre_manifest.file_count} files, "
                f"{book.pre_manifest.total_size:,} bytes",
            )

            # Step 3: locate latest backup
            backup_zip = find_latest_backup(self.backups, book.name)
            if not backup_zip:
                raise RuntimeError(
                    f"No backup zip found in {self.backups} matching {book.name!r}"
                )
            book.backup_zip = str(backup_zip)
            ensure_locally_available(backup_zip, self.log)
            self.log.info("Latest backup: %s", backup_zip)
            book.add_step("locate_backup", True, backup_zip.name)

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

            # Step 6: unzip backup into staging
            self.log.info("Unzipping backup into staging…")
            staging_book = self.staging / book.name
            if staging_book.exists():
                shutil.rmtree(staging_book)
            staging_book.mkdir(parents=True)
            with zipfile.ZipFile(backup_zip, "r") as zf:
                zf.extractall(staging_book)
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

            # Step 9: open restored project to confirm Scrivener accepts it
            scrivener_open(restored, self.log)
            self.shot(f"{book.name}_06_reopened", book)
            book.add_step("reopen_in_scrivener", True)
            scrivener_quit(self.log)

            # Step 10: post-restore manifest + comparison
            self.log.info("Computing post-restore manifest…")
            book.post_manifest = compute_manifest(restored)
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
            reason = (
                f"no backup zip found in {self.backups} "
                f"matching {book.name!r}"
            )
            book.failure_reason = f"dry-run: {reason}"
            book.add_step("locate_backup_dryrun", False, reason)
            self.log.warning("[dry-run] %s", reason)
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
        Loud, human-checkable evidence that a backup is real and restorable.

        Prints the per-book proof block to the log (i.e. stdout + run.log)
        AND writes it to ``<run_dir>/proof/<book>.txt`` so the user has a
        durable artifact, not just a console blip.

        The block contains:
          • SHA-256 of the backup zip itself (the literal file on disk)
          • the pre-flight content manifest (steady state)
          • the post-restore content manifest with per-file MATCH/MISMATCH
          • an attestation footer stating whether the hypothesis held
        """
        lines: list[str] = []
        bar = "═" * 70
        lines.append(bar)
        lines.append(f"PROOF — {book.name}")
        lines.append(bar)

        # 1. The backup zip itself, hashed in front of the user.
        if book.backup_zip:
            zp = Path(book.backup_zip)
            if zp.exists():
                stat = zp.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds"
                )
                lines.append("")
                lines.append("Backup file (real, on disk, hashed in your presence):")
                lines.append(f"    path    {zp}")
                lines.append(f"    size    {stat.st_size:,} bytes")
                lines.append(f"    mtime   {mtime}")
                lines.append(f"    sha256  {file_sha256(zp)}")

        pre = book.pre_manifest.content_entries() if book.pre_manifest else {}
        post = book.post_manifest.content_entries() if book.post_manifest else {}

        # 2. Pre-flight steady state.
        if pre:
            pre_bytes = sum(e.size for e in pre.values())
            lines.append("")
            lines.append(
                "Pre-flight steady state (BEFORE the backup was touched):"
            )
            lines.append(
                f"    {len(pre)} content file(s), {pre_bytes:,} bytes"
            )
            for relpath in sorted(pre):
                e = pre[relpath]
                lines.append(
                    f"      {relpath:<48s} {e.size:>9,} B  {e.sha256[:16]}…"
                )

        # 3. Post-restore manifest with per-file verdict.
        if pre and post:
            post_bytes = sum(e.size for e in post.values())
            lines.append("")
            lines.append(
                "Post-restore manifest (project rebuilt FROM THE ZIP):"
            )
            lines.append(
                f"    {len(post)} content file(s), {post_bytes:,} bytes"
            )
            matches = mismatches = missing = 0
            for relpath in sorted(pre):
                pre_e = pre[relpath]
                post_e = post.get(relpath)
                if post_e is None:
                    lines.append(
                        f"      {relpath:<48s} {'':>9}    "
                        f"{'':<16}  ✗ MISSING after restore"
                    )
                    missing += 1
                elif post_e.sha256 != pre_e.sha256:
                    lines.append(
                        f"      {relpath:<48s} {post_e.size:>9,} B  "
                        f"{post_e.sha256[:16]}…  ✗ MISMATCH "
                        f"(was {pre_e.sha256[:8]}, got {post_e.sha256[:8]})"
                    )
                    mismatches += 1
                else:
                    lines.append(
                        f"      {relpath:<48s} {post_e.size:>9,} B  "
                        f"{post_e.sha256[:16]}…  ✓ MATCH"
                    )
                    matches += 1

            # 4. Attestation footer.
            verdict = "HELD ✅" if book.status == "PASS" else "REJECTED ❌"
            lines.append("")
            lines.append("ATTESTATION")
            lines.append(f"    Status:     {book.status}")
            lines.append(
                f"    Verified:   {matches}/{len(pre)} content file(s) "
                "SHA-256 byte-identical to pre-flight"
            )
            if mismatches:
                lines.append(
                    f"    Mismatch:   {mismatches} file(s) differ from pre-flight"
                )
            if missing:
                lines.append(
                    f"    Missing:    {missing} file(s) absent after restore"
                )
            lines.append(f"    Hypothesis: {verdict}")
            lines.append(
                f"    At:         {datetime.now().isoformat(timespec='seconds')}"
            )

        # Dry-run gets its own attestation: state what was inspected,
        # don't pretend a hypothesis was tested.
        if book.status == "SKIPPED" and not post:
            lines.append("")
            lines.append("DRY-RUN ATTESTATION")
            lines.append("    Status:     SKIPPED (plan only, no restore)")
            inspected = []
            if book.backup_zip and Path(book.backup_zip).exists():
                inspected.append("backup zip presence + SHA-256")
            if pre:
                inspected.append("live folder pre-flight manifest")
            lines.append(
                f"    Inspected:  {', '.join(inspected) if inspected else 'nothing'}"
            )
            lines.append(
                "    Note:       no save was triggered, no files were moved."
            )

        if book.status == "FAIL" and not post:
            lines.append("")
            lines.append("NOTE")
            lines.append(
                "    Drill aborted before a post-restore manifest could be "
                "computed."
            )
            lines.append(f"    Reason: {book.failure_reason}")

        lines.append(bar)

        for line in lines:
            self.log.info(line)
        proof_dir = self.run_dir / "proof"
        proof_dir.mkdir(parents=True, exist_ok=True)
        (proof_dir / f"{book.name}.txt").write_text(
            "\n".join(lines) + "\n"
        )

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
            if target.exists():
                self.log.info("Original slot already populated, no rollback needed.")
                book.add_step("rollback", True, "target already present")
                return

            # Prefer the quarantined original (it's the literal pre-state)
            source = None
            if quarantined and quarantined.exists():
                source = quarantined
            elif safety_copy and safety_copy.exists():
                source = safety_copy

            if source is None:
                self.log.error("ROLLBACK IMPOSSIBLE — no source found. "
                               "Quarantine path: %s", self.quarantine)
                book.add_step("rollback", False, "no source available")
                return

            self.log.info("Rolling back from %s -> %s", source, target)
            shutil.copytree(source, target)
            book.add_step("rollback", True, f"restored from {source}")
        except Exception as e:  # noqa: BLE001
            self.log.exception("Rollback itself failed: %s", e)
            book.add_step("rollback", False, str(e))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


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
    parser.add_argument("--backups", type=Path, default=DEFAULT_BACKUPS)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument(
        "--book", type=str, default=None,
        help="Validate only this book (by name, no .scriv). Overrides --all.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help=(
            "Validate every .scriv in the local folder. "
            "Default is to validate only the most-recently-modified one."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Plan only — no Scrivener interaction, no file changes.",
    )
    parser.add_argument(
        "--screenshots", action="store_true",
        help=(
            "Capture full-screen screenshots at each visible step. Off by "
            "default; requires macOS Screen Recording permission."
        ),
    )
    parser.add_argument(
        "--keep-quarantine", action="store_true",
        help="Do not auto-purge the quarantine even if all books pass.",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This tool only runs on macOS (requires AppleScript + screencapture).",
              file=sys.stderr)
        return 2

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = args.run_root / f"run_{timestamp}"
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    log = setup_logging(run_dir)
    log.info("Run dir: %s", run_dir)
    log.info("Local:   %s", args.local)
    log.info("Backups: %s", args.backups)
    log.info("Mode:    %s", "DRY-RUN" if args.dry_run else "LIVE")

    if not args.local.exists():
        log.error("Local folder does not exist: %s", args.local)
        return 2
    if not args.backups.exists():
        log.error("Backup folder does not exist: %s", args.backups)
        return 2

    validator = Validator(
        local_dir=args.local,
        backup_dir=args.backups,
        run_dir=run_dir,
        log=log,
        screenshots=args.screenshots,
        dry_run=args.dry_run,
    )

    # Pre-flight: make sure Scrivener is closed (skip in dry-run — we don't
    # want a dry-run plan to disturb the user's actual editing session).
    if not args.dry_run:
        scrivener_quit(log)
    validator.shot("00_preflight")

    mode = "all" if args.all else "latest"
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
        try:
            validator.validate_book(book)
        finally:
            # Don't try to quit Scrivener in dry-run — we never opened it.
            if not args.dry_run:
                scrivener_quit(log)

    # Final report
    report_path = write_report(run_dir, books)
    log.info("Report: %s", report_path)

    failed = [b for b in books if b.status == "FAIL"]
    passed = [b for b in books if b.status == "PASS"]

    print()
    print("=" * 60)
    print(f"PASS: {len(passed)}    FAIL: {len(failed)}    "
          f"TOTAL: {len(books)}")
    print(f"Report:    {report_path}")
    print(f"Screens:   {validator.shots_dir}")
    print("=" * 60)

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
