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
from pydantic import ValidationError

from tunstrap import __version__
from tunstrap.cli_input import build_schema_from_env, build_single_node_schema
from tunstrap.daemon import spawn_daemon
from tunstrap.envrender import (
    format_exports,
    predicted_env_keys,
    render_env,
    render_output_var,
)
from tunstrap.exceptions import (
    DaemonError,
    DaemonHandshakeError,
    MultiNodeEnvUnsupported,
    SchemaValidationError,
    TunstrapError,
    exit_code_for,
)
from tunstrap.identity import IdentityCheckResult, verify_session
from tunstrap.schemas import DaemonOptions, InputSchema, OutputSchema
from tunstrap.session import SessionDir, SessionError, StopOutcome, stop_session

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
    if log_file is not None:
        raise click.UsageError(
            "--log-file conflicts with --input-env; set daemon.log_file in the payload"
        )
    if materialize:
        raise click.UsageError("--materialize conflicts with --input-env; run always materializes")


def _build_child_env(
    output: OutputSchema, *, output_var: str | None, inject_scalars: bool
) -> dict[str, str]:
    """Inherited env, plus scalars and the projected output JSON.

    ``inject_scalars`` is decided pre-spawn from the input node count.

    ``output_var`` carries ``render_output_var``'s projection, not the whole
    envelope: its consumer persists the value into an OpenTofu plan file.
    """
    child_env = dict(os.environ)
    if inject_scalars:
        child_env.update(render_env(output))
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
    payload: Any, cmd: list[str], *, output_var: str | None, inject_scalars: bool
) -> int:
    """Validate the success payload, build the child env, run the child.

    Every statement here runs inside ``_supervise_child``'s teardown ``try``,
    including ``OutputSchema.model_validate`` — which is exactly the case an
    earlier design left unguarded: a malformed success payload orphaned the
    daemon, because the session path was recovered from that same payload.
    """
    out = OutputSchema.model_validate(payload)
    child_env = _build_child_env(out, output_var=output_var, inject_scalars=inject_scalars)
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
    inject_scalars: bool,
    session_dir: str,
    grace_seconds: int,
    minted_root: str | None,
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
        return _run_child(payload, cmd, output_var=output_var, inject_scalars=inject_scalars)
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
def run_command(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
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

    Input is either a USER@HOST[:PORT] positional plus flags, or the complete
    InputSchema JSON in the environment variable named by --input-env. `--` is
    mandatory whenever the child command or any of its arguments begins with
    `-`.
    """
    connection, cmd = _split_run_args(args, input_env=input_env)
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
                materialize=materialize,
                log_file=log_file,
            )
            schema = build_schema_from_env(input_env)
            # The one place `run` mutates the supplied schema. It is an
            # invariant of the verb, not a flag precedence rule: render_env
            # needs a materialized kubeconfig path (envrender.py:42-43), and
            # an unmaterialized target would hand --output-var consumers
            # `path: null` and the kubernetes/helm providers an empty
            # config_path.
            schema.daemon.materialize = True
        elif connection is not None:
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
        else:  # pragma: no cover - _split_run_args already rejected this arity
            raise click.UsageError("run requires USER@HOST[:PORT] or --input-env VAR")
        if output_var is not None:
            _validate_output_var(output_var, schema)
        if output_var is None and len(schema.nodes) != 1:
            # Decided from the *input* node count so it can never orphan a
            # daemon: TUNSTRAP_<TARGET>_* has no node dimension, and
            # --output-var is the node-keyed channel that does.
            raise MultiNodeEnvUnsupported(
                "multi-node input requires --output-var; TUNSTRAP_* scalars are single-node only",
                {"nodes": sorted(schema.nodes)},
            )
        inject_scalars = len(schema.nodes) == 1
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
        message = spawn_daemon(schema, session_dir=session_path)
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
            inject_scalars=inject_scalars,
            session_dir=session_path,
            grace_seconds=grace_seconds,
            minted_root=minted_root,
        )
    except TunstrapError as exc:
        # An expected outcome keeps its own exit code. A lone required:false
        # node that failed yields a success envelope with no connections
        # (manager.py:99-107), and render_env raises MultiNodeEnvUnsupported
        # here — exit 1, not "unexpected failure during run". Teardown has
        # already run in _supervise_child's finally, as it has below.
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
    """
    try:
        _teardown_run_inner(session_dir, grace_seconds, minted_root=minted_root)
    except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _warn(f"run: teardown failed: {type(exc).__name__}: {exc}\n")
        # A raising stop primitive short-circuits _teardown_run_inner before it
        # reaches the root removal, so retry it here: a temp directory run
        # created must not outlive run even on the exceptional path.
        _discard_minted_root(minted_root)


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
    except SessionError:
        # No identity file: nothing to stop. Not an error — with the session
        # path minted before the spawn, a missing identity no longer means the
        # path is unknown, it means the daemon never recorded one.
        pass
    else:
        outcome = stop_session(session_dir, pid, grace_seconds, force=True)
        if not outcome.stopped and outcome.reason != "not found":
            _warn(f"run: daemon not stopped cleanly: {outcome.reason}\n")
    survivors = SessionDir.cleanup_path(session_dir)
    if survivors:
        _warn("run: could not remove: " + ", ".join(survivors) + "\n")
    if minted_root is not None:
        remaining = SessionDir.remove_root(minted_root)
        if remaining:
            _warn("run: could not remove session root: " + ", ".join(remaining) + "\n")


def _stop_outcome_json(outcome: StopOutcome) -> str:
    """Render a StopOutcome as ``stop``'s documented stdout JSON, key for key.

    Key order and omission rules are a public contract, pinned byte for byte
    across all seven outcomes by ``tests/unit/test_cli_stop_output.py``:
    ``stopped`` first, then ``reason`` when there is one, then ``forced`` only
    when True.
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
