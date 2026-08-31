"""``status`` surfaces the daemon's exit reason without changing its shape.

Validates: the ``tunnel_loss`` key appears only when the daemon left a record,
so every session that never lost a tunnel still renders exactly ``{"alive": …}``
-- the shape ``tests/unit/test_cli_runner.py`` pins by whole-dict equality --
and ``stop``'s byte-pinned envelope is not involved at all.
Code: tunstrap/cli_stop.py::status_command
Assertion: ``result.stdout`` compared byte for byte, because a machine-readable
envelope's key order and spacing are the contract, not just the decoded mapping.
Method: a real session dir with a real record file; no daemon is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tunstrap.cli import main

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, *, record: str | None) -> Path:
    """Build a session dir with no live daemon and an optional loss record."""
    data = tmp_path / "tunnel-data"
    data.mkdir(parents=True)
    (data / "daemon.pid").write_text("4242\n")
    if record is not None:
        (data / "tunnel-loss.json").write_text(record)
    return tmp_path


def test_status_without_a_record_is_byte_identical_to_before(tmp_path: Path) -> None:
    """The additive guarantee: no record, no new key, not even a reordering.

    This is the load-bearing half. Every existing caller parses ``{"alive": …}``
    and an unconditional second key -- or the same key emitted as ``null`` --
    would break the common path to explain an event that did not happen.
    """
    session = _session(tmp_path, record=None)

    result = CliRunner().invoke(main, ["status", "--session-dir", str(session)])

    assert result.exit_code == 0
    assert result.stdout == '{"alive": false}\n'


def test_status_reports_why_the_daemon_terminated_itself(tmp_path: Path) -> None:
    """The fix's user-visible payoff: ``alive: false`` *and* the reason for it.

    Before issue #33 a worker whose tunnel died stayed alive, so ``status`` said
    ``alive: true`` for a session serving nothing. Now the daemon exits, which
    makes ``alive`` truthful on its own, and the preserved record supplies the
    part PID-liveness can never carry: *why*.
    """
    body = {
        "self_terminated": True,
        "reason": "required tunnel lost: the daemon terminated itself",
        "losses": [{"node": "edge", "required": True}],
    }
    session = _session(tmp_path, record=json.dumps(body) + "\n")

    result = CliRunner().invoke(main, ["status", "--session-dir", str(session)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"alive": False, "tunnel_loss": body}
    assert result.stdout.startswith('{"alive": false, "tunnel_loss": '), result.stdout


def test_status_ignores_an_unusable_record(tmp_path: Path) -> None:
    """A truncated record must not turn ``status`` into a failure.

    The writer is a daemon that can be SIGKILLed mid-write, and ``status`` is
    the command an operator reaches for precisely when things are broken; it
    answering nothing at all would be the worst possible response.
    """
    session = _session(tmp_path, record='{"self_terminated": tr')

    result = CliRunner().invoke(main, ["status", "--session-dir", str(session)])

    assert result.exit_code == 0
    assert result.stdout == '{"alive": false}\n'
