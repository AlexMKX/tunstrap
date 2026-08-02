"""Real `terragrunt apply`/`destroy`/`output` through the shim, against the cluster.

Code: docs/recipe_terragrunt.md, tests/e2e/shim/tofu-tunstrap, tests/e2e/module/.
Method: stand up a consumer repo whose terragrunt.hcl uses the recipe's pinned
root block (verbatim) plus a copy of its extra_arguments mechanism, then drive
real Terragrunt through it. Read every result through the in-node oracle
(`kubectl_in_node`) and a recording `tofu` - never Terragrunt's own exit code.

Two configurations:
- ``test_terragrunt_apply_destroy_through_the_shim``: the recipe's recommended
  `commands` list (output absent). Drives apply/destroy and asserts the full
  four-row env asymmetry (-version / init / apply+destroy / output) from the
  recording tofu, AFTER destroy so every command's invocation is captured.
- ``test_tunnelled_output_through_tunstrap_run_parses_cleanly``: `output` ADDED
  to `commands` (the worst case) so `terragrunt output -json` runs through
  `tunstrap run`, proving tunstrap run's own stdout survives a real consumer's
  parse - the gap the branch review named. The recipe deliberately OMITS output
  (it reads state, not the cluster); this test proves the purity property under
  the worst case, it does not recommend tunnelled output.
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

# A COPY of the recipe's commands list (docs/recipe_terragrunt.md, terragrunt-unit
# block). Only the root block is extracted; the unit block is hand-copied because
# it also carries the payload, whose values are illustrative k3s defaults
# (root@22, /etc/rancher/k3s/k3s.yaml) that do not match the kind rig. So a recipe
# edit adding/removing a command will NOT drift this test - a known, documented
# limitation, not a silently-assumed equivalence.
RECIPE_COMMANDS = ["plan", "apply", "destroy", "refresh", "import"]


def _consumer_repo(
    tmp_path: Path,
    rig: dict[str, Any],
    module_src: Path,
    shim: Path,
    *,
    commands: list[str],
) -> Path:
    """A consumer working tree whose root block comes from the recipe verbatim.

    The `terragrunt.hcl` is the recipe's pinned **root block** (extracted, so a
    doc edit drifts this test) followed by a copy of the recipe's
    extra_arguments *mechanism* - the same `local.X != "" ? { ... } : {}`
    conditional - carrying a rig-built payload and the supplied `commands` list.

    Scaffolding the recipe does NOT carry, and why it does not need it:
    - the `locals` block. The recipe defers locals to the consumer; this supplies
      the rig's host and a pre-built payload.
    - the payload as a heredoc string rather than the recipe's inline
      `jsonencode({...})`. The recipe's map carries illustrative k3s values; the
      rig is kind, so the payload is built by the rig's own `tunstrap_input_json`
      (the same builder test_tofu_providers uses). The delivery mechanism
      (extra_arguments.env_vars -> TUNSTRAP_INPUT) is the recipe's, unchanged.
    - the module at the repo root (`source = "."`), copied from tests/e2e/module/.

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
    commands_hcl = ", ".join(f'"{c}"' for c in commands)
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
        f"    commands  = [{commands_hcl}]\n"
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
    runs in .terragrunt-cache/<hash>/, not the module copy. TUNSTRAP_INPUT and
    TF_VAR_tunstrap are both popped, so within this env extra_arguments.env_vars
    is the *only* source of TUNSTRAP_INPUT and `tunstrap run --output-var` the
    only source of TF_VAR_tunstrap. The TF_VAR_tunstrap pop makes the
    tunnelled-branch inference hermetic by construction: the value the recorder
    sees in an apply/destroy env could only have been set by tunstrap run, not
    inherited from an ambient export. The ambient KUBECONFIG scrub here is the
    OUTER guard; the shim's `env -u KUBECONFIG` is the inner one - both are
    needed for the routing exclusion to hold.
    """
    env = tofu_env(module, cache)
    env.pop("TF_DATA_DIR", None)
    if recorder_bin is not None:
        env["PATH"] = f"{recorder_bin}:{env['PATH']}"
    env.pop("TUNSTRAP_INPUT", None)
    env.pop("TF_VAR_tunstrap", None)
    return env


def _tg(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run `terragrunt --no-color <args>` in `repo`, captured, never raising."""
    return subprocess.run(
        ["terragrunt", "--no-color", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _invocations_by_command(dump_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Recorded tofu invocations grouped by first argv token, oldest first."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for argv, env_seen in collect_tofu_invocations(dump_dir):
        grouped.setdefault(argv[0], []).append(env_seen)
    return grouped


def _assert_row(
    label: str,
    envs: list[dict[str, str]],
    *,
    has_nonempty: tuple[str, ...] = (),
    lacks: tuple[str, ...] = (),
) -> None:
    """Each recorded invocation of ``label`` carries ``has_nonempty`` (set to a
    non-empty value) and lacks every key in ``lacks``.

    Fails-when-broken per row: an empty `envs` (the command never ran through the
    recording tofu) fails first; a missing key fails naming it; a present
    forbidden key fails naming it. The asymmetry this encodes is the recipe's
    measured behaviour, so a regression in the shim's init/-version bypass or in
    tunstrap run's TUNSTRAP_INPUT scrub surfaces here.
    """
    assert envs, f"no `{label}` invocation was recorded"
    for key in has_nonempty:
        msg = f"`{label}` invocation did not carry non-empty {key}"
        assert all(e.get(key, "") != "" for e in envs), msg
    for key in lacks:
        msg = f"`{label}` invocation carried {key}, which it must not"
        assert all(key not in e for e in envs), msg


def test_terragrunt_apply_destroy_through_the_shim(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
    tmp_path: Path,
) -> None:
    """Apply+destroy through the shim with the recommended commands; full asymmetry.

    `output` is deliberately ABSENT from `commands` (the recipe's
    recommendation: output reads state, not the cluster). So `terragrunt output
    -json` here takes the shim's pass-through branch (line 17, `exec tofu`);
    `tunstrap run` is NOT in that pipeline. The tunnelled-output purity claim has
    its own test below - this one is about apply/destroy and the four-row
    delivery/bypass asymmetry.

    Every env assertion runs AFTER destroy, so destroy's invocation (and every
    prior command's) is in the recorder. Fails-when-broken, per assertion:
    - apply rc 0 + oracle namespace/configmap/release: a broken env_vars delivery
      or --output-var chain drops tofu to the inert branch (127.0.0.1:0); apply
      fails, nothing is created (demonstrated in the no-output-var test below).
    - -version row (neither var): env_vars skips -version and the shim bypasses
      it; if either regressed, -version would carry a var and the row fails.
    - init row (TUNSTRAP_INPUT set, no TF_VAR_tunstrap): env_vars reaches
      auto-init but the shim bypasses init; if the bypass regressed, init would
      carry TF_VAR_tunstrap (a redundant tunnel per plan) and the row fails. The
      row quantifies over every recorded init via ``all()``, so a future
      Terragrunt that auto-inits for a command outside ``commands`` (an init
      without TUNSTRAP_INPUT) would also redden it - a loud signal, not a silent
      pass; both failure shapes are the intended behaviour.
    - apply/destroy rows (TF_VAR_tunstrap set; TUNSTRAP_INPUT and KUBECONFIG
      absent): TUNSTRAP_INPUT absence is the ssh_pkey scrub (recipe:"The input
      variable is scrubbed"); KUBECONFIG absence is the shim's `env -u`. If
      either leaked, the row fails.
    - output row (neither var): output pass-through; if it carried a var, output
      was not pass-through and the row fails.
    - output value == envelope path: a STATE-integrity check (the path tofu
      recorded during apply == the envelope). It does NOT prove routing - that
      exclusion comes from `env -u KUBECONFIG` plus `tofu_env`, not this compare.
    """
    require_tools("terragrunt", "git")
    bin_rec = tmp_path / "bin_rec"
    dump_dir = tmp_path / "dumps"
    write_tofu_recorder(bin_rec, dump_dir)
    repo = _consumer_repo(tmp_path, kube_rig, tofu_module, SHIM, commands=RECIPE_COMMANDS)
    env = _terragrunt_env(tofu_module, tofu_plugin_cache, bin_rec)

    applied = _tg(repo, env, "apply", "-auto-approve")
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

    # output pass-through (output not in commands): run while state exists, assert later.
    out = _tg(repo, env, "output", "-json")
    out_detail = f"terragrunt output -json (pass-through) failed:\n{out.stdout}{out.stderr}"
    assert out.returncode == 0, out_detail

    destroyed = _tg(repo, env, "destroy", "-auto-approve")
    destroy_failed = f"terragrunt destroy failed:\n{destroyed.stdout}{destroyed.stderr}"
    assert destroyed.returncode == 0, destroy_failed
    wait_for_namespace_gone("tunstrap-e2e")

    # --- recorder asymmetry: every command's invocation is now captured ---
    by_cmd = _invocations_by_command(dump_dir)
    _assert_row("-version", by_cmd.get("-version", []), lacks=("TUNSTRAP_INPUT", "TF_VAR_tunstrap"))
    _assert_row(
        "init", by_cmd.get("init", []), has_nonempty=("TUNSTRAP_INPUT",), lacks=("TF_VAR_tunstrap",)
    )
    for cmd in ("apply", "destroy"):
        _assert_row(
            cmd,
            by_cmd.get(cmd, []),
            has_nonempty=("TF_VAR_tunstrap",),
            lacks=("TUNSTRAP_INPUT", "KUBECONFIG"),
        )
    _assert_row("output", by_cmd.get("output", []), lacks=("TUNSTRAP_INPUT", "TF_VAR_tunstrap"))

    # state integrity: the path tofu recorded during apply == the envelope path.
    # Not a routing proof (routing exclusion = env -u KUBECONFIG + tofu_env).
    parsed = json.loads(out.stdout)
    kubepath = parsed["kubepath_used"]["value"]
    envelope = json.loads(by_cmd["apply"][-1]["TF_VAR_tunstrap"])
    expected_path = envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]
    path_detail = f"output kubepath_used={kubepath!r} envelope path={expected_path!r}"
    assert kubepath == expected_path, path_detail


def test_tunnelled_output_through_tunstrap_run_parses_cleanly(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
    tmp_path: Path,
) -> None:
    """`terragrunt output -json` through `tunstrap run` parses cleanly (the claim).

    This is the test the branch review named as missing. With `output` ADDED to
    `commands`, env_vars delivers TUNSTRAP_INPUT for output, the shim routes it
    through `tunstrap run`, and `terragrunt output -json` parses tofu's stdout
    that has traversed tunstrap run. That is the purity risk the shim's `# Never
    write to stdout` comment guards, and it was previously proven only with a
    FAKE tofu under a pass-through shim - the wrong path.

    Framing: tunnelled `output` is the WORST CASE, not a recommendation. The
    recipe omits `output` from `commands` on purpose (output reads state, not the
    cluster; tunnelling it is wasteful). This test forces the worst case to prove
    the purity property holds when tunstrap run IS in the pipeline; it does not
    suggest adding `output` to `commands`.

    Fails-when-broken:
    - `json.loads(out.stdout)`: if `tunstrap run` wrote anything to stdout, the
      JSON would not parse. Proven against a deliberate break below aimed AT this
      claim - a `tunstrap` wrapper on PATH that writes to stdout before exec'ing
      the real binary, so the emission originates at the tunstrap-run step (the
      stream under test), not at the shim. A shim echo would prove only shim-
      author discipline and reuses the old pass-through check's shape; this
      proves the purity property the comment actually guards. Verbatim red in the
      task report.
    - output row carries TF_VAR_tunstrap: confirms the invocation actually went
      through tunstrap run (not pass-through). If `output` were absent from
      commands this would fail - pinning the test to the tunnelled path.
    - the pollution sub-check is DISCRIMINATING: it asserts the marker IS in the
      stream and IS what breaks the parse, so an unrelated failure (empty stdout)
      cannot masquerade as the property under test.
    """
    require_tools("terragrunt", "git")
    bin_rec = tmp_path / "bin_rec"
    dump_dir = tmp_path / "dumps"
    write_tofu_recorder(bin_rec, dump_dir)
    repo = _consumer_repo(
        tmp_path, kube_rig, tofu_module, SHIM, commands=[*RECIPE_COMMANDS, "output"]
    )
    env = _terragrunt_env(tofu_module, tofu_plugin_cache, bin_rec)

    applied = _tg(repo, env, "apply", "-auto-approve")
    apply_failed = f"terragrunt apply failed:\n{applied.stdout}{applied.stderr}"
    assert applied.returncode == 0, apply_failed

    # --- the central claim: tunstrap run's stdout survives a real consumer's parse ---
    out = _tg(repo, env, "output", "-json")
    out_detail = f"terragrunt output -json (tunnelled) failed:\n{out.stdout}{out.stderr}"
    assert out.returncode == 0, out_detail
    parsed = json.loads(out.stdout)
    kubepath = parsed["kubepath_used"]["value"]

    # Confirm it actually traversed tunstrap run (not the pass-through branch):
    # the output invocation must carry TF_VAR_tunstrap. _terragrunt_env pops any
    # ambient TF_VAR_tunstrap (and `tofu_env` never injects it), so within this
    # run the only thing that can set it is `tunstrap run --output-var` - its
    # presence is the tunnelled-branch proof, resting on the env scrub, not on
    # an absolute "only tunstrap run sets it" (a caller could export it).
    by_cmd = _invocations_by_command(dump_dir)
    _assert_row(
        "output",
        by_cmd.get("output", []),
        has_nonempty=("TF_VAR_tunstrap",),
        lacks=("TUNSTRAP_INPUT",),
    )
    # kubepath_used reads STATE (written during apply), so the expected value is
    # the APPLY invocation's envelope path - NOT the output invocation's. Output
    # opens a fresh tunnel with its own session dir, so its path is always a
    # different temp dir; comparing against it would fail on every green run.
    envelope = json.loads(by_cmd["apply"][-1]["TF_VAR_tunstrap"])
    expected_path = envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]
    path_detail = (
        f"tunnelled output kubepath_used={kubepath!r} apply envelope path={expected_path!r}"
    )
    assert kubepath == expected_path, path_detail

    # --- break aimed at THIS claim: `tunstrap run` writes to stdout.
    # The shim execs into `tunstrap run`, so fd 1 of that invocation IS the stream
    # terragrunt parses as tofu's stdout. A shim-echo would prove only shim-author
    # discipline (and reuses the old pass-through check's shape - an inherited
    # break, which the review correctly rejected). To prove the purity property
    # the comment actually guards, wrap `tunstrap` on PATH so the wrapper writes a
    # marker to stdout before exec'ing the real binary: the emission originates at
    # the tunstrap-run step, the exact stream under test. Nothing under tunstrap/
    # or the shim is touched (no in-place edit, so no exec-bit trap to restore).
    real_tunstrap = shutil.which("tunstrap")
    assert real_tunstrap is not None, "e2e_preflight guarantees tunstrap on PATH"
    wrap_dir = tmp_path / "tunstrap_wrap"
    wrap_dir.mkdir()
    marker = "LEAK-FROM-TUNSTRAP-RUN"
    wrapper = wrap_dir / "tunstrap"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = run ]; then printf "%s\\n" "{marker}"; fi\n'
        f'exec "{real_tunstrap}" "$@"\n'
    )
    wrapper.chmod(0o755)
    polluted_env = dict(env)
    polluted_env["PATH"] = f"{wrap_dir}:{env['PATH']}"
    polluted = _tg(repo, polluted_env, "output", "-json")
    # Discriminating: the marker MUST be in the stream and MUST be what breaks the
    # parse, so an unrelated failure (empty stdout) cannot satisfy this check.
    marker_absent = (
        f"pollution marker absent from stdout - the break did not reach the "
        f"stream, so this is not evidence about tunstrap run's stdout purity "
        f"(rc={polluted.returncode}, stdout={polluted.stdout[:200]!r})"
    )
    assert marker in polluted.stdout, marker_absent
    with pytest.raises(json.JSONDecodeError):
        json.loads(polluted.stdout)

    destroyed = _tg(repo, env, "destroy", "-auto-approve")
    destroy_failed = f"terragrunt destroy failed:\n{destroyed.stdout}{destroyed.stderr}"
    assert destroyed.returncode == 0, destroy_failed
    wait_for_namespace_gone("tunstrap-e2e")


