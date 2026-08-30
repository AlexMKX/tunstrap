"""The `--input-env` variable must not be inherited by the child process.

Under the documented recipe that variable holds the InputSchema, whose
``ssh_pkey`` is an SSH private key in PEM form. The child is ``tofu``, which
hands its environment to every provider plugin, ``external`` data source and
``local-exec`` provisioner. ``run`` is the one component that knows this
variable is secret-bearing, so it is the one component that can remove it.

Code: tunstrap/cli.py (_build_child_env, _run_child, _supervise_child)
Assertion: the variable is absent from the child environment, and the *literal
PEM bytes* appear under no key at all — paired with a positive check that the
environment is still a real inherited one, so a fix that handed the child an
empty environment could not pass.
Method: CliRunner with spawn_daemon, subprocess.Popen and _teardown_run
monkeypatched; the child env is captured off the fake Popen. The one
exception is the reused-NAME case, which ``run`` now rejects outright, so it
calls ``_build_child_env`` directly — see that test's own docstring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT"

# Distinctive on purpose: every absence assertion is made against these exact
# bytes, so it cannot be satisfied by a payload that was never generated.
SSH_PKEY_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "TUNSTRAP-UNIT-SSH-PRIVATE-KEY-MUST-NEVER-REACH-THE-CHILD\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)

INPUT_PAYLOAD = json.dumps(
    {
        "nodes": {
            "node": {
                "host": "h.example.net",
                "user": "u",
                "ssh_pkey": SSH_PKEY_PEM,
                "remote_targets": {"db": "127.0.0.1:5432"},
            }
        }
    }
)


def _success_payload(session_dir: str) -> dict[str, Any]:
    return {
        "connections": {
            "node": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}},
        },
        "pid": 99,
        "session_dir": session_dir,
        "started_at": "2026-07-31T00:00:00Z",
    }


class FakePopen:
    last_env: dict[str, str] | None = None

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        FakePopen.last_env = env
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        """Accept forwarded signals; the fake child ignores them."""


@pytest.fixture(name="spawn")
def _spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    def _install(message: dict[str, Any]) -> None:
        def _spawn_daemon(
            schema: Any, session_dir: str | None = None, *, input_env: str | None = None
        ) -> dict[str, Any]:
            seen.append({"schema": schema, "session_dir": session_dir, "input_env": input_env})
            return message

        monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_env = None
    seen.append(_install)
    return seen


def _run(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path, *args: str
) -> dict[str, str]:
    """Drive one full `run --input-env VAR [args] -- true`."""
    spawn[0]({"kind": "success", "payload": _success_payload(str(tmp_path))})
    monkeypatch.setenv(VAR, INPUT_PAYLOAD)
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, *args, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    return FakePopen.last_env


def test_input_env_variable_is_scrubbed_from_the_child_environment(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """The variable holding the InputSchema is not inherited by the child."""
    env = _run(monkeypatch, spawn, tmp_path)

    assert VAR not in env, f"{VAR} carries the SSH private key and was inherited by the child"


def test_run_forwards_the_input_variable_name_to_the_spawn(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """``spawn_daemon`` is told, by name, which variable is secret-bearing.

    The worker's scrub cannot be keyed on a literal — ``--input-env`` takes an
    arbitrary name — so the name has to be forwarded. This pins the forwarding
    at unit level; that the detached worker's environment really loses it is
    proven against a real process in
    ``tests/integration/test_daemon_input_env.py``. Red if ``run`` stops
    passing the argument: the parameter then keeps its ``None`` default.
    """
    _run(monkeypatch, spawn, tmp_path)

    assert spawn[-1]["input_env"] == VAR, "run must tell spawn_daemon which variable to scrub"


def test_ssh_private_key_reaches_no_child_variable(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """Asserted on the key bytes, so a copy under any other name is caught too."""
    env = _run(monkeypatch, spawn, tmp_path)
    blob = "\n".join(f"{k}={v}" for k, v in env.items())

    assert SSH_PKEY_PEM not in blob, "the SSH private key reached the child environment"
    assert "TUNSTRAP-UNIT-SSH-PRIVATE-KEY" not in blob, "SSH key material reached the child"
    # Anti-vacuity: the child env must still be a real inherited environment.
    assert "PATH" in env, "child env must still inherit os.environ"


def test_scrub_is_narrow_and_leaves_the_rest_of_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """Only the named variable is removed, not the environment at large."""
    monkeypatch.setenv("TUNSTRAP_UNRELATED_KEEP_ME", "keep")
    env = _run(monkeypatch, spawn, tmp_path)

    assert env["TUNSTRAP_UNRELATED_KEEP_ME"] == "keep"
    session_dir_survived = env["TUNSTRAP_SESSION_DIR"] == str(tmp_path)
    assert session_dir_survived, "the session scalars must survive the scrub"


def test_scrub_runs_before_injection_so_a_reused_name_is_not_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_child_env` pops ``input_env`` before assigning ``output_var``,
    so reusing one NAME for both never restores the input secret under it.

    ``run`` itself can no longer produce ``--input-env X --output-var X`` --
    ``_validate_output_var`` rejects the combination as a usage error
    (issue #31) -- so this exercises ``_build_child_env`` directly at unit
    level, which is the only place left that combination can still be
    constructed. The scrub happens first, then the projected output is
    written; the reverse order would delete the output and leave the child
    with neither -- or, worse, leave the secret in place.
    """
    from tunstrap.cli import _build_child_env
    from tunstrap.schemas import OutputSchema

    monkeypatch.setenv(VAR, INPUT_PAYLOAD)
    out = OutputSchema.model_validate(_success_payload("/s"))
    env = _build_child_env(out, output_var=VAR, input_env=VAR)

    assert VAR in env, "the output variable should have been written under the reused name"
    child_values = "\n".join(env.values())
    assert SSH_PKEY_PEM not in child_values, "the input secret survived in the child environment"
    assert json.loads(env[VAR])["session"]["pid"] == 99, "the value must be the output envelope"


def test_input_payload_shutdown_grace_controls_teardown(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """The payload daemon block, not a hidden CLI default, sets run's grace.

    The recorder delegates to ``cleaning_teardown`` rather than swallowing the
    call: ``run`` mints a real temp directory before spawning, so a stub that
    only recorded would leave one ``/tmp/tunstrap-run-*`` root behind on every
    run of this test.
    """
    observed: list[int] = []

    def _teardown(path: str, grace: int, *, minted_root: str | None = None) -> None:
        observed.append(grace)
        cleaning_teardown(path, grace, minted_root=minted_root)

    monkeypatch.setattr(cli_mod, "_teardown_run", _teardown)
    payload = json.loads(INPUT_PAYLOAD)
    payload["daemon"] = {"shutdown_grace_seconds": 23}
    spawn[0]({"kind": "success", "payload": _success_payload(str(tmp_path))})
    monkeypatch.setenv(VAR, json.dumps(payload))
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert observed == [23]
