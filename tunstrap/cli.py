"""Command-line interface. Subcommands are added in later tasks."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Any, NoReturn, TypeVar

import click

from tunstrap.cli_input import (
    build_flag_schema,
    build_schema_from_env,
    build_schema_from_stdin,
)
from tunstrap.daemon import spawn_daemon
from tunstrap.envrender import (
    format_exports,
    materialized_output_path,
    predicted_env_keys,
    render_kube_env,
    render_output_var,
    write_materialized_output,
)
from tunstrap.exceptions import (
    DaemonError,
    DaemonHandshakeError,
    TunstrapError,
    exit_code_for,
)
from tunstrap.identity import IdentityCheckResult, verify_session
from tunstrap.schemas import InputSchema, OutputSchema
from tunstrap.session import (
    SessionDir,
    SessionError,
    SessionIdentityUnreadable,
    StopOutcome,
    stop_session,
)

_FC = TypeVar("_FC", bound=Callable[..., object])
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def _show_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Lazy ``--version`` callback: resolve ``__version__`` only when invoked.

    Replaces ``@click.version_option(__version__, ...)`` so importing ``cli``
    does not trigger ``importlib.metadata`` (the package ``__init__`` resolves
    ``__version__`` lazily too). Every ``tunstrap`` invocation that does not pass
    ``--version`` skips the cost entirely.
    """
    if not value or ctx.resilient_parsing:
        return
    # ``__version__`` is provided dynamically by tunstrap/__init__.py's PEP 562
    # __getattr__; pylint cannot see it statically (false-positive E0611).
    # pylint: disable-next=import-outside-toplevel,no-name-in-module
    from tunstrap import __version__

    click.echo(f"tunstrap, version {__version__}")
    ctx.exit()


@click.group(cls=_UsageExit64)
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_show_version,
    help="Show the version and exit.",
)
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


