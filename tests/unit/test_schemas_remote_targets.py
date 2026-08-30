"""Unit tests for _parse_host_port and RemoteTarget validator."""

from __future__ import annotations

import pytest

from tunstrap.schemas import (
    InputSchema,
    NodeInput,
    NodeOutput,
    OutputSchema,
    RemoteTarget,
    _parse_host_port,
)

pytestmark = pytest.mark.unit


class TestParseHostPort:
    """Coverage of the host:port parser."""

    def test_ipv4_with_port(self) -> None:
        """IPv4 literal splits cleanly on the last colon."""
        assert _parse_host_port("10.0.0.1:6443") == ("10.0.0.1", 6443)

    def test_dns_name_with_port(self) -> None:
        """A bare hostname keeps its string form."""
        assert _parse_host_port("node.local:22") == ("node.local", 22)

    def test_ipv6_bracketed(self) -> None:
        """IPv6 loopback requires brackets to disambiguate from the port colon."""
        assert _parse_host_port("[::1]:6443") == ("::1", 6443)

    def test_ipv6_full(self) -> None:
        """Full IPv6 address parses identically once bracketed."""
        assert _parse_host_port("[2001:db8::1]:443") == ("2001:db8::1", 443)

    def test_missing_colon_fails(self) -> None:
        """No colon means no port, which is a hard error."""
        with pytest.raises(ValueError, match="missing ':' in target"):
            _parse_host_port("10.0.0.1")

    def test_empty_host_fails(self) -> None:
        """A leading colon yields an empty host."""
        with pytest.raises(ValueError, match="empty host"):
            _parse_host_port(":6443")

    def test_empty_host_bracketed_fails(self) -> None:
        """Empty bracketed host is also rejected."""
        with pytest.raises(ValueError, match="empty host"):
            _parse_host_port("[]:6443")

    def test_port_zero_fails(self) -> None:
        """Port 0 is reserved and not a legal forward target."""
        with pytest.raises(ValueError, match=r"port must be 1\.\.65535"):
            _parse_host_port("10.0.0.1:0")

    def test_port_too_large_fails(self) -> None:
        """Port 65536 is out of range."""
        with pytest.raises(ValueError, match=r"port must be 1\.\.65535"):
            _parse_host_port("10.0.0.1:65536")

    def test_port_non_numeric_fails(self) -> None:
        """Non-numeric port is rejected before range check."""
        with pytest.raises(ValueError, match=r"port must be 1\.\.65535"):
            _parse_host_port("10.0.0.1:abc")

    def test_ipv6_without_brackets_fails(self) -> None:
        """Unbracketed IPv6 is ambiguous; insist on the [host]:port form."""
        with pytest.raises(ValueError, match="IPv6 target must use"):
            _parse_host_port("::1:6443")


def _minimal_node(**overrides: object) -> dict[str, object]:
    """Helper: produce a NodeInput payload with the smallest valid auth + targets."""
    base: dict[str, object] = {
        "host": "127.0.0.1",
        "user": "tester",
        "ssh_pkey": "PEM",
        "remote_targets": {"p": "127.0.0.1:6443"},
    }
    base.update(overrides)
    return base


