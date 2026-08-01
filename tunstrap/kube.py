"""Kube mode: parse a remote kubeconfig, choose a TLS server name, patch it.

One kube_target maps to exactly one cluster: the kubeconfig's
current-context. Other contexts/clusters are ignored and left byte-stable
in the patched output. The fetched kubeconfig is untrusted input: it is
parsed in ruamel round-trip/safe mode and parse failures become a typed
KubeParseError (never a daemon crash).
"""

from __future__ import annotations

import asyncio
import base64
import io
import socket as _socket
import ssl as _ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.x509.oid import ExtensionOID
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from tunstrap.exceptions import KubeParseError
from tunstrap.schemas import KubeTarget, KubeTargetOutput, TunnelWarning

if TYPE_CHECKING:
    import asyncssh

__all__ = [
    "KubeParseError",
    "KubeconfigView",
    "ProbeFn",
    "choose_tls_server_name",
    "default_san_probe",
    "dump_kubeconfig",
    "parse_kubeconfig",
    "patch_view",
    "run_kube_targets",
    "sans_from_cert",
]


@dataclass
class KubeconfigView:
    """Extracted current-context view plus the live parsed document.

    `doc` is the round-trip ruamel document used later for in-place patching
    (comments/key order preserved). The scalar fields are the extracted
    current-context cluster/user material.
    """

    doc: object
    context_name: str
    cluster_name: str
    user_name: str
    server: str
    certificate_authority_data: str
    client_certificate_data: str
    client_key_data: str
    ignored_contexts: list[str] = field(default_factory=list)


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    return y


