"""The e2e rig itself: self-containment, and real kube API traffic through it.

Code: tests/e2e/conftest.py, tests/e2e/docker-compose.yml.
Method: inspect the generated key material, then drive `tunstrap start` against
the kind cluster and talk to the API server through the resulting tunnel.
"""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.compose import compose_command
from tests.e2e import conftest
from tests.e2e.rig import (
    CLUSTER_NAME,
    COMPOSE_FILE,
    CONTROL_PLANE,
    HERE,
    REPO_ROOT,
    kubectl_in_node,
    recorded_argvs,
    require_tools,
    tunstrap_input_json,
    write_fake_tofu,
    write_tofu_recorder,
)

pytestmark = [pytest.mark.e2e]


def test_rig_generates_its_own_keypair(e2e_ssh_keypair: tuple[str, str]) -> None:
    """The e2e suite owns its key material and never reads the integration rig's."""
    private_pem, public_line = e2e_ssh_keypair
    assert private_pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_line.startswith("ssh-ed25519 ")

    priv_path = HERE / "_keys" / "id_test"
    assert priv_path.is_file()
    assert priv_path.stat().st_mode & 0o777 == 0o600
    assert (HERE / "_keys" / "id_test.pub").is_file()


def test_rig_borrows_nothing_from_another_suite(e2e_ssh_keypair: tuple[str, str]) -> None:
    """No cross-suite import, no cross-suite mount, and our own key material."""
    del e2e_ssh_keypair  # requested so the keypair exists before we assert on it

    # Parsed as an AST, not scanned as text. A substring search for the
    # forbidden path would match this test's own source - its docstring, its
    # own condition, and rig.py's module docstring all name that path in prose -
    # and could therefore never pass. Import statements are the thing that
    # actually creates a code dependency, and they are unambiguous in an AST.
    foreign: set[str] = set()
    for module in sorted(HERE.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Import):
                foreign.update(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("tests")
                    and not alias.name.startswith(("tests.compose", "tests.e2e"))
                )
            elif isinstance(node, ast.ImportFrom):
                if node.level >= 2:
                    foreign.add("." * node.level + (node.module or ""))
                elif (
                    node.module
                    and node.module.startswith("tests")
                    and not node.module.startswith(("tests.compose", "tests.e2e"))
                ):
                    foreign.add(node.module)
    assert not foreign, f"e2e modules importing another suite: {sorted(foreign)}"

    # Every bind mount is relative to this directory. Matching on ":/" catches
    # any host:container pair, including one written as ../integration/_keys,
    # which a "starts with ./" filter would silently skip.
    compose = (HERE / "docker-compose.yml").read_text()
    lines = [line.strip() for line in compose.splitlines()]
    mounts = sorted(line[2:] for line in lines if line.startswith("- ") and ":/" in line)
    assert mounts == [
        "./_keys:/keys:ro",
        "./_kube:/etc/kube:ro",
        "./_sshd_conf:/config/sshd/sshd_config.d:ro",
    ], mounts

    # ...and the three things those mounts point at are ours.
    assert (HERE / "_keys" / "id_test").is_file()
    assert (HERE / "_keys" / "id_test.pub").is_file()
    assert (HERE / "_sshd_conf" / "allow_tcpfwd.conf").is_file()


def test_sshd_forwarding_dropin_is_tracked_and_correct() -> None:
    """The drop-in that turns AllowTcpForwarding on is committed, not generated."""
    dropin = HERE / "_sshd_conf" / "allow_tcpfwd.conf"
    assert dropin.read_text().strip() == "AllowTcpForwarding yes"

    compose = (HERE / "docker-compose.yml").read_text()
    assert "./_sshd_conf:/config/sshd/sshd_config.d:ro" in compose
    assert "./_keys:/keys:ro" in compose
    assert "./_kube:/etc/kube:ro" in compose


