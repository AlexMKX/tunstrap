"""The consumer-facing tofu shim: dispatch, exit codes, and stdout purity.

Code: tests/e2e/shim/tofu-tunstrap (and its negative control).
Method: drive the shim with a fake `tofu` on PATH, so the assertions are about
the shim and tunstrap rather than about OpenTofu.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.rig import CONTROL_SHIM, SHIM, recorded_argvs, write_fake_tofu

pytestmark = [pytest.mark.e2e]


def _shim_body(path: Path) -> str:
    """The shim's executable lines, with comments and blank lines removed."""
    lines = [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


def test_both_shims_are_executable_posix_sh() -> None:
    """Each shim is mode 0755 and declares /bin/sh, so `exec` semantics hold."""
    for path in (SHIM, CONTROL_SHIM):
        assert path.is_file(), path
        assert path.stat().st_mode & 0o777 == 0o755, path
        assert path.read_text().startswith("#!/bin/sh\n"), path


def test_control_shim_differs_only_in_output_var() -> None:
    """The negative control is the real shim minus --output-var, and nothing else."""
    real = _shim_body(SHIM)
    control = _shim_body(CONTROL_SHIM)
    assert real != control
    assert real.replace(" --output-var TF_VAR_tunstrap", "") == control


def test_shim_uses_the_documented_invocation() -> None:
    """The shim runs the exact command the spec designs, including `env -u`."""
    real = _shim_body(SHIM)
    assert "tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap" in real
    assert '-- env -u KUBECONFIG tofu "$@"' in real
    assert 'case "$1" in init|-version) exec tofu "$@" ;; esac' in real
    assert '[ -n "$TUNSTRAP_INPUT" ] || exec tofu "$@"' in real


def _shim_env(bin_dir: Path) -> dict[str, str]:
    """Parent environment with the fake tofu first on PATH and no stale payload."""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("TUNSTRAP_INPUT", None)
    env.pop("KUBECONFIG", None)
    return env


def test_init_passes_through_even_with_a_poisoned_payload(
    e2e_preflight: None, tmp_path: Path
) -> None:
    """`init` never reads TUNSTRAP_INPUT, so an invalid one cannot stop it."""
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _shim_env(bin_dir)
    # Deliberately invalid *and* non-empty: any accidental `tunstrap run` exits 1
    # with SchemaValidationError, pre-spawn, and tofu never launches.
    env["TUNSTRAP_INPUT"] = "{invalid"

    result = subprocess.run(
        [str(SHIM), "init", "-input=false"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded_argvs(marker_dir) == [["init", "-input=false"]]


def test_version_passes_through_even_with_a_poisoned_payload(
    e2e_preflight: None, tmp_path: Path
) -> None:
    """`-version` takes the same skip; Terragrunt probes it once per run."""
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _shim_env(bin_dir)
    env["TUNSTRAP_INPUT"] = "{invalid"

    result = subprocess.run(
        [str(SHIM), "-version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded_argvs(marker_dir) == [["-version"]]


def test_unset_payload_passes_through(e2e_preflight: None, tmp_path: Path) -> None:
    """With TUNSTRAP_INPUT unset the shim is a transparent exec to tofu."""
    del e2e_preflight
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line="FAKE_TOFU_RAN")
    env = _shim_env(bin_dir)
    assert "TUNSTRAP_INPUT" not in env

    result = subprocess.run(
        [str(SHIM), "plan"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "FAKE_TOFU_RAN\n"
    assert recorded_argvs(marker_dir) == [["plan"]]
