"""The ``tunstrap_tofu`` proxy: dispatch, exit codes, and stdout purity.

Code: tunstrap/tofu_proxy.py (the shipped console entry point).
Method: drive the proxy with a fake `tofu` on PATH (in front of the real one),
so the assertions are about the proxy and tunstrap rather than about OpenTofu.

This replaces the consumer shell shim the tier used to drive. The proxy is the
documented path (docs/recipe_terragrunt.md); the shell-shim-specific drift and
textual guards (byte-identity to a recipe snippet, ``env -u KUBECONFIG`` on the
command line) are gone with it. The KUBECONFIG property those guards served is
re-expressed behaviourally in test_terragrunt_apply.py, which records the proxy's
child environment and asserts KUBECONFIG absent — observing the child
environment rather than the command line.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.rig import (
    CONTROL_SHIM,
    TOFU_PROXY,
    recorded_argvs,
    tunstrap_input_json,
    write_fake_tofu,
)

pytestmark = [pytest.mark.e2e]


def test_control_shim_is_executable_posix_sh() -> None:
    """The negative control is mode 0755 and declares /bin/sh (a file terraform_binary probes)."""
    assert CONTROL_SHIM.is_file(), CONTROL_SHIM
    assert CONTROL_SHIM.stat().st_mode & 0o777 == 0o755, CONTROL_SHIM
    assert CONTROL_SHIM.read_text().startswith("#!/bin/sh\n"), CONTROL_SHIM


def _proxy_env(bin_dir: Path) -> dict[str, str]:
    """Parent environment with the fake tofu first on PATH and no stale payload.

    ``tunstrap_tofu`` itself resolves further down PATH (the editable-install
    venv); the fake tofu shadows it only for the proxy's own ``tofu`` exec.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("TUNSTRAP_INPUT", None)
    env.pop("KUBECONFIG", None)
    return env


def _run_proxy(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the installed ``tunstrap_tofu`` (PATH-resolved) with capture."""
    return subprocess.run([TOFU_PROXY, *argv], env=env, capture_output=True, text=True, check=False)


def test_init_passes_through_even_with_a_poisoned_payload(
    e2e_preflight: None, tmp_path: Path
) -> None:
    """`init` never reads TUNSTRAP_INPUT, so an invalid one cannot stop it.

    Load-bearing: Terragrunt's env_vars reaches the auto-init for a tunnelled
    command, so the proxy MUST bypass init to avoid a redundant tunnel per plan.
    """
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _proxy_env(bin_dir)
    # Deliberately invalid *and* non-empty: any accidental `tunstrap run` exits 1
    # with SchemaValidationError, pre-spawn, and tofu never launches.
    env["TUNSTRAP_INPUT"] = "{invalid"

    result = _run_proxy(["init", "-input=false"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded_argvs(marker_dir) == [["init", "-input=false"]]


def test_version_passes_through_even_with_a_poisoned_payload(
    e2e_preflight: None, tmp_path: Path
) -> None:
    """`-version` takes the same bypass; Terragrunt probes it once per run."""
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _proxy_env(bin_dir)
    env["TUNSTRAP_INPUT"] = "{invalid"

    result = _run_proxy(["-version"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded_argvs(marker_dir) == [["-version"]]


def test_chdir_init_passes_through_the_fixed_gap(e2e_preflight: None, tmp_path: Path) -> None:
    """`tofu -chdir=DIR init` bypasses — the gap the shell shim could not close.

    The shell shim's ``case "$1"`` saw ``-chdir=DIR`` as the first token and so
    built a needless tunnel for ``-chdir`` inits. The proxy parses argv past
    global flags, so ``init`` is correctly identified as the subcommand. This is
    the e2e expression of the fix; the bypass set is pinned exhaustively in
    tests/unit/test_tofu_proxy.py::test_should_bypass_returns_true_for_the_pinned_bypass_set.
    """
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _proxy_env(bin_dir)
    env["TUNSTRAP_INPUT"] = "{invalid"

    # Both = and space forms of -chdir must reach the init bypass.
    for argv in (["-chdir=somewhere", "init"], ["-chdir", "somewhere", "init"]):
        result = _run_proxy(argv, env)
        assert result.returncode == 0, f"{argv}: {result.stdout}{result.stderr}"
        assert recorded_argvs(marker_dir) == [argv], f"{argv} did not reach tofu verbatim"
        # Clear markers between forms so the next iteration's recorded_argvs is unambiguous.
        for m in marker_dir.glob("*.argv"):
            m.unlink()


def test_unset_payload_passes_through(e2e_preflight: None, tmp_path: Path) -> None:
    """With TUNSTRAP_INPUT unset the proxy is a transparent exec to tofu."""
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _proxy_env(bin_dir)
    assert "TUNSTRAP_INPUT" not in env

    result = _run_proxy(["plan"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "FAKE_TOFU_RAN\n"
    assert recorded_argvs(marker_dir) == [["plan"]]


def test_child_exit_code_propagates_verbatim(kube_rig: dict[str, Any], tmp_path: Path) -> None:
    """A child exiting 42 makes the proxy exit 42, and the child provably ran."""
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    # 42 is outside tunstrap's reserved set (1, 2, 3, 4, 64), distinct from the
    # launch-failure code 127, and below the 128+N band _run_child maps a
    # signalled child into - so no tunstrap path can produce it by accident.
    write_fake_tofu(bin_dir, marker_dir, exit_code=42, stdout_line="FAKE_TOFU_EXIT_42")
    env = _proxy_env(bin_dir)
    env["TUNSTRAP_INPUT"] = tunstrap_input_json(kube_rig)

    result = _run_proxy(["apply"], env)
    detail = f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.returncode == 42, detail
    assert result.stdout == "FAKE_TOFU_EXIT_42\n"
    assert recorded_argvs(marker_dir) == [["apply"]]


def test_tunnelled_stdout_is_byte_identical_to_the_untunnelled_child(
    kube_rig: dict[str, Any], tmp_path: Path
) -> None:
    """Under the proxy, fd 1 belongs to tofu and to nothing else."""
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_STDOUT_SENTINEL")
    base = _proxy_env(bin_dir)

    # Oracle: the same proxy, the same fake tofu, the same argv - but with
    # TUNSTRAP_INPUT unset, so the proxy's first branch execs straight into the
    # child and tunstrap is not in the picture at all.
    direct = _run_proxy(["apply"], base)
    tunnelled = _run_proxy(["apply"], env={**base, "TUNSTRAP_INPUT": tunstrap_input_json(kube_rig)})

    assert direct.returncode == 0, direct.stderr
    assert tunnelled.returncode == 0, tunnelled.stderr
    # Pin the oracle itself, so "both produced nothing" cannot pass.
    assert direct.stdout == "FAKE_TOFU_STDOUT_SENTINEL\n"
    assert tunnelled.stdout == direct.stdout
    assert recorded_argvs(marker_dir) == [["apply"], ["apply"]]
