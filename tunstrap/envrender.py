"""Render an OutputSchema for ``run``'s env-native session and structured channels.

``render_kube_env`` builds the node-count-agnostic kube channel
(``KUBECONFIG``/``KUBE_CONFIG_PATH(S)``), unconditional on node count.
``render_unified_output``/``render_output_var`` build the node-qualified
structure -- keyed by node, with kube credentials removed -- that ``run``
materializes to ``tunnel-data/output.json`` and optionally also exports under
``--output-var``. ``RUN_ENV_KEYS`` contains every key ``run`` injects or
scrubs, for the pre-spawn ``--output-var`` collision check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tunstrap.schemas import (
    OutputSchema,
    UnifiedFetchRef,
    UnifiedKubeRef,
    UnifiedNode,
    UnifiedSession,
)
from tunstrap.session import atomic_write

KUBE_ENV_NAMES: frozenset[str] = frozenset({"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"})
"""The three kube env var names ``run`` manages, as a single source of truth.

``_build_child_env`` removes these from the inherited environment
*unconditionally* -- before any injection, regardless of schema -- so a stray
operator ``KUBECONFIG``/``KUBE_CONFIG_PATH``/``KUBE_CONFIG_PATHS`` can never
leak into or override the channel (the e2e tier asserts ``KUBECONFIG`` is
absent from ``tofu``'s env). Because the scrub is unconditional,
``RUN_ENV_KEYS`` must reserve the same set *unconditionally* too, or a
``--output-var KUBECONFIG`` would pass the pre-spawn collision guard and then
have the inherited value clobbered (issue #23).

This is the *reserved and scrubbed* set, distinct from what
``_kube_channel_keys`` / ``render_kube_env`` *set*: the SET is
cardinality-dependent (1 file -> ``KUBECONFIG`` + ``KUBE_CONFIG_PATH``;
>=2 -> ``KUBECONFIG`` + ``KUBE_CONFIG_PATHS``; 0 -> nothing), but the SCRUB
always covers all three. One constant for both the guard and the scrubber is
what keeps that asymmetry from drifting back into two lists that must agree.
"""

RUN_ENV_KEYS: frozenset[str] = (
    frozenset({"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE"}) | KUBE_ENV_NAMES
)
"""Every environment key ``run`` injects or reserves by scrubbing."""


def _kube_channel_keys(count: int) -> set[str]:
    """Names of the kube-channel env keys the conditional contract exports.

    Precondition: ``count >= 1``. Exactly 1: KUBECONFIG + KUBE_CONFIG_PATH. >=2:
    KUBECONFIG + KUBE_CONFIG_PATHS. KUBE_CONFIG_PATH and KUBE_CONFIG_PATHS are
    never both present -- KUBE_CONFIG_PATH wins over KUBE_CONFIG_PATHS per the
    measured OpenTofu kubernetes/helm provider precedence (docs/specs/
    2026-08-10-issue15-provider-env-precedence.md), so exporting both once a
    second file exists would silently hide every cluster but the first. The
    union of every branch is ``KUBE_ENV_NAMES``; this function picks the
    cardinality-correct subset to *set*, while the scrub reserves them all.
    """
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


def render_start_json(output: OutputSchema) -> dict[str, Any]:
    """Build ``start --output json`` data without redundant materialized content.

    ``path is not None`` is the discriminator, not ``daemon.materialize``:
    materialized kube and fetched-file entries use the same allow-lists as
    ``run``; unmaterialized entries retain ``content_b64`` as stdout is their
    only delivery channel. The two discriminators are equivalent today because
    a session is bound once per daemon from ``schema.daemon.materialize``.
    """
    payload: dict[str, Any] = output.model_dump(mode="json")
    connections = payload["connections"]
    for node_name, node in output.connections.items():
        rendered_targets = connections[node_name]["kube_targets"]
        for target_name, target in node.kube_targets.items():
            if target.path is None:
                continue
            rendered_targets[target_name] = UnifiedKubeRef(
                path=target.path, context=target.context_name, endpoint=target.endpoint
            ).model_dump(exclude_none=True)
        rendered_fetch_files = connections[node_name]["fetch_files"]
        for fetch_name, fetched_file in node.fetch_files.items():
            if fetched_file.path is None:
                continue
            rendered_fetch_files[fetch_name] = UnifiedFetchRef(
                path=fetched_file.path, size=fetched_file.size, sha256=fetched_file.sha256
            ).model_dump(exclude_none=True)
    return payload


def materialized_output_path(session_dir: str) -> str:
    """The deterministic path the materialization writer writes to; shared so
    _build_child_env's TUNSTRAP_OUTPUT_FILE and the actual writer never
    independently compute a different path for the same file.
    """
    return str(Path(session_dir) / "tunnel-data" / "output.json")


def write_materialized_output(output: OutputSchema) -> None:
    """Atomically write the unified output structure to its deterministic path."""
    atomic_write(
        Path(materialized_output_path(output.session_dir)), render_output_var(output).encode()
    )


def format_exports(env: dict[str, str]) -> str:
    """Render an env mapping as POSIX-safe ``export K='V'`` lines."""
    lines = [f"export {key}='{_shell_single_quote(value)}'" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def _shell_single_quote(value: str) -> str:
    """Escape a value for inclusion inside single quotes in POSIX sh."""
    return value.replace("'", "'\\''")