def _start_schema(
    connection: str | None,
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
    output_fmt: str,
) -> InputSchema:
    """Pick ``start``'s input channel and assemble the schema from it.

    The two channels are mutually exclusive and the guards that enforce that
    belong here, with the assembly: a connection argument forbids JSON on
    stdin, and connection flags require a connection argument. Stdin is not
    consumed under ``--ssh-password-stdin`` — the password read owns it.

    ``--output env`` forces materialization (``render_kube_env`` needs a
    kubeconfig on disk) in both channels: flag mode via ``force_materialize``,
    and a stdin payload here, overriding its own ``daemon.materialize`` --
    otherwise a stdin payload declaring ``kube_targets`` with
    ``materialize: false`` would reach the unconditional ``render_kube_env``
    call with an unmaterialized path and raise a bare ``ValueError``.
    """
    if connection is None:
        if _conn_flags_present(
            ssh_key=ssh_key,
            ssh_key_passphrase=ssh_key_passphrase,
            ssh_password_stdin=ssh_password_stdin,
            targets=targets,
            kube=kube,
            fetch=fetch,
        ):
            raise click.UsageError("connection flags require a USER@HOST[:PORT] argument")
        schema = build_schema_from_stdin(sys.stdin.read())
        if output_fmt == "env":
            schema.daemon.materialize = True
        return schema

    if not ssh_password_stdin and sys.stdin.read().strip():
        raise click.UsageError(
            "cannot combine a connection argument with JSON on stdin; use flags or stdin, not both"
        )
    return build_flag_schema(
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


def _emit_start_result(message: dict[str, Any], output_fmt: str) -> None:
    """Write ``start``'s envelope to stdout, then exit with the mapped code.

    Success under ``--output env`` is the only combination rendered as shell
    exports; everything else is the raw JSON payload. A ``success`` kind (or an
    unrecognised one) returns instead of exiting, leaving Click to exit 0 —
    the same fall-through the original if-chain had. ``--output env`` also
    materializes ``output.json`` under the session dir, same as ``run``, so a
    consumer reading ``TUNSTRAP_OUTPUT_FILE`` sees the same contract either way.
    """
    kind = message["kind"]
    if kind == "success" and output_fmt == "env":
        out = OutputSchema.model_validate(message["payload"])
        write_materialized_output(out)
        env = {
            "TUNSTRAP_SESSION_DIR": out.session_dir,
            "TUNSTRAP_PID": str(out.pid),
            "TUNSTRAP_OUTPUT_FILE": materialized_output_path(out.session_dir),
        }
        env.update(render_kube_env(out))
        sys.stdout.write(format_exports(env))
    else:
        sys.stdout.write(json.dumps(message["payload"]) + "\n")
    sys.stdout.flush()
    code = {"required_failure": 2, "daemon_error": 4, "session_active": 3}.get(kind)
    if code is not None:
        sys.exit(code)


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
def start_command(
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
        schema = _start_schema(
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
            output_fmt=output_fmt,
        )
        _emit_start_result(spawn_daemon(schema, session_dir=session_dir), output_fmt)
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


def _split_run_args(
    args: tuple[str, ...], *, input_env: str | None
) -> tuple[str | None, list[str]]:
    """Split ``run``'s single variadic into (CONNECTION, child command).

    Click distributes post-``--`` tokens over the declared positionals in
    order, so a separate CONNECTION positional binds the child's program name:
    ``run --input-env X -- tofu plan`` yields ``connection='tofu'``. One
    variadic split after parsing is the only surface that can express "no
    connection" while keeping the documented ``run USER@HOST -- CMD`` form and
    its integration tests working.
    """
    connection: str | None = None
    rest: tuple[str, ...] = args
    if input_env is None:
        if not args:
            raise click.UsageError("run requires USER@HOST[:PORT] or --input-env VAR")
        connection = args[0]
        rest = args[1:]
    cmd = list(rest)
    # Click consumes the first `--` and only the first, so a doubled separator
    # leaves a literal "--" at the head of the child command.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise click.UsageError("run requires a command: tunstrap run USER@HOST ... -- CMD [ARGS]")
    return connection, cmd


def _validate_output_var(name: str, schema: InputSchema) -> None:
    """Reject an ``--output-var`` NAME that is invalid or would collide.

    Evaluated pre-spawn against the *input* schema, because the output schema
    does not exist yet and a usage error must never be able to orphan a
    daemon. Collision with an unrelated inherited variable is a documented
    overwrite; only the keys ``run`` itself injects are protected.
    """
    if not _ENV_NAME_RE.match(name):
        raise click.UsageError(f"--output-var NAME must match [A-Za-z_][A-Za-z0-9_]*; got {name!r}")
    if name in predicted_env_keys(schema):
        raise click.UsageError(
            f"--output-var {name} collides with an environment key run already injects"
        )


def _reject_flags_under_input_env(
    *,
    conn_flags: bool,
    auto_stop_idle_seconds: int | None,
    grace_seconds_set: bool,
    materialize: bool,
    log_file: str | None,
) -> None:
    """Every flag ``--input-env`` makes redundant is a usage error (64).

    Rejected rather than given a precedence order: the payload's ``daemon``
    block is complete and authoritative, so there must be exactly one place to
    look when a tunnel misbehaves. The daemon flags need their own rule
    because ``_connection_options`` attaches them but ``_conn_flags_present``
    deliberately excludes them.
    """
    if conn_flags:
        raise click.UsageError(
            "--input-env supplies the full InputSchema; connection flags are redundant"
        )
    if auto_stop_idle_seconds is not None:
        raise click.UsageError(
            "--auto-stop-idle-seconds conflicts with --input-env; "
            "set daemon.auto_stop_idle_seconds in the payload"
        )
    if grace_seconds_set:
        raise click.UsageError(
            "--grace-seconds conflicts with --input-env; "
            "set daemon.shutdown_grace_seconds in the payload"
        )
    if log_file is not None:
        raise click.UsageError(
            "--log-file conflicts with --input-env; set daemon.log_file in the payload"
        )
    if materialize:
        raise click.UsageError("--materialize conflicts with --input-env; run always materializes")


def _build_child_env(
    output: OutputSchema,
    *,
    output_var: str | None,
    input_env: str | None,
    suppress_kubeconfig: bool = False,
) -> dict[str, str]:
    """Inherited env, scrubbed of the input payload, plus the exported channels.

    Unconditional on node count: the three session scalars
    (``TUNSTRAP_SESSION_DIR``/``_PID``/``_OUTPUT_FILE``) and the kube channel
    are injected regardless of how many nodes or kube targets exist.

    ``input_env`` names the variable holding the InputSchema, whose ``ssh_pkey``
    is an SSH private key. ``run`` is the one component that knows this variable
    is secret-bearing, and the child is ``tofu``, which hands its environment to
    every provider plugin, ``external`` data source and ``local-exec``
    provisioner — so it is removed here. The removal happens *before* anything is
    injected, so an operator who passes the same NAME to both flags gets the
    projected output rather than the untouched secret restored under it.

    ``output_var`` carries ``render_output_var``'s projection, not the whole
    envelope: its consumer persists the value into an OpenTofu plan file.

    ``suppress_kubeconfig`` drops ``KUBECONFIG``/``KUBE_CONFIG_PATH``/
    ``KUBE_CONFIG_PATHS`` — both anything inherited and anything
    ``render_kube_env`` injects. The proxy uses this so a broken
    ``TF_VAR_tunstrap`` → ``config_path`` chain cannot silently reach the
    cluster through a materialized-file ``KUBECONFIG`` (the property the
    consumer shim used to buy with ``env -u KUBECONFIG``).
    """
    child_env = dict(os.environ)
    if input_env is not None:
        child_env.pop(input_env, None)
    child_env["TUNSTRAP_SESSION_DIR"] = output.session_dir
    child_env["TUNSTRAP_PID"] = str(output.pid)
    child_env["TUNSTRAP_OUTPUT_FILE"] = materialized_output_path(output.session_dir)
    child_env.update(render_kube_env(output))
    if suppress_kubeconfig:
        for key in ("KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"):
            child_env.pop(key, None)
    if output_var is not None:
        child_env[output_var] = render_output_var(output)
    return child_env


def _mint_session_dir(session_dir: str | None) -> tuple[str, str | None]:
    """Return (the session path to use, the root ``run`` minted, or ``None``).

    ``run`` must know the session path **before** spawning. When the caller
    supplies none, the worker generates it (``session.py:53-56``), so the
    parent could only recover the path by parsing the success envelope — which
    makes cleanup depend on the very object whose validation can fail. Minting
    it here makes the path a precondition of spawning.

    ``SessionDir.create`` accepts a supplied absolute path and does
    ``mkdir(parents=True, exist_ok=True)`` (``session.py:57-63``), so an empty
    pre-created directory is valid worker input. A supplied path sets
    ``generated=False``, so the worker never removes the root; ``run``
    therefore removes the root it minted itself, and only that one.
    """
    if session_dir is not None:
        return session_dir, None
    minted = tempfile.mkdtemp(prefix="tunstrap-run-")
    return minted, minted


def _discard_minted_root(minted_root: str | None) -> None:
    """Remove a session root ``run`` minted but never spawned into. Never raises."""
    if minted_root is not None:
        SessionDir.remove_root(minted_root)


def _fail_before_child(exc: TunstrapError) -> NoReturn:
    """Report a ``run`` failure raised before the child started, then exit.

    stderr rather than stdout because under the tofu-proxy pattern fd 1 belongs
    to the child; the exit code is the exception's mapped one, never a generic
    failure code. Shared by all three pre-child handlers so that they differ
    only in the cleanup each one owes.
    """
    sys.stderr.write(json.dumps(exc.to_error_output()) + "\n")
    sys.exit(exit_code_for(exc))


def _report_unexpected(exc: BaseException) -> None:
    """Report an unexpected post-spawn failure as DaemonError JSON on stderr.

    Mirrors ``start``'s top-level guard (``cli.py:241-254``) except for the
    channel: under the tofu-proxy pattern fd 1 belongs to the child, so ``run``
    never writes a diagnostic to stdout.
    """
    sys.stderr.write(
        json.dumps(
            DaemonError(
                "unexpected failure during run", {"type": type(exc).__name__}
            ).to_error_output()
        )
        + "\n"
    )


def _run_child(
    payload: Any,
    cmd: list[str],
    *,
    output_var: str | None,
    input_env: str | None,
    suppress_kubeconfig: bool = False,
) -> int:
    """Validate the success payload, materialize output.json, run the child.

    Every statement here runs inside ``_supervise_child``'s teardown ``try``,
    including ``OutputSchema.model_validate`` — which is exactly the case an
    earlier design left unguarded: a malformed success payload orphaned the
    daemon, because the session path was recovered from that same payload.
    Materialization runs unconditionally, before the child starts, so
    ``TUNSTRAP_OUTPUT_FILE`` always names a file that already exists.
    """
    out = OutputSchema.model_validate(payload)
    write_materialized_output(out)
    child_env = _build_child_env(
        out,
        output_var=output_var,
        input_env=input_env,
        suppress_kubeconfig=suppress_kubeconfig,
    )
    # Popen + .wait() (not subprocess.run) so SIGINT/SIGTERM can be
    # forwarded to the child while it runs in the foreground.
    # pylint: disable-next=consider-using-with
    proc = subprocess.Popen(cmd, env=child_env)

    def _forward(signum: int, _frame: object) -> None:
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)
    returncode = proc.wait()
    if returncode < 0:
        # Popen reports "killed by signal N" as -N, and sys.exit hands that to
        # the OS, which truncates modulo 256 -- a SIGTERMed child surfaced as
        # 241. 128+N is the shell convention every caller already reads out of
        # $?, so a wrapped tofu killed by a signal reports 143, not 241.
        return 128 - returncode
    return returncode


def _supervise_child(  # pylint: disable=too-many-arguments
    payload: Any,
    cmd: list[str],
    *,
    output_var: str | None,
    input_env: str | None,
    session_dir: str,
    grace_seconds: int,
    minted_root: str | None,
    suppress_kubeconfig: bool = False,
) -> int:
    """Own the whole post-spawn window; the daemon is stopped on every path.

    The ``try`` opens on the first statement and the caller invokes this with
    nothing between it and a successful ``spawn_daemon`` — the payload is read
    out of the envelope beforehand, because an argument expression is evaluated
    in the caller and so would sit outside this window. Handlers are saved into
    a list *inside* the ``try``, so even a failure capturing them leaves the
    teardown reachable, and the restoration loop is nested in its own ``try``
    whose ``finally`` performs the teardown.
    """
    saved: list[tuple[int, Any]] = []
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            saved.append((signum, signal.getsignal(signum)))
        return _run_child(
            payload,
            cmd,
            output_var=output_var,
            input_env=input_env,
            suppress_kubeconfig=suppress_kubeconfig,
        )
    except OSError as exc:
        sys.stderr.write(f"run: failed to launch command: {exc}\n")
        return 127
    finally:
        try:
            # A distinct name: `signum` above is bound to signal.Signals, and
            # reusing it here for the plain int in `saved` fails mypy --strict.
            for saved_signum, handler in saved:
                signal.signal(saved_signum, handler)
        finally:
            _teardown_run(session_dir, grace_seconds, minted_root=minted_root)


@main.command("run")
@_connection_options
@click.option("--input-env", "input_env", default=None, metavar="VAR")
@click.option("--output-var", "output_var", default=None, metavar="NAME")
@click.option("--session-dir", "session_dir", default=None)
@click.option("--grace-seconds", "grace_seconds", type=int, default=10, show_default=True)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def run_command(  # pylint: disable=too-many-locals,too-many-statements
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password_stdin: bool,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    auto_stop_idle_seconds: int | None,
    materialize: bool,
    log_file: str | None,
    input_env: str | None,
    output_var: str | None,
    session_dir: str | None,
    grace_seconds: int,
    args: tuple[str, ...],
    suppress_kubeconfig: bool = False,
) -> None:
    """Open a tunnel, run CMD with TUNSTRAP_*/KUBECONFIG injected, then tear down.

    Input is either a USER@HOST[:PORT] positional plus flags, or the complete
    InputSchema JSON in the environment variable named by --input-env. `--` is
    mandatory whenever the child command or any of its arguments begins with
    `-`.
    """
    connection, cmd = _split_run_args(args, input_env=input_env)
    context = click.get_current_context(silent=True)
    grace_seconds_set = (
        context is not None
        and context.get_parameter_source("grace_seconds") != click.core.ParameterSource.DEFAULT
    )
    try:
        if input_env is not None:
            _reject_flags_under_input_env(
                conn_flags=_conn_flags_present(
                    ssh_key=ssh_key,
                    ssh_key_passphrase=ssh_key_passphrase,
                    ssh_password_stdin=ssh_password_stdin,
                    targets=targets,
                    kube=kube,
                    fetch=fetch,
                ),
                auto_stop_idle_seconds=auto_stop_idle_seconds,
                grace_seconds_set=grace_seconds_set,
                materialize=materialize,
                log_file=log_file,
            )
            schema = build_schema_from_env(input_env)
            grace_seconds = schema.daemon.shutdown_grace_seconds
            # The one place `run` mutates the supplied schema. It is an
            # invariant of the verb, not a flag precedence rule:
            # render_kube_env needs a materialized kubeconfig path
            # (envrender.py), and an unmaterialized target would hand
            # --output-var consumers `path: null` and the kubernetes/helm
            # providers an empty config_path.
            schema.daemon.materialize = True
        elif connection is not None:
            schema = build_flag_schema(
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
        else:  # pragma: no cover - _split_run_args already rejected this arity
            raise click.UsageError("run requires USER@HOST[:PORT] or --input-env VAR")
        if output_var is not None:
            _validate_output_var(output_var, schema)
    except TunstrapError as exc:
        # The validation window: nothing has been minted and nothing spawned,
        # so there is nothing to clean up. A click.UsageError is unrelated to
        # TunstrapError and passes this handler untouched.
        _fail_before_child(exc)

    # The spawn window opens here. Minting is its first statement and the last
    # before the spawn itself, so every check above ran before the first side
    # effect, and both names below are bound on every path that can reach the
    # handlers -- `spawn_daemon` is the only source of either exception.
    session_path, minted_root = _mint_session_dir(session_dir)
    try:
        message = spawn_daemon(schema, session_dir=session_path, input_env=input_env)
    except DaemonHandshakeError as exc:
        # Parent-side, past the detach: `Popen` has already launched a worker,
        # so one may be running and holding the session lock. Taking the
        # worker-authored path below would delete a live daemon's directory and
        # leave nothing able to stop it.
        _teardown_run(session_path, grace_seconds, minted_root=minted_root)
        _fail_before_child(exc)
    except TunstrapError as exc:
        # Worker-authored, or raised before the detach: nothing of ours is
        # running, so there is only the empty minted directory to remove.
        _discard_minted_root(minted_root)
        _fail_before_child(exc)

    try:
        kind = message["kind"]
        payload = message["payload"]
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # An envelope we cannot index leaves us unable to tell whether a worker
        # is live, and an orphan is the one outcome this window exists to
        # prevent — so tear down rather than guess. Harmless when no daemon
        # ran: _teardown_run_inner simply finds no recorded pid.
        _teardown_run(session_path, grace_seconds, minted_root=minted_root)
        _report_unexpected(exc)
        sys.exit(4)

    if kind != "success":
        # No daemon of ours is running on these paths, and session_active means
        # the pid under the session dir belongs to somebody else's live
        # session, so teardown here would stop a daemon we do not own.
        _discard_minted_root(minted_root)
        sys.stderr.write(json.dumps(payload) + "\n")
        sys.exit({"required_failure": 2, "session_active": 3, "daemon_error": 4}.get(kind, 4))

    # Nothing whatsoever between a successful spawn and the try that owns
    # teardown: _supervise_child opens it on its first statement.
    try:
        returncode = _supervise_child(
            payload,
            cmd,
            output_var=output_var,
            input_env=input_env,
            session_dir=session_path,
            grace_seconds=grace_seconds,
            minted_root=minted_root,
            suppress_kubeconfig=suppress_kubeconfig,
        )
    except TunstrapError as exc:
        # An expected outcome keeps its own exit code, mapped through
        # exit_code_for rather than the generic "unexpected failure during
        # run". Nothing in the current post-spawn window raises a
        # TunstrapError, but a caller of _run_child's helpers doing so in the
        # future gets its own exit code instead of a blanket exit 4. Teardown
        # has already run in _supervise_child's finally, as it has below.
        sys.stderr.write(json.dumps(exc.to_error_output()) + "\n")
        returncode = exit_code_for(exc)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Teardown has already run in _supervise_child's finally.
        _report_unexpected(exc)
        returncode = 4
    sys.exit(returncode)


def run_via_env_input(
    input_env: str,
    output_var: str,
    child_cmd: list[str],
    *,
    suppress_kubeconfig: bool = False,
) -> None:
    """Invoke ``run``'s env-input mode programmatically, without re-parsing.

    A programmatic caller (the ``tunstrap_tofu`` console entry) already knows
    its input/output variable names and child command and does not need Click's
    argv parser. This delegates to ``run_command``'s callback so every pre-spawn
    check, the whole spawn window, and the teardown path are shared with the CLI
    entry — not duplicated. ``run_command`` always exits; the trailing
    ``sys.exit`` is the unreachable safety net for readers and type checkers.

    No ``grace_seconds`` parameter: ``run_command`` overwrites it from
    ``schema.daemon.shutdown_grace_seconds`` before use, same as ``--input-env``.

    Generic on purpose: this helper carries no Terraform vocabulary, only the
    variables and flags a programmatic ``run`` caller supplies. The Terraform-
    specific decisions (``TUNSTRAP_INPUT``, ``TF_VAR_tunstrap``, ``tofu``) live
    in the proxy module.

    ``run_command`` is invoked via ``.callback``, outside Click's group, so the
    ``_UsageExit64`` wrapper that turns a ``click.UsageError`` into exit 64 does
    not apply here. A ``UsageError`` from ``run``'s pre-spawn validation
    (e.g. an ``--output-var`` name that collides with an injected key) is caught
    and rendered with Click's own formatter, then exits 64 — preserving the
    group's contract rather than surfacing as a raw traceback.
    """
    run = run_command.callback
    if run is None:
        # Unreachable in practice: Click always sets `.callback` on a
        # decorated command. It must not be an `assert` though - that is an
        # AssertionError, which is outside TunstrapError and so escapes the
        # CLI's handler as a traceback, and `python -O` erases the check
        # altogether, leaving a TypeError on the next line instead.
        raise TunstrapError("run_command has no callback; Click wiring is broken", {})
    try:
        run(
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
            # run_command's own default (cli.py:578); overwritten before use.
            grace_seconds=10,
            args=tuple(child_cmd),
            suppress_kubeconfig=suppress_kubeconfig,
        )
    except click.UsageError as exc:
        # Mirror _UsageExit64 (cli.py:53-59): render Click's usage message and
        # exit 64, never a raw traceback. Fires pre-spawn, so no daemon to clean.
        exc.show()
        sys.exit(64)
    sys.exit(0)  # pragma: no cover — run_command.callback always exits


def _teardown_run(session_dir: str, grace_seconds: int, *, minted_root: str | None) -> None:
    """Stop the daemon and clean up without propagating any exception or using stdout.

    Under the tofu-proxy pattern fd 1 belongs to the child, so every teardown
    diagnostic is attempted on stderr and none of them changes the exit code:
    a child that ran and returned 7 still exits 7, even if teardown or its
    diagnostic is interrupted.

    A raising teardown preserves the session data, exactly as a reported stop
    failure does, and for a stronger reason: ``StopOutcome(False, …)`` means we
    know the daemon survived, while an exception — ``stop_session`` failing on
    a recycled pid, or a second Ctrl-C landing inside the grace poll — means we
    know nothing about its state. Removing the root would take the identity
    file with it and leave a possibly-live daemon nobody can find. Nothing
    else here can realistically raise: ``read_identity`` raises only
    ``SessionError`` and its ``SessionIdentityUnreadable`` subclass, both of
    which the inner function handles by name, and ``cleanup_path`` and
    ``remove_root`` are non-raising by construction (``session.py:_rmtree_reporting``).
    """
    try:
        _teardown_run_inner(session_dir, grace_seconds, minted_root=minted_root)
    except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _warn_preserved(session_dir, f"teardown failed: {type(exc).__name__}: {exc}", minted_root)


def _warn_preserved(session_dir: str, cause: str, minted_root: str | None) -> None:
    """Report an unresolved teardown and the command that finishes it by hand.

    One wording for both ways teardown can end without a confirmed stop — a
    reported failure and a raised one — because they carry the same operator
    consequence: a daemon that may still be running, whose identity file under
    ``session_dir`` is the only handle on it, so the data is kept rather than
    deleted.

    The command is exactly what ``stop`` accepts: ``--session-dir`` and
    ``--grace-seconds``, nothing else. ``stop`` already forces unconditionally
    (``stop_command`` passes ``force=True``), so there is no ``--force`` to
    offer — and a diagnostic naming a flag that does not exist is worse than
    none, because the operator follows it and gets a usage error.

    ``stop`` removes ``tunnel-data`` but never its own ``--session-dir``
    argument, which is normally the operator's own directory; it cannot tell a
    run-minted temp root from one somebody cares about. ``run`` can — it minted
    it — so the disposal note is emitted here, and only for a minted root.
    """
    recovery = f"run: {cause}; preserving session data. Recover with: "
    recovery += f"tunstrap stop --session-dir {session_dir}\n"
    if minted_root is not None:
        recovery += (
            f"run: {minted_root} was created by run and is not removed by that "
            f"command; delete it once the daemon is dealt with\n"
        )
    _warn(recovery)


def _warn(message: str) -> None:
    """Attempt a teardown diagnostic without allowing a closed stderr to escape."""
    try:
        sys.stderr.write(message)
    except BaseException:  # noqa: BLE001, S110  # pylint: disable=broad-exception-caught
        pass


def _teardown_run_inner(session_dir: str, grace_seconds: int, *, minted_root: str | None) -> None:
    """Stop the daemon, remove tunnel-data and any minted root; report on stderr."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionIdentityUnreadable as exc:
        # Something is recorded there and we cannot address it: no pid means no
        # stop, and no way to prove nothing is running. Identical unknown state
        # to a failed stop, so identical answer — preserve rather than delete.
        # Must precede the SessionError arm; it is a subclass of it.
        _warn_preserved(session_dir, f"cannot read the daemon identity: {exc}", minted_root)
        return
    except SessionError:
        # No identity file at all: nothing to stop. Not an error — with the
        # session path minted before the spawn, a missing identity no longer
        # means the path is unknown, it means the daemon never recorded one.
        pass
    else:
        outcome = stop_session(session_dir, pid, grace_seconds, force=True)
        if not _stop_resolved(outcome):
            _warn_preserved(
                session_dir, f"daemon not stopped cleanly: {outcome.reason}", minted_root
            )
            return
    survivors = SessionDir.cleanup_path(session_dir)
    if survivors:
        _warn("run: could not remove: " + ", ".join(survivors) + "\n")
    if minted_root is not None:
        remaining = SessionDir.remove_root(minted_root)
        if remaining:
            _warn("run: could not remove session root: " + ", ".join(remaining) + "\n")


def _stop_resolved(outcome: StopOutcome) -> bool:
    """True when the daemon is known to be gone, so its session data is safe to delete.

    The single expression of that rule. ``run``'s teardown and ``stop`` both
    have to decide it, and stating it twice is how they drift — which is
    exactly what happened: ``_teardown_run_inner`` preserved on an unresolved
    outcome while ``stop`` deleted unconditionally, so following the recovery
    command ``run`` prints destroyed the identity file the preservation existed
    to keep.

    ``"not found"`` is a *resolved* outcome, not a failure: it means no daemon
    is recorded as running, which is the normal shape when auto-stop-idle
    already fired. Everything else with ``stopped=False`` leaves the daemon's
    state unknown.
    """
    return outcome.stopped or outcome.reason == "not found"


def _stop_outcome_json(outcome: StopOutcome) -> str:
    """Render a StopOutcome as ``stop``'s documented stdout JSON, key for key.

    Key order and omission rules are a public contract, pinned byte for byte
    across all seven outcomes by ``tests/unit/test_cli_stop_output.py``:
    ``stopped`` first, then ``reason`` when there is one, then ``forced`` only
    when True, then ``preserved`` only when the session data was kept.

    ``preserved`` is additive and omitted when false, so every previously
    emitted shape — including the most-parsed ``{"stopped": true}`` — is
    byte-identical to before. It is here rather than left for the caller to
    infer because the rule is not derivable without string-matching
    ``reason`` against ``"not found"``, and a caller that has to replicate an
    internal reason string to learn whether state is still on disk is a caller
    we have set up to break.
    """
    body: dict[str, object] = {"stopped": outcome.stopped}
    if outcome.reason is not None:
        body["reason"] = outcome.reason
    if outcome.forced:
        body["forced"] = True
    if not _stop_resolved(outcome):
        body["preserved"] = True
    return json.dumps(body)


def _emit_stop_outcome(outcome: StopOutcome, session_dir: str) -> None:
    """Write ``stop``'s envelope on stdout, plus the stderr notice when data was kept.

    Both of ``stop``'s exits report through here, so an outcome cannot be
    reported without the signal that belongs to it. The identity-read failures
    used to render their own JSON literal inline, which is precisely how they
    ended up preserving ``tunnel-data`` while emitting no ``preserved`` key —
    a caller reading the envelope concluded the directory had been cleaned.
    """
    sys.stdout.write(_stop_outcome_json(outcome))
    sys.stdout.write("\n")
    sys.stdout.flush()
    if not _stop_resolved(outcome):
        _warn(
            f"tunstrap stop: daemon not stopped: {outcome.reason}; "
            f"session data preserved under {session_dir}\n"
        )


@main.command("stop")
@click.option("--session-dir", "session_dir", required=True)
@click.option("--grace-seconds", type=int, default=10, show_default=True)
def stop_command(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon recorded under <session-dir>/tunnel-data and clean it up."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError as exc:
        # All three identity-read failures — missing, unreadable, malformed —
        # return before cleanup, so all three preserve and all three must say
        # so. Deliberately not the split ``run`` makes: there
        # ``SessionIdentityUnreadable`` decides whether to delete, while here
        # nothing is deleted either way.
        _emit_stop_outcome(StopOutcome(False, str(exc)), session_dir)
        sys.exit(0)
    outcome = stop_session(session_dir, pid, grace_seconds, force=True)
    _emit_stop_outcome(outcome, session_dir)
    if _stop_resolved(outcome):
        # Deleting on an unresolved outcome would make the recovery command
        # ``run`` prints destroy the identity file it was invoked to recover.
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
