"""The Terragrunt recipe: its published HCL must decode, and its published
snippets must not drift from what the e2e tier drives.

Code: docs/recipe_terragrunt.md.
Method: extract the labelled fenced blocks straight out of the document and
- drive the Terragrunt blocks through real `terragrunt hcl validate` / `render`
  (schema decode + terraform_binary resolution), exercising BOTH sides of the
  unit's env_vars ternary;
- check the module-side snippet against the driven module file.
The document is the source of truth; the tests hold no retyped copy. Mirrors the
AST guard in test_rig.py and the drift guards in test_shim.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.e2e.rig import (
    HERE,
    RECIPE_MD,
    extract_labeled_blocks,
    require_tools,
    strip_comments,
)

pytestmark = [pytest.mark.e2e]

RECIPE = RECIPE_MD
MODULE_MAIN_TF = HERE / "module" / "main.tf"


def _consumer_repo(tmp_path: Path) -> Path:
    """A faithful consumer working tree: git-init'd, so get_repo_root() resolves.

    The recipe's terraform_binary uses ${get_repo_root()}, which shells out to
    `git rev-parse --show-toplevel`; a plain tmp_path is not a git repo, so the
    expression would error under `render` before the test learns anything. A real
    consumer repo is a git repo, so this is the honest setup. `git init` alone is
    enough - `--show-toplevel` resolves against the worktree, not HEAD, so no
    commit is needed (verified: render resolves the path with zero commits).
    """
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    return repo


def _uncomment(block: str) -> str:
    """Strip a leading ``#`` and one space from each line of a commented block.

    The recipe's ``terragrunt-locals`` block is a commented-out ``locals {...}``
    example (it reads as guidance, not live config). Uncommenting yields the HCL a
    consumer would write. Applied per-line so indentation survives and only the
    comment marker goes.
    """
    return "\n".join(
        line[2:] if line.startswith("# ") else line.removeprefix("#") for line in block.splitlines()
    )


def test_root_block_parses_and_resolves_the_shim(tmp_path: Path) -> None:
    """The recipe's root block is valid Terragrunt and terraform_binary reaches the shim.

    Fails-when-broken: a misplaced terraform_binary (back inside terraform{})
    fails `hcl validate` with "An argument named terraform_binary is not expected
    here"; a broken path expression fails `render`; a path that resolves to a
    non-executable fails the is_file/os.access check. (The verbatim red for the
    misplaced-terraform_binary case is in the SDD report for the original pin.)
    """
    require_tools("terragrunt", "git")
    blocks = extract_labeled_blocks(RECIPE.read_text())
    missing_root = (
        "recipe is missing its ```hcl terragrunt-root``` block - the label that "
        "pins the terraform_binary snippet is gone"
    )
    assert "terragrunt-root" in blocks, missing_root

    repo = _consumer_repo(tmp_path)
    # A real tofu is not needed: hcl validate is static, render evaluates the path
    # but does not invoke the binary.
    shim = repo / "bin" / "tofu-tunstrap"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)

    root_config = repo / "terragrunt.hcl"
    root_config.write_text(blocks["terragrunt-root"])
    root_validate = subprocess.run(
        ["terragrunt", "hcl", "validate", "--working-dir", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    root_detail = "root terragrunt.hcl failed schema validation:\n"
    root_detail += f"{root_validate.stderr}{root_validate.stdout}"
    assert root_validate.returncode == 0, root_detail

    rendered = subprocess.run(
        ["terragrunt", "render", "--config", "terragrunt.hcl", "--format", "json"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    render_detail = f"root terragrunt.hcl failed to render:\n{rendered.stderr}{rendered.stdout}"
    assert rendered.returncode == 0, render_detail
    resolved = json.loads(rendered.stdout)["terraform_binary"]
    expected = str(shim)
    resolved_msg = f"terraform_binary resolved to {resolved!r}, expected the shim at {expected}"
    assert resolved == expected, resolved_msg
    exec_msg = f"terraform_binary resolved to {resolved!r}, which is not an executable file"
    assert Path(resolved).is_file() and os.access(resolved, os.X_OK), exec_msg


@pytest.mark.parametrize("empty_host", [False, True], ids=["host-set", "host-empty"])
def test_unit_block_decodes_both_ternary_sides(empty_host: bool, tmp_path: Path) -> None:
    """The recipe's unit block decodes on BOTH sides of the env_vars ternary.

    The env_vars map is `local.cluster_host != "" ? { TUNSTRAP_INPUT = jsonencode(...) } : {}`.
    hcl validate evaluates only the taken branch, so to force the
    jsonencode branch (every consumer-visible key: nodes, kube_targets, daemon)
    the host must be non-empty. The earlier pin set cluster_host="" and so only
    ever decoded the inert `{}` side. Both params now decode.

    The locals come from the recipe's own commented ``terragrunt-locals`` example
    (uncommented) - not a non-recipe stub: the recipe documents the shape and
    this enforces it. For host-empty the example's host is zeroed via a
    value-agnostic regex, so the placeholder can change without breaking the test.

    Fails-when-broken (host-set): a malformed key in the jsonencode branch - e.g.
    a projection field rename - fails hcl validate. (host-empty): a broken
    ternary or undefined local fails the same way.
    """
    require_tools("terragrunt", "git")
    blocks = extract_labeled_blocks(RECIPE.read_text())
    missing_unit = (
        "recipe is missing its ```hcl terragrunt-unit``` block - the label that "
        "pins the extra_arguments snippet is gone"
    )
    missing_locals = (
        "recipe is missing its ```hcl terragrunt-locals``` block - the commented "
        "locals example that lets this test drop its non-recipe stub is gone"
    )
    assert "terragrunt-unit" in blocks, missing_unit
    assert "terragrunt-locals" in blocks, missing_locals

    locals_block = _uncomment(blocks["terragrunt-locals"])
    if empty_host:
        locals_block = re.sub(r'(cluster_host\s*=\s*)"[^"]*"', r'\1""', locals_block)

    repo = _consumer_repo(tmp_path)
    (repo / "terragrunt.hcl").write_text(f"{locals_block}\n\n{blocks['terragrunt-unit']}")
    validated = subprocess.run(
        ["terragrunt", "hcl", "validate", "--working-dir", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    side = "host-empty (inert `{}` branch)" if empty_host else "host-set (jsonencode branch)"
    detail = f"unit terragrunt.hcl ({side}) failed schema validation:\n"
    detail += f"{validated.stderr}{validated.stdout}"
    assert validated.returncode == 0, detail


def test_module_snippet_matches_the_driven_module() -> None:
    """The recipe's module snippet does not drift from tests/e2e/module/main.tf.

    The recipe's ```hcl tf-module``` block is the provider-wiring excerpt a
    consumer copies; main.tf is the file the e2e tier drives. They are not
    byte-identical (main.tf adds resources + output, and the comments were
    written separately), so compare executable lines: every logic line in the
    snippet must appear in the driven module. A projection field rename in either
    source breaks this.

    Fails-when-broken: edit a logic line in the snippet (e.g. rename
    kube_targets.k3s.path) without matching main.tf and that line is reported
    missing. Reuses strip_comments (the shim drift-guard helper), extending that
    idiom rather than adding a new one. Needs no tools - pure text comparison.
    """
    blocks = extract_labeled_blocks(RECIPE.read_text())
    missing_module = (
        "recipe is missing its ```hcl tf-module``` block - the label that pins "
        "the module-side snippet is gone"
    )
    assert "tf-module" in blocks, missing_module

    recipe_logic = strip_comments(blocks["tf-module"]).splitlines()
    driven_set = set(strip_comments(MODULE_MAIN_TF.read_text()).splitlines())
    missing = [line for line in recipe_logic if line not in driven_set]
    drift = f"recipe ```hcl tf-module``` logic lines missing from {MODULE_MAIN_TF}:\n" + "\n".join(
        missing
    )
    assert not missing, drift
