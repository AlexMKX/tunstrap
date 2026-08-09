"""`stop`'s stdout contract, pinned byte for byte.

Validates: after the stop mechanism moved into the silent session.stop_session
primitive, `tunstrap stop` still writes exactly the JSON it wrote before —
same keys, same order, same spacing, same trailing newline.
Code: tunstrap/cli.py (stop_command, _stop_outcome_json)
Assertion: result.stdout equals a literal recorded from the pre-refactor
implementation; result.stderr is empty.
Method: monkeypatch cli.stop_session to return each StopOutcome and
SessionDir.read_identity/cleanup_path so no real daemon is involved. Uses
result.stdout (not result.output), because click 8.4's CliRunner interleaves
stderr into .output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tunstrap import cli as cli_mod
from tunstrap import session as session_mod
from tunstrap.cli import main
from tunstrap.session import SessionError, SessionIdentityUnreadable, StopOutcome

pytestmark = pytest.mark.unit


@pytest.fixture(name="stubbed_session")
def _stubbed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(lambda _sd: 4242))
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (StopOutcome(False, "not found"), '{"stopped": false, "reason": "not found"}\n'),
        (
            StopOutcome(False, "identity mismatch"),
            '{"stopped": false, "reason": "identity mismatch", "preserved": true}\n',
        ),
        (
            StopOutcome(False, "identity check unavailable"),
            '{"stopped": false, "reason": "identity check unavailable", "preserved": true}\n',
        ),
        (StopOutcome(True), '{"stopped": true}\n'),
        (
            StopOutcome(False, "still alive"),
            '{"stopped": false, "reason": "still alive", "preserved": true}\n',
        ),
        (
            StopOutcome(False, "identity changed during grace"),
            '{"stopped": false, "reason": "identity changed during grace", "preserved": true}\n',
        ),
        (StopOutcome(True, forced=True), '{"stopped": true, "forced": true}\n'),
    ],
)
def test_stop_stdout_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_session: None,
    outcome: StopOutcome,
    expected: str,
) -> None:
    """Each StopOutcome renders as the exact bytes stop wrote before the refactor.

    ``preserved`` is the one addition, and it is additive: it appears only on
    the four outcomes where stop now keeps the session data, so the three
    resolved shapes — including the most-parsed ``{"stopped": true}`` — are
    unchanged to the byte. It is on stdout because it is machine-readable
    state; the *human* notice that goes with it is asserted on stderr below.
    """
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _grace, *, force: outcome)
    result = CliRunner().invoke(main, ["stop", "--session-dir", "/s"])
    assert result.exit_code == 0
    assert result.stdout == expected


@pytest.mark.parametrize(
    "outcome, warns",
    [
        (StopOutcome(True), False),
        (StopOutcome(True, forced=True), False),
        (StopOutcome(False, "not found"), False),
        (StopOutcome(False, "identity mismatch"), True),
        (StopOutcome(False, "identity check unavailable"), True),
        (StopOutcome(False, "still alive"), True),
        (StopOutcome(False, "identity changed during grace"), True),
    ],
)
def test_stop_warns_on_stderr_exactly_when_it_preserves(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_session: None,
    outcome: StopOutcome,
    warns: bool,
) -> None:
    """The human notice goes to stderr, and only when data was actually kept.

    stdout is a machine-readable envelope that callers parse, so a sentence for
    a person cannot go there — that is the repo's stdout-purity invariant. But
    an operator who runs the recovery command and gets ``stopped: false`` needs
    to be told the data is still on disk, or the silence reads as "nothing
    left to do".

    Both directions are parametrized: a notice on a *resolved* outcome would be
    noise on the normal path, and is caught here too.
    """
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _grace, *, force: outcome)
    result = CliRunner().invoke(main, ["stop", "--session-dir", "/s"])
    assert result.exit_code == 0
    if warns:
        assert "session data preserved under /s" in result.stderr
        assert str(outcome.reason) in result.stderr
    else:
        assert result.stderr == "", f"a resolved stop must stay silent: {result.stderr!r}"


@pytest.mark.parametrize(
    "raised",
    [
        SessionError("cannot read identity from /s/tunnel-data: nope"),
        SessionIdentityUnreadable("cannot read identity from /s/tunnel-data: nope"),
    ],
    ids=["missing", "unreadable-or-malformed"],
)
def test_stop_identity_failure_envelope_is_byte_exact(
    monkeypatch: pytest.MonkeyPatch, raised: SessionError
) -> None:
    """Every identity-read failure reports ``preserved``, byte for byte.

    These paths return before cleanup, so they *do* keep ``tunnel-data`` — and
    they used to render their own JSON literal inline, omitting ``preserved``
    entirely. A caller parsing the envelope therefore read "no preserved key"
    as "the directory was cleaned", which was false on exactly these outcomes.

    Byte-exact rather than ``json.loads``: the point of the field is a stable
    machine-readable envelope, so key order, spacing and the trailing newline
    are the contract, not just the decoded mapping. The reason text is pinned
    to a controlled message so the whole line can be asserted.

    Both the base ``SessionError`` and the ``SessionIdentityUnreadable``
    subclass are covered, and both render identically: in ``stop`` nothing is
    deleted either way, so signalling a difference would describe one that does
    not exist. ``run`` splits them because there the distinction decides
    whether to delete.
    """

    def _boom(_session_dir: str) -> int:
        raise raised

    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(_boom))
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, ["stop", "--session-dir", "/s"])
    assert result.exit_code == 0
    assert result.stdout == (
        '{"stopped": false, "reason": "cannot read identity from /s/tunnel-data: nope",'
        ' "preserved": true}\n'
    )
    assert "session data preserved under /s" in result.stderr


def _session_with_identity(root: Path, pid: str = "4242\n") -> tuple[Path, Path]:
    """Build a real session dir holding a real identity file and a real secret."""
    data = root / "tunnel-data"
    data.mkdir(parents=True)
    (data / "daemon.pid").write_text(pid)
    (data / "materialized.kubeconfig").write_text("credential-bearing")
    return data, data / "daemon.pid"


@pytest.mark.parametrize(
    "outcome",
    [StopOutcome(True), StopOutcome(True, forced=True), StopOutcome(False, "not found")],
    ids=["stopped", "forced", "not-found"],
)
def test_stop_still_deletes_on_a_resolved_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: StopOutcome
) -> None:
    """The negative control, and the load-bearing half of the whole change.

    Without it, "preserve on everything" would satisfy the preservation tests
    while leaking every session dir tunstrap ever created. ``not found`` is
    included deliberately: it is a *resolved* outcome — the normal shape once
    auto-stop-idle has fired — and reading it as a failure would mean the
    common path stopped cleaning up.
    """
    data, _ = _session_with_identity(tmp_path)
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _grace, *, force: outcome)

    result = CliRunner().invoke(main, ["stop", "--session-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert not data.exists(), "a resolved stop must still remove tunnel-data"


@pytest.mark.parametrize(
    "reason",
    ["identity mismatch", "identity check unavailable", "still alive", "identity changed"],
)
def test_stop_keeps_the_identity_on_an_unresolved_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str
) -> None:
    """An unresolved stop leaves the operator the handle it could not use itself."""
    data, identity = _session_with_identity(tmp_path)
    monkeypatch.setattr(
        cli_mod, "stop_session", lambda _sd, _pid, _grace, *, force: StopOutcome(False, reason)
    )

    result = CliRunner().invoke(main, ["stop", "--session-dir", str(tmp_path)])

    assert result.exit_code == 0
    ate_the_handle = "stop deleted the identity of a daemon it could not stop"
    assert data.exists() and identity.read_text() == "4242\n", ate_the_handle


@pytest.mark.parametrize(
    "shape",
    ["missing", "unreadable", "malformed"],
    ids=["missing", "unreadable", "malformed"],
)
def test_stop_deletes_nothing_when_it_cannot_read_the_identity(tmp_path: Path, shape: str) -> None:
    """All three identity-read failures leave the session dir alone.

    ``SessionIdentityUnreadable`` exists now, but it is not applied here, and
    this pins why: the split's purpose is to decide *whether to delete*, and
    ``stop`` already deletes nothing on any of the three — it returns before
    reaching cleanup. Distinguishing them would add surface with no behavioural
    consequence. The operator still sees which one it was, because the reason
    string carries the underlying OSError or ValueError text.

    A missing identity is deliberately not treated as "safe to clean" here, as
    it is in ``run``: ``stop`` has no way to know the directory is not a daemon
    that is starting up right now and has yet to write its pid.
    """
    data = tmp_path / "tunnel-data"
    data.mkdir(parents=True)
    (data / "materialized.kubeconfig").write_text("credential-bearing")
    if shape == "unreadable":
        (data / "daemon.pid").mkdir()
    elif shape == "malformed":
        (data / "daemon.pid").write_text("not-a-pid\n")

    result = CliRunner().invoke(main, ["stop", "--session-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["stopped"] is False
    assert (data / "materialized.kubeconfig").exists(), "stop deleted state it could not assess"
    # The envelope must carry the signal that matches what just happened on
    # disk. Asserted against a real OSError/ValueError reason -- which no test
    # can spell in advance -- so the ends of the line are pinned instead: key
    # order, spacing, `preserved` last, trailing newline.
    assert result.stdout.startswith('{"stopped": false, "reason": "')
    unsignalled = f"a preserving outcome reported no preserved key: {result.stdout!r}"
    assert result.stdout.endswith('", "preserved": true}\n'), unsignalled
    assert f"session data preserved under {tmp_path}" in result.stderr


def test_stop_with_a_non_positive_pid_through_the_cli_never_signals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The composed path: a corrupt ``-1`` pid through the real CLI never signals.

    Every other layer is tested with the layer below it stubbed, so no test
    drives a non-positive pid through the real CLI. A regression that reopens
    the hole by a different route — a new ``stop_session`` caller that skips
    ``read_identity``, or a change to which exception ``cli.stop_command``
    catches — would pass the whole suite today. This writes ``-1`` into a real
    ``tunnel-data/daemon.pid`` and invokes ``stop`` through Click's runner with
    ``os.kill`` replaced by a recorder, so the only way it stays green is for
    the chain to hold end to end: ``read_identity`` rejects ``-1``, and even if
    it did not, ``stop_session``'s entry guard does.

    The recorder staying empty is the blast-radius contract; ``preserved: true``
    with ``tunnel-data`` still on disk is the disposal contract for a daemon
    that cannot be addressed (not ``not found``, which would delete). Asserted
    on ``result.stdout`` because Click's ``CliRunner`` interleaves stderr into
    ``result.output``.
    """
    data, _ = _session_with_identity(tmp_path, pid="-1\n")
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda p, s: sent.append((p, s)))

    result = CliRunner().invoke(main, ["stop", "--session-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert sent == [], f"os.kill was called with a non-positive pid: {sent}"
    body = json.loads(result.stdout)
    assert body["stopped"] is False
    assert body["preserved"] is True
    assert data.exists(), "tunnel-data must survive a pid it could not address"
