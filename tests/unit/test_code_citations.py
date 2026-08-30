"""Keep live code citations resolvable and independent of source line numbers."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHON_SUFFIX = "." + "py"
_SYMBOL_SEPARATOR = ":" + ":"
_CITATION_RE = re.compile(
    r"(?<![\w.-])([\w./-]+"
    + re.escape(_PYTHON_SUFFIX)
    + r")"
    + re.escape(_SYMBOL_SEPARATOR)
    + r"([A-Za-z_][A-Za-z0-9_]*)"
)
_LINE_CITATION_RE = re.compile(
    r"(?<![\w.-])[\w./-]+" + re.escape(_PYTHON_SUFFIX) + r":[0-9]+(?:-[0-9]+)?"
)


def _tracked_in_scope_files() -> list[Path]:
    """Return the tracked live files whose code citations are maintained."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    paths = [Path(path) for path in tracked.split("\0") if path]
    # docs/superpowers/plans/** and docs/specs/** are frozen historical records.
    # Their line references were accurate when written; rewriting them would
    # falsify that history, so this deliberately excludes them from the guard.
    return [
        REPO_ROOT / path
        for path in paths
        if path == Path("README.md")
        or path == Path("docs/recipe_terragrunt.md")
        or (path.parts[0] in {"tunstrap", "tests"} and path.suffix == _PYTHON_SUFFIX)
    ]


def _defined_symbols(path: Path) -> set[str]:
    """Return module symbols and methods defined by a Python source file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
            if isinstance(node, ast.ClassDef):
                symbols.update(
                    member.name
                    for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            symbols.update(target.id for target in targets if isinstance(target, ast.Name))
    return symbols


def test_live_symbol_citations_resolve() -> None:
    """Every live path-and-symbol citation identifies a definition in its file."""
    violations: list[str] = []
    for source_path in _tracked_in_scope_files():
        for relative_path, symbol in _CITATION_RE.findall(source_path.read_text()):
            target_path = REPO_ROOT / relative_path
            citation = relative_path + _SYMBOL_SEPARATOR + symbol
            if not target_path.is_file() or symbol not in _defined_symbols(target_path):
                violations.append(citation)

    assert not violations, f"unresolvable code citations: {violations}"


def test_live_citations_do_not_use_line_numbers() -> None:
    """Live citations must name a stable symbol rather than a shifting line."""
    violations = [
        str(source_path.relative_to(REPO_ROOT))
        for source_path in _tracked_in_scope_files()
        if _LINE_CITATION_RE.search(source_path.read_text())
    ]

    assert not violations, f"line-number code citations: {violations}"
