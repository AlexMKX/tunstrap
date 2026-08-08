"""Regression test for issue #15's collision trap.

k3s ships current-context/cluster/user all named "default". Two k3s targets
therefore collide on the upstream names verbatim -- that is the case this
test drives, not a kind-style already-unique name (kind's context is
`kind-<cluster>`, so a kind-based test would pass without proving anything;
see the ticket's "Testing trap" section).

RED against current tunstrap/kube.py: both node's kube_targets report
context_name == cluster_name == "default", so a `KUBECONFIG=a:b` merge would
have the second silently overwrite the first's context/cluster/user entries.

GREEN after identity renaming: each output's context_name/cluster_name/user
identity is the deterministic `tunstrap-<node>-<target>` name, distinct per
node even though both fixtures carry identical upstream names.
"""

from __future__ import annotations

from typing import Any

import pytest

from tunstrap.kube import run_kube_targets
from tunstrap.schemas import KubeTarget

pytestmark = pytest.mark.unit

_K3S_STYLE = """\
apiVersion: v1
clusters:
- cluster:
    server: https://{ip}:6443
    certificate-authority-data: Y2EtZGF0YQ==
  name: default
contexts:
- context: {{cluster: default, user: default}}
  name: default
current-context: default
kind: Config
preferences: {{}}
users:
- name: default
  user:
    client-certificate-data: Y2VydC1kYXRh
    client-key-data: a2V5LWRhdGE=
"""


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

    def __init__(self, file_bytes: bytes, local_port: int) -> None:
        self._file_bytes = file_bytes
        self._local_port = local_port

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
        return _FakeListener(self._local_port)


async def _probe_ok(_host: str, _port: int) -> bytes:
    return b"DERCERT"


@pytest.mark.asyncio
async def test_two_k3s_style_targets_get_distinct_deterministic_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two nodes whose upstream k3s kubeconfigs both name everything 'default'.

    Under the current implementation this fails: both outputs report
    context_name == cluster_name == "default" -- the exact collision a
    KUBECONFIG merge cannot resolve. Under the spike's recommended
    combination each is `tunstrap-<node>-kube`, distinct by construction.
    """
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["node.example.net"], []),
    )

    node_a_conn = _FakeConn(_K3S_STYLE.format(ip="192.0.2.10").encode(), 40001)
    node_b_conn = _FakeConn(_K3S_STYLE.format(ip="192.0.2.20").encode(), 40002)
    target = {"kube": KubeTarget.model_validate({"kubeconfig_path": "/etc/rancher/k3s/k3s.yaml"})}

    outputs_a, failures_a, _ = await run_kube_targets(
        node_a_conn, target, connect_timeout=5, probe=_probe_ok, node_name="node-a"
    )
    outputs_b, failures_b, _ = await run_kube_targets(
        node_b_conn, target, connect_timeout=5, probe=_probe_ok, node_name="node-b"
    )
    assert failures_a == []
    assert failures_b == []
    out_a = outputs_a["kube"]
    out_b = outputs_b["kube"]

    # The collision trap: upstream names are identical on both sides.
    assert out_a.context_name != out_b.context_name, (
        "both targets report the SAME context_name "
        f"({out_a.context_name!r}) -- a KUBECONFIG merge of the two "
        "materialized files would collide on this name"
    )
    assert out_a.cluster_name != out_b.cluster_name
    assert out_a.context_name == "tunstrap-node-a-kube"
    assert out_b.context_name == "tunstrap-node-b-kube"
    assert out_a.cluster_name == "tunstrap-node-a-kube"
    assert out_b.cluster_name == "tunstrap-node-b-kube"

    # The rename must also reach the serialized document, not just the
    # extracted KubeTargetOutput fields (dump_kubeconfig is what a
    # KUBECONFIG-list consumer actually parses).
    import base64

    dumped_a = base64.b64decode(out_a.content_b64).decode()
    dumped_b = base64.b64decode(out_b.content_b64).decode()
    assert "current-context: tunstrap-node-a-kube" in dumped_a
    assert "current-context: tunstrap-node-b-kube" in dumped_b
    assert "name: default" not in dumped_a
    assert "name: default" not in dumped_b
