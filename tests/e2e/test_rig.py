"""The e2e rig itself: self-containment, and real kube API traffic through it.

Code: tests/e2e/conftest.py, tests/e2e/docker-compose.yml.
Method: inspect the generated key material, then drive `tunstrap start` against
the kind cluster and talk to the API server through the resulting tunnel.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from tests.e2e.rig import CLUSTER_NAME, CONTROL_PLANE, HERE, kubectl_in_node

pytestmark = [pytest.mark.e2e]


def test_rig_generates_its_own_keypair(e2e_ssh_keypair: tuple[str, str]) -> None:
    """The e2e suite owns its key material and never reads the integration rig's."""
    private_pem, public_line = e2e_ssh_keypair
    assert private_pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_line.startswith("ssh-ed25519 ")

    priv_path = HERE / "_keys" / "id_test"
    assert priv_path.is_file()
    assert priv_path.stat().st_mode & 0o777 == 0o600
    assert (HERE / "_keys" / "id_test.pub").is_file()


def test_rig_borrows_nothing_from_another_suite(e2e_ssh_keypair: tuple[str, str]) -> None:
    """No cross-suite import, no cross-suite mount, and our own key material."""
    del e2e_ssh_keypair  # requested so the keypair exists before we assert on it

    # Parsed as an AST, not scanned as text. A substring search for the
    # forbidden path would match this test's own source - its docstring, its
    # own condition, and rig.py's module docstring all name that path in prose -
    # and could therefore never pass. Import statements are the thing that
    # actually creates a code dependency, and they are unambiguous in an AST.
    foreign: set[str] = set()
    for module in sorted(HERE.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Import):
                foreign.update(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("tests") and not alias.name.startswith("tests.e2e")
                )
            elif isinstance(node, ast.ImportFrom):
                if node.level >= 2:
                    foreign.add("." * node.level + (node.module or ""))
                elif (
                    node.module
                    and node.module.startswith("tests")
                    and not node.module.startswith("tests.e2e")
                ):
                    foreign.add(node.module)
    assert not foreign, f"e2e modules importing another suite: {sorted(foreign)}"

    # Every bind mount is relative to this directory. Matching on ":/" catches
    # any host:container pair, including one written as ../integration/_keys,
    # which a "starts with ./" filter would silently skip.
    compose = (HERE / "docker-compose.yml").read_text()
    lines = [line.strip() for line in compose.splitlines()]
    mounts = sorted(line[2:] for line in lines if line.startswith("- ") and ":/" in line)
    assert mounts == [
        "./_keys:/keys:ro",
        "./_kube:/etc/kube:ro",
        "./_sshd_conf:/config/sshd/sshd_config.d:ro",
    ], mounts

    # ...and the three things those mounts point at are ours.
    assert (HERE / "_keys" / "id_test").is_file()
    assert (HERE / "_keys" / "id_test.pub").is_file()
    assert (HERE / "_sshd_conf" / "allow_tcpfwd.conf").is_file()


def test_sshd_forwarding_dropin_is_tracked_and_correct() -> None:
    """The drop-in that turns AllowTcpForwarding on is committed, not generated."""
    dropin = HERE / "_sshd_conf" / "allow_tcpfwd.conf"
    assert dropin.read_text().strip() == "AllowTcpForwarding yes"

    compose = (HERE / "docker-compose.yml").read_text()
    assert "./_sshd_conf:/config/sshd/sshd_config.d:ro" in compose
    assert "./_keys:/keys:ro" in compose
    assert "./_kube:/etc/kube:ro" in compose


def test_generated_rig_paths_are_gitignored() -> None:
    """_keys/ and _kube/ never enter the index; _sshd_conf/ always does."""
    repo_root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(
        ["git", "ls-files", "tests/e2e"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "tests/e2e/_sshd_conf/allow_tcpfwd.conf" in tracked
    assert not [p for p in tracked if p.startswith("tests/e2e/_keys/")]
    assert not [p for p in tracked if p.startswith("tests/e2e/_kube/")]


def test_cluster_node_is_ready_through_the_independent_oracle(kind_cluster: str) -> None:
    """The in-node kubectl oracle reaches the API server without any tunnel.

    Assertion audit - what each line can actually catch:
    - `returncode`/`stdout`: BEHAVIOURAL, but weakly so. Re-runs the oracle live,
      so it catches a control plane that died between fixture setup and now, and
      it catches `kindest/node` dropping the bundled kubectl - which would
      silently disable this tier's only independent read-back path. It is weakly
      self-referential about the *name*: CONTROL_PLANE is both the `docker exec`
      target and the expected value, so a rename moves both sides together. What
      survives is a real pin on kind's convention that the node name equals the
      container name, which `kube_targets` depends on.
    - `kind_cluster == CLUSTER_NAME`: PIN, not behavioural. The fixture yields
      that constant, so this cannot fail against a broken cluster. Kept because
      that name is also the SSH forward target and the expected tls_server_name,
      so it documents the coupling at the point of use.
    - the `wait` probe: BEHAVIOURAL, and the only line here that makes this
      test's name true.
    """
    probe = kubectl_in_node("get", "nodes", "-o", "name")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == f"node/{CONTROL_PLANE}"
    assert kind_cluster == CLUSTER_NAME

    # `get nodes -o name` prints the node whatever its condition, so nothing
    # above distinguishes Ready from NotReady. Readiness is really enforced by
    # `kind create --wait 90s` + check=True in the fixture; this asserts it
    # directly so the test's name is earned rather than assumed. --timeout=0
    # means "check once and do not wait", so a NotReady node fails immediately
    # instead of hanging.
    ready = kubectl_in_node("wait", "--for=condition=Ready", "node", "--all", "--timeout=0")
    assert ready.returncode == 0, ready.stderr


def test_in_node_kubeconfig_has_the_shape_the_tunnel_flow_depends_on(
    node_kubeconfig: Path,
) -> None:
    """admin.conf names the control plane by DNS and embeds CA + client creds.

    Assertion audit - two of these five are deliberate belt-and-braces, not
    behavioural checks, and saying so is the point:
    - `server:` line: PIN. The fixture already `pytest.fail`s on this exact
      substring and then writes that same string to the file, so it is strictly
      implied and cannot fire while the fixture is as it is. Kept as a
      regression guard on the *fixture*: if someone later rewrites the file, or
      drops the fixture's check, this catches it here where the dependency is
      documented. The fixture's own failure message is the better diagnostic.
    - mode 0644: PIN, for the same reason - the fixture chmods 0644
      unconditionally with an absolute mode, so no fixture-produced file can
      violate this. Kept because 0600 would break the unprivileged `tester` user
      reading it over SSH, which is otherwise invisible until Task 2.4 fails
      obscurely.
    - the three `-data:` assertions: BEHAVIOURAL. Nothing in the fixture looks at
      them, so they pin upstream kubeconfig content: if kind ever emitted
      external credentials (an exec plugin, or a `client-key` path instead of
      embedded data), `parse_kubeconfig` would raise and these fail first,
      naming the reason.
    """
    text = node_kubeconfig.read_text()
    assert f"server: https://{CONTROL_PLANE}:6443" in text  # pin (see docstring)
    assert "certificate-authority-data:" in text
    assert "client-certificate-data:" in text
    assert "client-key-data:" in text
    assert node_kubeconfig.stat().st_mode & 0o777 == 0o644  # pin (see docstring)
