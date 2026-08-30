"""Pydantic models for CLI input/output. Single source of JSON shape."""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_FETCH_FILES_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")


def _validate_identifier_key(kind: str, name: str) -> None:
    """Reject a dict key that is not a safe ≤64-char identifier.

    Shared by fetch_files / kube_targets / nodes key validation.
    """
    if len(name) > 64:
        raise ValueError(f"{kind} key {name!r}: max 64 chars")
    if not _FETCH_FILES_KEY_RE.match(name):
        raise ValueError(f"{kind} key {name!r}: must match ^[a-zA-Z_][a-zA-Z0-9_-]*$")


def materialized_file_name(kind: str, node_name: str, item_name: str) -> str:
    """Render a materialized-file leaf name for validated item kind and names.

    The kind prefix makes names collision-free across kinds. Within-kind
    uniqueness relies on the InputSchema validator failing closed.
    """
    return f"{kind}-{node_name}-{item_name}"


def _parse_host_port(value: str) -> tuple[str, int]:
    """Parse 'host:port' or '[ipv6]:port' into (host, port).

    Raises ValueError on any malformed input. The host is not validated
    against DNS rules; let the SSH server reject unresolvable names at
    connect time. The port is range-checked to 1..65535.
    """
    if value.startswith("["):
        # IPv6 path: [host]:port
        closing = value.find("]:")
        if closing == -1:
            raise ValueError("IPv6 target must use [host]:port form")
        host = value[1:closing]
        port_str = value[closing + 2 :]
    else:
        if "::" in value and value.count(":") > 1:
            raise ValueError("IPv6 target must use [host]:port form")
        if ":" not in value:
            raise ValueError("missing ':' in target")
        host, port_str = value.rsplit(":", 1)
    if not host:
        raise ValueError("empty host")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError("port must be 1..65535") from exc
    if port < 1 or port > 65535:
        raise ValueError("port must be 1..65535")
    return host, port


class SSHOptions(BaseModel):
    """SSH transport options reachable from public input.

    Only fields actually consumed by tunstrap/ssh.py::open_connection
    and open_local_forwards. Anything else is dead config and rejected.
    """

    model_config = ConfigDict(extra="forbid")

    compression: bool = False
    connect_timeout: int = 60


class DaemonOptions(BaseModel):
    """Daemon-side knobs: log file, startup/shutdown deadlines, idle auto-stop."""

    model_config = ConfigDict(extra="forbid")

    log_file: str | None = None
    shutdown_grace_seconds: int = 10
    startup_timeout_seconds: int = Field(
        default=300,
        ge=1,
        description=(
            "Maximum time the parent waits for the worker's startup IPC response. "
            "On expiry the parent terminates the worker and waits up to "
            "shutdown_grace_seconds before killing it."
        ),
    )
    auto_stop_idle_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Auto-shutdown timeout in seconds. If set, the daemon sends "
            "itself SIGTERM when no tunnel forward has had an active "
            "connection for this many seconds. Timer starts when the "
            "daemon comes up; any open or close of a forward connection "
            "resets it. Active long-lived connections prevent shutdown. "
            "Null (default) disables auto-shutdown."
        ),
    )
    materialize: bool = Field(
        default=False,
        description=(
            "If True, fetched/patched files (e.g. kube_targets kubeconfig) are "
            "written mode 0600 into the session dir's tunnel-data/ and removed "
            "on stop/atexit. Default False keeps content off disk."
        ),
    )


class FileSpec(BaseModel):
    """A single file the daemon should read once at start."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    required: bool = Field(
        default=True,
        description=(
            "If false, a failure to fetch this file is recorded as "
            "FetchedFile.error and does not fail the node."
        ),
    )

    @field_validator("path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        """Reject shell-expanded paths because the daemon must address an exact remote file."""
        if value.startswith("~"):
            raise ValueError("path must be literal (no '~' expansion)")
        if not value.startswith("/"):
            raise ValueError("path must be absolute (start with '/')")
        return value


class KubeTarget(BaseModel):
    """One kubeconfig to fetch, forward, SAN-probe, and patch.

    Exactly one cluster is handled: the kubeconfig's current-context.
    `tls_server_name` overrides the SAN probe entirely. `insecure_fallback`
    governs what happens when no usable TLS name can be determined.
    """

    model_config = ConfigDict(extra="forbid")

    kubeconfig_path: str = Field(min_length=1, max_length=4096)
    tls_server_name: str | None = None
    insecure_fallback: bool = Field(
        default=False,
        description=(
            "If the SAN probe yields no usable name and no tls_server_name is "
            "given: True emits insecure-skip-tls-verify (drops CA) + a warning; "
            "False fails the target (subject to `required`)."
        ),
    )
    required: bool = Field(
        default=True,
        description="If False, this target's failure does not fail the node.",
    )

    @field_validator("kubeconfig_path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        """Reject shorthand paths before a remote kubeconfig request is constructed."""
        if value.startswith("~"):
            raise ValueError("kubeconfig_path must be literal (no '~' expansion)")
        if not value.startswith("/"):
            raise ValueError("kubeconfig_path must be absolute (start with '/')")
        return value


class RemoteTarget(BaseModel):
    """Parsed host:port target. Stored on NodeInput after validation."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)