class TestRemoteTargetsValidator:
    """Pydantic validator on NodeInput.remote_targets."""

    def test_valid_target_parses_into_remote_target(self) -> None:
        """After validation, dict values are RemoteTarget instances."""
        node = NodeInput.model_validate(_minimal_node(remote_targets={"kubeapi": "10.0.0.1:6443"}))
        assert node.remote_targets == {"kubeapi": RemoteTarget(host="10.0.0.1", port=6443)}

    def test_empty_remote_targets_alone_rejected(self) -> None:
        """A node with only empty remote_targets (no kube_targets or fetch_files) is rejected."""
        with pytest.raises(ValueError, match="at least one of"):
            NodeInput.model_validate(_minimal_node(remote_targets={}))

    def test_too_many_remote_targets_rejected(self) -> None:
        """Hard cap at 16 entries mirrors fetch_files."""
        too_many = {f"h{i}": "127.0.0.1:6443" for i in range(17)}
        with pytest.raises(ValueError, match="at most 16 entries"):
            NodeInput.model_validate(_minimal_node(remote_targets=too_many))

    def test_invalid_handle_chars_rejected(self) -> None:
        """Handle regex matches fetch_files: alnum + underscore + hyphen."""
        with pytest.raises(ValueError, match="must match"):
            NodeInput.model_validate(_minimal_node(remote_targets={"bad.name": "127.0.0.1:6443"}))

    def test_handle_starting_with_digit_rejected(self) -> None:
        """Handles must start with a letter or underscore."""
        with pytest.raises(ValueError, match="must match"):
            NodeInput.model_validate(_minimal_node(remote_targets={"1bad": "127.0.0.1:6443"}))

    def test_malformed_value_surfaces_handle_and_value(self) -> None:
        """Parsing errors include the handle and the offending string for diagnostics."""
        with pytest.raises(ValueError) as exc:
            NodeInput.model_validate(_minimal_node(remote_targets={"kubeapi": "10.0.0.1"}))
        msg = str(exc.value)
        assert "kubeapi" in msg
        assert "10.0.0.1" in msg
        assert "missing ':'" in msg

    def test_legacy_remote_ports_rejected_with_extra_forbid(self) -> None:
        """The old list[int] shape must be rejected by extra=forbid."""
        payload = _minimal_node(remote_ports=[6443])
        del payload["remote_targets"]  # legacy callers had no remote_targets
        with pytest.raises(ValueError, match="remote_ports"):
            NodeInput.model_validate(payload)

    def test_legacy_local_ports_rejected_with_extra_forbid(self) -> None:
        """local_ports is no longer accepted; OS-assigned only."""
        with pytest.raises(ValueError, match="local_ports"):
            NodeInput.model_validate(_minimal_node(local_ports=[5000]))

    def test_duplicate_target_values_allowed(self) -> None:
        """Two handles pointing at the same host:port yield two RemoteTarget entries."""
        node = NodeInput.model_validate(
            _minimal_node(
                remote_targets={
                    "p1": "127.0.0.1:6443",
                    "p2": "127.0.0.1:6443",
                }
            )
        )
        assert node.remote_targets["p1"] == node.remote_targets["p2"]
        assert len(node.remote_targets) == 2

    def test_input_schema_validates_via_node_input(self) -> None:
        """End-to-end through InputSchema (the actual top-level shape)."""
        schema = InputSchema.model_validate({"nodes": {"edge1": _minimal_node()}})
        assert "edge1" in schema.nodes
        assert isinstance(schema.nodes["edge1"].remote_targets["p"], RemoteTarget)

    def test_remote_target_dict_form_accepted(self) -> None:
        """The dict form produced by model_dump() round-trips back into RemoteTarget."""
        node = NodeInput.model_validate(
            _minimal_node(remote_targets={"kubeapi": {"host": "10.0.0.1", "port": 6443}})
        )
        assert node.remote_targets == {"kubeapi": RemoteTarget(host="10.0.0.1", port=6443)}

    def test_remote_targets_round_trip_via_model_dump(self) -> None:
        """Full model_dump() -> model_validate() round trip preserves remote_targets."""
        node = NodeInput.model_validate(_minimal_node(remote_targets={"kubeapi": "10.0.0.1:6443"}))
        rebuilt = NodeInput.model_validate(node.model_dump())
        assert rebuilt.remote_targets == node.remote_targets


class TestNodeOutputShape:
    """Output payload now uses dict[str, int] for ports."""

    def test_node_output_ports_is_dict_int(self) -> None:
        """Direct construction with handle→local_port works."""
        out = NodeOutput(ports={"kubeapi": 54321, "prom": 54322})
        assert out.ports == {"kubeapi": 54321, "prom": 54322}
        assert out.fetch_files == {}

    def test_node_output_rejects_list_form(self) -> None:
        """The legacy list[ConnectionEntry] form is no longer accepted."""
        with pytest.raises(ValueError):
            NodeOutput.model_validate({"ports": [{"local_port": 12345}]})

    def test_output_schema_round_trip(self) -> None:
        """OutputSchema serialises and re-validates with the new ports dict."""
        original = OutputSchema(
            connections={"edge1": NodeOutput(ports={"kubeapi": 54321})},
            pid=123,
            session_dir="/tmp/x",
            started_at="2026-05-20T00:00:00Z",
        )
        dumped = original.model_dump()
        rebuilt = OutputSchema.model_validate(dumped)
        assert rebuilt.connections["edge1"].ports == {"kubeapi": 54321}
