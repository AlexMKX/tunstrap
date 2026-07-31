"""Command-line interface. Subcommands are added in later tasks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import Callable, TypeVar

import click
from pydantic import ValidationError

from tunstrap import __version__
from tunstrap.cli_input import build_single_node_schema
from tunstrap.daemon import spawn_daemon
from tunstrap.envrender import format_exports, render_env
from tunstrap.exceptions import (
    DaemonError,
    TunstrapError,
    SchemaValidationError,
    exit_code_for,
)
from tunstrap.identity import IdentityCheckResult, verify_session
from tunstrap.schemas import DaemonOptions, InputSchema, OutputSchema
from tunstrap.session import SessionDir, SessionError, StopOutcome, stop_session

_FC = TypeVar("_FC", bound=Callable[..., object])


class _UsageExit64(click.Group):
    """Remap Click usage errors from default exit 2 to exit 64.

    Click's default ``standalone_mode=True`` swallows ``UsageError`` inside
    ``BaseCommand.main`` and exits with code 2 before any caller-level
    ``except`` block sees the exception. We force non-standalone mode so we
    can catch the error ourselves, render Click's usual message, and exit
    with the documented usage-error code (64, sysexits.h ``EX_USAGE``).
    """

    def main(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)  # type: ignore[call-overload]
        except click.UsageError as exc:
            exc.show()
            sys.exit(64)


@click.group(cls=_UsageExit64)
@click.version_option(__version__, prog_name="tunstrap")
def main() -> None:
    """tunstrap: SSH tunnel manager for ephemeral environments."""


def _connection_options(func: _FC) -> _FC:
    """Attach the shared single-node connection flags to a command."""
    decorators = [
        click.option(
            "--ssh-key",
            "ssh_key",
            default=None,
            help=(
                "Path to a private key file. If omitted and --ssh-password-stdin is not used,"
                " keys from $SSH_AUTH_SOCK (ssh-agent) are used."
            ),
        ),
        click.option("--ssh-key-passphrase", "ssh_key_passphrase", default=None),
        click.option("--ssh-password-stdin", "ssh_password_stdin", is_flag=True, default=False),
        click.option("--target", "targets", multiple=True, metavar="NAME=HOST:PORT"),
        click.option("--kube", "kube", multiple=True, metavar="NAME=/abs/path"),
        click.option("--fetch", "fetch", multiple=True, metavar="NAME=/abs/path"),
        click.option("--auto-stop-idle-seconds", "auto_stop_idle_seconds", type=int, default=None),
        click.option("--materialize", "materialize", is_flag=True, default=False),
        click.option("--log-file", "log_file", default=None),
    ]
    for dec in reversed(decorators):
        func = dec(func)
    return func


def _conn_flags_present(
    *,
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password_stdin: bool,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
) -> bool:
    return any([ssh_key, ssh_key_passphrase, ssh_password_stdin, targets, kube, fetch])


def _schema_from_flags(
    connection: str,
    *,
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password_stdin: bool,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    auto_stop_idle_seconds: int | None,
    materialize: bool,
    log_file: str | None,
    force_materialize: bool = False,
) -> InputSchema:
    ssh_password: str | None = None
    if ssh_password_stdin:
        ssh_password = sys.stdin.readline().rstrip("\n")
    daemon = DaemonOptions(
        auto_stop_idle_seconds=auto_stop_idle_seconds,
        materialize=materialize or force_materialize,
        log_file=log_file,
    )
    return build_single_node_schema(
        connection=connection,
        ssh_key=ssh_key,
        ssh_key_passphrase=ssh_key_passphrase,
        ssh_password=ssh_password,
        targets=targets,
        kube=kube,
        fetch=fetch,
        daemon_opts=daemon,
    )


@main.command("start")
@click.argument("connection", required=False)
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@_connection_options
@click.option(
    "--output",
    "output_fmt",
    type=click.Choice(["json", "env"]),
    default="json",
    show_default=True,
)
@click.option("--session-dir", "session_dir", default=None)
def start_command(  # pylint: disable=too-many-arguments,too-many-branches,too-many-statements
    connection: str | None,
    extra: tuple[str, ...],
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password_stdin: bool,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    auto_stop_idle_seconds: int | None,
    materialize: bool,
    log_file: str | None,
    output_fmt: str,
    session_dir: str | None,
) -> None:
    """Open tunnels and daemonize. Input: USER@HOST[:PORT] flags, or JSON on stdin."""
    try:
        if extra:
            raise click.UsageError("`--` invokes a child command; use `tunstrap run ... -- CMD`")
        conn_flags = _conn_flags_present(
            ssh_key=ssh_key,
            ssh_key_passphrase=ssh_key_passphrase,
            ssh_password_stdin=ssh_password_stdin,
            targets=targets,
            kube=kube,
            fetch=fetch,
        )
        if connection is None and conn_flags:
            raise click.UsageError("connection flags require a USER@HOST[:PORT] argument")

        if connection is not None:
            # Flag mode: check that stdin is empty (conflict guard)
            stdin_peek = sys.stdin.read() if not ssh_password_stdin else ""
            if stdin_peek.strip():
                raise click.UsageError(
                    "cannot combine a connection argument with JSON on stdin; "
                    "use flags or stdin, not both"
                )
            schema = _schema_from_flags(
                connection,
                ssh_key=ssh_key,
                ssh_key_passphrase=ssh_key_passphrase,
                ssh_password_stdin=ssh_password_stdin,
                targets=targets,
                kube=kube,
                fetch=fetch,
                auto_stop_idle_seconds=auto_stop_idle_seconds,
                materialize=materialize,
                log_file=log_file,
                force_materialize=(output_fmt == "env"),
            )
        else:
            raw = sys.stdin.read()
            if not raw.strip():
                raise SchemaValidationError(
                    "no input: provide USER@HOST[:PORT] or JSON on stdin", {}
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SchemaValidationError(
                    "stdin is not valid JSON", {"position": exc.pos}
                ) from exc
            try:
                schema = InputSchema.model_validate(payload)
            except ValidationError as exc:
                raise SchemaValidationError(
                    "input does not satisfy the InputSchema contract",
                    {
                        "errors": exc.errors(
                            include_input=False, include_url=False, include_context=False
                        )
                    },
                ) from exc

        message = spawn_daemon(schema, session_dir=session_dir)
        kind = message["kind"]
        if kind == "success" and output_fmt == "env":
            out = OutputSchema.model_validate(message["payload"])
            sys.stdout.write(format_exports(render_env(out)))
        else:
            sys.stdout.write(json.dumps(message["payload"]) + "\n")
        sys.stdout.flush()
        if kind == "required_failure":
            sys.exit(2)
        if kind == "daemon_error":
            sys.exit(4)
        if kind == "session_active":
            sys.exit(3)
        # kind == "success" → exit 0 (default)
    except click.UsageError:
        raise
    except TunstrapError as exc:
        sys.stdout.write(json.dumps(exc.to_error_output()) + "\n")
        sys.stdout.flush()
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Top-level guard: surface any unexpected failure as DaemonError JSON and
        # exit 4 instead of dumping a Python traceback to a caller.
        sys.stdout.write(
            json.dumps(
                DaemonError(
                    "unexpected failure during start",
                    {"type": type(exc).__name__},
                ).to_error_output()
            )
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(4)


@main.command("run")
@click.argument("connection", required=True)
@_connection_options
@click.option("--session-dir", "session_dir", default=None)
@click.option("--grace-seconds", "grace_seconds", type=int, default=10, show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run_command(  # pylint: disable=too-many-arguments,too-many-locals
    connection: str,
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password_stdin: bool,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    auto_stop_idle_seconds: int | None,
    materialize: bool,
    log_file: str | None,
    session_dir: str | None,
    grace_seconds: int,
    command: tuple[str, ...],
) -> None:
    """Open a tunnel, run CMD with TUNSTRAP_*/KUBECONFIG injected, then tear down."""
    cmd = list(command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise click.UsageError("run requires a command: tunstrap run USER@HOST ... -- CMD [ARGS]")

    try:
        schema = _schema_from_flags(
            connection,
            ssh_key=ssh_key,
            ssh_key_passphrase=ssh_key_passphrase,
            ssh_password_stdin=ssh_password_stdin,
            targets=targets,
            kube=kube,
            fetch=fetch,
            auto_stop_idle_seconds=auto_stop_idle_seconds,
            materialize=materialize,
            log_file=log_file,
            force_materialize=True,
        )
        message = spawn_daemon(schema, session_dir=session_dir)
    except TunstrapError as exc:
        sys.stderr.write(json.dumps(exc.to_error_output()) + "\n")
        sys.exit(exit_code_for(exc))

    kind = message["kind"]
    if kind != "success":
        sys.stderr.write(json.dumps(message["payload"]) + "\n")
        sys.exit({"required_failure": 2, "session_active": 3, "daemon_error": 4}.get(kind, 4))

    out = OutputSchema.model_validate(message["payload"])
    child_env = {**os.environ, **render_env(out)}
    resolved_session_dir = out.session_dir

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    try:
        # Popen + .wait() (not subprocess.run) so SIGINT/SIGTERM can be
        # forwarded to the child while it runs in the foreground.
        proc = subprocess.Popen(  # noqa: SIM115  # pylint: disable=consider-using-with
            cmd, env=child_env
        )

        def _forward(signum: int, _frame: object) -> None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGINT, _forward)
        signal.signal(signal.SIGTERM, _forward)
        returncode = proc.wait()
    except OSError as exc:
        sys.stderr.write(f"run: failed to launch command: {exc}\n")
        returncode = 127
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        _teardown_run(resolved_session_dir, grace_seconds)
    sys.exit(returncode)


def _teardown_run(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon for session_dir and clean up. Never raises, never uses stdout.

    Under the tofu-proxy pattern fd 1 belongs to the child, so every teardown
    diagnostic goes to stderr and none of them changes the exit code: a child
    that ran and returned 7 still exits 7.
    """
    try:
        _teardown_run_inner(session_dir, grace_seconds)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        sys.stderr.write(f"run: teardown failed: {type(exc).__name__}: {exc}\n")