def test_terragrunt_apply_without_output_var_fails_through_terragrunt(
    kube_rig: dict[str, Any],
    tofu_module: Path,
    tofu_plugin_cache: Path,
    tmp_path: Path,
) -> None:
    """Negative control for the config_path route (NOT the stdout claim).

    The shim's CONTROL variant opens the tunnel (tunstrap run still runs) but
    never exports TF_VAR_tunstrap, so var.tunstrap keeps its "" default, the
    module takes its inert branch, and the providers dial https://127.0.0.1:0.
    That the apply fails - read through the oracle, not Terragrunt's exit code -
    is what makes the positive apply test's success meaningful.

    This targets the config_path/`--output-var` route. It is NOT the break for
    the stdout-purity claim (that is the pollution sub-check in
    test_tunnelled_output_through_tunstrap_run_parses_cleanly); it overlaps
    test_tofu_providers::test_apply_without_output_var_fails_even_with_the_tunnel_up
    because the config_path route is worth guarding at both the direct-shim and
    Terragrunt-env_vars layers. The cluster is confirmed healthy throughout.
    """
    require_tools("terragrunt", "git")
    repo = _consumer_repo(tmp_path, kube_rig, tofu_module, CONTROL_SHIM, commands=RECIPE_COMMANDS)
    env = _terragrunt_env(tofu_module, tofu_plugin_cache)

    applied = _tg(repo, env, "apply", "-auto-approve")
    combined = applied.stdout + applied.stderr
    success = (
        "apply SUCCEEDED through Terragrunt without --output-var: something other "
        "than the decoded config_path is reaching the cluster, and the positive "
        "test's success proves nothing.\n" + combined
    )
    assert applied.returncode != 0, success
    assert "127.0.0.1:0" in combined, combined

    namespace = kubectl_in_node("get", "namespace", "tunstrap-e2e", "-o", "name")
    assert namespace.returncode != 0
    assert "not found" in namespace.stderr.lower()

    health = kubectl_in_node("get", "--raw", "/healthz")
    assert health.returncode == 0, health.stderr
    assert health.stdout.strip() == "ok"
