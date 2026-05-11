# ScrivCheck

[![Tests](https://github.com/fyodoriv/ScrivCheck/actions/workflows/tests.yml/badge.svg)](https://github.com/fyodoriv/ScrivCheck/actions/workflows/tests.yml)

A chaos engineering drill for Scrivener backups. One command runs the full
restore exercise across every project you have, verifies the restored
content is byte-identical to the original, and opens an HTML dashboard
showing the results — including a screenshot of each project open in
Scrivener as visual proof.

Built because manually running "save → copy → delete → unzip → restore →
verify" once a quarter is exactly the kind of recovery drill that gets
skipped right up until the day you actually need it.

> **Platform:** macOS only (reads Scrivener prefs via `defaults`, captures
> screenshots via `screencapture`). Tested with Scrivener 3. No Scrivener
> interaction at runtime for the validation steps — no Automation permission
> needed, no focus loss until the screenshot phase.

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

The tool runs all 10 steps for every `.scriv` project it finds, opens
Scrivener to capture a screenshot of the restored project, computes SHA-256
manifests before and after restore, and writes a structured report plus an
HTML dashboard that opens automatically in your browser.

## Why chaos engineering, not just a backup script

Backup scripts test that backups *exist*. This tool tests that backups
*work*. The difference matters: a corrupt zip looks identical to a good
zip until you try to restore it, and a `.scriv` package that silently
loses one file is worse than no backup at all because you trust it.

The tool applies five chaos engineering principles:

| Principle | Implementation |
|---|---|
| **Define steady state** | SHA-256 manifest of every file in each `.scriv` is captured before any change. Volatile files (search index, UI state, `.DS_Store`) are excluded from strict comparison because Scrivener regenerates them. |
| **Form a hypothesis** | "Restoring the most recent matching backup yields a project whose user-content manifest is byte-identical to the steady state." Recorded in `report.json`; the result tells you whether it held. |
| **Inject the fault** | The tool doesn't *look at* the backup — it actually moves the original out of the way and reconstructs the project from the zip. The only way to prove a backup is restorable. |
| **Verify steady state** | Post-restore manifest is computed and compared. PASS requires every content file (`Files/Data/**`, `Files/Docs/**`) to be present with matching SHA-256. |
| **Contain blast radius** | No `rm`, ever. Every "delete" is a `mv` to a per-run quarantine. A safety copy is taken **before** the original is moved, so two independent copies exist at every critical moment. Any failure auto-rolls-back from quarantine. Per-book isolation means a failure on book N can't damage the others. |

The data-safety invariant is enforced by an integration test in
`tests/test_validation_flow.py`: at every observable point during a run,
the original must exist in at least one of the local folder, the
quarantine, or the safety-copies directory.

## Install

Requires Python 3.10+. No third-party dependencies.

```bash
git clone https://github.com/fyodoriv/ScrivCheck.git ~/apps/scrivCheck
chmod +x ~/apps/scrivCheck/scrivcheck.py
mkdir -p ~/.local/bin
ln -sf ~/apps/scrivCheck/scrivcheck.py ~/.local/bin/scrivcheck
```

`~/.local/bin` is on PATH on most modern macOS setups. If it isn't, add
this to your `~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Spotlight app

`ScrivCheck.app` is included in the repo. Copy it to `~/Applications/` to
launch ScrivCheck from Spotlight:

```bash
cp -r ~/apps/scrivCheck/ScrivCheck.app ~/Applications/
```

The app is a compiled Swift launcher that wraps the Python script so that
macOS attributes Screen Recording permission to "ScrivCheck" rather than
"python3". The first time macOS may ask you to confirm opening an app from
an unidentified developer — right-click → Open if so. After that,
`⌘Space ScrivCheck` launches it directly.

> **Recompiling the launcher** (optional): if you want to build it yourself
> rather than trust the binary in the repo, run:
> ```bash
> swiftc ScrivCheck.app/Contents/MacOS/ScrivCheck.swift \
>     -o ScrivCheck.app/Contents/MacOS/ScrivCheck
> ```
> Swift source is at `ScrivCheck.app/Contents/MacOS/ScrivCheck.swift`.

## Usage

After install, the tool is one word from anywhere in your terminal:

```bash
scrivcheck                            # validate ALL .scriv books (default)
scrivcheck --latest                   # only the most-recently-modified book
scrivcheck --all                      # same as default (explicit)
scrivcheck --book "MyNovel"           # one specific book
scrivcheck --dry-run                  # plan only — no file changes
scrivcheck --no-screenshots           # skip screen captures entirely
scrivcheck --screenshot-books "A,B"   # screenshot only these books
scrivcheck --screenshot-all-books     # screenshot every validated book
scrivcheck --keep-quarantine          # keep quarantine even on success
scrivcheck \                          # custom folder paths
    --local "/path/to/local/scriv/folder" \
    --backups "/path/to/backups"
```

### Screenshots

Screenshots are **on by default** and show Scrivener open with the
restored project — visual proof the backup worked. Only a configurable
subset of books gets a Scrivener screenshot by default (see
[Customization](#customization) below).

The HTML dashboard shows the active list below the summary bar, with the
two commands to change it.

### Defaults that respect your workflow

- **All books**, by default. Every `.scriv` in the local folder is
  validated in one run. Use `--latest` or `--book "Name"` to narrow scope.
- **Backup path auto-discovered.** `--backups` is optional; the path is
  read from Scrivener's preferences (`SCRAutomaticBackupPath` in
  `com.literatureandlatte.scrivener3`). Works even when you've redirected
  backups to Dropbox / iCloud / a custom folder. Falls back to
  `~/Library/Application Support/Scrivener/Backups` only when prefs can't
  be read.
- **No Scrivener interaction during validation.** The tool validates the
  latest *existing* backup zip without launching Scrivener, stealing focus,
  or needing Automation permission. Save manually in Scrivener (`⌘S`)
  before running `scrivcheck` if you want a fresh backup validated.
- **HTML dashboard auto-opens** in your default browser after every run,
  showing status badges, backup details, content excerpts, and embedded
  screenshots.
- **Dry-run is a pure plan.** Hashes the backup zip, computes the
  pre-flight manifest, prints the plan. No file is moved.

Defaults assume Scrivener's standard layout (local folder under
`~/Scrivener local`, backups wherever Scrivener prefs say) but everything
is overridable via CLI flags.

## Customization

### Which books get Scrivener screenshots

`scrivcheck.py` has a constant near the top of the config section:

```python
DEFAULT_SCREENSHOT_BOOKS: tuple[str, ...] = (
    "ИЖ",
    "Подпольная Фабрика Смузи",
    "00-00",
    "Рассвет",
)
```

**Change this to your own book names.** Books not in this list are still
fully validated (SHA-256 drill, report, proof block) — they just don't get
a Scrivener screenshot. Override at runtime without editing the file:

```bash
# Screenshot only these two books this run
scrivcheck --screenshot-books "MyNovel,MyOtherBook"

# Screenshot every book this run
scrivcheck --screenshot-all-books

# Skip screenshots entirely
scrivcheck --no-screenshots
```

## Output

Each run writes a timestamped directory to `~/ScrivCheck/`:

```
~/ScrivCheck/run_2026-05-03_14-30-22/
├── report.json            # machine-readable state, every step,
│                          #   every manifest, every diff
├── report.txt             # human-readable summary
├── report.html            # dashboard — auto-opens in browser
├── proof/
│   └── MyBook.txt         # per-book verbose proof block (see below)
├── screenshots/
│   ├── 001_00_preflight.png
│   └── 002_MyBook_scrivener.png   # only for books in the screenshot list
├── logs/
│   └── run.log            # full debug-level log
└── quarantine/            # preserved only when validation fails
    ├── originals/         # the "deleted" originals (still safe)
    ├── safety-copies/     # the defense-in-depth copies
    └── staging/           # where backups were unzipped
```

If everything passes, the quarantine is purged. If anything fails, it
stays put and the path is loud-printed at the end of the run.

### Proof block

For every book, the tool prints (and saves to `proof/<book>.txt`) a
human-checkable evidence block:

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

The SHA-256 of the zip is computed in front of you — you can re-verify with
`shasum -a 256 <zip>` at any time.

## Getting a fresh backup before validation

`scrivcheck` looks for an existing backup zip first. If one is found, it
validates that. If none is found, it creates one from the live `.scriv`
directory so the drill always runs, even on first use.

To validate today's work with a Scrivener-generated backup:

1. In Scrivener, hit **`⌘S`** (or *File → Save*). With *Settings →
   Backup → Back up on save* enabled, this writes a fresh backup zip.
2. Run `scrivcheck`.

## Failure modes

| What you see | What it means | What to do |
|---|---|---|
| `No existing backup — creating one: ...` | No backup zip found; `scrivcheck` created one from the live project | Nothing — expected first-run experience. Run `scrivcheck` after `⌘S` to validate a Scrivener-generated backup |
| `verify_manifest` fails with `content_changed` | The backup is older than the just-saved state | Enable *Back up on save* in Scrivener (or save manually before running the drill) |
| `verify_manifest` fails with `content_missing` | The backup is genuinely incomplete | The last backup didn't capture everything — investigate before relying on it |
| `Zip-slip blocked: entry '...' resolves outside the staging directory` | A backup zip carries a path-traversal entry | Treat the zip as suspect. Take a fresh backup from inside Scrivener |
| `Refusing to extract symlink entry` | A backup zip contains a symlink (Scrivener doesn't write these) | Same: don't trust the zip |
| `Not enough free space on <fs>'s filesystem` | Drill aborted before quarantine move (needs ≈2.5× the project size as headroom) | Free up disk space |
| `Scrivener is running. The pre-flight manifest may catch files mid-write...` | Informational — drill proceeds | Quit Scrivener first for a paranoid-clean run |
| Screenshots blank or warnings about Screen Recording | Screen Recording permission not yet granted to ScrivCheck | Grant in *System Settings → Privacy & Security → Screen Recording*, or run with `--no-screenshots` |

In all failure cases, originals and safety copies remain in the quarantine
directory printed at the end of the run.

## macOS permissions

The default `scrivcheck` invocation needs **no permission grants** — it
only reads files and runs `defaults read`.

You'll be prompted the first time you run screenshots:

- **Screen Recording** — required by `screencapture`. The permission
  dialog will show **"ScrivCheck"** (not "python3") when launched via
  `ScrivCheck.app`. Grant it in *System Settings → Privacy & Security →
  Screen Recording*. Run with `--no-screenshots` to skip entirely.

## Development

```bash
# Run the full test suite
python3 -m unittest discover -s tests -v

# Run a single test module
python3 -m unittest tests.test_validation_flow -v

# Run with coverage (CI enforces 100%)
python3 -m pip install coverage
python3 -m coverage run --source=scrivcheck -m unittest discover -s tests
python3 -m coverage report -m --fail-under=100
```

Tests use only the standard library. Continuous integration runs on
Ubuntu against Python 3.10–3.13 via GitHub Actions and fails if coverage
drops below 100%.

The macOS-specific code paths (`defaults`, `screencapture`, `pgrep`,
`brctl`) are mocked at the `subprocess` boundary so chaos engineering
invariants — including rollback paths and adversarial-zip defenses — run
on every CI push.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