def _teardown_run_inner(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon and remove tunnel-data, reporting failures on stderr."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError:
        # No identity file: nothing to stop. Not an error — with the session
        # path minted before the spawn, a missing identity no longer means the
        # path is unknown, it means the daemon never recorded one.
        pass
    else:
        outcome = stop_session(session_dir, pid, grace_seconds, force=True)
        if not outcome.stopped:
            sys.stderr.write(f"run: daemon not stopped cleanly: {outcome.reason}\n")
    survivors = SessionDir.cleanup_path(session_dir)
    if survivors:
        sys.stderr.write("run: could not remove: " + ", ".join(survivors) + "\n")


def _stop_outcome_json(outcome: StopOutcome) -> str:
    """Render a StopOutcome as ``stop``'s documented stdout JSON, key for key.

    Key order and omission rules reproduce the pre-refactor writes in
    ``_kill_with_identity``: ``stopped`` first, then ``reason`` when there is
    one, then ``forced`` only when True.
    """
    body: dict[str, object] = {"stopped": outcome.stopped}
    if outcome.reason is not None:
        body["reason"] = outcome.reason
    if outcome.forced:
        body["forced"] = True
    return json.dumps(body)


@main.command("stop")
@click.option("--session-dir", "session_dir", required=True)
@click.option("--grace-seconds", type=int, default=10, show_default=True)
def stop_command(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon recorded under <session-dir>/tunnel-data and clean it up."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError as exc:
        sys.stdout.write(json.dumps({"stopped": False, "reason": str(exc)}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(0)
    outcome = stop_session(session_dir, pid, grace_seconds, force=True)
    sys.stdout.write(_stop_outcome_json(outcome))
    sys.stdout.write("\n")
    sys.stdout.flush()
    SessionDir.cleanup_path(session_dir)


@main.command("status")
@click.option("--session-dir", "session_dir", required=True)
def status_command(session_dir: str) -> None:
    """Report whether the daemon for the given session dir is alive."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError:
        alive = False
    else:
        alive = verify_session(session_dir, pid) == IdentityCheckResult.match
    sys.stdout.write(json.dumps({"alive": alive}))
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    main()
