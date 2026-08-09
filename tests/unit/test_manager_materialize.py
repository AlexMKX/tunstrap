"""TunnelManager materialization keeps kubeconfigs and fetched files distinct.

Code: tunstrap/manager.py.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tunstrap.manager import TunnelManager
from tunstrap.schemas import FetchedFile, InputSchema, KubeTargetOutput
from tunstrap.session import SessionDir

pytestmark = pytest.mark.unit


def _manager(session: SessionDir) -> TunnelManager:
    """Build a manager whose session is available to the materializers."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "node": {
                    "host": "host",
                    "user": "user",
                    "ssh_password": "password",
                    "fetch_files": {"file": {"path": "/etc/file"}},
                }
            }
        }
    )
    return TunnelManager(schema, session=session)


def _kube(content: bytes) -> KubeTargetOutput:
    """Return a minimal materializable kube target carrying ``content``."""
    return KubeTargetOutput(
        cluster_name="cluster",
        context_name="context",
        local_port=6443,
        endpoint="https://127.0.0.1:6443",
        tls_server_name=None,
        certificate_authority_data="ca",
        client_certificate_data="cert",
        client_key_data="key",
        content_b64=base64.b64encode(content).decode(),
    )


def test_materialize_kube_target_uses_kube_namespace(tmp_path: Path) -> None:
    """A kube target is written to a kube-prefixed tunnel-data leaf with mode 0600."""
    session = SessionDir.create(supplied=None, base=tmp_path)
    kube = {"config": _kube(b"patched-kubeconfig")}

    _manager(session)._materialize_kube_targets(session, "node", kube)

    path = Path(kube["config"].path or "")
    assert path == Path(session.session_dir) / "tunnel-data" / "kube-node-config"
    assert path.read_bytes() == b"patched-kubeconfig"
    assert path.stat().st_mode & 0o777 == 0o600
    session.cleanup()


def test_materialize_fetch_file_uses_fetch_namespace(tmp_path: Path) -> None:
    """A fetched file is written to a fetch-prefixed tunnel-data leaf with mode 0600."""
    session = SessionDir.create(supplied=None, base=tmp_path)
    fetched = {
        "config": FetchedFile(
            content_b64=base64.b64encode(b"fetched-file").decode(), size=12, sha256="a" * 64
        )
    }

    _manager(session)._materialize_fetch_files(session, "node", fetched)

    path = Path(fetched["config"].path or "")
    assert path == Path(session.session_dir) / "tunnel-data" / "fetch-node-config"
    assert path.read_bytes() == b"fetched-file"
    assert path.stat().st_mode & 0o777 == 0o600
    session.cleanup()


def test_materializers_keep_same_named_fetch_bytes_out_of_kubeconfig(tmp_path: Path) -> None:
    """Same logical names in both kinds retain separate on-disk bytes and paths."""
    session = SessionDir.create(supplied=None, base=tmp_path)
    manager = _manager(session)
    fetched_bytes = b"fetched-file"
    kube_bytes = b"patched-kubeconfig client_key_data: private-key"
    fetched = {
        "config": FetchedFile(
            content_b64=base64.b64encode(fetched_bytes).decode(),
            size=len(fetched_bytes),
            sha256="a" * 64,
        )
    }
    kube = {"config": _kube(kube_bytes)}

    manager._materialize_fetch_files(session, "node", fetched)
    manager._materialize_kube_targets(session, "node", kube)

    fetch_path = Path(fetched["config"].path or "")
    kube_path = Path(kube["config"].path or "")
    assert fetch_path != kube_path
    assert fetch_path.read_bytes() == fetched_bytes
    assert fetch_path.read_bytes() != kube_bytes
    assert kube_path.read_bytes() == kube_bytes
    session.cleanup()
