"""Build a single-node InputSchema from CLI flags (issue #6)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from tunstrap.exceptions import SchemaValidationError
from tunstrap.schemas import DaemonOptions, InputSchema


def parse_endpoint(endpoint: str) -> tuple[str, str, int]:
    """Parse ``USER@HOST[:PORT]`` into (user, host, port); default port 22."""
    user, sep, hostpart = endpoint.partition("@")
    if not sep or not user:
        raise SchemaValidationError("connection must be USER@HOST[:PORT]", {"value": endpoint})
    host, port = _split_host_port(hostpart, endpoint)
    return user, host, port


def _split_host_port(hostpart: str, original: str) -> tuple[str, int]:
    if hostpart.startswith("["):  # IPv6 literal: [addr] or [addr]:port
        end = hostpart.find("]")
        if end == -1:
            raise SchemaValidationError("malformed IPv6 host (missing ']')", {"value": original})
        host = hostpart[1:end]
        rest = hostpart[end + 1 :]
        if rest == "":
            return host, 22
        if not rest.startswith(":"):
            raise SchemaValidationError("expected ':PORT' after ']'", {"value": original})
        return host, _parse_port(rest[1:], original)
    if ":" in hostpart:
        host, _, raw_port = hostpart.rpartition(":")
        if not host:
            raise SchemaValidationError("connection missing host", {"value": original})
        return host, _parse_port(raw_port, original)
    if not hostpart:
        raise SchemaValidationError("connection missing host", {"value": original})
    return hostpart, 22


def _parse_port(raw: str, original: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise SchemaValidationError("port must be an integer", {"value": original}) from exc
    if not 1 <= port <= 65535:
        raise SchemaValidationError("port out of range 1-65535", {"value": original})
    return port


def parse_named(items: tuple[str, ...], label: str) -> dict[str, str]:
    """Parse repeated ``NAME=VALUE`` flags; reject empty/missing/duplicate."""
    out: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise SchemaValidationError(f"--{label} must be NAME=VALUE", {"value": item})
        if not name:
            raise SchemaValidationError(f"--{label} has empty NAME", {"value": item})
        if not value:
            raise SchemaValidationError(f"--{label} has empty VALUE", {"value": item})
        if name in out:
            raise SchemaValidationError(f"--{label} duplicate name {name!r}", {"value": item})
        out[name] = value
    return out


def build_single_node_schema(
    *,
    connection: str,
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password: str | None,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    daemon_opts: DaemonOptions,
) -> InputSchema:
    """Assemble a one-node InputSchema from parsed CLI inputs."""
    user, host, port = parse_endpoint(connection)

    pkey: str | None = None
    if ssh_key is not None:
        try:
            pkey = Path(ssh_key).read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaValidationError(
                "cannot read --ssh-key file", {"path": ssh_key, "error": str(exc)}
            ) from exc

    remote_targets = parse_named(targets, "target")
    kube_named = parse_named(kube, "kube")
    fetch_named = parse_named(fetch, "fetch")

    node: dict[str, object] = {
        "host": host,
        "port": port,
        "user": user,
        "ssh_pkey": pkey,
        "ssh_password": ssh_password,
        "ssh_pkey_passphrase": ssh_key_passphrase,
        "remote_targets": remote_targets,
    }
    if kube_named:
        node["kube_targets"] = {k: {"kubeconfig_path": v} for k, v in kube_named.items()}
    if fetch_named:
        node["fetch_files"] = {k: {"path": v} for k, v in fetch_named.items()}

    try:
        # Fixed, schema-safe node key: the node-key grammar forbids leading
        # digits/dots/colons, so the host (which may be an IPv4/IPv6 literal)
        # cannot be the key. Single-node CLI mode always uses "node".
        return InputSchema.model_validate(
            {"nodes": {"node": node}, "daemon": daemon_opts.model_dump()}
        )
    except ValidationError as exc:
        raise SchemaValidationError(
            "CLI input does not satisfy the schema",
            {"errors": exc.errors(include_input=False, include_url=False, include_context=False)},
        ) from exc
