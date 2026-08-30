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


# A fetched kubeconfig that already carries the name tunstrap would generate
# for (node="edge", target="k3s") in its clusters list. This is the
# reserved-namespace shadow the rename guard must reject; the test proves the
# rejection reaches the operator as a per-target warning, not an unhandled
# traceback. node="edge" + target key "k3s" -> "tunstrap-edge-k3s".
_PRE_EXISTING_IDENTITY_KUBE = b"""\
apiVersion: v1
clusters:
- cluster:
    server: https://192.0.2.10:6443
    certificate-authority-data: Y2EtZGF0YQ==
  name: default
- cluster:
    server: https://192.0.2.99:6443
    certificate-authority-data: Y2EtZGF0YQ==
  name: tunstrap-edge-k3s
contexts:
- context: {cluster: default, user: default}
  name: default
current-context: default
kind: Config
preferences: {}
users:
- name: default
  user:
    client-certificate-data: Y2VydC1kYXRh
    client-key-data: a2V5LWRhdGE=
"""


@pytest.mark.asyncio
async def test_pre_existing_identity_surfaces_as_per_target_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reserved-namespace collision surfaces as a per-target warning, not a crash.

    ``rename_identities`` raises ``KubeParseError``; ``run_kube_targets`` must
    catch it and report ``kube_target <name>: <msg>`` exactly like a fetch or
    parse failure, fold it into ``required_failures`` only when the target is
    required (the default), and close the listener it opened. Without the
    try/except, the error would propagate unhandled out of
    ``run_kube_targets``: ``KubeParseError`` is not in
    ``_NODE_STARTUP_ERRORS``, so ``_start_one`` would not catch it, and the
    worker's top-level ``except Exception`` guard in ``_worker._run`` would
    catch it instead -- reporting a generic ``daemon_error`` IPC frame (exit
    4) while tearing down *every* node via ``manager.stop_all()``. That is a
    worse outcome than a per-target warning (it loses the whole tunnel set),
    not merely a CLI traceback.
    """
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["dev-kube-1"], []),
    )
    conn = _FakeConn(_PRE_EXISTING_IDENTITY_KUBE)
    outputs, required_failures, warnings = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
        node_name="edge",
    )
    assert outputs == {}, outputs
    assert required_failures == ["k3s"], required_failures
    assert any("k3s" in w.error and "tunstrap-edge-k3s" in w.error for w in warnings), [
        w.error for w in warnings
    ]


@pytest.mark.asyncio
async def test_non_selected_context_warning_discloses_reference_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-context warning must not claim the context is merely 'ignored'.

    ``multi_context.yaml``'s non-selected context ``kubernetes-admin@kubernetes``
    shares the active triple's cluster/user, so its references ARE rewritten
    during the rename. The warning wording must say plainly that the context
    was not selected but its cluster/user references are rewritten when they
    point at the renamed entries -- the old ``ignored context '<x>'`` wording
    and the module docstring's ``left byte-stable`` claim both misdescribed
    this and are the subject of issue #20's defect 2.
    """
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["dev-kube-1"], []),
    )
    conn = _FakeConn((FIXTURES / "multi_context.yaml").read_bytes())
    _, _, warnings = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
        node_name="edge",
    )
    ctx_warnings = [w.error for w in warnings if "kubernetes-admin@kubernetes" in w.error]
    assert ctx_warnings, [w.error for w in warnings]
    wording = ctx_warnings[0]
    assert "non-selected" in wording, wording
    assert "rewritten" in wording, wording
    # The misleading bare 'ignored context' wording is gone.
    assert "ignored context" not in wording, wording


@pytest.mark.asyncio
async def test_rewrite_disclosure_not_emitted_when_target_fails_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rewrite disclosure must not fire when the target later fails.

    The non-selected-context warning claims the context's ``cluster``/``user``
    references were rewritten. That disclosure is only true on the success
    path -- ``rename_identities`` must actually have run. This target drives a
    failure that occurs *after* the ignored-context loop's old position but
    *before* the rename: ``multi_context.yaml`` (two contexts) with an apiserver
    SAN probe that yields no usable name and ``insecure_fallback`` left false,
    so ``_resolve_tls`` returns ``(None, False)`` and the target fails at the
    "no usable TLS name" branch -- after ``_split_host_port`` and before
    ``rename_identities``. No rewrite happened, so no warning may carry the
    rewrite wording. (Issue #20 defect 1: the loop used to sit before the
    failure paths and pre-emptively disclosed a rewrite that never occurred.)
    """
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: ([], []),
    )
    conn = _FakeConn((FIXTURES / "multi_context.yaml").read_bytes())
    outputs, required_failures, warnings = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
        node_name="edge",
    )
    # The downstream TLS failure is real: nothing produced, target required.
    assert outputs == {}, outputs
    assert required_failures == ["k3s"], required_failures
    assert any("no usable TLS name" in w.error for w in warnings), [w.error for w in warnings]
    # The false disclosure must be absent: no rewrite ran.
    rewrite_warnings = [w.error for w in warnings if "rewritten" in w.error]
    assert rewrite_warnings == [], [w.error for w in warnings]
