"""The Terragrunt recipe: its published HCL must be valid Terragrunt config.

Code: docs/recipe_terragrunt.md.
Method: extract the labeled ```hcl blocks straight out of the document and drive
them through real `terragrunt hcl validate` (schema decode) and `terragrunt
render` (full decode, which evaluates get_repo_root()), so a reader cannot edit
a snippet into something broken without this going red. The document is the
source of truth; the test holds no retyped copy. This mirrors the AST guard in
test_rig.py and the comment-strip guard in test_shim.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.rig import RECIPE_MD, extract_labeled_hcl_blocks, require_tools

pytestmark = [pytest.mark.e2e]

RECIPE = RECIPE_MD


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


def test_recipe_terragrunt_config_parses_and_resolves_the_shim(tmp_path: Path) -> None:
    """The recipe's HCL is valid Terragrunt, and terraform_binary reaches the shim.

    Pulls both labeled ```hcl blocks out of docs/recipe_terragrunt.md and runs
    each through real Terragrunt. No retyped copy: if the document drifts, this
    drifts with it.

    Fails-when-broken: against the pre-fix recipe - which placed
    `terraform_binary` *inside* the `terraform {}` block - `hcl validate` exits 1
    with `An argument named "terraform_binary" is not expected here.` That is the
    defect that shipped because the recipe had never been run through real
    Terragrunt; this test runs it.

    Assertion audit:
    - root `hcl validate` (rc 0): BEHAVIOURAL, and the load-bearing line. The
      one that catches a misplaced terraform_binary - the defect this test exists
      to pin. The verbatim red from the unfixed document is recorded in the SDD
      report for this task.
    - root `render` + resolved == shim path: BEHAVIOURAL. Proves the
      terraform_binary expression evaluates to the shim's location (get_repo_root
      resolves, the relative suffix is right). `render` does not probe the binary
      - the -version probe under `plan`/`apply` is the next task - so this pins
      resolution, not execution.
    - resolved path is an existing executable: BEHAVIOURAL. Catches a path that
      evaluates but points at nothing (a renamed binary, a typo in the suffix).
    - unit `hcl validate` (rc 0): BEHAVIOURAL. Pins the extra_arguments block's
      schema. The recipe leaves `locals` to the consumer, so a minimal empty stub
      is prepended - that stub is NOT recipe content and does not alter the
      extracted block.

    Tool requirement follows the host-kubectl precedent (test_rig.py:263):
    terragrunt and git are required here, where they are used, not tier-wide.

    Assert messages are bound to names rather than written inline: black and ruff
    format disagree on where to break `assert <cond>, <long message>`, and both
    are CI gates (see test_shim.py:162 for the same workaround).
    """
    require_tools("terragrunt", "git")

    blocks = extract_labeled_hcl_blocks(RECIPE.read_text())
    missing_root = (
        "recipe is missing its ```hcl terragrunt-root``` block - the label that "
        "pins the terraform_binary snippet is gone"
    )
    missing_unit = (
        "recipe is missing its ```hcl terragrunt-unit``` block - the label that "
        "pins the extra_arguments snippet is gone"
    )
    assert "terragrunt-root" in blocks, missing_root
    assert "terragrunt-unit" in blocks, missing_unit

    repo = _consumer_repo(tmp_path)
    # The shim the recipe's terraform_binary points at. A real tofu is not
    # needed: hcl validate is static, and render evaluates the path but does not
    # invoke the binary.
    shim = repo / "bin" / "tofu-tunstrap"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)

    # --- root block: schema-valid, and terraform_binary resolves to the shim ---
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

    # --- unit block: schema-valid (extra_arguments). The recipe defers locals
    # to the consumer ("Build local.cluster_host / local.ssh_private_key from
    # whatever your source of truth is"), so the snippet references locals it
    # does not define and cannot decode standalone (hcl validate reports
    # "Unsuitable value: value must be known" on the ternary). A minimal stub
    # holding exactly the two referenced names is prepended. It is NOT recipe
    # content and leaves the extracted block byte-for-byte intact. ---
    root_config.write_text(
        'locals {\n  cluster_host    = ""\n  ssh_private_key = ""\n}\n\n'
        + blocks["terragrunt-unit"]
    )
    unit_validate = subprocess.run(
        ["terragrunt", "hcl", "validate", "--working-dir", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    unit_detail = "unit terragrunt.hcl failed schema validation:\n"
    unit_detail += f"{unit_validate.stderr}{unit_validate.stdout}"
    assert unit_validate.returncode == 0, unit_detail
