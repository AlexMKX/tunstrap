"""Constants and helpers shared by the e2e tests.

A plain module rather than a conftest, so test modules can import these by name
without depending on how pytest loaded the conftest. Fixtures live in
``conftest.py``; everything importable lives here.

Self-contained by construction. This suite generates its own Ed25519 keypair
into ``tests/e2e/_keys/`` and commits its own sshd drop-in under
``tests/e2e/_sshd_conf/``. It reaches into no other suite: the integration rig's
``_keys/`` directory is gitignored and is created only as a side effect of a
fixture pytest never loads here, so borrowing it would work on a developer
machine and fail in a clean CI checkout.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn

import pytest

HERE = Path(__file__).resolve().parent

CLUSTER_NAME = "tunstrap-e2e"
# kind derives the container name from the cluster name. That name is both the
# SSH forward target and the expected tls_server_name, which is why the cluster
# name is fixed rather than randomised.
CONTROL_PLANE = f"{CLUSTER_NAME}-control-plane"
NODE_IMAGE = "kindest/node:v1.34.0"
COMPOSE_FILE = HERE / "docker-compose.yml"
IN_NODE_KUBECONFIG = "/etc/kube/admin.conf"
SHIM = HERE / "shim" / "tofu-tunstrap"
CONTROL_SHIM = HERE / "shim" / "tofu-tunstrap-novar"


def skip_or_fail(reason: str) -> NoReturn:
    """Skip locally, fail when TUNSTRAP_E2E_REQUIRE_ALL=1.

    A missing external tool is an environment fact, not a product failure, so on
    a workstation it is a skip naming the tool. In CI the job installs every
    tool itself, so a skip there means the job reports green while most of the
    tier never ran - which is exactly how a cluster tier rots into decoration.
    The e2e job sets TUNSTRAP_E2E_REQUIRE_ALL=1, which turns every such skip
    into a failure.
    """
    if os.environ.get("TUNSTRAP_E2E_REQUIRE_ALL") == "1":
        pytest.fail(reason + " [TUNSTRAP_E2E_REQUIRE_ALL=1: skipping is not allowed here]")
    pytest.skip(reason)


def require_tools(*names: str) -> None:
    """Skip (or, in CI, fail) unless every named binary is on PATH.

    ``tunstrap`` is deliberately not handled here - see ``e2e_preflight`` in
    conftest.py, where it is always a failure.
    """
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        skip_or_fail("e2e tier requires " + ", ".join(missing) + " on PATH")


def kubectl_in_node(*args: str) -> subprocess.CompletedProcess[str]:
    """Run kubectl *inside* the kind control-plane container.

    Deliberately an independent oracle. It does not traverse the tunnel, so a
    broken tunnel can never make a read-back appear to succeed, and it uses the
    version-matched kubectl shipped in the node image rather than whatever the
    host happens to have installed.
    """
    return subprocess.run(
        [
            "docker",
            "exec",
            CONTROL_PLANE,
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def tunstrap_input_json(rig: dict[str, Any], *, materialize: bool | None = None) -> str:
    """The InputSchema the shim reads from TUNSTRAP_INPUT.

    The node key is ``node`` and the kube-target key is ``k3s`` because
    ``module/main.tf`` decodes ``connections.node.kube_targets.k3s.path``.

    ``materialize`` is omitted by default, on purpose. ``run`` forces
    ``daemon.materialize = True`` on an --input-env payload ("the one place run
    mutates the supplied schema"), so leaving it out keeps that invariant
    load-bearing for the whole tier: if the forcing were ever removed, ``path``
    would come back null, ``config_path`` would be empty, and every provider
    test would fail. ``start`` does *not* force it, so the one test that drives
    ``start`` directly passes ``materialize=True`` explicitly.
    """
    daemon: dict[str, Any] = {"auto_stop_idle_seconds": 300}
    if materialize is not None:
        daemon["materialize"] = materialize
    return json.dumps(
        {
            "nodes": {
                "node": {
                    "host": rig["host"],
                    "port": rig["port"],
                    "user": rig["user"],
                    "ssh_pkey": rig["private_pem"],
                    "kube_targets": {"k3s": {"kubeconfig_path": rig["kubeconfig_in_node_path"]}},
                }
            },
            "daemon": daemon,
        }
    )


def tofu_env(
    module_dir: Path,
    cache: Path,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """A hermetic environment for one tofu invocation.

    KUBECONFIG is removed and HOME is redirected to a scratch directory on
    purpose: an ambient kubeconfig, or an operator's ~/.kube/config, would let a
    broken TF_VAR_tunstrap -> config_path chain still reach a cluster. That is
    the silent pass this whole tier exists to prevent. This scrubs the *parent*
    environment; the shim's own `env -u KUBECONFIG` scrubs the one `run` injects.
    """
    env = dict(os.environ)
    env.pop("KUBECONFIG", None)
    env.pop("TUNSTRAP_INPUT", None)
    scratch_home = module_dir.parent / "home"
    scratch_home.mkdir(exist_ok=True)
    env["HOME"] = str(scratch_home)
    env["TF_DATA_DIR"] = str(module_dir / ".terraform")
    env["TF_PLUGIN_CACHE_DIR"] = str(cache)
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    if extra:
        env.update(extra)
    return env


def write_tofu_recorder(bin_dir: Path, dump_dir: Path) -> Path:
    """Install a `tofu` on PATH that records argv + env, then execs the real one.

    Recording *and* exec'ing - rather than faking - means the environment the
    test asserts on is the environment of the invocation that actually talked to
    the cluster, not a stand-in for it.

    `env -0` is used rather than `env` because NUL separation is unambiguous for
    values containing newlines; a line-oriented dump could be misread.
    """
    real = shutil.which("tofu")
    if real is None:  # pragma: no cover - require_tools ran first
        pytest.skip("e2e tier requires tofu on PATH")
    bin_dir.mkdir(parents=True, exist_ok=True)
    dump_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "tofu"
    script.write_text(
        "#!/bin/sh\n"
        f'dump="{dump_dir}/$$"\n'
        'printf "%s\\n" "$@" > "$dump.argv"\n'
        'env -0 > "$dump.env0"\n'
        f'exec "{real}" "$@"\n'
    )
    script.chmod(0o755)
    return script


def write_fake_tofu(
    bin_dir: Path,
    marker_dir: Path,
    *,
    exit_code: int,
    stdout_line: str,
) -> Path:
    """Install a fake `tofu`: record argv, print one fixed line, exit `exit_code`.

    Deterministic by construction - one fixed line, no timestamps, no
    environment echo - because the stdout-purity assertion compares its bytes
    across two runs.

    The line is emitted with ``printf '%s\\n'`` and shell-quoted via
    ``shlex.quote``: the previous ``printf "{line}\\n"`` baked the line into a
    printf *format string*, so a ``%`` was read as a directive, a ``"`` broke the
    quoting, and a ``\\`` was an escape. Every prior call happened to pass a line
    free of those bytes, so the latent defect never surfaced. The stdout-purity
    task is the one that has to push adversarial bytes through the stream, so the
    emitter was hardened rather than the test data tamed.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "tofu"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{marker_dir}/$$.argv"\n'
        f"printf '%s\\n' {shlex.quote(stdout_line)}\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


def read_env_dump(path: Path) -> dict[str, str]:
    """Parse an `env -0` dump. NUL-separated, so any value is safe."""
    result: dict[str, str] = {}
    for chunk in path.read_bytes().split(b"\0"):
        if not chunk:
            continue
        key, _sep, value = chunk.partition(b"=")
        result[key.decode()] = value.decode()
    return result


def collect_tofu_invocations(dump_dir: Path) -> list[tuple[list[str], dict[str, str]]]:
    """Every recorded tofu invocation as (argv, env), oldest first."""
    invocations: list[tuple[list[str], dict[str, str]]] = []
    for env_path in sorted(dump_dir.glob("*.env0"), key=lambda p: p.stat().st_mtime):
        argv = env_path.with_suffix(".argv").read_text().splitlines()
        invocations.append((argv, read_env_dump(env_path)))
    return invocations


def recorded_argvs(marker_dir: Path) -> list[list[str]]:
    """Every fake-tofu invocation's argv, oldest first."""
    return [
        path.read_text().splitlines()
        for path in sorted(marker_dir.glob("*.argv"), key=lambda p: p.stat().st_mtime)
    ]


def wait_for_namespace_gone(name: str, timeout: float = 120.0) -> None:
    """Block until the named Namespace is really gone, via the in-node oracle."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        probe = kubectl_in_node("get", "namespace", name, "-o", "name")
        if probe.returncode != 0 and "not found" in probe.stderr.lower():
            return
        last = f"rc={probe.returncode} stdout={probe.stdout!r} stderr={probe.stderr!r}"
        time.sleep(2.0)
    pytest.fail(f"namespace {name!r} still present after {timeout:.0f}s: {last}")
