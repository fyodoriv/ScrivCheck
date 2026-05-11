# ScrivCheck

A chaos engineering drill for Scrivener backups. One command runs the full
restore exercise across every project you have, verifies the restored
content is byte-identical to the original, and produces screenshot proof.

Built because manually running through "save → copy → delete → unzip →
restore → verify" once a quarter is exactly the kind of recovery drill
that gets skipped right up until the day you actually need it.

> **Status:** macOS only (reads Scrivener prefs via `defaults`,
> optionally captures screenshots via `screencapture`).
> Tested with Scrivener 3. No Scrivener interaction at runtime — no
> Automation permission needed, no focus loss.

## What it does

Replaces this manual checklist:

1. Open Scrivener, save to make a backup
2. Open the local Scrivener folder
3. Make a copy of the book file
4. Delete the original
5. Open the backup folder
6. Unzip the latest backup
7. Move the unzipped file to the local folder
8. Rename it to the original name
9. Open the book in Scrivener
10. Confirm it's in the correct state

…with one command:

```bash
./scrivcheck.py
```

The tool runs all 10 steps for every `.scriv` project it finds, captures
a screenshot at each visible step, computes SHA-256 manifests of the
project before and after restore, and writes a structured report — plus
an HTML dashboard that opens automatically in your browser.

## Why chaos engineering, not just a backup script

Backup scripts test that backups *exist*. This tool tests that backups
*work*. The difference matters: a corrupt zip looks identical to a good
zip until you try to restore it, and a `.scriv` package that loses one
file silently is worse than no backup at all because you trust it.

The tool applies five chaos engineering principles:

| Principle | Implementation |
|---|---|
| **Define steady state** | SHA-256 manifest of every file in each `.scriv` is captured before any change. Volatile files (search index, UI state, `.DS_Store`) are excluded from strict comparison because Scrivener regenerates them. |
| **Form a hypothesis** | "Restoring the most recent matching backup yields a project whose user-content manifest is byte-identical to the steady state." This is recorded in `report.json` and the result tells you whether it held. |
| **Inject the fault** | The tool doesn't *look at* the backup — it actually moves the original out of the way and reconstructs the project from the zip. The only way to prove a backup is restorable. |
| **Verify steady state** | Post-restore manifest is computed and compared. PASS requires every content file (`Files/Data/**`, `Files/Docs/**`) to be present with matching SHA-256. |
| **Contain blast radius** | No `rm`, ever. Every "delete" is a `mv` to a per-run quarantine. A safety copy is taken **before** the original is moved, so two independent copies exist at every critical moment. Any failure auto-rolls-back from quarantine. Per-book isolation means a failure on book N can't damage the others. |

The data-safety invariant is enforced by an integration test that you
can read in `tests/test_validation_flow.py`: at every observable point
during a run, the original must exist in at least one of the local
folder, the quarantine, or the safety-copies directory. If a future
change ever breaks that invariant, the test fails before the change
ships.

## Install

Requires Python 3.10+. No third-party dependencies.

```bash
git clone https://github.com/fyodoriv/ScrivCheck.git ~/apps/ScrivCheck
chmod +x ~/apps/ScrivCheck/scrivcheck.py
mkdir -p ~/.local/bin
ln -sf ~/apps/ScrivCheck/scrivcheck.py ~/.local/bin/scrivcheck
```

`~/.local/bin` is on PATH on most modern macOS setups. If it isn't, add
this line to your `~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Spotlight app

`ScrivCheck.app` is included in the repo. Copy it to `~/Applications/` to
launch ScrivCheck from Spotlight:

```bash
cp -r ~/apps/ScrivCheck/ScrivCheck.app ~/Applications/
```

The first time macOS may ask you to confirm opening an app from an
unidentified developer — right-click → Open if so. After that, `⌘Space
ScrivCheck` launches it directly.

## Usage

After install, the entire tool is one word from anywhere in your terminal:

```bash
scrivcheck                       # validate ALL .scriv books — the default
scrivcheck --latest              # validate only the most-recently-modified book
scrivcheck --all                 # same as default (all books)
scrivcheck --book "MyNovel"      # validate one specific book
scrivcheck --dry-run             # plan only — does NOT touch Scrivener
scrivcheck --screenshots         # extra screenshots at each visible step
scrivcheck --keep-quarantine     # keep the quarantine even on success
scrivcheck \                     # different folders
    --local "/path/to/local/scriv/folder" \
    --backups "/path/to/backups"
