"""Programmatic entry points for ``run`` that must not depend on Click internals."""

from __future__ import annotations

import sys

import click


def run_via_env_input(
    input_env: str,
    output_var: str,
    child_cmd: list[str],
    *,
    suppress_kubeconfig: bool = False,
) -> None:
    """Run env-input mode without exposing a non-CLI parameter on ``run``.

    The OpenTofu proxy already has parsed its fixed input, output, and child
    arguments. It calls the plain implementation so Click remains responsible
    only for translating command-line arguments, while both paths retain the
    same validation, spawn, and teardown behavior.
    """
    # tofu_proxy's lazy import provides the pass-through fast path; this adds
    # defence in depth for other programmatic callers.
    from tunstrap.cli import _run_command  # pylint: disable=import-outside-toplevel

    try:
        _run_command(
            ssh_key=None,
            ssh_key_passphrase=None,
            ssh_password_stdin=False,
            targets=(),
            kube=(),
            fetch=(),
            auto_stop_idle_seconds=None,
            materialize=False,
            log_file=None,
            input_env=input_env,
            output_var=output_var,
            session_dir=None,
            grace_seconds=10,
            grace_seconds_set=False,
            args=tuple(child_cmd),
            suppress_kubeconfig=suppress_kubeconfig,
        )
    except click.UsageError as exc:
        # Programmatic calls bypass main's UsageError wrapper, but must retain
        # its documented shell-compatible exit status.
        exc.show()
        sys.exit(64)
    sys.exit(0)  # pragma: no cover — _run_command always exits
