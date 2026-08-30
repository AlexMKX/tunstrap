"""``tunstrap_tofu`` console-entry unit tests.

Covers the three branches of the proxy (`tunstrap/tofu_proxy.py`):
  1. pass-through when ``TUNSTRAP_INPUT`` is unset/empty,
  2. pass-through for no-cluster subcommands (``init``/``version``/…), with the
     ``-chdir`` gap fixed by parsing argv past global flags,
  3. the tunnelled branch, which reuses ``run``'s hardened path in-process with
     ``KUBECONFIG`` suppressed so a broken ``config_path`` chain cannot fall
     back to an inherited or injected value.

Also guards the cost discipline: importing the proxy module must not pull in
``tunstrap.cli`` or any heavy dependency, so the pass-through branches pay only
interpreter startup plus the package ``__init__``.

No cluster, docker or network: ``spawn_daemon``/``Popen``/``_teardown_run`` are
monkeypatched exactly as in ``test_cli_run_output_var.py``, and ``os.execvp``
is intercepted so the pass-through branches never actually replace the process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import tunstrap.tofu_proxy as proxy
from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.run_invocation import run_via_env_input

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT"


class _ExecvpCalled(Exception):
    """Sentinel raised by the fake execvp so main() does not continue past it."""


def _node(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "host": "h.example.net",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"db": "127.0.0.1:5432"},
    }
    base.update(overrides)
    return base


def _payload(nodes: dict[str, Any] | None = None) -> str:
    return json.dumps({"nodes": nodes if nodes is not None else {"node": _node()}})


def _conn(**ports: int) -> dict[str, Any]:
    return {"ports": dict(ports), "fetch_files": {}, "kube_targets": {}}


def _success(connections: dict[str, Any], *, session_dir: str) -> dict[str, Any]:
    return {
        "kind": "success",
        "payload": {
            "connections": connections,
            "pid": 99,
            "session_dir": session_dir,
            "started_at": "2026-07-31T00:00:00Z",
        },
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


@pytest.fixture(name="capturing_execvp")
def _capturing_execvp(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``os.execvp`` with a recorder that raises instead of replacing us.

    Records the ``argv`` list (whose ``[0]`` is conventionally the program name,
    so it already carries ``tofu``); ``prog`` is constant and asserted separately
    by ``test_exec_tofu_calls_os_execvp_with_tofu_argv``.
    """
    seen: list[list[str]] = []

    def _fake_execvp(prog: str, argv: list[str]) -> None:
        del prog  # asserted in the dedicated _exec_tofu test
        seen.append(list(argv))
        raise _ExecvpCalled("execvp")

    # Patch the ``os`` reference the proxy module looks ``execvp`` up on.
    monkeypatch.setattr(proxy.os, "execvp", _fake_execvp)
    return seen


