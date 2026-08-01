"""OpenTofu's kubernetes and helm providers, driven through a tunstrap tunnel.

Code: tests/e2e/module/, tests/e2e/shim/tofu-tunstrap.
Method: run the real shim against a per-test copy of the module and a real kind
cluster; read results back through an oracle that does not use the tunnel.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.rig import (
    CONTROL_SHIM,
    SHIM,
    collect_tofu_invocations,
    kubectl_in_node,
    tofu_env,
    tunstrap_input_json,
    wait_for_namespace_gone,
    write_tofu_recorder,
)

pytestmark = [pytest.mark.e2e]


def test_plan_succeeds_with_the_inert_branch(tofu_module: Path, tofu_plugin_cache: Path) -> None:
    """With TF_VAR_tunstrap unset the module still plans: try(jsondecode()) holds."""
    env = tofu_env(tofu_module, tofu_plugin_cache)
    assert "TF_VAR_tunstrap" not in env

    init = subprocess.run(
        ["tofu", "init", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, init.stdout + init.stderr

    planned = subprocess.run(
        ["tofu", "plan", "-input=false", "-refresh=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "2 to add" in planned.stdout

    # Exit 0 and "2 to add" both hold for a module that quietly reached a real
    # cluster - they say the plan happened, not that it was *inert*. The output
    # is what separates the two: local.kubepath is "" only when the try() chain
    # found no path, which is the state the Task 4.2 negative control depends
    # on. Without this line a module that had silently picked up an ambient
    # kubeconfig would sail through.
    assert 'kubepath_used = ""' in planned.stdout


def test_apply_creates_real_objects_through_the_tunnel_and_destroy_removes_them(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
    tmp_path: Path,
) -> None:
    """The whole chain: --output-var -> jsondecode -> config_path -> real objects."""
    bin_dir = tmp_path / "bin"
    dump_dir = tmp_path / "dumps"
    write_tofu_recorder(bin_dir, dump_dir)
    env = tofu_env(
        tofu_module,
        tofu_plugin_cache,
        extra={
            "TUNSTRAP_INPUT": tunstrap_input_json(kube_rig),
            "PATH": f"{bin_dir}:{tofu_env(tofu_module, tofu_plugin_cache)['PATH']}",
        },
    )

    init = subprocess.run(
        [str(SHIM), "init", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, init.stdout + init.stderr

    applied = subprocess.run(
        [str(SHIM), "apply", "-auto-approve", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "Apply complete!" in applied.stdout

    # --- assertions 1, 2, 3: read back through the oracle that never tunnels ---
    namespace = kubectl_in_node(
        "get", "namespace", "tunstrap-e2e", "-o", "jsonpath={.metadata.name}"
    )
    assert namespace.returncode == 0, namespace.stderr
    assert namespace.stdout == "tunstrap-e2e"

    configmap = kubectl_in_node(
        "get",
        "configmap",
        "probe-cm",
        "-n",
        "tunstrap-e2e",
        "-o",
        "jsonpath={.data.proof}",
    )
    assert configmap.returncode == 0, configmap.stderr
    assert configmap.stdout == "through-the-tunnel"

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
    assert release.stdout == "sh.helm.release.v1.probe.v1"

    # --- assertion 4a + 7: what the real invocations actually saw ---
    invocations = collect_tofu_invocations(dump_dir)
    by_command = {argv[0]: env_seen for argv, env_seen in invocations}
    assert sorted(by_command) == ["apply", "init"]

    init_env = by_command["init"]
    assert init_env["TUNSTRAP_INPUT"] != ""
    assert "TF_VAR_tunstrap" not in init_env

    apply_env = by_command["apply"]
    assert "KUBECONFIG" not in apply_env
    assert "TF_VAR_tunstrap" in apply_env

    # --- assertion 4c: the path the module used is the path the envelope gave ---
    envelope = json.loads(apply_env["TF_VAR_tunstrap"])
    expected_path = envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]
    assert expected_path
    state = json.loads((tofu_module / "terraform.tfstate").read_text())
    assert state["outputs"]["kubepath_used"]["value"] == expected_path

    # --- assertion 5: destroy really removes them ---
    destroyed = subprocess.run(
        [str(SHIM), "destroy", "-auto-approve", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert destroyed.returncode == 0, destroyed.stdout + destroyed.stderr
    assert "Destroy complete!" in destroyed.stdout
    wait_for_namespace_gone("tunstrap-e2e")


def test_apply_without_output_var_fails_even_with_the_tunnel_up(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
) -> None:
    """Negative control: the decoded config_path is the ONLY route to the cluster."""
    env = tofu_env(
        tofu_module,
        tofu_plugin_cache,
        extra={"TUNSTRAP_INPUT": tunstrap_input_json(kube_rig)},
    )

    init = subprocess.run(
        [str(CONTROL_SHIM), "init", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, init.stdout + init.stderr

    applied = subprocess.run(
        [str(CONTROL_SHIM), "apply", "-auto-approve", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = applied.stdout + applied.stderr
    success_msg = (
        "apply SUCCEEDED without TF_VAR_tunstrap: something other than the "
        "decoded config_path is reaching the cluster, and every positive "
        "assertion in this tier proves nothing.\n" + combined
    )
    assert applied.returncode != 0, success_msg
    assert "127.0.0.1:0" in combined, combined

    # Nothing was created.
    namespace = kubectl_in_node("get", "namespace", "tunstrap-e2e", "-o", "name")
    assert namespace.returncode != 0
    assert "not found" in namespace.stderr.lower()

    # The cluster was alive the whole time, so the failure above is
    # attributable to the missing variable rather than to a dead rig.
    health = kubectl_in_node("get", "--raw", "/healthz")
    assert health.returncode == 0, health.stderr
    assert health.stdout.strip() == "ok"
