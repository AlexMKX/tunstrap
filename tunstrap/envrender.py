"""Render an OutputSchema into the two channels ``run`` exports (#6/#5).

The scalar channel (``render_env``) exports only hosts, ports and paths. The
structured channel (``render_output_var``) exports a projection of the whole
envelope with the kube credentials removed, because its consumer persists it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tunstrap.exceptions import MultiNodeEnvUnsupported
from tunstrap.schemas import (
    InputSchema,
    OutputSchema,
    UnifiedFetchRef,
    UnifiedKubeRef,
    UnifiedNode,
    UnifiedSession,
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _key(name: str) -> str:
    """Sanitise a target/kube name into an env-var segment (upper, _-joined)."""
    return _NON_ALNUM.sub("_", name.upper())


def _kube_channel_keys(count: int) -> set[str]:
    """Names of the kube-channel env keys the conditional contract exports.

    0 files: nothing. Exactly 1: KUBECONFIG + KUBE_CONFIG_PATH. >=2:
    KUBECONFIG + KUBE_CONFIG_PATHS. KUBE_CONFIG_PATH and KUBE_CONFIG_PATHS are
    never both present -- KUBE_CONFIG_PATH wins over KUBE_CONFIG_PATHS per the
    measured OpenTofu kubernetes/helm provider precedence (docs/artifacts/
    2026-08-07-issue15-provider-env-findings.md), so exporting both once a
    second file exists would silently hide every cluster but the first.
    """
    if count == 0:
        return set()
    if count == 1:
        return {"KUBECONFIG", "KUBE_CONFIG_PATH"}
    return {"KUBECONFIG", "KUBE_CONFIG_PATHS"}


def render_kube_env(output: OutputSchema) -> dict[str, str]:
    """Build the node-count-agnostic kube channel: KUBECONFIG plus the
    OpenTofu-provider-facing var the conditional contract picks.

    This channel has no node dimension: it collects one materialized path per
    kube_target across every node, so it is safe to call for any node count.
    """
    kube_paths: list[str] = []
    for node in output.connections.values():
        for kname, target in node.kube_targets.items():
            if target.path is None:
                raise ValueError(f"kube target {kname!r} not materialized; cannot set KUBECONFIG")
            kube_paths.append(target.path)
    if not kube_paths:
        return {}
    joined = ":".join(kube_paths)
    return {key: joined for key in _kube_channel_keys(len(kube_paths))}


def render_env(output: OutputSchema) -> dict[str, str]:
    """Build the TUNSTRAP_* env mapping for a single-node OutputSchema."""
    if len(output.connections) != 1:
        raise MultiNodeEnvUnsupported(
            "render_env requires exactly one node",
            {"nodes": sorted(output.connections)},
        )
    (node,) = output.connections.values()

    env: dict[str, str] = {
        "TUNSTRAP_SESSION_DIR": output.session_dir,
        "TUNSTRAP_PID": str(output.pid),
    }

    def put(key: str, value: str) -> None:
        if key in env:
            raise ValueError(f"env key collision: {key}")
        env[key] = value

    for tname, port in node.ports.items():
        base = _key(tname)
        put(f"TUNSTRAP_{base}_HOST", "127.0.0.1")
        put(f"TUNSTRAP_{base}_PORT", str(port))
        put(f"TUNSTRAP_{base}_ENDPOINT", f"127.0.0.1:{port}")

    for kname, target in node.kube_targets.items():
        base = _key(kname)
        if target.path is None:
            raise ValueError(f"kube target {kname!r} not materialized; cannot set KUBECONFIG")
        put(f"TUNSTRAP_{base}_KUBECONFIG", target.path)
        put(f"TUNSTRAP_{base}_ENDPOINT", target.endpoint)

    for key, value in render_kube_env(output).items():
        put(key, value)
    return env


def render_output_var(output: OutputSchema) -> str:
    """Serialise the unified structure for ``--output-var``."""
    return json.dumps(render_unified_output(output), separators=(",", ":"))


def render_unified_output(output: OutputSchema) -> dict[str, Any]:
    """Build the unified, node-qualified structure without content payloads.

    Callers must ensure fetched files are already materialized and carry their
    path before calling this function.
    """
    nodes: dict[str, object] = {}
    for node_name, node in output.connections.items():
        kube = {
            kname: UnifiedKubeRef(
                path=target.path, context=target.context_name, endpoint=target.endpoint
            )
            for kname, target in node.kube_targets.items()
        }
        ports = {tname: f"127.0.0.1:{port}" for tname, port in node.ports.items()}
        fetch_files = {
            fname: (
                UnifiedFetchRef(error=f.error)
                if f.error is not None
                else UnifiedFetchRef(path=f.path, size=f.size, sha256=f.sha256)
            )
            for fname, f in node.fetch_files.items()
        }
        nodes[node_name] = UnifiedNode(ports=ports, kube=kube, fetch_files=fetch_files).model_dump(
            exclude_none=True
        )
    session = UnifiedSession(
        session_dir=output.session_dir,
        pid=output.pid,
        started_at=output.started_at,
        warnings=output.warnings,
    ).model_dump(mode="json")
    return {"session": session, "nodes": nodes}


def predicted_env_keys(schema: InputSchema) -> set[str]:
    """Conservatively predict env keys that may be produced for this schema.

    Used pre-spawn to reject an ``--output-var`` NAME that would collide with
    an injected key, before a daemon exists to orphan. Multi-node input injects
    no ``TUNSTRAP_*`` scalars, but kube-channel keys remain possible.

    The result is a superset of the eventual actual key set when an optional
    target fails and is absent from the output. A superset only rejects more
    names, never fewer, so it cannot let a real collision through.
    """
    keys: set[str] = set()
    if len(schema.nodes) == 1:
        (node,) = schema.nodes.values()
        keys.update({"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID"})
        for tname in node.remote_targets:
            base = _key(tname)
            keys.update(
                {
                    f"TUNSTRAP_{base}_HOST",
                    f"TUNSTRAP_{base}_PORT",
                    f"TUNSTRAP_{base}_ENDPOINT",
                }
            )
        for kname in node.kube_targets or {}:
            base = _key(kname)
            keys.update({f"TUNSTRAP_{base}_KUBECONFIG", f"TUNSTRAP_{base}_ENDPOINT"})
    if any(node.kube_targets for node in schema.nodes.values()):
        keys.update({"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"})
    return keys


def format_exports(env: dict[str, str]) -> str:
    """Render an env mapping as POSIX-safe ``export K='V'`` lines."""
    lines = [f"export {key}='{_shell_single_quote(value)}'" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def _shell_single_quote(value: str) -> str:
    """Escape a value for inclusion inside single quotes in POSIX sh."""
    return value.replace("'", "'\\''")
