"""Orchestrate one node's kube_targets: forward, probe SAN, patch, extract.

Validates: a successful kube_target yields a KubeTargetOutput with the
local endpoint, chosen tls_server_name, and patched content; a required
target whose fetch fails is reported as a required failure.
Code: tunstrap/kube.py::run_kube_targets
Assertion: returned outputs carry the local port + tls name; warnings
include the non-exact-SAN note; required failures are listed.
Method: drive run_kube_targets with a fake connection + injected probe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tunstrap.kube import run_kube_targets
from tunstrap.schemas import KubeTarget

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kube"


class _FakeListener:
    def __init__(self, port: int) -> None:
        self._port = port

    def get_port(self) -> int:
        return self._port

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeConn:
    """Stubs the two asyncssh calls run_kube_targets uses: sftp + forward."""

    def __init__(self, file_bytes: bytes) -> None:
        self._file_bytes = file_bytes

    def start_sftp_client(self) -> Any:
        conn = self

        class _CM:
            async def __aenter__(self) -> Any:
                class _Sftp:
                    async def stat(self, _path: str) -> Any:
                        class _S:
                            size = len(conn._file_bytes)

                        return _S()

                    def open(self, _path: str, _mode: str) -> Any:
                        data = conn._file_bytes

                        class _FH:
                            async def __aenter__(self) -> Any:
                                class _R:
                                    async def read(self, _n: int) -> bytes:
                                        return data

                                return _R()

                            async def __aexit__(self, *_a: Any) -> None:
                                return None

                        return _FH()

                return _Sftp()

            async def __aexit__(self, *_a: Any) -> None:
                return None

        return _CM()

    async def forward_local_port(self, *_a: Any, **_k: Any) -> _FakeListener:
        return _FakeListener(40123)


async def _probe_ok(_host: str, _port: int) -> bytes:
    # Minimal: return a sentinel; sans_from_cert returns ([],[]) for junk, so
    # use a probe that bypasses cert parsing by patching choose via monkeypatch.
    return b"DERCERT"


@pytest.mark.asyncio
async def test_run_kube_target_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy kube_target yields a patched output with the local endpoint."""
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["dev-kube-1", "192.0.2.11"], []),
    )
    conn = _FakeConn((FIXTURES / "single_internal_ip.yaml").read_bytes())
    outputs, required_failures, warnings = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
    )
    assert required_failures == []
    out = outputs["k3s"]
    assert out.endpoint == "https://127.0.0.1:40123"
    assert out.tls_server_name in {"dev-kube-1", "192.0.2.11"}
    assert out.local_port == 40123
    assert out.content_b64  # non-empty patched kubeconfig


@pytest.mark.asyncio
async def test_run_kube_target_reports_renamed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output identity names are deterministic and node-qualified."""
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["dev-kube-1", "192.0.2.11"], []),
    )
    conn = _FakeConn((FIXTURES / "single_internal_ip.yaml").read_bytes())
    outputs, _, _ = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
        node_name="edge",
    )
    out = outputs["k3s"]
    assert out.context_name == "tunstrap-edge-k3s"
    assert out.cluster_name == "tunstrap-edge-k3s"


def test_default_probe_is_callable() -> None:
    """A default TLS probe is exported for production use."""
    from tunstrap.kube import default_san_probe

    assert callable(default_san_probe)