@pytest.fixture(name="spawn")
def _spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Install the run-path stubs (spawn_daemon/Popen/_teardown_run) shared with
    the output-var suite, so the tunnelled branch can be exercised without a
    real daemon or child."""
    seen: list[Any] = []

    def _install(message: dict[str, Any]) -> None:
        def _spawn_daemon(
            schema: Any, session_dir: str | None = None, *, input_env: str | None = None
        ) -> dict[str, Any]:
            seen.append(schema)
            return message

        monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_env = None
    seen.append(_install)  # seen[0] is the installer; schemas follow
    return seen


def _run_main(argv: list[str]) -> None:
    """Invoke the proxy entry with a controlled argv (prog name is arbitrary)."""
    sys.argv = ["tunstrap_tofu", *argv]
    proxy.main()


# --------------------------------------------------------------------------- #
# Branch 1: pass-through when TUNSTRAP_INPUT is unset / empty / whitespace.
# --------------------------------------------------------------------------- #


def test_passthrough_when_input_env_unset_execs_tofu(
    monkeypatch: pytest.MonkeyPatch, capturing_execvp: list[list[str]]
) -> None:
    """No payload → the proxy must exec tofu untouched and never reach run."""
    monkeypatch.delenv(VAR, raising=False)
    with pytest.raises(_ExecvpCalled):
        _run_main(["plan", "-out=x"])
    assert capturing_execvp == [["tofu", "plan", "-out=x"]]


def test_passthrough_when_input_env_empty_execs_tofu(
    monkeypatch: pytest.MonkeyPatch, capturing_execvp: list[list[str]]
) -> None:
    monkeypatch.setenv(VAR, "")
    with pytest.raises(_ExecvpCalled):
        _run_main(["plan"])
    assert capturing_execvp == [["tofu", "plan"]]


def test_passthrough_when_input_env_whitespace_execs_tofu(
    monkeypatch: pytest.MonkeyPatch, capturing_execvp: list[list[str]]
) -> None:
    monkeypatch.setenv(VAR, "   \n\t ")
    with pytest.raises(_ExecvpCalled):
        _run_main(["plan"])
    assert capturing_execvp == [["tofu", "plan"]]


# --------------------------------------------------------------------------- #
# Branch 2: pass-through for no-cluster subcommands, with the -chdir gap fixed.
# --------------------------------------------------------------------------- #


# The bypass set the proxy ships, pinned exhaustively. Behaviour must match the
# shell shim (``case "$1" in init|-version)``) plus the no-cluster extras
# (``version`` subcommand, ``-help``, no subcommand), so that everything else
# TUNNELS when TUNSTRAP_INPUT is set. Tunelling everything-not-bypassed is the
# load-bearing half: TUNSTRAP_INPUT is set only for commands the consumer
# deliberately listed in Terragrunt's ``commands``, so the proxy must honour a
# deliberate opt-in (e.g. ``output`` — the e2e tier at test_terragrunt_apply.py
# lists it and asserts the tunnelled row) rather than second-guess it with an
# allow-list of its own.
_BYPASS_CASES = [
    (["init"], "auto-init shares the plan command's env_vars (measured fact 4)"),
    (["version"], "version subcommand (modern tofu); no cluster contact"),
    (["-version"], "version flag prints and exits; no cluster contact"),
    (["-help"], "help prints and exits; no cluster contact"),
    ([], "no subcommand prints help; no cluster contact"),
    (["validate"], "validate checks against installed provider schemas only; no cluster"),
    (["fmt"], "fmt touches only local .tf files; no cluster"),
    # The documented gap the shell shim could not close (its ``case "$1"`` saw
    # ``-chdir`` as the first token, so it tunnelled a needless init):
    (["-chdir=somewhere", "init"], "-chdir=DIR init: shell matched $1 only"),
    (["-chdir", "somewhere", "init"], "-chdir DIR init (space form)"),
    (["--chdir=somewhere", "init"], "long --chdir=DIR init"),
    (["-chdir=somewhere", "-version"], "-chdir=DIR -version"),
]
_TUNNEL_CASES = [
    # The provider-API commands the recipe enumerates as needing a tunnel:
    "plan",
    "apply",
    "destroy",
    "refresh",
    "import",
    "console",
    # Commands that read state/files only — these tunnel too, NOT because they
    # need the cluster, but because TUNSTRAP_INPUT being set means the CONSUMER
    # asked for them (Terragrunt's ``commands`` list is the authority). The
    # proxy must not veto that:
    "output",
    "show",
    "state",
    "taint",
    "untaint",
    "providers",
    "test",
    # An unknown subcommand tunnels rather than guesses — failing loudly inside
    # run/tofu beats silently bypassing something the consumer opted into.
    "weird-unknown-cmd",
]


@pytest.mark.parametrize(("argv", "_why"), _BYPASS_CASES)
def test_should_bypass_returns_true_for_the_pinned_bypass_set(argv: list[str], _why: str) -> None:
    """Every bypass row bypasses (decided structurally, past global flags)."""
    del _why
    # pylint: disable=protected-access
    assert proxy._should_bypass(argv) is True


@pytest.mark.parametrize("cmd", _TUNNEL_CASES)
def test_should_bypass_returns_false_for_everything_else(cmd: str) -> None:
    """Everything not in the bypass set tunnels — incl. consumer opt-ins.

    This is the row that caught the v1 allow-list defect: an allow-list of
    ``{plan,apply,…}`` bypassed ``output``/``validate``/``test``, vetoing a
    consumer that had deliberately listed them in Terragrunt's ``commands``.
    """
    # pylint: disable=protected-access
    assert proxy._should_bypass([cmd]) is False


def test_should_bypass_is_not_a_substring_match() -> None:
    """``init`` as a flag value is consumed, not read as the subcommand."""
    # pylint: disable=protected-access
    assert proxy._should_bypass(["-chdir", "init", "plan"]) is False
    assert proxy._should_bypass(["-chdir=init", "plan"]) is False


@pytest.mark.parametrize(("argv", "_why"), _BYPASS_CASES)
def test_bypass_decision_execs_tofu(
    monkeypatch: pytest.MonkeyPatch,
    capturing_execvp: list[list[str]],
    argv: list[str],
    _why: str,
) -> None:
    """A bypass-row argv with TUNSTRAP_INPUT set execs tofu untouched."""
    del _why
    monkeypatch.setenv(VAR, _payload())
    with pytest.raises(_ExecvpCalled):
        _run_main(argv)
    assert capturing_execvp == [["tofu", *argv]]


@pytest.mark.parametrize("cmd", ["plan", "output", "test", "-chdir=x plan"])
def test_tunnel_decision_runs_tofu_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capturing_execvp: list[list[str]],
    spawn: list[Any],
    cmd: str,
    tmp_path: Path,
) -> None:
    """Everything outside the bypass set tunnels — honouring the consumer opt-in.

    ``output`` is the casualty that caught the v1 allow-list: the e2e tier lists
    it in ``commands`` and asserts the tunnelled row, which an allow-list of
    cluster-only commands would have bypassed.
    """
    monkeypatch.setenv(VAR, _payload())
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as excinfo:  # run_command exits with child code
        _run_main(cmd.split())
    assert excinfo.value.code == 0
    assert capturing_execvp == [], "a tunnel-row command must not exec tofu directly"


# --------------------------------------------------------------------------- #
# Branch 3: the tunnelled path — in-process, KUBECONFIG suppressed.
# --------------------------------------------------------------------------- #


def test_tunnelled_invokes_run_with_input_env_and_output_var(
    monkeypatch: pytest.MonkeyPatch,
    capturing_execvp: list[list[str]],
    spawn: list[Any],
    tmp_path: Path,
) -> None:
    """The proxy works even if Click's callback attribute is unavailable.

    The proxy is a programmatic caller, not a Click extension: it must use the
    plain run implementation directly rather than smuggling a private control
    flag through ``run_command.callback``. Replacing the callback with ``None``
    is the mutation this test rejects.
    """
    monkeypatch.setattr(cli_mod.run_command, "callback", None)
    monkeypatch.setenv(VAR, _payload())
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=str(tmp_path)))
    with pytest.raises(SystemExit):
        _run_main(["plan"])
    assert capturing_execvp == []


def test_tunnelled_suppresses_kubeconfig_in_child_env(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """A single-node kube payload must NOT leave KUBECONFIG in tofu's env, but
    MUST still carry KUBE_CONFIG_PATH -- Mode A's provider channel.

    ``suppress_kubeconfig`` drops only the injected KUBECONFIG (issue #14):
    dropping KUBE_CONFIG_PATH/_PATHS too would make Mode A -- the plan-safe,
    env-native kube delivery the recipe tells consumers to use through this
    exact entry point -- unusable through the proxy. ``_build_child_env``
    always drops any inherited KUBECONFIG/KUBE_CONFIG_PATH/_PATHS before
    injection, on both paths, so a stray operator environment can never
    contribute to the child's kube channel either.

    This is the one place the assertion can fire: the input has one node with
    a kube target, so ``render_kube_env`` WOULD inject KUBECONFIG; only
    suppression removes it.
    """
    # An operator-inherited KUBECONFIG would mask a missing suppression only
    # if render_kube_env did not inject; here it does inject (kube target
    # present), so the assertion catches both an inherited and an injected
    # KUBECONFIG. Clear the inherited one to isolate the injected path.
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv(
        VAR,
        _payload({"node": _node(kube_targets={"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}})}),
    )
    # KubeTargetOutput requires the three credential fields; the projection drops
    # them from TF_VAR_tunstrap, but the success envelope still carries them.
    kube = {
        "cluster_name": "c",
        "context_name": "ctx",
        "local_port": 41111,
        "endpoint": "https://127.0.0.1:41111",
        "tls_server_name": "node",
        "certificate_authority_data": "Y2E=",
        "client_certificate_data": "Y2VydA==",
        "client_key_data": "a2V5",
        "content_b64": "a3ViZWNvbmZpZw==",
        "path": "/s/tunnel-data/node-k3s",
    }
    session_dir = str(tmp_path)
    spawn[0](
        _success(
            {"node": {"ports": {}, "fetch_files": {}, "kube_targets": {"k3s": kube}}},
            session_dir=session_dir,
        )
    )
    with pytest.raises(SystemExit) as excinfo:
        _run_main(["plan"])
    assert excinfo.value.code == 0
    assert FakePopen.last_env is not None
    assert "KUBECONFIG" not in FakePopen.last_env, (
        "KUBECONFIG leaked into tofu's env: a broken config_path chain would "
        "silently reach the cluster via this fallback"
    )
    assert FakePopen.last_env["KUBE_CONFIG_PATH"] == kube["path"], (
        "KUBE_CONFIG_PATH must survive suppression: it is Mode A's provider "
        "channel through tunstrap_tofu (issue #14)"
    )
    # The structured channel the module decodes config_path from is still present.
    assert "TF_VAR_tunstrap" in FakePopen.last_env
    # The non-KUBECONFIG scalars are untouched (suppression is targeted).
    assert FakePopen.last_env.get("TUNSTRAP_SESSION_DIR") == session_dir


def test_tunnelled_drops_an_inherited_kubeconfig_in_the_multi_node_case(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """An operator-inherited KUBECONFIG is dropped in the multi-node case too.

    This fixture's connections are ports-only (no kube_targets), so
    ``render_kube_env`` injects nothing to overwrite the inherited value --
    the removal observed here is ``_build_child_env``'s unconditional
    pre-injection scrub of inherited KUBECONFIG/KUBE_CONFIG_PATH/_PATHS, not
    the ``suppress_kubeconfig`` pop (that scrub fires regardless of
    ``suppress_kubeconfig``, so this test would pass with it False too). The
    parametrized ``test_inherited_kube_env_never_survives_even_without_kube_targets``
    in ``test_cli_run_output_var.py`` pins that property directly at the
    ``_build_child_env`` level; this test is its proxy-path duplicate,
    observed through the real ``tunstrap_tofu`` entry point end to end.
    """
    # Inherited operator KUBECONFIG present.
    monkeypatch.setenv("KUBECONFIG", "/tmp/operator/.kube/config-some-other-cluster")
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    spawn[0](_success({"a": _conn(db=5432), "b": _conn(db=5433)}, session_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as excinfo:
        _run_main(["plan"])
    assert excinfo.value.code == 0
    assert FakePopen.last_env is not None
    assert "KUBECONFIG" not in FakePopen.last_env, (
        "inherited operator KUBECONFIG survived the multi-node path: a broken "
        "chain could reach the operator's own cluster via this fallback"
    )
    # Multi-node still gets the structured channel and the three session
    # survivors, but no target-scoped scalar (there is no node dimension for
    # TUNSTRAP_<TARGET>_* to disambiguate, so none must ever reappear).
    assert "TF_VAR_tunstrap" in FakePopen.last_env
    survivors = {"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE", VAR}
    leaked_scalars = [
        k for k in FakePopen.last_env if k.startswith("TUNSTRAP_") and k not in survivors
    ]
    assert leaked_scalars == []


def test_tunnelled_propagates_child_exit_code(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """The child's exit code reaches the caller verbatim (outside reserved set)."""
    monkeypatch.setenv(VAR, _payload())
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=str(tmp_path)))
    FakePopen.last_env = None

    class _Exit42(FakePopen):
        def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
            super().__init__(cmd, env)
            self.returncode = 42

    monkeypatch.setattr(cli_mod.subprocess, "Popen", _Exit42)
    with pytest.raises(SystemExit) as excinfo:
        _run_main(["plan"])
    assert excinfo.value.code == 42


