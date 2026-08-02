"""Real `terragrunt apply`/`destroy` through the shim, against the live cluster.

Code: docs/recipe_terragrunt.md (the pinned config), tests/e2e/shim/tofu-tunstrap,
tests/e2e/module/.
Method: stand up a faithful consumer repo whose terragrunt.hcl is built from the
recipe's own pinned root block (verbatim) plus the recipe's extra_arguments
mechanism, then drive a real `terragrunt apply`/`destroy` through it. Read every
result back through the in-node oracle (`kubectl_in_node`) - never through
Terragrunt's own success report - and through a recording `tofu` that captures
the environment each invocation actually saw.

This is the test the branch's final review named as missing: no test exercised a
real Terragrunt consumer parsing the stream. test_tofu_providers proves the
chain with the shim called directly; this proves it with real Terragrunt setting
TUNSTRAP_INPUT via extra_arguments.env_vars, probing -version, auto-running init,
and reading `terragrunt output -json` back through the whole chain.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.rig import (
    CONTROL_SHIM,
    RECIPE_MD,
    SHIM,
    collect_tofu_invocations,
    extract_labeled_blocks,
    kubectl_in_node,
    require_tools,
    tofu_env,
    tunstrap_input_json,
    wait_for_namespace_gone,
    write_tofu_recorder,
)

pytestmark = [pytest.mark.e2e]


def _consumer_repo(tmp_path: Path, rig: dict[str, Any], module_src: Path, shim: Path) -> Path:
    """A consumer working tree whose config comes from the recipe.

    The `terragrunt.hcl` is the recipe's pinned **root block verbatim**
    (extracted straight out of docs/recipe_terragrunt.md, so a doc edit drifts
    this test with it) followed by the recipe's extra_arguments *mechanism* -
    same `commands` list, same `local.X != "" ? { TUNSTRAP_INPUT = ... } : {}`
    conditional - carrying a rig-built payload.

    Scaffolding the recipe does NOT carry, and why it does not need it:
    - the `locals` block. The recipe defers locals to the consumer ("Build
      local.cluster_host / local.ssh_private_key from whatever your source of
      truth is"); this one supplies the rig's host and a pre-built payload.
    - the payload as a heredoc string rather than the recipe's inline
      `jsonencode({...})` HCL map. The recipe's map carries illustrative k3s
      values (root@22, /etc/rancher/k3s/k3s.yaml); the rig is kind
      (tester@<random port>, /etc/kube/admin.conf), so the payload is built by
      the rig's own `tunstrap_input_json` - the same builder test_tofu_providers
      uses, guaranteeing the two tiers agree. The delivery mechanism
      (extra_arguments.env_vars -> TUNSTRAP_INPUT) is the recipe's, unchanged.
    - the module at the repo root (`source = "."`). Copied from
      tests/e2e/module/, the same module the rest of the tier drives.

    git-init'd so the recipe's ${get_repo_root()} resolves to this directory.
    """
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    shutil.copy2(shim, bin_dir / "tofu-tunstrap")
    os.chmod(bin_dir / "tofu-tunstrap", 0o755)
    for entry in module_src.iterdir():
        dst = repo / entry.name
        if dst.exists():
            continue
        if entry.is_dir():
            shutil.copytree(entry, dst)
        else:
            shutil.copy2(entry, dst)

    root_block = extract_labeled_blocks(RECIPE_MD.read_text())["terragrunt-root"]
    payload = tunstrap_input_json(rig)
    unit_block = (
        "locals {\n"
        f'  cluster_host        = "{rig["host"]}"\n'
        "  tunstrap_input_json = <<EOT\n"
        f"{payload}\n"
        "EOT\n"
        "}\n\n"
        "terraform {\n"
        '  source = "."\n\n'
        '  extra_arguments "tunstrap" {\n'
        '    commands  = ["plan", "apply", "destroy", "refresh", "import"]\n'
        "    arguments = []\n"
        '    env_vars = local.cluster_host != "" ? {\n'
        "      TUNSTRAP_INPUT = local.tunstrap_input_json\n"
        "    } : {}\n"
        "  }\n"
        "}\n"
    )
    (repo / "terragrunt.hcl").write_text(f"{root_block}\n\n{unit_block}")
    return repo


def _terragrunt_env(module: Path, cache: Path, recorder_bin: Path | None = None) -> dict[str, str]:
    """A hermetic env for a terragrunt process, with an optional recording tofu.

    `tofu_env` scrubs KUBECONFIG and redirects HOME (the silent-pass guards the
    whole tier depends on); TF_DATA_DIR is dropped because under Terragrunt tofu
    runs in .terragrunt-cache/<hash>/, not the module copy. TUNSTRAP_INPUT is
    absent so extra_arguments.env_vars is the *only* source of it - exactly the
    recipe's mechanism. ``recorder_bin`` is prepended to PATH when the test needs
    to capture what each tofu invocation saw; the negative control passes None.
    """
    env = tofu_env(module, cache)
    env.pop("TF_DATA_DIR", None)
    if recorder_bin is not None:
        env["PATH"] = f"{recorder_bin}:{env['PATH']}"
    env.pop("TUNSTRAP_INPUT", None)
    return env


def _invocations_by_command(dump_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Recorded tofu invocations grouped by first argv token, oldest first."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for argv, env_seen in collect_tofu_invocations(dump_dir):
        grouped.setdefault(argv[0], []).append(env_seen)
    return grouped


def test_terragrunt_apply_destroy_through_the_shim(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
    tmp_path: Path,
) -> None:
    """Real Terragrunt apply+destroy through the shim, verified by the oracle.

    The recipe's central claim end-to-end: extra_arguments.env_vars delivers
    TUNSTRAP_INPUT -> the shim opens a tunnel via tunstrap run -> tofu reaches
    the cluster through the decoded config_path -> `terragrunt output -json`
    reads the result back. Every positive check is read through the in-node
    oracle or the recording tofu, never Terragrunt's own exit code alone.

    Fails-when-broken, per assertion:
    - apply rc 0 + oracle namespace/configmap/release: if env_vars delivery or
      the shim's --output-var chain breaks, tofu takes the inert branch
      (https://127.0.0.1:0), apply fails and nothing is created. (Demonstrated
      verbatim against the no-output-var control in the companion test below.)
    - apply env has TF_VAR_tunstrap; init/-version do NOT: TF_VAR_tunstrap is
      set only by `tunstrap run --output-var`, which the shim calls only for
      non-init/non-version commands. If the bypass regressed, init/-version
      would carry TF_VAR_tunstrap and this fails - and the tier would pay a
      redundant tunnel per init.
    - `terragrunt output -json` value == envelope path: if tofu reached the
      cluster any other way (an ambient KUBECONFIG, a hardcoded path), the value
      would not equal the tunnel-materialized path. This is the exact
      chain-integrity assertion that once caught a break the apply-success check
      missed.
    - stdout-pollution sub-check: a shim that writes to stdout on the output
      path makes `terragrunt output -json` unparseable - the literal claim
      "Terragrunt parses the shim's stdout correctly".
    - destroy + namespace-gone: if destroy skipped the tunnel (or the chain),
      resources would survive and wait_for_namespace_gone times out.
    """
    require_tools("terragrunt", "git")
    bin_rec = tmp_path / "bin_rec"
    dump_dir = tmp_path / "dumps"
    write_tofu_recorder(bin_rec, dump_dir)
    repo = _consumer_repo(tmp_path, kube_rig, tofu_module, SHIM)
    env = _terragrunt_env(tofu_module, tofu_plugin_cache, bin_rec)

    # --- assertion 1: apply through Terragrunt, through the shim, through the tunnel ---
    applied = subprocess.run(
        ["terragrunt", "--no-color", "apply", "-auto-approve"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    apply_failed = f"terragrunt apply failed:\n{applied.stdout}{applied.stderr}"
    assert applied.returncode == 0, apply_failed

    namespace = kubectl_in_node(
        "get", "namespace", "tunstrap-e2e", "-o", "jsonpath={.metadata.name}"
    )
    ns_detail = f"rc={namespace.returncode} stdout={namespace.stdout!r} stderr={namespace.stderr!r}"
    assert namespace.returncode == 0, ns_detail
    assert namespace.stdout == "tunstrap-e2e", ns_detail

    configmap = kubectl_in_node(
        "get", "configmap", "probe-cm", "-n", "tunstrap-e2e", "-o", "jsonpath={.data.proof}"
    )
    assert configmap.returncode == 0, configmap.stderr
    assert configmap.stdout == "through-the-tunnel", configmap.stdout

    release = kubectl_in_node(
        "get",
        "secret",
        "sh.helm.release.v1.probe.v1",
        "-n",
        "tunstrap-e2e",
        "-o",
        "jsonpath={.metadata.name}",
    )
    assert release.returncode == 0, release.stderr
    assert release.stdout == "sh.helm.release.v1.probe.v1", release.stdout

    # --- assertions 2 + 3: what the real invocations saw (delivery + bypass) ---
    by_cmd = _invocations_by_command(dump_dir)

    apply_envs = by_cmd.get("apply", [])
    assert apply_envs, "no `tofu apply` invocation was recorded"
    # tunstrap run opened the tunnel and projected the envelope onto TF_VAR_tunstrap
    assert all("TF_VAR_tunstrap" in e for e in apply_envs), "apply ran without TF_VAR_tunstrap"
    assert all("KUBECONFIG" not in e for e in apply_envs), "KUBECONFIG leaked into the tofu child"

    init_envs = by_cmd.get("init", [])
    assert init_envs, "no `tofu init` invocation was recorded"
    # env_vars reaches the automatic init (TUNSTRAP_INPUT is set) ...
    init_input_msg = "auto-init did not receive TUNSTRAP_INPUT - env_vars is not reaching init"
    assert all(e.get("TUNSTRAP_INPUT", "") != "" for e in init_envs), init_input_msg
    # ... but the shim bypassed it: no tunstrap run, so no TF_VAR_tunstrap, no tunnel
    init_tunnel_msg = "init built a tunnel (TF_VAR_tunstrap present) - the init bypass regressed"
    assert all("TF_VAR_tunstrap" not in e for e in init_envs), init_tunnel_msg

    version_envs = by_cmd.get("-version", [])
    assert version_envs, "no `-version` probe was recorded"
    # the probe is the one path env_vars does NOT reach, and the shim bypasses it too
    version_input_msg = (
        "the -version probe saw TUNSTRAP_INPUT - it must not (measured: env_vars "
        "skips -version; the probe fires ~50ms before any hook)"
    )
    assert all("TUNSTRAP_INPUT" not in e for e in version_envs), version_input_msg
    version_tunnel_msg = "the -version probe built a tunnel - the -version bypass regressed"
    assert all("TF_VAR_tunstrap" not in e for e in version_envs), version_tunnel_msg

    # --- assertion 5: terragrunt output -json reads the value through the chain ---
    out = subprocess.run(
        ["terragrunt", "--no-color", "output", "-json"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out_detail = f"terragrunt output -json failed:\n{out.stdout}{out.stderr}"
    assert out.returncode == 0, out_detail
    # json.loads is the load-bearing assertion: it proves the shim left stdout
    # clean enough for a real consumer to parse (every prior purity test used a
    # FAKE tofu; this is the first one through real tofu + real Terragrunt).
    parsed = json.loads(out.stdout)
    kubepath = parsed["kubepath_used"]["value"]
    # chain integrity: the path tofu used == the path tunstrap projected
    envelope = json.loads(apply_envs[-1]["TF_VAR_tunstrap"])
    expected_path = envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]
    path_detail = f"output kubepath_used={kubepath!r} envelope path={expected_path!r}"
    assert kubepath == expected_path, path_detail

    # assertion 5 break: a shim that writes to stdout on the output path breaks
    # the parse. Only `output` is polluted so TG's own -version probe still works.
    shim_path = repo / "bin" / "tofu-tunstrap"
    clean_shim = shim_path.read_text()
    shebang, rest = clean_shim.split("\n", 1)
    pollution = "case $1 in output) echo POLLUTION-ON-STDOUT >&1 ;; esac"
    shim_path.write_text(f"{shebang}\n{pollution}\n{rest}")
    try:
        polluted_out = subprocess.run(
            ["terragrunt", "--no-color", "output", "-json"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(polluted_out.stdout)
    finally:
        shim_path.write_text(clean_shim)
        os.chmod(shim_path, 0o755)

    # --- assertion 4: destroy through Terragrunt removes everything ---
    destroyed = subprocess.run(
        ["terragrunt", "--no-color", "destroy", "-auto-approve"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    destroy_failed = f"terragrunt destroy failed:\n{destroyed.stdout}{destroyed.stderr}"
    assert destroyed.returncode == 0, destroy_failed
    wait_for_namespace_gone("tunstrap-e2e")


def test_terragrunt_apply_without_output_var_fails_through_terragrunt(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
    tmp_path: Path,
) -> None:
    """Deliberate break: drop --output-var, and the apply must fail through TG.

    The companion negative control. The shim's CONTROL variant opens the tunnel
    (tunstrap run still runs) but never exports TF_VAR_tunstrap, so var.tunstrap
    keeps its "" default, the module takes its inert branch, and the providers
    dial https://127.0.0.1:0. That the apply fails - read through the oracle,
    not Terragrunt's exit code - is what makes the positive test's success
    meaningful: without --output-var the chain is incomplete and no resource is
    created.

    The cluster is confirmed healthy throughout, so the failure is attributable
    to the missing variable rather than to a dead rig. Mirrors
    test_tofu_providers::test_apply_without_output_var_fails_even_with_the_tunnel_up
    but routed through real Terragrunt's env_vars path.
    """
    require_tools("terragrunt", "git")
    repo = _consumer_repo(tmp_path, kube_rig, tofu_module, CONTROL_SHIM)
    env = _terragrunt_env(tofu_module, tofu_plugin_cache)

    applied = subprocess.run(
        ["terragrunt", "--no-color", "apply", "-auto-approve"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = applied.stdout + applied.stderr
    success = (
        "apply SUCCEEDED through Terragrunt without --output-var: something other "
        "than the decoded config_path is reaching the cluster, and the positive "
        "test's success proves nothing.\n" + combined
    )
    assert applied.returncode != 0, success
    assert "127.0.0.1:0" in combined, combined

    # Nothing was created.
    namespace = kubectl_in_node("get", "namespace", "tunstrap-e2e", "-o", "name")
    assert namespace.returncode != 0
    assert "not found" in namespace.stderr.lower()

    # The cluster was alive the whole time, so the failure is the missing
    # variable, not a dead rig.
    health = kubectl_in_node("get", "--raw", "/healthz")
    assert health.returncode == 0, health.stderr
    assert health.stdout.strip() == "ok"