def parse_kubeconfig(raw: bytes) -> KubeconfigView:
    """Parse raw kubeconfig bytes and extract the current-context view.

    Raises KubeParseError on malformed YAML, missing current-context, or an
    unresolvable cluster/user reference.
    """
    try:
        doc = _yaml().load(io.BytesIO(raw))
    except YAMLError as exc:
        raise KubeParseError(f"kubeconfig is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise KubeParseError("kubeconfig root is not a mapping")

    current = doc.get("current-context")
    if not current or not isinstance(current, str):
        raise KubeParseError("kubeconfig has no current-context")

    contexts = doc.get("contexts") or []
    ctx = _find_named(contexts, current)
    if ctx is None:
        raise KubeParseError(f"current-context {current!r} not found in contexts")
    _ctx_body_raw = ctx.get("context")
    ctx_body: dict[str, object] = _ctx_body_raw if isinstance(_ctx_body_raw, dict) else {}
    _cluster_name_raw = ctx_body.get("cluster")
    _user_name_raw = ctx_body.get("user")
    if not _cluster_name_raw or not _user_name_raw:
        raise KubeParseError(f"context {current!r} missing cluster or user")
    if not isinstance(_cluster_name_raw, str) or not isinstance(_user_name_raw, str):
        raise KubeParseError(f"context {current!r} cluster or user is not a string")
    cluster_name: str = _cluster_name_raw
    user_name: str = _user_name_raw

    cluster = _find_named(doc.get("clusters") or [], cluster_name)
    if cluster is None:
        raise KubeParseError(f"cluster {cluster_name!r} not found")
    _cluster_body_raw = cluster.get("cluster")
    cluster_body: dict[str, object] = (
        _cluster_body_raw if isinstance(_cluster_body_raw, dict) else {}
    )
    server = cluster_body.get("server")
    if not server or not isinstance(server, str):
        raise KubeParseError(f"cluster {cluster_name!r} has no server")

    user = _find_named(doc.get("users") or [], user_name)
    if user is None:
        raise KubeParseError(f"user {user_name!r} not found")
    _user_body_raw = user.get("user")
    user_body: dict[str, object] = _user_body_raw if isinstance(_user_body_raw, dict) else {}

    ignored = [
        str(c.get("name")) for c in contexts if isinstance(c, dict) and c.get("name") != current
    ]

    return KubeconfigView(
        doc=doc,
        context_name=current,
        cluster_name=str(cluster_name),
        user_name=str(user_name),
        server=server,
        certificate_authority_data=_string_field(
            cluster_body, "certificate-authority-data", cluster_name
        ),
        client_certificate_data=_string_field(user_body, "client-certificate-data", user_name),
        client_key_data=_string_field(user_body, "client-key-data", user_name),
        ignored_contexts=ignored,
    )


def _find_named(items: object, name: str) -> dict[str, object] | None:
    """Return the first list entry whose 'name' equals `name`, else None."""
    if not isinstance(items, list):
        return None
    for entry in items:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def sans_from_cert(cert_der: bytes) -> tuple[list[str], list[str]]:
    """Return (dns_sans, ip_sans) parsed from a DER-encoded certificate.

    A certificate with no SAN extension returns ([], []). A malformed
    certificate also returns ([], []) — both are "no usable name" for the
    caller, which then applies its insecure_fallback policy. Narrowly catches
    the two expected cryptography failure modes; anything else propagates.
    """
    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except ValueError:
        # Malformed DER — treated as "no usable name", not a daemon crash.
        return [], []
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        # Cert has no SAN extension at all.
        return [], []
    san = ext.value
    if not isinstance(san, x509.SubjectAlternativeName):
        return [], []
    dns = list(san.get_values_for_type(x509.DNSName))
    ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    return dns, ips


def choose_tls_server_name(
    *,
    original_host: str,
    dns_sans: list[str],
    ip_sans: list[str],
) -> tuple[str | None, bool]:
    """Choose a tls-server-name; return (name, fellback).

    Preference: original host if present in SAN (DNS or IP); else first DNS
    SAN; else first IP SAN; else None. `fellback` is True whenever the chosen
    name is not an exact match of `original_host` (including the None case).
    """
    if original_host in dns_sans or original_host in ip_sans:
        return original_host, False
    if dns_sans:
        return dns_sans[0], True
    if ip_sans:
        return ip_sans[0], True
    return None, True


def patch_view(
    view: KubeconfigView,
    *,
    local_port: int,
    tls_server_name: str | None,
    insecure: bool,
) -> None:
    """Patch the current-context cluster in-place on the ruamel doc.

    Rewrites `server:` to the local forwarded endpoint. On secure patch sets
    `tls-server-name`. On insecure patch sets `insecure-skip-tls-verify: true`
    and removes `certificate-authority-data`. Other clusters are untouched.
    """
    doc = view.doc
    assert isinstance(doc, dict)
    cluster = _find_named(doc.get("clusters") or [], view.cluster_name)
    assert cluster is not None  # parse_kubeconfig guaranteed this
    body_raw = cluster["cluster"]
    assert isinstance(body_raw, dict), "parse_kubeconfig guaranteed cluster.cluster is a dict"
    body: dict[str, object] = body_raw
    body["server"] = f"https://127.0.0.1:{local_port}"
    if insecure:
        body["insecure-skip-tls-verify"] = True
        body.pop("certificate-authority-data", None)
        body.pop("tls-server-name", None)
    else:
        if tls_server_name is not None:
            body["tls-server-name"] = tls_server_name


def dump_kubeconfig(view: KubeconfigView) -> bytes:
    """Serialise the (patched) ruamel doc back to YAML bytes."""
    buf = io.BytesIO()
    _yaml().dump(view.doc, buf)
    return buf.getvalue()


def _string_field(body: dict[str, object], field_name: str, owner: str) -> str:
    """Return ``body[field_name]`` if absent or a string; raise on wrong type.

    Empty/missing fields return ``""`` (kubeconfigs may legitimately omit CA in
    insecure mode). A present-but-non-string value is a malformed kubeconfig.
    """
    value = body.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KubeParseError(f"{owner!r} {field_name} must be a string, got {type(value).__name__}")
    return value


ProbeFn = Callable[[str, int], Awaitable[bytes]]


async def run_kube_targets(  # pylint: disable=too-many-locals,too-many-branches  # reason: per-target try/except/warning fan-out
    conn: asyncssh.SSHClientConnection,
    kube_targets: dict[str, KubeTarget],
    *,
    connect_timeout: int,
    probe: ProbeFn,
    node_name: str = "",
) -> tuple[dict[str, KubeTargetOutput], list[str], list[TunnelWarning]]:
    """Process every kube_target for a node; return (outputs, required_failures, warnings).

    The local-forward listeners opened here are owned by the live SSH
    connection (`conn`) and stay open for as long as the connection does; the
    manager keeps `conn` in its `_NodeRuntime` and closes it on `stop_all`, so
    the forwards do not need to be returned separately. Each target is
    independent: a failure on one (subject to its `required`) does not abort the
    others.
    """
    outputs: dict[str, KubeTargetOutput] = {}
    required_failures: list[str] = []
    warnings: list[TunnelWarning] = []

    for name, target in kube_targets.items():
        try:
            raw = await asyncio.wait_for(
                _fetch_one(conn, target.kubeconfig_path), timeout=connect_timeout
            )
            view = parse_kubeconfig(raw)
        except (KubeParseError, OSError, asyncio.TimeoutError) as exc:
            warnings.append(TunnelWarning(node=node_name, error=f"kube_target {name}: {exc}"))
            if target.required:
                required_failures.append(name)
            continue

        for ignored in view.ignored_contexts:
            warnings.append(
                TunnelWarning(
                    node=node_name,
                    error=f"kube_target {name}: ignored context {ignored!r}",
                    skipped=False,
                )
            )

        try:
            host, port = _split_host_port(view.server)
        except KubeParseError as exc:
            warnings.append(TunnelWarning(node=node_name, error=f"kube_target {name}: {exc}"))
            if target.required:
                required_failures.append(name)
            continue
        listener = await conn.forward_local_port("127.0.0.1", 0, host, port)
        local_port = listener.get_port()

        tls_name, insecure = await _resolve_tls(
            target=target,
            host=host,
            local_port=local_port,
            probe=probe,
            node_name=node_name,
            name=name,
            warnings=warnings,
        )
        if tls_name is None and not insecure:
            if target.required:
                required_failures.append(name)
            warnings.append(
                TunnelWarning(node=node_name, error=f"kube_target {name}: no usable TLS name")
            )
            listener.close()
            await listener.wait_closed()
            continue

        patch_view(view, local_port=local_port, tls_server_name=tls_name, insecure=insecure)
        patched = dump_kubeconfig(view)
        outputs[name] = KubeTargetOutput(
            cluster_name=view.cluster_name,
            context_name=view.context_name,
            local_port=local_port,
            endpoint=f"https://127.0.0.1:{local_port}",
            tls_server_name=None if insecure else tls_name,
            certificate_authority_data="" if insecure else view.certificate_authority_data,
            client_certificate_data=view.client_certificate_data,
            client_key_data=view.client_key_data,
            content_b64=base64.b64encode(patched).decode("ascii"),
            path=None,
        )
    return outputs, required_failures, warnings


def _split_host_port(server: str) -> tuple[str, int]:
    """Parse a kubeconfig ``server`` URL into (host, port).

    Uses urllib for robust parsing (handles IPv6 brackets, paths, query
    strings). Only https is accepted; a missing port defaults to 443.
    Raises KubeParseError on a malformed or non-https server URL.
    """
    try:
        parts = urlsplit(server)
    except ValueError as exc:
        raise KubeParseError(f"server URL is malformed: {server!r}: {exc}") from exc
    if parts.scheme != "https":
        raise KubeParseError(f"server URL must be https, got {parts.scheme!r}: {server!r}")
    host = parts.hostname
    if not host:
        raise KubeParseError(f"server URL has no host: {server!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise KubeParseError(f"server URL has an invalid port: {server!r}: {exc}") from exc
    return host, port if port is not None else 443


async def _fetch_one(conn: asyncssh.SSHClientConnection, path: str) -> bytes:
    """Read a single small file over SFTP (1 MiB cap), return raw bytes."""
    async with conn.start_sftp_client() as sftp:
        stat = await sftp.stat(path)
        if stat.size is not None and stat.size > (1 << 20):
            raise OSError("kubeconfig exceeds 1 MiB cap")
        async with sftp.open(path, "rb") as fh:
            data = await fh.read((1 << 20) + 1)
    raw = data if isinstance(data, bytes) else data.encode()
    if len(raw) > (1 << 20):
        raise OSError("kubeconfig exceeds 1 MiB cap")
    return raw


async def _resolve_tls(
    *,
    target: KubeTarget,
    host: str,
    local_port: int,
    probe: ProbeFn,
    node_name: str,
    name: str,
    warnings: list[TunnelWarning],
) -> tuple[str | None, bool]:
    """Determine (tls_server_name, insecure) for one target."""
    if target.tls_server_name is not None:
        return target.tls_server_name, False
    cert_der = await probe("127.0.0.1", local_port)
    dns_sans, ip_sans = sans_from_cert(cert_der)
    chosen, fellback = choose_tls_server_name(
        original_host=host, dns_sans=dns_sans, ip_sans=ip_sans
    )
    if chosen is None:
        if target.insecure_fallback:
            warnings.append(
                TunnelWarning(
                    node=node_name,
                    error=f"kube_target {name}: TLS verification disabled (insecure_fallback)",
                    skipped=False,
                )
            )
            return None, True
        return None, False
    if fellback:
        warnings.append(
            TunnelWarning(
                node=node_name,
                error=f"kube_target {name}: tls-server-name fell back to {chosen!r}",
                skipped=False,
            )
        )
    return chosen, False


async def default_san_probe(host: str, port: int) -> bytes:
    """TLS-handshake to host:port and return the peer certificate in DER form.

    Verification is disabled for the handshake itself (we only want the cert
    to read its SAN); the resulting tls-server-name is what re-enables real
    verification for the client. Runs the blocking socket work in a thread.
    """

    def _connect() -> bytes:
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        with _socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=None) as tls:
                der = tls.getpeercert(binary_form=True)
        if der is None:
            raise OSError("no peer certificate presented")
        return der

    return await asyncio.to_thread(_connect)