def test_generated_rig_paths_are_gitignored() -> None:
    """_keys/ and _kube/ never enter the index; _sshd_conf/ always does."""
    repo_root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(
        ["git", "ls-files", "tests/e2e"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "tests/e2e/_sshd_conf/allow_tcpfwd.conf" in tracked
    assert not [p for p in tracked if p.startswith("tests/e2e/_keys/")]
    assert not [p for p in tracked if p.startswith("tests/e2e/_kube/")]


def test_cluster_node_is_ready_through_the_independent_oracle(kind_cluster: str) -> None:
    """The in-node kubectl oracle reaches the API server without any tunnel.

    Assertion audit - what each line can actually catch:
    - `returncode`/`stdout`: BEHAVIOURAL, but weakly so. Re-runs the oracle live,
      so it catches a control plane that died between fixture setup and now, and
      it catches `kindest/node` dropping the bundled kubectl - which would
      silently disable this tier's only independent read-back path. It is weakly
      self-referential about the *name*: CONTROL_PLANE is both the `docker exec`
      target and the expected value, so a rename moves both sides together. What
      survives is a real pin on kind's convention that the node name equals the
      container name, which `kube_targets` depends on.
    - `kind_cluster == CLUSTER_NAME`: PIN, not behavioural. The fixture yields
      that constant, so this cannot fail against a broken cluster. Kept because
      that name is also the SSH forward target and the expected tls_server_name,
      so it documents the coupling at the point of use.
    - the `wait` probe: BEHAVIOURAL, and the only line here that makes this
      test's name true.
    """
    probe = kubectl_in_node("get", "nodes", "-o", "name")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == f"node/{CONTROL_PLANE}"
    assert kind_cluster == CLUSTER_NAME

    # `get nodes -o name` prints the node whatever its condition, so nothing
    # above distinguishes Ready from NotReady. Readiness is really enforced by
    # `kind create --wait 90s` + check=True in the fixture; this asserts it
    # directly so the test's name is earned rather than assumed. --timeout=0
    # means "check once and do not wait", so a NotReady node fails immediately
    # instead of hanging.
    ready = kubectl_in_node("wait", "--for=condition=Ready", "node", "--all", "--timeout=0")
    assert ready.returncode == 0, ready.stderr


def test_in_node_kubeconfig_has_the_shape_the_tunnel_flow_depends_on(
    node_kubeconfig: Path,
) -> None:
    """admin.conf names the control plane by DNS and embeds CA + client creds.

    Assertion audit - two of these five are deliberate belt-and-braces, not
    behavioural checks, and saying so is the point:
    - `server:` line: PIN. The fixture already `pytest.fail`s on this exact
      substring and then writes that same string to the file, so it is strictly
      implied and cannot fire while the fixture is as it is. Kept as a
      regression guard on the *fixture*: if someone later rewrites the file, or
      drops the fixture's check, this catches it here where the dependency is
      documented. The fixture's own failure message is the better diagnostic.
    - mode 0644: PIN, for the same reason - the fixture chmods 0644
      unconditionally with an absolute mode, so no fixture-produced file can
      violate this. Kept because 0600 would break the unprivileged `tester` user
      reading it over SSH, which is otherwise invisible until Task 2.4 fails
      obscurely.
    - the three `-data:` assertions: BEHAVIOURAL. Nothing in the fixture looks at
      them, so they pin upstream kubeconfig content: if kind ever emitted
      external credentials (an exec plugin, or a `client-key` path instead of
      embedded data), `parse_kubeconfig` would raise and these fail first,
      naming the reason.
    """
    text = node_kubeconfig.read_text()
    assert f"server: https://{CONTROL_PLANE}:6443" in text  # pin (see docstring)
    assert "certificate-authority-data:" in text
    assert "client-certificate-data:" in text
    assert "client-key-data:" in text
    assert node_kubeconfig.stat().st_mode & 0o777 == 0o644  # pin (see docstring)


def test_rig_publishes_a_dynamic_port_and_accepts_the_generated_key(
    kube_rig: dict[str, Any],
) -> None:
    """The rig hands back a live, authenticated SSH endpoint on a random port.

    Assertion audit - what actually carries weight here:
    - The strongest check is *implicit*: reaching the body at all means
      `kube_rig` completed, and `kube_rig` does not yield until `_wait_for_ssh`
      has run a real authenticated `echo ssh-ready` over SSH. A dead or
      unauthenticated sshd errors this test in setup, before any assert runs.
      Measured against a container holding a decoy key: the probe failed with
      "never accepted an authenticated SSH exec ... within 6s" while a plain TCP
      connect to the same port succeeded.
    - `port != 2222`: BEHAVIOURAL, and the load-bearing assertion of the body.
      Compose publishes "127.0.0.1::2222", i.e. a random host port, so a fixture
      that assumed 2222 would connect to nothing - or to an unrelated local
      service - and every downstream tunnelling test would fail with an opaque
      SSH error. Observed ports across runs: 33161, 33162 (ephemeral range).
    - `port > 1024`: BEHAVIOURAL (weak). Catches a parse that yielded 0 or a
      negative from `docker compose port` output.
    - `kubeconfig_in_node_path == "/etc/kube/admin.conf"`: BEHAVIOURAL as a
      cross-file pin. The fixture yields the IN_NODE_KUBECONFIG constant while
      this compares against the literal, so changing the constant without
      changing docker-compose.yml's `./_kube:/etc/kube:ro` mount fails here.
    - `host`, `user`, `control_plane`: PINS. The fixture yields those literals
      and constants, so they cannot fail against a broken rig. Kept because they
      are the exact tuple Task 2.5 and every Phase 4/5 tunnelling test consume.
    - `docker compose ps` contains "sshd-kube": BEHAVIOURAL. Separates "port
      discovery is wrong" from "the container is dead".
    """
    assert kube_rig["host"] == "127.0.0.1"
    assert kube_rig["port"] > 1024
    assert kube_rig["port"] != 2222
    assert kube_rig["user"] == "tester"
    assert kube_rig["control_plane"] == CONTROL_PLANE
    assert kube_rig["kubeconfig_in_node_path"] == "/etc/kube/admin.conf"

    listed = subprocess.run(
        compose_command(COMPOSE_FILE, REPO_ROOT, "e2e", "ps", "--format", "{{.Name}}"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "sshd-kube" in listed.stdout


def test_tunnel_carries_real_kube_api_traffic(kube_rig: dict[str, Any], tmp_path: Path) -> None:
    """A tunstrap tunnel to sshd-kube reaches the real API server, warning-free.

    The tier's first-failure gate. Everything before it tests the harness; this
    is the first test that pushes real Kubernetes API traffic through a real
    tunstrap tunnel, so once it is green a later failure can be attributed to
    the feature under test rather than to the rig.

    Assertion audit - every assertion here is behavioural, and each has a
    distinct symptom (demonstrated by the assertions below):
    - `started.returncode == 0`: fires if the forwarding drop-in is missing or
      the service is off kind's network, because the SAN probe itself traverses
      the forward during `start` and is refused `administratively prohibited`.
    - `warnings == []` and the materialized kubeconfig's `tls-server-name`:
      pin the clean, exact-SAN-match branch. A silent downgrade to
      insecure-skip-tls-verify would still let kubectl succeed, so without
      these the test would pass while the security property it exists to prove
      had regressed.
    - `endpoint` is a local https URL and `path` is not None: materialization
      and patching. `run` forces materialize, `start` does not, hence the
      explicit materialize=True here.
    - the `kubectl` probe: the actual gate. `node/<control-plane>` distinguishes
      "reached the real API server" from "reached something that answered" - a
      TLS handshake that terminated anywhere else cannot produce that exact node
      name. Proven to fail while `start` still exits 0 by breaking the forward
      after a successful start, with the independent in-node oracle staying
      green throughout to show the cluster was healthy and the tunnel was not.
    """
    # The in-node oracle deliberately uses the node image's own kubectl, so the
    # tier needs no host kubectl until here - this is the first consumer of one.
    require_tools("kubectl")

    session_dir = str(tmp_path / "session")
    started = subprocess.run(
        ["tunstrap", "start", "--session-dir", session_dir],
        input=tunstrap_input_json(kube_rig, materialize=True),
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, f"stdout={started.stdout!r} stderr={started.stderr!r}"
    envelope = json.loads(started.stdout)
    try:
        assert envelope["warnings"] == []
        target = envelope["connections"]["node"]["kube_targets"]["k3s"]
        assert target["endpoint"].startswith("https://127.0.0.1:")
        kubeconfig = target["path"]
        assert kubeconfig is not None, "materialize=True must yield a path"
        assert f"tls-server-name: {CONTROL_PLANE}" in Path(kubeconfig).read_text()
        assert set(target) == {"path", "context", "endpoint"}

        probe = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig, "get", "nodes", "-o", "name"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == f"node/{CONTROL_PLANE}"
    finally:
        subprocess.run(
            [
                "tunstrap",
                "stop",
                "--session-dir",
                session_dir,
                "--grace-seconds",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )


def test_ssh_readiness_bounds_each_attempt_and_honours_its_deadline(
    e2e_ssh_keypair: tuple[str, str],
) -> None:
    """A stalled listener cannot make the readiness poll overrun its deadline.

    Needs no cluster and no Docker. AsyncSSH's default login timeout is 120s -
    longer than the probe's own 90s deadline - so an unbounded attempt against a
    peer that accepts and then never speaks SSH blocks past the deadline
    entirely, and the "within Ns" in the failure message becomes a false claim.

    Fails-when-broken: without a per-attempt bound this takes ~120s for a 4s
    deadline and the elapsed assertion fires. It cannot pass vacuously either -
    if the probe wrongly reported success against a listener that never sent a
    version banner, `pytest.raises` would fail instead.
    """
    private_pem, _public_line = e2e_ssh_keypair

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    # Closing a socket does not wake a thread already blocked in accept() on
    # Linux, so the loop polls with a timeout and watches a stop flag instead -
    # otherwise cleanup would sit out a full join timeout on every run.
    listener.settimeout(0.25)
    port = int(listener.getsockname()[1])
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept_and_stay_silent() -> None:
        while not stop.is_set():
            try:
                conn, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            # Deliberately never write an SSH version banner. This is the
            # "listening but not speaking" peer that hangs a login.
            held.append(conn)

    accepter = threading.Thread(target=_accept_and_stay_silent, daemon=True)
    accepter.start()

    deadline_s = 4.0
    try:
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception) as excinfo:
            conftest._wait_for_ssh(port, private_pem, timeout=deadline_s)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        accepter.join(timeout=5.0)
        listener.close()
        for conn in held:
            conn.close()

    assert f"127.0.0.1:{port}" in str(excinfo.value)
    # The claim in the message must be true: it says "within 4s", so it must not
    # have taken 120. Slack covers one in-flight attempt plus scheduling.
    overran = f"readiness poll overran: claimed {deadline_s:.0f}s, took {elapsed:.1f}s"
    assert elapsed < deadline_s + 5.0, overran


def _kubeconfig_stub(tmp_path: Path) -> Path:
    """A file that satisfies kube_rig's ordering guard without a real cluster."""
    stub = tmp_path / "admin.conf"
    stub.write_text("stub\n")
    return stub


def test_kube_rig_arms_its_teardown_before_compose_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compose up that half-starts and then fails must still be torn down.

    Needs no cluster and no Docker: `subprocess.run` is replaced, so this
    exercises the fixture's control flow directly.

    Fails-when-broken: if `try` opens *after* `compose up`, the finally is never
    armed, no `down` is issued, and the leaked stack survives the run. That is
    the same defect the 2.3 review found in `kind_cluster`.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if "up" in cmd:
            # Containers may already exist at this point - this is precisely the
            # case where teardown matters.
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    generator = conftest.kube_rig.__wrapped__(("pem", "pub"), _kubeconfig_stub(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        next(generator)

    assert any("down" in call for call in calls), (
        "compose up failed and no `compose down` followed - the teardown was "
        f"never armed. Calls seen: {calls}"
    )


def test_kube_rig_reports_a_failed_compose_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teardown that fails must be visible, not silent.

    Fails-when-broken: with `check=False` and no inspection, a failed
    `compose down` leaves the stack running and says nothing, so the next run
    inherits a dirty rig with no clue why. `pytest.warns` reports DID NOT WARN.
    """

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "port" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "127.0.0.1:12345\n", "")
        if "down" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "error: network kind is in use")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(conftest, "_wait_for_ssh", lambda *a, **k: None)

    generator = conftest.kube_rig.__wrapped__(("pem", "pub"), _kubeconfig_stub(tmp_path))
    rig = next(generator)
    assert rig["port"] == 12345  # discovered, not assumed

    with pytest.warns(UserWarning, match="network kind is in use"), pytest.raises(StopIteration):
        next(generator)


def test_write_fake_tofu_forwards_an_adversarial_stdout_line_byte_identical(
    tmp_path: Path,
) -> None:
    """write_fake_tofu emits percent, quote and backslash bytes unmangled.

    The emitter once baked the line into a printf *format string*, so a percent
    was read as a directive, a double-quote broke the shell quoting, and a
    backslash was an escape. Three tasks passed without tripping it because every
    line was tame (FAKE_TOFU_RAN, FAKE_TOFU_EXIT_42). The stdout-purity task is
    exactly the one that has to push adversarial bytes, so the emitter was
    switched to ``printf '%s\\n' <shlex.quote(line)>``.

    Fails-when-broken: against the old emitter this line is a shell syntax error
    (unbalanced quote), so the script exits non-zero with empty stdout and both
    assertions fire. The exact-byte check (not "no percent left over") is what
    closes the door on a future emitter that mangles some *other* byte: a novel
    contaminant still changes the length or content and fails the equality.

    Needs no cluster and no Docker - the fake script is run directly.
    """
    bin_dir = tmp_path / "bin"
    marker_dir = tmp_path / "marks"
    # One line carrying all three adversarial bytes at once.
    line = 'pre%smid"post\\tail'
    write_fake_tofu(bin_dir, marker_dir, exit_code=0, stdout_line=line)

    result = subprocess.run(
        [str(bin_dir / "tofu"), "ignored-argv"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (line + "\n").encode()
    # The fix touched the whole generated script; confirm argv recording survived.
    assert recorded_argvs(marker_dir) == [["ignored-argv"]]


def test_write_tofu_recorder_locks_down_its_diagnostic_dumps(tmp_path: Path) -> None:
    """The 0700/0600 modes the recorder sets are asserted, not merely written.

    For `init` invocations the dumped environment carries `TUNSTRAP_INPUT`, i.e.
    the generated SSH private key, so the recorder deliberately overrides the
    default umask. Nothing checked it, so dropping either `chmod` — or the
    `dump_dir.chmod(0o700)` — was a silent regression on a directory holding key
    material.

    Exact compares (`& 0o777 == …`), not `not ... & 0o077`: a mode that merely
    happens to be private under this runner's umask must not pass for a mode the
    rig actually set. The directory check is the load-bearing one, since it makes
    the files unreachable by other users whatever their own mode; the per-file
    checks are defence in depth for a directory mode that later regresses.

    Needs `tofu` on PATH (the recorder execs it) but no cluster: `-version` is
    served locally.
    """
    require_tools("tofu")
    bin_dir = tmp_path / "bin"
    dump_dir = tmp_path / "dumps"
    script = write_tofu_recorder(bin_dir, dump_dir)

    result = subprocess.run([str(script), "-version"], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr

    assert dump_dir.stat().st_mode & 0o777 == 0o700, "the dump directory is not owner-only"
    argv_dumps = sorted(dump_dir.glob("*.argv"))
    env_dumps = sorted(dump_dir.glob("*.env0"))
    assert len(argv_dumps) == 1, f"expected exactly one argv dump, got {argv_dumps}"
    assert len(env_dumps) == 1, f"expected exactly one env dump, got {env_dumps}"
    for dump in (*argv_dumps, *env_dumps):
        assert dump.stat().st_mode & 0o777 == 0o600, f"{dump.name} is not owner-only"
    # Anti-vacuity: the dumps must be the real recording, not empty files that
    # would satisfy a mode check while proving the recorder does nothing.
    assert argv_dumps[0].read_text() == "-version\n"
    assert b"PATH=" in env_dumps[0].read_bytes()