class NodeInput(BaseModel):
    """One SSH endpoint plus its local-forward and fetch-files requests."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 22
    user: str
    ssh_pkey: str | None = None
    ssh_password: str | None = None
    ssh_pkey_passphrase: str | None = None
    remote_targets: dict[str, RemoteTarget] = Field(default_factory=dict)
    ssh_options: SSHOptions = Field(default_factory=SSHOptions)
    required: bool = Field(
        default=True,
        description=(
            "If false, a failure to start this node's tunnel or to fetch its "
            "required files is downgraded to a TunnelWarning instead of "
            "aborting `start`."
        ),
    )
    fetch_files: dict[str, FileSpec] | None = None
    kube_targets: dict[str, KubeTarget] | None = None

    @field_validator("remote_targets", mode="before")
    @classmethod
    def _validate_remote_targets(cls, value: object) -> dict[str, RemoteTarget]:
        """Normalize legacy strings at the boundary so workers see one target shape."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("remote_targets must be a dict")
        if len(value) > 16:
            raise ValueError("remote_targets: at most 16 entries per node")
        parsed: dict[str, RemoteTarget] = {}
        for handle, raw in value.items():
            if not isinstance(handle, str) or not _FETCH_FILES_KEY_RE.match(handle):
                raise ValueError(
                    f"remote_targets key {handle!r}: must match ^[a-zA-Z_][a-zA-Z0-9_-]*$"
                )
            if len(handle) > 64:
                raise ValueError(f"remote_targets key {handle!r}: max 64 chars")
            if isinstance(raw, RemoteTarget):
                parsed[handle] = raw
                continue
            if isinstance(raw, dict):
                try:
                    parsed[handle] = RemoteTarget.model_validate(raw)
                except ValidationError as exc:
                    messages = "; ".join(
                        error["msg"]
                        for error in exc.errors(
                            include_input=False, include_url=False, include_context=False
                        )
                    )
                    raise ValueError(
                        f"remote_targets[{handle!r}]: invalid dict form: {messages}"
                    ) from exc
                continue
            if not isinstance(raw, str):
                raise ValueError(f"remote_targets[{handle!r}]: value must be a 'host:port' string")
            try:
                host, port = _parse_host_port(raw)
            except ValueError as exc:
                raise ValueError(f"remote_targets[{handle!r}]: {raw!r}: {exc}") from exc
            parsed[handle] = RemoteTarget(host=host, port=port)
        return parsed

    @field_validator("fetch_files")
    @classmethod
    def _validate_fetch_files(cls, value: dict[str, FileSpec] | None) -> dict[str, FileSpec] | None:
        """Reject ambiguous or unbounded fetch maps before they reach one SFTP channel."""
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("fetch_files: omit field instead of empty dict")
        if len(value) > 16:
            raise ValueError("fetch_files: at most 16 entries per node")
        for name in value:
            _validate_identifier_key("fetch_files", name)
        return value

    @field_validator("kube_targets")
    @classmethod
    def _validate_kube_targets(
        cls, value: dict[str, KubeTarget] | None
    ) -> dict[str, KubeTarget] | None:
        """Keep kube names safe for their later materialized-file namespace."""
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("kube_targets: omit field instead of empty dict")
        if len(value) > 16:
            raise ValueError("kube_targets: at most 16 entries per node")
        for name in value:
            _validate_identifier_key("kube_targets", name)
        return value

    @model_validator(mode="after")
    def _validate_node_does_something(self) -> NodeInput:
        """Reject inert SSH sessions that would consume a daemon without output."""
        if not self.remote_targets and not self.kube_targets and not self.fetch_files:
            raise ValueError(
                "node must define at least one of remote_targets, kube_targets, fetch_files"
            )
        return self


