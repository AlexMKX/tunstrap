"""Reject unpublishable local paths and ignored-document references.

Docs that need a literal local-home example may opt in with the HTML comment
``<!-- tracked-doc-path-guard: allow-home-path -->``. The exception is limited
to that document and only exempts home-path examples; ignored references and
unresolved findings citations still fail.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_HOME_PATH_RE = re.compile("/" + r"home/[^/\s]+/")
_ARTIFACTS_PATH = "docs" + "/artifacts/"
_DOC_PATH_RE = re.compile(r"(?<![\w.-])(docs/[\w./-]+)")
_FINDINGS_NAME_RE = re.compile(r"(?<![\w.-])([\w.-]+-findings\.md)\b")
_ALLOW_HOME_PATH_MARKER = "tracked-doc-path-guard: allow-home-path"


def _tracked_files() -> list[Path]:
    """Return every tracked regular file, relative to the repository root."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [REPO_ROOT / path for path in tracked.decode().split("\0") if path]


def _is_ignored(path: str) -> bool:
    """Return whether Git's ignore rules exclude ``path``."""
    return (
        subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", path],
            cwd=REPO_ROOT,
            check=False,
        ).returncode
        == 0
    )


def _unresolved_references(content: str, tracked: set[Path]) -> set[str]:
    """Find ignored path references and bare findings names absent from Git."""
    violations = {
        reference for reference in _DOC_PATH_RE.findall(content) if _is_ignored(reference)
    }
    tracked_names = {path.name for path in tracked}
    violations.update(
        name for name in _FINDINGS_NAME_RE.findall(content) if name not in tracked_names
    )
    return violations


def test_tracked_files_contain_no_local_home_paths_or_artifact_citations() -> None:
    """Public tracked content cannot depend on local machines or ignored files."""
    violations: list[str] = []
    tracked = _tracked_files()
    tracked_set = set(tracked)
    for path in tracked:
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        relative_path = path.relative_to(REPO_ROOT)
        allows_home_path = relative_path.parts[0] == "docs" and _ALLOW_HOME_PATH_MARKER in content
        has_home_path = _HOME_PATH_RE.search(content) and not allows_home_path
        has_artifact_citation = path.name != ".gitignore" and _ARTIFACTS_PATH in content
        references = (
            set() if path.name == ".gitignore" else _unresolved_references(content, tracked_set)
        )
        if has_home_path or has_artifact_citation or references:
            violations.append(f"{relative_path}: {sorted(references)}")

    assert not violations, (
        "tracked files contain local-home paths, ignored-document citations, "
        "or unresolved findings references: "
        f"{violations}"
    )