```

### Defaults that respect your workflow

- **All books**, by default. Every `.scriv` in the local folder is validated
  in one run. Use `--latest` to restrict to the project you're actively
  writing in, or `--book "Name"` for one specific book.
- **Backup path auto-discovered.** If `--backups` is omitted, the path
  is read from Scrivener's preferences
  (`SCRAutomaticBackupPath` in `com.literatureandlatte.scrivener3`).
  This Just Works™ even when you've redirected backups to Dropbox /
  iCloud / a custom folder. Falls back to
  `~/Library/Application Support/Scrivener/Backups` only when prefs
  can't be read.
- **No Scrivener interaction.** Earlier releases tried to drive
  Scrivener via AppleScript to trigger a fresh backup before
  validation. That was unreliable: Scrivener 3's dictionary rejects
  every `save` form (-1708 errAEEventNotHandled), and even
  `quit saving yes` does not fire the *Back up on save* hook
  (which is gated on user-initiated Cmd+S). The tool now validates
  the latest *existing* backup zip and never launches Scrivener,
  steals focus, or needs Automation permission. Save manually in
  Scrivener (`⌘S`) before running `scrivcheck` if you want a fresh
  backup validated.
- **HTML dashboard auto-opens** in your default browser after every
  run, showing status badges, backup details, content excerpts, and
  embedded screenshots.
- **Dry-run is a pure plan**. Hashes the backup zip, computes the
  pre-flight manifest of the live project, prints the plan. No file
  is moved, nothing destructive happens.

Defaults assume Scrivener's standard layout (local folder under `~/Scrivener
local`, backups under `~/Library/CloudStorage/Dropbox/Apps/Scrivener`) but
everything is overridable via CLI flags.

## Output

Each run writes a timestamped directory to `~/ScrivCheck/`:

```
~/ScrivCheck/run_2026-05-03_14-30-22/
├── report.json            # full machine-readable state, every step,
│                          #   every manifest, every diff
├── report.txt             # human-readable summary
├── report.html            # dashboard — auto-opens in browser
├── proof/
│   └── MyBook.txt         # per-book verbose proof block (see below)
├── screenshots/                # only when --screenshots is passed
│   ├── 000_00_preflight.png
│   ├── 001_MyBook_01_opened.png
│   ├── 002_MyBook_03_after_quarantine.png
│   ├── 003_MyBook_04_unzipped.png
│   └── 004_MyBook_05_restored.png
├── logs/
│   └── run.log            # full debug-level log
└── quarantine/            # only present if validation failed
    ├── originals/         # the "deleted" originals (still safe)
    ├── safety-copies/     # the defense-in-depth copies
    └── staging/           # where backups were unzipped
```

If everything passes, the quarantine is purged. If anything fails, it
stays put and the path is loud-printed at the end of the run.

### Proof block

For every book, the tool prints (and saves to `proof/<book>.txt`) a
loud, human-checkable evidence block. Sample (synthetic):

```
══════════════════════════════════════════════════════════════════════
PROOF — MyBook
══════════════════════════════════════════════════════════════════════

Backup file (real, on disk, hashed in your presence):
    path    /Users/.../Dropbox/Apps/Scrivener/MyBook.bak.zip
    size    482,317 bytes
    mtime   2026-05-03T14:30:18
    sha256  9f3a1c…b274c1d8

Pre-flight steady state (BEFORE the backup was touched):
    3 content file(s), 47 bytes
      Files/Data/UUID-1/content.rtf            16 B  3a7bd3e2dde7c1f0…
      Files/Data/UUID-2/content.rtf            16 B  a5f9c2b1aa6c8d20…
      Files/Data/UUID-3/content.rtf             8 B  e29cefe7e7a89c30…

Post-restore manifest (project rebuilt FROM THE ZIP):
    3 content file(s), 47 bytes
      Files/Data/UUID-1/content.rtf            16 B  3a7bd3e2dde7c1f0…  ✓ MATCH
      Files/Data/UUID-2/content.rtf            16 B  a5f9c2b1aa6c8d20…  ✓ MATCH
      Files/Data/UUID-3/content.rtf             8 B  e29cefe7e7a89c30…  ✓ MATCH

ATTESTATION
    Status:     PASS
    Verified:   3/3 content file(s) SHA-256 byte-identical to pre-flight
    Hypothesis: HELD ✅
    At:         2026-05-03T14:30:33