def test_run_via_env_input_preserves_the_exit_64_usage_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``click.UsageError`` from ``run`` surfaces as exit 64, not a traceback.

    ``run_via_env_input`` calls the plain run implementation outside Click's group,
    so the ``_UsageExit64`` wrapper that normally turns a ``UsageError`` into
    exit 64 does not apply. Without an explicit guard the error propagates as a
    raw traceback. Triggered here with an ``--output-var`` name that collides
    with a key ``run`` injects (``KUBECONFIG`` for a kube-target payload): the
    collision is detected pre-spawn, so no daemon is orphaned either.
    """
    monkeypatch.setenv(
        VAR, _payload({"node": _node(kube_targets={"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}})})
    )

    def _spawn_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("spawn_daemon must not be reached on a pre-spawn usage error")

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_must_not_run)
    with pytest.raises(SystemExit) as excinfo:
        run_via_env_input(VAR, "KUBECONFIG", ["true"])  # KUBECONFIG collides
    assert excinfo.value.code == 64


# --------------------------------------------------------------------------- #
# Cost discipline: importing the proxy must not pull in heavy deps.
# --------------------------------------------------------------------------- #


def test_importing_proxy_does_not_pull_in_cli_or_heavy_deps() -> None:
    """The pass-through branches must pay no cli/click/pydantic/asyncssh import.

    Fresh interpreter, imports ONLY ``tunstrap.tofu_proxy``: none of the heavy
    modules the tunnelled branch uses may be loaded. This is the deterministic
    guard on the cost discipline; the measured per-invocation timing lives in
    the task report. ``importlib.metadata`` is included so a regression to an
    eager ``__version__`` lookup in ``tunstrap/__init__.py`` (which costs ~41 ms)
    fails here too.
    """
    blocked = {
        "tunstrap.cli",
        "click",
        "pydantic",
        "asyncssh",
        "cryptography",
        "ruamel",
        "importlib.metadata",
    }
    script = (
        "import sys, tunstrap.tofu_proxy as p; "
        f"loaded = sorted({blocked!r} & set(sys.modules)); "
        "import json; print(json.dumps(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout)
    msg = f"proxy module import pulled in heavy deps: {result.stdout}\nstderr: {result.stderr}"
    assert loaded == [], msg


def test_exec_tofu_calls_os_execvp_with_tofu_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass-through primitive execs ``tofu`` with the prog name prepended."""
    seen: dict[str, object] = {}

    def _fake(prog: str, argv: list[str]) -> None:
        seen["prog"] = prog
        seen["argv"] = list(argv)
        raise _ExecvpCalled("execvp")  # so the NoReturn helper does not sys.exit

    monkeypatch.setattr(proxy.os, "execvp", _fake)
    with pytest.raises(_ExecvpCalled):
        proxy._exec_tofu(["plan", "-out=x"])  # pylint: disable=protected-access
    assert seen == {"prog": "tofu", "argv": ["tofu", "plan", "-out=x"]}


def test_exec_tofu_missing_binary_exits_127_without_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing tofu is a shell-style launch failure, not a traceback."""

    def _missing(_prog: str, _argv: list[str]) -> None:
        raise FileNotFoundError("tofu not found")

    monkeypatch.setattr(proxy.os, "execvp", _missing)
    with pytest.raises(SystemExit) as excinfo:
        proxy._exec_tofu(["plan"])  # pylint: disable=protected-access
    captured = capsys.readouterr()
    assert excinfo.value.code == 127
    assert captured.out == ""
    assert captured.err == "tunstrap_tofu: cannot execute tofu: tofu not found\n"
