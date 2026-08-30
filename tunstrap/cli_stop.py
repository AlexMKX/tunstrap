"""``stop`` and ``status``: the post-spawn lifecycle verbs (issue #32).

Split out of ``cli.py`` so that module stays under pylint's 1000-line
``max-module-lines`` cap with real headroom instead of a suppression.
``cli.py`` imports this module and registers both commands with
``main.add_command`` — the registration ``@main.command`` performs — with
the same import direction as ``cli_input``/``envrender``: ``cli`` imports
the split module, never the reverse. ``_warn`` lives here under the same
one-way rule; ``cli.py`` re-imports it for ``run``'s teardown reporters.

``stop_session`` and ``verify_session`` are imported from the modules that
define them (``session`` and ``identity``); the unit suite patches this
module's bindings — ``cli_stop.stop_session`` and
``cli_stop.verify_session`` — to drive the commands without a real daemon.

``stop``'s stdout envelope is byte-pinned by
``tests/unit/test_cli_stop_output.py``; key order, spacing, omission rules
and the stderr preservation notice are a public contract.
"""

from __future__ import annotations

import json
import sys

import click

from tunstrap.identity import IdentityCheckResult, verify_session
from tunstrap.session import SessionDir, SessionError, StopOutcome, stop_session


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


def _warn(message: str) -> None:
    """Attempt a teardown diagnostic without allowing a closed stderr to escape.

    Lives here — rather than in ``cli.py``, whose teardown reporters call it
    three times — so the import direction stays ``cli`` → ``cli_stop`` for
    every shared symbol, matching the registration direction.
    """
    try:
        sys.stderr.write(message)
    except BaseException:  # noqa: BLE001, S110  # pylint: disable=broad-exception-caught
        pass


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


@click.command("stop")
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
        sys.exit(1)
    outcome = stop_session(session_dir, pid, grace_seconds, force=True)
    _emit_stop_outcome(outcome, session_dir)
    if _stop_resolved(outcome):
        # Deleting on an unresolved outcome would make the recovery command
        # ``run`` prints destroy the identity file it was invoked to recover.
        SessionDir.cleanup_path(session_dir)
        return
    sys.exit(1)


@click.command("status")
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
