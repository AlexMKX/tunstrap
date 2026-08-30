"""`--output-var` must not carry kube credentials into a Terraform variable.

The value of the variable named by ``--output-var`` becomes ``TF_VAR_tunstrap``
under the documented recipe. OpenTofu persists root-module variable values in
the plan file, which pipelines routinely archive, and renders unmarked
variables in diagnostics — so anything in this channel must be assumed to reach
durable storage that is not treated as a secret.

``KubeTargetOutput`` carries ``client_key_data`` (a private key),
``content_b64`` (the whole patched kubeconfig, which embeds that key) and
``client_certificate_data`` (no key, but it discloses the Kubernetes RBAC
identity). None are needed here: ``run`` forces ``materialize=True``, so the
consumer chain reads ``path`` off disk. The unified projection (``UnifiedKubeRef``)
narrows further than credential removal alone -- it carries exactly
``{path, context, endpoint}``, dropping every other field on
``KubeTargetOutput`` (``cluster_name``, ``local_port``, ``tls_server_name``,
``certificate_authority_data``) even though none of those four are credentials.

Code: tunstrap/envrender.py (render_unified_output, render_output_var),
tunstrap/schemas.py (UnifiedKubeRef), tunstrap/cli.py (_build_child_env)
Assertion: the *literal bytes* of the fixture's key material must not appear
anywhere in the child environment, paired with an exact-equality check on the
surviving fields so a payload that stopped being produced cannot satisfy the
absence assertions vacuously.
Method: CliRunner with spawn_daemon, subprocess.Popen and _teardown_run
monkeypatched; the child env is captured off the fake Popen.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# Distinctive, realistic material. Every absence assertion below is made
# against these exact strings, so it cannot be satisfied by an empty or
# never-generated payload.
CLIENT_KEY_PEM = (
    "-----BEGIN EC PRIVATE KEY-----\n"
    "TUNSTRAP-UNIT-KUBE-CLIENT-PRIVATE-KEY-MUST-NEVER-BE-PERSISTED\n"
    "-----END EC PRIVATE KEY-----\n"
)
CLIENT_CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "TUNSTRAP-UNIT-KUBE-CLIENT-CERT-CN-admin-O-system-masters\n"
    "-----END CERTIFICATE-----\n"
)
CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "TUNSTRAP-UNIT-CLUSTER-CA-PUBLIC-TRUST-ANCHOR\n"
    "-----END CERTIFICATE-----\n"
)

CLIENT_KEY_B64 = _b64(CLIENT_KEY_PEM)
CLIENT_CERT_B64 = _b64(CLIENT_CERT_PEM)
CA_B64 = _b64(CA_PEM)

# The patched kubeconfig really does embed the client key, which is why
# content_b64 is exactly as dangerous as client_key_data itself.
KUBECONFIG_TEXT = (
    "apiVersion: v1\n"
    "clusters:\n- cluster:\n    certificate-authority-data: " + CA_B64 + "\n"
    "users:\n- user:\n    client-key-data: " + CLIENT_KEY_B64 + "\n"
)
CONTENT_B64 = _b64(KUBECONFIG_TEXT)

KUBE_PATH = "/s/tunnel-data/node-k3s"

SECRET_KUBE: dict[str, Any] = {
    "cluster_name": "probe-cluster",
    "context_name": "probe-context",
    "local_port": 41111,
    "endpoint": "https://127.0.0.1:41111",
    "tls_server_name": "probe-control-plane",
    "certificate_authority_data": CA_B64,
    "client_certificate_data": CLIENT_CERT_B64,
    "client_key_data": CLIENT_KEY_B64,
    "content_b64": CONTENT_B64,
    "path": KUBE_PATH,
}


def _secret_payload(session_dir: str) -> dict[str, Any]:
    return {
        "connections": {
            "node": {
                "ports": {"db": 5432},
                "fetch_files": {},
                "kube_targets": {"k3s": SECRET_KUBE},
            }
        },
        "pid": 99,
        "session_dir": session_dir,
        "started_at": "2026-07-31T00:00:00Z",
    }


INPUT_PAYLOAD = json.dumps(
    {
        "nodes": {
            "node": {
                "host": "h.example.net",
                "user": "u",
                "ssh_password": "p",
                "remote_targets": {"db": "127.0.0.1:5432"},
            }
        }
    }
)


class FakePopen:
    last_env: dict[str, str] | None = None

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        FakePopen.last_env = env
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        """Accept forwarded signals; the fake child ignores them."""


@pytest.fixture(name="spawn")
def _spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    def _install(message: dict[str, Any]) -> None:
        def _spawn_daemon(
            schema: Any, session_dir: str | None = None, *, input_env: str | None = None
        ) -> dict[str, Any]:
            seen.append(schema)
            return message

        monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_env = None
    seen.append(_install)
    return seen


def _run(monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path) -> dict[str, str]:
    """Drive one full `run --input-env VAR --output-var TF_VAR_tunstrap`."""
    spawn[0]({"kind": "success", "payload": _secret_payload(str(tmp_path))})
    monkeypatch.setenv(VAR, INPUT_PAYLOAD)
    result = CliRunner().invoke(
        main,
        ["run", "--input-env", VAR, "--output-var", "TF_VAR_tunstrap", "--", "true"],
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    return FakePopen.last_env


def test_output_var_never_carries_kube_private_key_material(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """No kube credential reaches TF_VAR_tunstrap, nor any other child variable.

    Asserted over the whole child environment rather than the one variable, so
    a change that moved the payload to a different name could not quietly
    reopen the hole.
    """
    env = _run(monkeypatch, spawn, tmp_path)
    blob = "\n".join(f"{k}={v}" for k, v in env.items())

    assert CLIENT_KEY_B64 not in blob, "kube client PRIVATE KEY reached the child environment"
    assert CLIENT_KEY_PEM not in blob, "kube client private key reached the child in PEM form"
    assert CONTENT_B64 not in blob, "the full patched kubeconfig reached the child environment"
    assert CLIENT_CERT_B64 not in blob, "kube client certificate (RBAC identity) reached the child"

    # Gone from the structure, not merely renamed.
    target = json.loads(env["TF_VAR_tunstrap"])["nodes"]["node"]["kube"]["k3s"]
    assert "client_key_data" not in target
    assert "client_certificate_data" not in target
    assert "content_b64" not in target


def test_output_var_keeps_every_field_the_consumer_chain_reads(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """The projection is exact: these fields survive, and nothing else does.

    This is the anti-vacuity half of the pair above. If the payload stopped
    being produced, or the kube target collapsed to ``{}``, every absence
    assertion would still pass while this one fails. The expected dict is
    narrower than credential removal alone: ``UnifiedKubeRef`` carries exactly
    ``{path, context, endpoint}`` -- ``cluster_name``, ``local_port``,
    ``tls_server_name`` and ``certificate_authority_data`` are also gone,
    because the design narrows to references only, not just to
    non-credentials.
    """
    env = _run(monkeypatch, spawn, tmp_path)
    target = json.loads(env["TF_VAR_tunstrap"])["nodes"]["node"]["kube"]["k3s"]

    assert target == {
        "path": KUBE_PATH,
        "context": "probe-context",
        "endpoint": "https://127.0.0.1:41111",
    }


def test_output_var_projection_leaves_the_rest_of_the_envelope_intact(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """Only kube credentials are dropped; the envelope is otherwise unchanged
    in shape, up one level under the unified structure's session/nodes split."""
    env = _run(monkeypatch, spawn, tmp_path)
    decoded = json.loads(env["TF_VAR_tunstrap"])

    assert decoded["session"]["pid"] == 99
    assert decoded["session"]["session_dir"] == str(tmp_path)
    assert decoded["session"]["started_at"] == "2026-07-31T00:00:00Z"
    assert decoded["nodes"]["node"]["ports"] == {"db": "127.0.0.1:5432"}
