# scrivener-backup-validator

A chaos engineering drill for Scrivener backups. One command runs the full
restore exercise across every project you have, verifies the restored
content is byte-identical to the original, and produces screenshot proof.

Built because manually running through "save → copy → delete → unzip →
restore → verify" once a quarter is exactly the kind of recovery drill
that gets skipped right up until the day you actually need it.

> **Status:** macOS only (depends on AppleScript and `screencapture`).
> Tested with Scrivener 3.

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
./validate_scrivener_backups.py
```

The tool runs all 10 steps for every `.scriv` project it finds, captures
a screenshot at each visible step, computes SHA-256 manifests of the
project before and after restore, and writes a structured report.

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
git clone https://github.com/fyodoriv/scrivener-backup-validator.git ~/apps/scrivener-backup-validator
chmod +x ~/apps/scrivener-backup-validator/validate_scrivener_backups.py
mkdir -p ~/.local/bin
ln -sf ~/apps/scrivener-backup-validator/validate_scrivener_backups.py \
       ~/.local/bin/scrivcheck
```

`~/.local/bin` is on PATH on most modern macOS setups. If it isn't, add
this line to your `~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Usage

After install, the entire tool is one word from anywhere in your terminal:

```bash
scrivcheck                       # full drill, all books — the default
scrivcheck --book "MyNovel"      # just one book
scrivcheck --dry-run             # plan only, do not move or copy anything
scrivcheck --no-screenshots      # skip macOS Screen Recording permission
scrivcheck --keep-quarantine     # keep the quarantine even on success
scrivcheck \                     # different folders
    --local "/path/to/local/scriv/folder" \
    --backups "/path/to/backups"
```

Defaults assume Scrivener's standard layout (local folder under `~/Scrivener
local`, backups under `~/Library/CloudStorage/Dropbox/Apps/Scrivener`) but
everything is overridable via CLI flags.

## Output

Each run writes a timestamped directory to `~/scrivener-validation/`:

```
~/scrivener-validation/run_2026-05-03_14-30-22/
├── report.json            # full machine-readable state, every step,
│                          #   every manifest, every diff
├── report.txt             # human-readable summary
├── proof/
│   └── MyBook.txt         # per-book verbose proof block (see below)
├── screenshots/
│   ├── 000_00_preflight.png
│   ├── 001_MyBook_01_opened.png
│   ├── 002_MyBook_02_saved.png
│   ├── 003_MyBook_03_after_quarantine.png
│   ├── 004_MyBook_04_unzipped.png
│   ├── 005_MyBook_05_restored.png
│   └── 006_MyBook_06_reopened.png
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

## Failure modes

| What you see | What it means | What to do |
|---|---|---|
| `No backup zip found in <dir> matching 'BookName'` | No file in the backup folder starts with the book's name | Check Scrivener's *Settings → Backup* — backup-on-save may not be configured, or the backup target may have changed |
| `verify_manifest` fails with `content_changed` entries | The backup is older than the just-saved state | Enable "Back up with each manual save" in Scrivener |
| `verify_manifest` fails with `content_missing` entries | The backup is genuinely incomplete | The last backup didn't capture everything — investigate before relying on it |
| Run hangs ~30s then aborts on `quit_scrivener` | Scrivener has a modal dialog open | Close the dialog and rerun |
| Screenshots all-black | Screen Recording permission was denied | Grant in *System Settings → Privacy & Security → Screen Recording*, or pass `--no-screenshots` |

In all cases the originals and safety copies remain in the quarantine
directory printed at the end of the run.

## macOS permissions

First run will prompt for two permissions:

- **Automation** (`Terminal → Scrivener`) — required for save and quit
  via AppleScript. Without it the tool can't drive Scrivener.
- **Screen Recording** (`Terminal`) — required for `screencapture` to
  capture window contents. Without it, screenshots are black; pass
  `--no-screenshots` and the rest still works.

Both are granted in *System Settings → Privacy & Security*.

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
python3 -m coverage run --source=validate_scrivener_backups -m unittest discover -s tests
python3 -m coverage report -m --fail-under=100
```

Tests use only the standard library (coverage is a development-only
extra). Continuous integration runs on Ubuntu against Python 3.10,
3.11, 3.12, and 3.13 via GitHub Actions (`.github/workflows/tests.yml`)
and the build fails if coverage drops below 100%.

The macOS-specific code paths (AppleScript, `screencapture`, Scrivener
open, `pgrep`, `brctl`) are mocked at the `subprocess` boundary so the
chaos engineering invariants — including the rollback paths — are
exercised on every CI run.

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. The code aims to keep the data-safety invariant
above any other property, including correctness of the diff. If you're
adding a new failure-handling path, please add a test in
`tests/test_validation_flow.py` that asserts the data-safety invariant
holds for that path.