══════════════════════════════════════════════════════════════════════
```

The SHA-256 of the zip is computed in front of you — there's no
"trust me", just hashes you can re-verify with `shasum -a 256` on the
backup file at any time.

## Getting a fresh backup before validation

`scrivcheck` looks for an existing backup zip first. If one is found,
it validates that. If none is found, it creates one from the live
`.scriv` directory and validates the newly created backup — so the
drill always runs, even on first use or for a project that's never
been backed up.

To validate today's work without relying on `scrivcheck` to create
the backup itself:

1. In Scrivener, hit **`⌘S`** (or *File → Save*). With *Settings →
   Backup → Back up on save* enabled, this writes a fresh backup zip
   to your configured backup folder.
2. Run `scrivcheck`.

Why not auto-trigger Scrivener's backup hook? Earlier releases tried
`tell application "Scrivener" to quit saving yes`, which saves the
document but does **not** fire the *Back up on save* hook (the hook
is gated on user-initiated `⌘S` only). Scrivener 3's dictionary also
rejects every `save` form via AppleScript (`-1708 errAEEventNotHandled`).
Creating the zip from the filesystem directly is the reliable fallback.

## Failure modes

| What you see | What it means | What to do |
|---|---|---|
| `No existing backup — creating one: ...` | No backup zip was found in the backup folder, so `scrivcheck` created one from the live project and proceeded with validation | Nothing — this is the expected first-run experience. Run `scrivcheck` again after a Scrivener `⌘S` if you want to validate a Scrivener-generated backup |
| `verify_manifest` fails with `content_changed` entries | The backup is older than the just-saved state | Enable *Back up on save* in Scrivener (or save manually before running the drill) |
| `verify_manifest` fails with `content_missing` entries | The backup is genuinely incomplete | The last backup didn't capture everything — investigate before relying on it |
| `Zip-slip blocked: entry '...' resolves outside the staging directory` | A backup zip carries an entry trying to escape the staging dir (path traversal). Either tampered or generated by a non-standard tool | Treat the zip as suspect. Don't reuse it. Take a fresh backup from inside Scrivener |
| `Refusing to extract symlink entry` | A backup zip contains a symlink, which Scrivener doesn't write | Same: don't trust the zip |
| `Not enough free space on <fs>'s filesystem` | The drill aborted before any quarantine move because the safety copy + unzipped backup wouldn't fit | Free up disk space (the drill needs ≈2.5× the project size as headroom) |
| `Scrivener is running. The pre-flight manifest may catch files mid-write...` | Informational only — drill proceeds. Manifest could capture a half-written state if Scrivener saves at the same instant | Quit Scrivener first if you want a paranoid-clean run |
| Screenshots all-black or warnings | Screen Recording permission denied | Grant in *System Settings → Privacy & Security → Screen Recording*, or simply omit `--screenshots` (the default) |

In all cases the originals and safety copies remain in the quarantine
directory printed at the end of the run.

## macOS permissions

The default `scrivcheck` invocation needs **no permission grants**.
The tool only reads files and runs `defaults read` to discover
Scrivener's backup folder.

You'll be prompted only if you opt into screenshots:

- **Screen Recording** (`Terminal`) — required by `screencapture` if
  you pass `--screenshots`. Granted in *System Settings → Privacy &
  Security → Screen Recording*.

## Development

```bash
# Run the full test suite
python3 -m unittest discover -s tests -v

# Run a single test module
python3 -m unittest tests.test_manifest -v

# Run a single test
python3 -m unittest tests.test_validation_flow.HappyPathTests.test_full_flow_produces_pass -v

# Run with coverage (CI enforces 100%)
python3 -m pip install coverage
python3 -m coverage run --source=scrivcheck -m unittest discover -s tests
python3 -m coverage report -m --fail-under=100
```

Tests use only the standard library (coverage is a development-only
extra). Continuous integration runs on Ubuntu against Python 3.10,
3.11, 3.12, and 3.13 via GitHub Actions (`.github/workflows/tests.yml`)
and the build fails if coverage drops below 100%.

The macOS-specific code paths (`defaults`, `screencapture`, `pgrep`,
`brctl`) are mocked at the `subprocess` boundary so the chaos
engineering invariants — including the rollback paths and the
adversarial-zip defenses — are exercised on every CI run.

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. The code aims to keep the data-safety invariant
above any other property, including correctness of the diff. If you're
adding a new failure-handling path, please add a test in
`tests/test_validation_flow.py` that asserts the data-safety invariant
holds for that path.
