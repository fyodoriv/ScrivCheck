"""Shared helpers for the test suite."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# Make the script importable from anywhere
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def make_fake_scriv(parent: Path, name: str, content: dict[str, bytes]) -> Path:
    """Build a fake .scriv package directory with the given relative-path
    contents. `parent` is created if it doesn't exist."""
    parent.mkdir(parents=True, exist_ok=True)
    pkg = parent / f"{name}.scriv"
    pkg.mkdir()
    for rel, data in content.items():
        full = pkg / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
    return pkg


def zip_scriv_package(scriv: Path, zip_path: Path, *, nested: bool = False) -> Path:
    """Zip a .scriv package. If nested=True, the zip contains a wrapper
    folder with the .scriv inside (a real-world Scrivener variant)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in scriv.rglob("*"):
            if f.is_file():
                if nested:
                    arcname = Path("wrapper") / scriv.name / f.relative_to(scriv)
                else:
                    arcname = Path(scriv.name) / f.relative_to(scriv)
                zf.write(f, arcname)
    return zip_path


# Standard fixture content so tests stay readable
SAMPLE_BOOK = {
    "project.scrivx": b"<?xml version='1.0'?><scrivx/>",
    "Files/Data/UUID-1/content.rtf": b"chapter one body",
    "Files/Data/UUID-2/content.rtf": b"chapter two body",
    "Files/Data/UUID-3/content.rtf": b"epilogue",
    "Files/search.indexes": b"<volatile-search-data>",
    "Settings/ui.plist": b"<volatile-ui-state>",
    ".DS_Store": b"\x00\x00",
}
