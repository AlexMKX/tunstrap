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
    build_start_schema,
    connection_flags_present,
)
from tunstrap.cli_stop import _stop_resolved, _warn, status_command, stop_command
from tunstrap.daemon import spawn_daemon
from tunstrap.envrender import (
    KUBE_ENV_NAMES,
    RUN_ENV_KEYS,
    format_exports,
    materialized_output_path,
    render_kube_env,
    render_output_var,
    render_start_json,
    write_materialized_output,
)
from tunstrap.exceptions import (
    DaemonError,
    DaemonHandshakeError,
    TunstrapError,
    exit_code_for,
)
from tunstrap.schemas import OutputSchema
from tunstrap.session import (
    SessionDir,
    SessionError,
    SessionIdentityUnreadable,
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
        """Force usage errors through the CLI's documented sysexits-compatible path."""
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


def _session_scalars(out: OutputSchema) -> dict[str, str]:
    """The three survivor scalars, shared to avoid a second hardcoded copy."""
    return {
        "TUNSTRAP_SESSION_DIR": out.session_dir,
        "TUNSTRAP_PID": str(out.pid),
        "TUNSTRAP_OUTPUT_FILE": materialized_output_path(out.session_dir),
    }


def _write_start_error(payload: object, output_fmt: str) -> None:
    """Write a start error where its selected output contract expects it."""
    stream = sys.stderr if output_fmt == "env" else sys.stdout
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


def _emit_start_result(message: dict[str, Any], output_fmt: str) -> None:
    """Write ``start``'s envelope to stdout, then exit with the mapped code.
    Only ``--output env`` renders shell exports; ``write_materialized_output``
    writes ``output.json``. ``TUNSTRAP_OUTPUT_FILE`` has ``run``'s contract;
    JSON projects on ``path is not None``. One session per daemon makes this
    match ``daemon.materialize``; unmaterialized retain ``content_b64``;
    success and unrecognised kinds return, so Click exits 0.
    """
    kind = message["kind"]
    if kind == "success" and output_fmt == "env":
        out = OutputSchema.model_validate(message["payload"])
        write_materialized_output(out)
        env = _session_scalars(out)
        env.update(render_kube_env(out))
        sys.stdout.write(format_exports(env))
    elif kind == "success":
        out = OutputSchema.model_validate(message["payload"])
        sys.stdout.write(json.dumps(render_start_json(out)) + "\n")
    else:
        _write_start_error(message["payload"], output_fmt)
    if kind == "success":
        sys.stdout.flush()
    code = {"required_failure": 2, "daemon_error": 4, "session_active": 3}.get(kind)
    if code is not None:
        sys.exit(code)


def _start_recovery_handles(message: object) -> tuple[str, int] | None:
    """Return usable handles from a success envelope that failed after spawning.

    These fields are read without validating the whole payload because payload
    validation is itself one of the post-spawn operations that can fail.  A
    non-string path or non-positive/bool pid is not safe to print as a recovery
    handle, so a malformed envelope retains the generic error contract.
    """
    if not isinstance(message, dict) or message.get("kind") != "success":
        return None
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    session_dir = payload.get("session_dir")
    pid = payload.get("pid")
    if (
        not isinstance(session_dir, str)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
    ):
        return None
    return session_dir, pid


def _report_start_post_spawn_failure(
    exc: BaseException,
    message: object,
    supplied_session_dir: str | None,
    output_fmt: str,
) -> None:
    """Report an output failure without discarding a live daemon's handles.

    ``start`` is detached and does not own teardown. For a success envelope,
    the worker has already reported the authoritative root, so pre-minting adds
    nothing here; it would only help on the handshake-failure path, which this
    change does not address.
    """
    details: dict[str, object] = {"type": type(exc).__name__}
    handles = _start_recovery_handles(message)
    if handles is not None:
        session_dir, pid = handles
        details["session_dir"] = session_dir
        details["pid"] = pid
        _warn_preserved(
            session_dir,
            f"output failed after daemon start: {type(exc).__name__}: {exc}",
            None,
            verb="start",
        )
    elif supplied_session_dir is not None:
        details["session_dir"] = supplied_session_dir
        _warn_preserved(
            supplied_session_dir,
            f"output failed after daemon start: {type(exc).__name__}: {exc}",
            None,
            verb="start",
        )
    _write_start_error(
        DaemonError("unexpected failure during start", details).to_error_output(), output_fmt
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
def start_command(  # pylint: disable=too-many-locals
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
        schema = build_start_schema(
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
        message = spawn_daemon(schema, session_dir=session_dir)
        try:
            _emit_start_result(message, output_fmt)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            _report_start_post_spawn_failure(exc, message, session_dir, output_fmt)
            sys.exit(4)
    except click.UsageError:
        raise
    except TunstrapError as exc:
        _write_start_error(exc.to_error_output(), output_fmt)
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Top-level guard: surface any unexpected failure as DaemonError JSON and
        # exit 4 instead of dumping a Python traceback to a caller.
        _write_start_error(
            DaemonError(
                "unexpected failure during start",
                {"type": type(exc).__name__},
            ).to_error_output(),
            output_fmt,
        )
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


def _validate_output_var(name: str, *, input_env: str | None) -> None:
    """Reject an ``--output-var`` NAME that is invalid or would collide.

    Evaluated pre-spawn, because the output schema does not exist yet and a
    usage error must never be able to orphan a daemon. Collision with an
    unrelated inherited variable is a documented overwrite; only the keys
    ``run`` itself injects or scrubs, plus ``--input-env``'s own NAME, are
    protected. ``_build_child_env`` pops ``input_env`` before assigning
    ``output_var``, so the two sharing a NAME is harmless today -- but that
    is an ordering property, not a contract, and reusing one NAME for both
    is almost certainly an operator mistake regardless.
    """
    if not _ENV_NAME_RE.match(name):
        raise click.UsageError(f"--output-var NAME must match [A-Za-z_][A-Za-z0-9_]*; got {name!r}")
    if name in RUN_ENV_KEYS:
        raise click.UsageError(
            f"--output-var {name} collides with an environment key run already injects"
        )
    if name == input_env:
        raise click.UsageError(f"--output-var {name} collides with --input-env {name}")


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
    because ``_connection_options`` attaches them but
    ``cli_input.connection_flags_present`` deliberately excludes them.
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

    Session scalars (``_session_scalars``) + kube channel are both unconditional on node count.

    ``input_env`` names the variable holding the InputSchema, whose ``ssh_pkey``
    is an SSH private key. ``run`` is the one component that knows this variable
    is secret-bearing, and the child is ``tofu``, which hands its environment to
    every provider plugin, ``external`` data source and ``local-exec``
    provisioner — so it is removed here. The removal happens *before* anything is
    injected, so an operator who passes the same NAME to both flags gets the
    projected output rather than the untouched secret restored under it.

    ``output_var`` carries ``render_output_var``'s projection, not the whole
    envelope: its consumer persists the value into an OpenTofu plan file.

    ``suppress_kubeconfig`` drops only the *injected* ``KUBECONFIG``, keeping
    ``KUBE_CONFIG_PATH``/``KUBE_CONFIG_PATHS`` -- Mode A's proxy channel. Providers
    never read plain ``KUBECONFIG`` (measured in #15); the guard protects
    ``kubectl``/``helm`` CLI children and ``local-exec`` provisioners. Inherited
    names of all three are always dropped before injection, on both paths.
    """
    child_env = dict(os.environ)
    if input_env is not None:
        child_env.pop(input_env, None)
    for key in KUBE_ENV_NAMES:
        child_env.pop(key, None)
    child_env.update(_session_scalars(output))
    child_env.update(render_kube_env(output))
    if suppress_kubeconfig:
        child_env.pop("KUBECONFIG", None)
    if output_var is not None:
        child_env[output_var] = render_output_var(output)
    return child_env


def _mint_session_dir(session_dir: str | None) -> tuple[str, str | None]:
    """Return (the session path to use, the root ``run`` minted, or ``None``).

    ``run`` must know the session path **before** spawning. When the caller
    supplies none, the worker generates it
    (``tunstrap/session.py::SessionDir``), so the
    parent could only recover the path by parsing the success envelope — which
    makes cleanup depend on the very object whose validation can fail. Minting
    it here makes the path a precondition of spawning.

    ``SessionDir.create`` accepts a supplied absolute path, creates it 0700
    when absent, and when present requires it owned by the current user and
    clears its group/other write bits -- so an empty pre-created directory
    under any umask is valid worker input. A supplied path sets
    ``generated=False``; ``run`` removes only the root it minted itself.
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

    Mirrors ``start``'s top-level guard (``tunstrap/cli.py::start_command``) except for the
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
    Materialization runs before the child starts, so ``TUNSTRAP_OUTPUT_FILE``
    always names a file that already exists.
    """
    out = OutputSchema.model_validate(payload)
    write_materialized_output(out)
    child_env = _build_child_env(
        out,
        output_var=output_var,
        input_env=input_env,
        suppress_kubeconfig=suppress_kubeconfig,
    )
    try:
        # Popen + .wait() (not subprocess.run) so SIGINT/SIGTERM can be
        # forwarded to the child. Caught narrowly here, not around this whole
        # function: an OSError from write_materialized_output above must not
        # be misreported as "failed to launch command".
        # pylint: disable-next=consider-using-with
        proc = subprocess.Popen(cmd, env=child_env)
    except OSError as exc:
        sys.stderr.write(f"run: failed to launch command: {exc}\n")
        return 127

    def _forward(signum: int, _frame: object) -> None:
        """Forward termination to the child so its process semantics remain visible."""
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

    No ``except OSError`` here: ``_run_child`` handles the one OSError this
    window reports as "failed to launch command" (``Popen``) internally, so
    any other OSError reaches ``run_command``'s generic handler as ``DaemonError``
    instead, not misattributed to the child command.
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
) -> None:
    """Open a tunnel, run CMD with TUNSTRAP_*/KUBECONFIG injected, then tear down.

    The Click command owns only CLI-shaped arguments. Keeping the operational
    implementation plain lets programmatic callers share its checks and
    cleanup without treating Click's callback attribute as an internal API.
    """
    context = click.get_current_context(silent=True)
    grace_seconds_set = (
        context is not None
        and context.get_parameter_source("grace_seconds") != click.core.ParameterSource.DEFAULT
    )
    _run_command(
        ssh_key=ssh_key,
        ssh_key_passphrase=ssh_key_passphrase,
        ssh_password_stdin=ssh_password_stdin,
        targets=targets,
        kube=kube,
        fetch=fetch,
        auto_stop_idle_seconds=auto_stop_idle_seconds,
        materialize=materialize,
        log_file=log_file,
        input_env=input_env,
        output_var=output_var,
        session_dir=session_dir,
        grace_seconds=grace_seconds,
        grace_seconds_set=grace_seconds_set,
        args=args,
        suppress_kubeconfig=False,
    )


def _run_command(  # pylint: disable=too-many-locals,too-many-statements
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
    grace_seconds_set: bool,
    args: tuple[str, ...],
    *,
    suppress_kubeconfig: bool,
) -> None:
    """Open a tunnel, run CMD with TUNSTRAP_*/KUBECONFIG injected, then tear down.

    Input is either a USER@HOST[:PORT] positional plus flags, or the complete
    InputSchema JSON in the environment variable named by --input-env. `--` is
    mandatory whenever the child command or any of its arguments begins with
    `-`.
    """
    connection, cmd = _split_run_args(args, input_env=input_env)
    try:
        if input_env is not None:
            _reject_flags_under_input_env(
                conn_flags=connection_flags_present(
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
            _validate_output_var(output_var, input_env=input_env)
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
        # An expected outcome keeps its own exit code via exit_code_for, not
        # the generic exit 4 below. Nothing in the post-spawn window raises a
        # TunstrapError today; this is future-proofing for one that does.
        sys.stderr.write(json.dumps(exc.to_error_output()) + "\n")
        returncode = exit_code_for(exc)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Teardown has already run in _supervise_child's finally.
        _report_unexpected(exc)
        returncode = 4
    sys.exit(returncode)


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


def _warn_preserved(
    session_dir: str, cause: str, minted_root: str | None, *, verb: str = "run"
) -> None:
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
    caller-minted temp root from one somebody cares about.  The caller that
    minted a root supplies it here, so the disposal note is emitted only then.
    """
    recovery = f"{verb}: {cause}; preserving session data. Recover with: "
    recovery += f"tunstrap stop --session-dir {session_dir}\n"
    if minted_root is not None:
        recovery += (
            f"{verb}: {minted_root} was created by {verb} and is not removed by that "
            f"command; delete it once the daemon is dealt with\n"
        )
    _warn(recovery)


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


# ``stop``/``status`` live in ``cli_stop`` (issue #32) and register here, which
# is the registration ``@main.command`` performed for them before the split.
# Import direction follows ``cli_input``/``envrender``: this module imports the
# split module, never the reverse. Registered at the foot of the file to keep
# the historical order (start, run, stop, status).
main.add_command(stop_command)
main.add_command(status_command)


if __name__ == "__main__":  # pragma: no cover
    main()
