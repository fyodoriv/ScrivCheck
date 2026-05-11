#!/usr/bin/env bash
# One-shot setup: move this folder into ~/apps, run the tests, init git,
# create a public GitHub repo, and push. Run this from inside the
# unpacked scrivener-backup-validator folder (anywhere on disk).
#
# Usage:
#     ./setup.sh                       # public repo (default)
#     ./setup.sh --private             # private repo
#     ./setup.sh --skip-github         # only do the ~/apps + git init part
#
# Requirements: macOS, Python 3.10+, gh CLI authenticated (`gh auth status`).

set -euo pipefail

VISIBILITY="--public"
DO_GITHUB=1

for arg in "$@"; do
    case "$arg" in
        --private)      VISIBILITY="--private" ;;
        --skip-github)  DO_GITHUB=0 ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *)
            echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

REPO_NAME="scrivCheck"
TARGET="$HOME/apps/$REPO_NAME"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "$SOURCE_DIR" == "$TARGET" ]]; then
    echo "Already in $TARGET — skipping move."
else
    if [[ -e "$TARGET" ]]; then
        echo "✗ $TARGET already exists. Refusing to overwrite." >&2
        echo "  Move or delete it first, then re-run." >&2
        exit 1
    fi
    echo "→ Moving project to $TARGET"
    mkdir -p "$HOME/apps"
    # rsync if available (handles excludes cleanly), else cp + manual clean
    if command -v rsync >/dev/null 2>&1; then
        rsync -a \
            --exclude '__pycache__' \
            --exclude '.pytest_cache' \
            --exclude '*.pyc' \
            "$SOURCE_DIR/" "$TARGET/"
    else
        cp -R "$SOURCE_DIR" "$TARGET"
        find "$TARGET" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
        find "$TARGET" -name '*.pyc' -delete 2>/dev/null || true
    fi
    cd "$TARGET"
fi

cd "$TARGET"

echo "→ Running test suite"
python3 -m unittest discover -s tests
echo "✓ Tests pass"

if [[ ! -d .git ]]; then
    if ! git config --get user.email >/dev/null || ! git config --get user.name >/dev/null; then
        cat <<'EOF' >&2
✗ Git is not configured. Set your identity first:
    git config --global user.name  "Your Name"
    git config --global user.email "you@example.com"
Then re-run this script.
EOF
        exit 1
    fi
    echo "→ Initializing git repo"
    git init -b main >/dev/null
    git add .
    git commit -m "Initial release: chaos engineering drill for Scrivener backups" >/dev/null
    echo "✓ Initial commit created"
else
    echo "✓ Git repo already initialized"
fi

if [[ $DO_GITHUB -eq 0 ]]; then
    echo
    echo "Done. Skipping GitHub push as requested."
    echo "Project is at: $TARGET"
    exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
    cat <<EOF >&2

gh CLI is not installed. Install it with:
    brew install gh
    gh auth login

Then re-run this script, or push manually:
    cd $TARGET
    gh repo create $REPO_NAME $VISIBILITY --source=. --remote=origin --push
EOF
    exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
    echo "✗ gh is installed but not authenticated. Run: gh auth login" >&2
    exit 1
fi

if git remote get-url origin >/dev/null 2>&1; then
    echo "✓ Remote 'origin' already configured — pushing"
    git push -u origin main
else
    echo "→ Creating GitHub repo and pushing"
    gh repo create "$REPO_NAME" $VISIBILITY --source=. --remote=origin --push
fi

URL=$(gh repo view --json url -q .url)
echo
echo "✓ Done."
echo "  Local:  $TARGET"
echo "  Remote: $URL"
