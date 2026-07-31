"""Render an OutputSchema into TUNSTRAP_* environment variables (#6/#5)."""

from __future__ import annotations

import re

from tunstrap.exceptions import MultiNodeEnvUnsupported
from tunstrap.schemas import InputSchema, OutputSchema

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _key(name: str) -> str:
    """Sanitise a target/kube name into an env-var segment (upper, _-joined)."""
    return _NON_ALNUM.sub("_", name.upper())


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

    kube_paths: list[str] = []
    for kname, target in node.kube_targets.items():
        base = _key(kname)
        if target.path is None:
            raise ValueError(f"kube target {kname!r} not materialized; cannot set KUBECONFIG")
        put(f"TUNSTRAP_{base}_KUBECONFIG", target.path)
        put(f"TUNSTRAP_{base}_ENDPOINT", target.endpoint)
        kube_paths.append(target.path)

    if kube_paths:
        put("KUBECONFIG", ":".join(kube_paths))
    return env


def predicted_env_keys(schema: InputSchema) -> set[str]:
    """Env keys ``render_env`` will produce for this *input* schema.

    Used pre-spawn to reject an ``--output-var`` NAME that would collide with
    an injected key, before a daemon exists to orphan. Multi-node input
    injects no scalars at all, so the answer there is the empty set.

    The result is a superset of the eventual actual key set when an optional
    target fails and is absent from the output. A superset only rejects more
    names, never fewer, so it cannot let a real collision through.
    """
    if len(schema.nodes) != 1:
        return set()
    (node,) = schema.nodes.values()
    keys = {"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID"}
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
    if node.kube_targets:
        keys.add("KUBECONFIG")
    return keys


def format_exports(env: dict[str, str]) -> str:
    """Render an env mapping as POSIX-safe ``export K='V'`` lines."""
    lines = [f"export {key}='{_shell_single_quote(value)}'" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def _shell_single_quote(value: str) -> str:
    """Escape a value for inclusion inside single quotes in POSIX sh."""
    return value.replace("'", "'\\''")
