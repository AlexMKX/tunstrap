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

import pytest
from click.testing import CliRunner

from tunstrap import cli as cli_mod
from tunstrap.cli import main
from tunstrap.session import StopOutcome

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
            '{"stopped": false, "reason": "identity mismatch"}\n',
        ),
        (
            StopOutcome(False, "identity check unavailable"),
            '{"stopped": false, "reason": "identity check unavailable"}\n',
        ),
        (StopOutcome(True), '{"stopped": true}\n'),
        (
            StopOutcome(False, "still alive"),
            '{"stopped": false, "reason": "still alive"}\n',
        ),
        (
            StopOutcome(False, "identity changed during grace"),
            '{"stopped": false, "reason": "identity changed during grace"}\n',
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
    """Each StopOutcome renders as the exact bytes stop wrote before the refactor."""
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _grace, force: outcome)
    result = CliRunner().invoke(main, ["stop", "--session-dir", "/s"])
    assert result.exit_code == 0
    assert result.stdout == expected
    assert result.stderr == "", f"stop must not write to stderr: {result.stderr!r}"


def test_stop_missing_identity_branch_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unreadable-identity branch still prints its SessionError text and exits 0."""

    def _boom(_session_dir: str) -> int:
        raise cli_mod.SessionError("cannot read identity from /s/tunnel-data: nope")

    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(_boom))
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, ["stop", "--session-dir", "/s"])
    assert result.exit_code == 0
    assert result.stdout == (
        '{"stopped": false, "reason": "cannot read identity from /s/tunnel-data: nope"}\n'
    )
