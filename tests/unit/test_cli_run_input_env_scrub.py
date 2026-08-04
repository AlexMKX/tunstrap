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
monkeypatched; the child env is captured off the fake Popen.
"""

from __future__ import annotations

import json
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

SUCCESS_PAYLOAD: dict[str, Any] = {
    "connections": {
        "node": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}},
    },
    "pid": 99,
    "session_dir": "/s",
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
        def _spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
            seen.append(schema)
            return message

        monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_env = None
    seen.append(_install)
    return seen


def _run(monkeypatch: pytest.MonkeyPatch, spawn: list[Any], *args: str) -> dict[str, str]:
    """Drive one full `run --input-env VAR [args] -- true`."""
    spawn[0]({"kind": "success", "payload": SUCCESS_PAYLOAD})
    monkeypatch.setenv(VAR, INPUT_PAYLOAD)
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, *args, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    return FakePopen.last_env


def test_input_env_variable_is_scrubbed_from_the_child_environment(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """The variable holding the InputSchema is not inherited by the child."""
    env = _run(monkeypatch, spawn)

    assert VAR not in env, f"{VAR} carries the SSH private key and was inherited by the child"


def test_ssh_private_key_reaches_no_child_variable(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Asserted on the key bytes, so a copy under any other name is caught too."""
    env = _run(monkeypatch, spawn)
    blob = "\n".join(f"{k}={v}" for k, v in env.items())

    assert SSH_PKEY_PEM not in blob, "the SSH private key reached the child environment"
    assert "TUNSTRAP-UNIT-SSH-PRIVATE-KEY" not in blob, "SSH key material reached the child"
    # Anti-vacuity: the child env must still be a real inherited environment.
    assert "PATH" in env, "child env must still inherit os.environ"


def test_scrub_is_narrow_and_leaves_the_rest_of_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Only the named variable is removed, not the environment at large."""
    monkeypatch.setenv("TUNSTRAP_UNRELATED_KEEP_ME", "keep")
    env = _run(monkeypatch, spawn)

    assert env["TUNSTRAP_UNRELATED_KEEP_ME"] == "keep"
    assert env["TUNSTRAP_DB_PORT"] == "5432", "the injected scalars must survive the scrub"


def test_scrub_runs_before_injection_so_a_reused_name_is_not_restored(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """`--input-env X --output-var X` yields the output, never the input secret.

    Nothing rejects reusing one name for both flags (``_validate_output_var``
    only guards the keys ``run`` injects), so the ordering inside
    ``_build_child_env`` is what decides this: the scrub happens first, then
    the projected output is written. The reverse order would delete the output
    and leave the child with neither — or, worse, leave the secret in place.
    """
    env = _run(monkeypatch, spawn, "--output-var", VAR)

    assert VAR in env, "the output variable should have been written under the reused name"
    assert SSH_PKEY_PEM not in env[VAR], "the input secret survived under the reused name"
    assert json.loads(env[VAR])["pid"] == 99, "the value must be the output envelope"


def test_input_payload_shutdown_grace_controls_teardown(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """The payload daemon block, not a hidden CLI default, sets run's grace."""
    observed: list[int] = []

    def _teardown(_path: str, grace: int, *, minted_root: str | None = None) -> None:
        del minted_root
        observed.append(grace)

    monkeypatch.setattr(cli_mod, "_teardown_run", _teardown)
    payload = json.loads(INPUT_PAYLOAD)
    payload["daemon"] = {"shutdown_grace_seconds": 23}
    spawn[0]({"kind": "success", "payload": SUCCESS_PAYLOAD})
    monkeypatch.setenv(VAR, json.dumps(payload))
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert observed == [23]
