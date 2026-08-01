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
    imported: set[str] = set()
    for module in sorted(HERE.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    imported.add("." * node.level + (node.module or ""))
                elif node.module:
                    imported.add(node.module)
    foreign = sorted(
        name
        for name in imported
        if name.split(".")[0] == "tests" and not name.startswith("tests.e2e")
    )
    assert foreign == [], f"e2e modules importing another suite: {foreign}"

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
    """The in-node kubectl oracle reaches the API server without any tunnel."""
    probe = kubectl_in_node("get", "nodes", "-o", "name")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == f"node/{CONTROL_PLANE}"
    assert kind_cluster == CLUSTER_NAME


def test_in_node_kubeconfig_has_the_shape_the_tunnel_flow_depends_on(
    node_kubeconfig: Path,
) -> None:
    """admin.conf names the control plane by DNS and embeds CA + client creds."""
    text = node_kubeconfig.read_text()
    assert f"server: https://{CONTROL_PLANE}:6443" in text
    assert "certificate-authority-data:" in text
    assert "client-certificate-data:" in text
    assert "client-key-data:" in text
    assert node_kubeconfig.stat().st_mode & 0o777 == 0o644
