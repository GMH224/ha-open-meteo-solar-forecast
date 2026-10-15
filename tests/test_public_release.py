"""Guard for the public repository (15 Oct 2026, owner request).

The repository and its release zip are public. Private installation data
(location, station, hardware, entry ids) live in ``confidential.md``, which
is kept outside the repository and never published. This test reads that
file's *Never publish* list, when the file is available, and fails if any of
its strings appears in a file of the repository. The rules that need no
private data run always.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".mypy_cache", ".pytest_cache", "__pycache__", ".venv", "venv", "node_modules"}
CONFIDENTIAL_NAME = "confidential.md"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_ALLOWED = {"noreply@anthropic.com"}


def _repository_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.name.lower() == CONFIDENTIAL_NAME:
            continue
        files.append(path)
    return files


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _confidential_file() -> Path | None:
    candidates = [os.environ.get("OMSF_CONFIDENTIAL"), ROOT / CONFIDENTIAL_NAME, ROOT.parent / CONFIDENTIAL_NAME]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def never_publish(text: str) -> list[str]:
    """Backticked bullet items of the '## Never publish' section."""
    match = re.search(r"^## Never publish\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if match is None:
        return []
    return [item for item in re.findall(r"^- `([^`]+)`\s*$", match.group(1), re.MULTILINE) if item.strip()]


def test_confidential_file_is_never_committed_or_archived():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert any(line.strip().lower() == CONFIDENTIAL_NAME for line in ignore)
    assert any(line.split()[:2] == [CONFIDENTIAL_NAME, "export-ignore"] for line in attributes if line.strip())


def test_no_email_addresses_in_the_repository():
    found = []
    for path in _repository_files():
        text = _text(path)
        if text is None:
            continue
        for address in EMAIL.findall(text):
            if address.lower() not in EMAIL_ALLOWED:
                found.append(f"{path.relative_to(ROOT)}: {address}")
    assert not found, found


def test_parser_reads_only_the_never_publish_section():
    text = "# x\n- `outside`\n## Never publish\n\n- `one`\n- `two words`\n- not backticked\n## Other\n- `later`\n"
    assert never_publish(text) == ["one", "two words"]


def test_no_confidential_string_in_the_repository():
    source = _confidential_file()
    if source is None:
        pytest.skip("confidential.md not available (public checkout)")
    secrets = never_publish(source.read_text(encoding="utf-8"))
    assert secrets, "confidential.md has no '## Never publish' list"
    lowered = [s.lower() for s in secrets]
    found = []
    for path in _repository_files():
        text = _text(path)
        if text is None:
            continue
        low = text.lower()
        for secret, needle in zip(secrets, lowered, strict=True):
            if needle in low:
                found.append(f"{path.relative_to(ROOT)}: {secret}")
    assert not found, found
