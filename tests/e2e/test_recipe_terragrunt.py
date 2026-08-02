"""The Terragrunt recipe: its published HCL must decode, and its published
snippets must not drift from what the e2e tier drives.

Code: docs/recipe_terragrunt.md.
Method: extract the labelled fenced blocks straight out of the document and
- drive the Terragrunt blocks through real `terragrunt hcl validate` / `render`
  (schema decode + terraform_binary resolution), exercising BOTH sides of the
  unit's env_vars ternary;
- check the module-side snippet against the driven module file.
The document is the source of truth; the tests hold no retyped copy. Mirrors the
AST guard in test_rig.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
    """A bare consumer working tree for one recipe-block check.

    `terragrunt hcl validate` and `render` evaluate static HCL and need no git
    repo (verified). Earlier this helper was git-init'd to make
    ``${get_repo_root()}`` resolve, but the recipe's root block no longer uses
    that expression (it is a literal path or a run_cmd), so neither git nor a
    commit is required. Kept as a helper for shape parity with a real consumer
    directory.
    """
    repo = tmp_path / "consumer"
    repo.mkdir()
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


def test_root_block_points_terraform_binary_at_the_proxy(tmp_path: Path) -> None:
    """Both root-block forms in the recipe are valid and reach tunstrap_tofu.

    The recipe carries TWO labelled root fences: ``terragrunt-root`` (the literal-
    path default) and ``terragrunt-root-runcmd`` (the optional run_cmd form).
    The literal fence is validated statically (it carries a consumer-specific
    placeholder, so it cannot render to a real path); the run_cmd fence is
    RENDERED and must resolve to the actual installed ``tunstrap_tofu`` on PATH,
    executable - a strictly stronger pin than the literal allows, and the proof
    that the recipe's run_cmd form works. hcl validate also catches a misplaced
    terraform_binary (back inside terraform{}, which TG rejects with "An argument
    named terraform_binary is not expected here"). (The verbatim red for the
    misplaced case is in the SDD report for the original pin.)
    """
    require_tools("terragrunt")
    installed = shutil.which("tunstrap_tofu")
    path_msg = "tunstrap_tofu not on PATH; e2e_preflight should have failed the tier"
    assert installed is not None, path_msg
    blocks = extract_labeled_blocks(RECIPE.read_text())
    for label in ("terragrunt-root", "terragrunt-root-runcmd"):
        assert label in blocks, f"recipe is missing its ```hcl {label}``` block"

    # Literal fence: static validation only (its path is a placeholder).
    repo = tmp_path / "literal"
    repo.mkdir()
    (repo / "terragrunt.hcl").write_text(blocks["terragrunt-root"])
    root_validate = subprocess.run(
        ["terragrunt", "hcl", "validate", "--working-dir", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    root_detail = "literal root terragrunt.hcl failed schema validation:\n"
    root_detail += f"{root_validate.stderr}{root_validate.stdout}"
    assert root_validate.returncode == 0, root_detail

    # run_cmd fence: render must resolve to the real installed tunstrap_tofu.
    repo2 = tmp_path / "runcmd"
    repo2.mkdir()
    (repo2 / "terragrunt.hcl").write_text(blocks["terragrunt-root-runcmd"])
    rendered = subprocess.run(
        ["terragrunt", "render", "--config", "terragrunt.hcl", "--format", "json"],
        cwd=repo2,
        capture_output=True,
        text=True,
        check=False,
    )
    render_detail = (
        f"run_cmd root terragrunt.hcl failed to render:\n{rendered.stderr}{rendered.stdout}"
    )
    assert rendered.returncode == 0, render_detail
    resolved = json.loads(rendered.stdout)["terraform_binary"]
    resolved_msg = (
        f"terraform_binary resolved to {resolved!r}, expected the installed {installed!r}"
    )
    assert resolved == installed, resolved_msg
    exec_msg = f"terraform_binary resolved to {resolved!r}, which is not an executable file"
    assert Path(resolved).is_file() and os.access(resolved, os.X_OK), exec_msg


def test_the_run_cmd_marker_is_load_bearing_for_terragrunt_output_json(
    tmp_path: Path,
) -> None:
    """The recipe's ``--terragrunt-quiet`` first arg to run_cmd keeps output clean.

    The recipe's run_cmd option documents ``--terragrunt-quiet`` as load-bearing:
    it is the FIRST ARGUMENT TO run_cmd (which consumes it to suppress logging the
    command's output), NOT a terragrunt CLI flag (which does not exist in v1.1.1 -
    a round-trip was spent confusing the two). Without it, run_cmd prepends the
    resolved path to every ``terragrunt output -json`` and the JSON no longer
    parses - the same shape as ``env -u KUBECONFIG`` in the old shim (drop the
    incantation, fail somewhere unrelated).

    Uses ``command -v tofu`` (not the proxy) to isolate run_cmd's marker
    behaviour from tunnelling; the marker is a run_cmd property independent of
    which command it wraps. Needs no cluster - one trivial output + apply.
    """
    require_tools("terragrunt", "tofu")
    repo = tmp_path / "repo"
    repo.mkdir()
    mod = repo / "mod"
    mod.mkdir()
    (mod / "main.tf").write_text('output "x" { value = "hello" }\n')
    env = dict(os.environ)
    env.pop("TUNSTRAP_INPUT", None)

    def write_root(*, with_marker: bool) -> None:
        marker = '"--terragrunt-quiet", ' if with_marker else ""
        (repo / "terragrunt.hcl").write_text(
            'terraform { source = "./mod" }\n'
            f'terraform_binary = run_cmd({marker}"sh", "-c", "command -v tofu")\n'
        )

    # Build state once (with the marker, so apply is clean).
    write_root(with_marker=True)
    applied = subprocess.run(
        ["terragrunt", "apply", "-auto-approve"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert applied.returncode == 0, f"apply failed:\n{applied.stdout}{applied.stderr}"

    # WITH marker: output -json parses to the expected value.
    write_root(with_marker=True)
    with_marker = subprocess.run(
        ["terragrunt", "output", "-json"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert with_marker.returncode == 0, with_marker.stderr
    assert json.loads(with_marker.stdout)["x"]["value"] == "hello"

    # WITHOUT marker: the resolved path is prepended -> the JSON does not parse.
    write_root(with_marker=False)
    no_marker = subprocess.run(
        ["terragrunt", "output", "-json"], cwd=repo, env=env, capture_output=True, text=True
    )
    leak_msg = (
        f"expected the resolved tofu path prepended to stdout without the marker; "
        f"got: {no_marker.stdout[:120]!r}"
    )
    assert no_marker.stdout.splitlines()[0].endswith("/tofu"), leak_msg
    with pytest.raises(json.JSONDecodeError):
        json.loads(no_marker.stdout)


def test_recipe_install_fence_tells_the_consumer_to_install(tmp_path: Path) -> None:
    """The recipe's install fence points at the package, not a copied file.

    Re-aims the drift guard that used to pin the recipe's ```sh tofu-shim```
    snippet byte-identical to tests/e2e/shim/tofu-tunstrap. That shim is retired
    (tunstrap_tofu replaces it), so the pin now guards what the recipe tells a
    consumer to DO to get the proxy: a ```sh install``` fence whose command
    installs the package (yielding both tunstrap and tunstrap_tofu). Fails-when-
    broken: the fence is removed, renamed, or stops installing the package (e.g.
    reverts to a `cp bin/tofu-tunstrap` instruction), and the guard catches it.
    """
    del tmp_path
    blocks = extract_labeled_blocks(RECIPE.read_text())
    missing_install = (
        "recipe is missing its ```sh install``` block - the label that pins the "
        "install instruction is gone (it replaced the retired ```sh tofu-shim```)"
    )
    assert "install" in blocks, missing_install
    install_cmd = blocks["install"]
    uv_msg = f"install fence no longer uses `uv tool install`: {install_cmd!r}"
    assert "uv tool install" in install_cmd, uv_msg
    pkg_msg = f"install fence no longer installs the tunstrap package: {install_cmd!r}"
    assert "tunstrap" in install_cmd, pkg_msg


def test_shell_shim_alt_fence_is_valid_and_bypasses(tmp_path: Path) -> None:
    """The labelled alternative shell shim pastes, parses, and bypasses.

    The recipe's ```sh tofu-shim-alt``` fence is the one artifact a consumer can
    still paste verbatim (the lower-overhead alternative to ``tunstrap_tofu``),
    so it is pinned rather than left unlabelled: ``sh -n`` proves it parses, and a
    bypass smoke test (the shim, a fake ``tofu`` first on PATH, ``TUNSTRAP_INPUT``
    unset) proves the fast-path pass-through actually reaches ``tofu``. Needs no
    cluster - just ``sh`` (always present).
    """
    blocks = extract_labeled_blocks(RECIPE.read_text())
    missing = (
        "recipe is missing its ```sh tofu-shim-alt``` block - the labelled alternative shim is gone"
    )
    assert "tofu-shim-alt" in blocks, missing
    snippet = blocks["tofu-shim-alt"]

    shim = tmp_path / "tofu-tunstrap"
    shim.write_text(snippet)
    shim.chmod(0o755)
    syntax = subprocess.run(["sh", "-n", str(shim)], capture_output=True, text=True)
    assert syntax.returncode == 0, f"tofu-shim-alt fails sh -n: {syntax.stderr}"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "tofu"
    fake.write_text('#!/bin/sh\nprintf "FAKE:%s\\n" "$1"\nexit 0\n')
    fake.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("TUNSTRAP_INPUT", None)

    result = subprocess.run([str(shim), "plan"], env=env, capture_output=True, text=True)
    detail = f"shim-alt bypass smoke failed: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.returncode == 0, detail
    assert result.stdout == "FAKE:plan\n", detail


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
    require_tools("terragrunt")
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