class InputSchema(BaseModel):
    """Top-level input read from stdin by ``tunstrap start``."""

    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, NodeInput]
    daemon: DaemonOptions = Field(default_factory=DaemonOptions)

    @field_validator("nodes")
    @classmethod
    def _validate_auth(cls, value: dict[str, NodeInput]) -> dict[str, NodeInput]:
        """Fail validation before a worker discovers it has no SSH credential source."""
        for name, node in value.items():
            _validate_identifier_key("node", name)
            if not node.ssh_pkey and not node.ssh_password:
                if not os.environ.get("SSH_AUTH_SOCK"):
                    raise ValueError(
                        f"node {name!r}: provide ssh_pkey, ssh_password, or run ssh-agent"
                        " (SSH_AUTH_SOCK)"
                    )
        return value

    @model_validator(mode="after")
    def _validate_kube_identity_names_are_unique(self) -> InputSchema:
        """Reject colliding kube identities and materialized-file leaf names."""
        identity_seen: dict[str, tuple[str, str]] = {}
        for node_name, node in self.nodes.items():
            for target_name in node.kube_targets or {}:
                joined = f"tunstrap-{node_name}-{target_name}"
                if joined in identity_seen:
                    other_node, other_target = identity_seen[joined]
                    raise ValueError(
                        f"kube identity name collision: ({node_name!r}, {target_name!r}) "
                        f"and ({other_node!r}, {other_target!r}) both render {joined!r}"
                    )
                identity_seen[joined] = (node_name, target_name)

        materialized_seen: dict[str, tuple[str, str, str]] = {}
        for node_name, node in self.nodes.items():
            for kind, items in (("kube", node.kube_targets), ("fetch", node.fetch_files)):
                for item_name in items or {}:
                    leaf = materialized_file_name(kind, node_name, item_name)
                    if leaf in materialized_seen:
                        other_kind, other_node, other_item = materialized_seen[leaf]
                        raise ValueError(
                            "materialized file name collision: "
                            f"({kind!r}, {node_name!r}, {item_name!r}) and "
                            f"({other_kind!r}, {other_node!r}, {other_item!r}) "
                            f"both render {leaf!r}"
                        )
                    materialized_seen[leaf] = (kind, node_name, item_name)
        return self


class FetchedFile(BaseModel):
    """Either a successful read (content_b64+size+sha256) or an error string."""

    model_config = ConfigDict(extra="forbid")

    content_b64: str | None = None
    path: str | None = None
    size: int | None = None
    sha256: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_xor(self) -> FetchedFile:
        """Preserve an unambiguous success-or-error envelope for optional fetches."""
        has_success = self.content_b64 is not None
        has_error = self.error is not None
        if has_success and has_error:
            raise ValueError("FetchedFile: cannot set both content_b64 and error")
        if not has_success and not has_error:
            raise ValueError("FetchedFile: must set either content_b64 or error")
        if has_success and (self.size is None or self.sha256 is None):
            raise ValueError("FetchedFile: content_b64 requires size and sha256")
        if has_error and (self.size is not None or self.sha256 is not None):
            raise ValueError("FetchedFile: error branch must not set size/sha256")
        return self


class KubeTargetOutput(BaseModel):
    """Extracted, ready-to-use fields for one kube_target's current-context cluster.

    `content_b64` is the full patched kubeconfig. `path` is set only when
    daemon.materialize is True. On insecure fallback,
    `certificate_authority_data` is "" and `tls_server_name` is null.
    """

    model_config = ConfigDict(extra="forbid")

    cluster_name: str
    context_name: str
    local_port: int
    endpoint: str
    tls_server_name: str | None
    certificate_authority_data: str
    client_certificate_data: str
    client_key_data: str
    content_b64: str
    path: str | None = None


class NodeOutput(BaseModel):
    """Per-node success payload: ports, fetched files, and kube targets."""

    model_config = ConfigDict(extra="forbid")

    ports: dict[str, int]
    fetch_files: dict[str, FetchedFile] = Field(default_factory=dict)
    kube_targets: dict[str, KubeTargetOutput] = Field(default_factory=dict)


class TunnelWarning(BaseModel):
    """Non-fatal failure on an optional node, surfaced in the warnings array."""

    model_config = ConfigDict(extra="forbid")

    node: str
    error: str
    skipped: bool = True


class OutputSchema(BaseModel):
    """Success envelope returned by ``tunstrap start`` on stdout."""

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, NodeOutput]
    pid: int
    session_dir: str
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)


class UnifiedKubeRef(BaseModel):
    """Kube reference in the unified output: never credentials, never content."""

    model_config = ConfigDict(extra="forbid")

    path: str | None
    context: str
    endpoint: str


class UnifiedSession(BaseModel):
    """Session metadata block of the unified output."""

    model_config = ConfigDict(extra="forbid")

    session_dir: str
    pid: int
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)


class UnifiedFetchRef(BaseModel):
    """Fetched-file reference in the unified output: never content_b64."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    size: int | None = None
    sha256: str | None = None
    error: str | None = None


class UnifiedNode(BaseModel):
    """One node's body in the unified output: ports, kube refs, fetch_files."""

    model_config = ConfigDict(extra="forbid")

    ports: dict[str, str] = Field(default_factory=dict)
    kube: dict[str, UnifiedKubeRef] = Field(default_factory=dict)
    fetch_files: dict[str, UnifiedFetchRef] = Field(default_factory=dict)


class UnifiedOutput(BaseModel):
    """The entire consumer-facing output: two reserved top-level keys."""

    model_config = ConfigDict(extra="forbid")

    session: UnifiedSession
    nodes: dict[str, UnifiedNode]


class ErrorOutput(BaseModel):
    """Error envelope from ``start``: JSON stdout, or diagnostic stderr for env output."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
