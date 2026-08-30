# CLI run modes (#6 + #5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add flag-driven single-node input + `--output env` to `start` (#6) and a foreground `run … -- CMD` wrapper that injects `TUNSTRAP_*`/`KUBECONFIG`, runs a child, and tears the tunnel down (#5).

**Architecture:** Two new pure modules — `cli_input.py` (CLI flags → `InputSchema`) and `envrender.py` (`OutputSchema` → `TUNSTRAP_*` env). `start` gains an optional `USER@HOST[:PORT]` arg, connection flags, and `--output json|env`; `run` is a new command sharing both modules and adding child-process lifecycle. `NodeInput` is relaxed to allow kube-only nodes. No daemon/IPC/lock changes (stable from Task A).

**Tech Stack:** Python 3.10+, Click, Pydantic v2, `subprocess`, `signal`, pytest. Spec: `docs/specs/2026-06-25-cli-run-modes-design.md`. Use `.venv/bin/{pytest,ruff,mypy,pylint,vulture}`; integration needs Docker and `.venv/bin` on `PATH`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tunstrap/schemas.py` | Relax `NodeInput`: empty `remote_targets` allowed when `kube_targets`/`fetch_files` present; reject a node that does nothing. |
| `tunstrap/cli_input.py` | New. Parse `USER@HOST[:PORT]` + `NAME=VALUE` flags → single-node `InputSchema`. |
| `tunstrap/envrender.py` | New. `render_env(OutputSchema)->dict`, `format_exports(dict)->str`. |
| `tunstrap/cli.py` | `start` gains connection arg/flags/`--output`; new `run`; conflict validation; shared `_connection_options` decorator. |
| `README.md` | Document flag mode, `--output env`, `run`. |

---

### Task B0: Relax `NodeInput` to allow kube-only / fetch-only nodes

**Files:**
- Modify: `tunstrap/schemas.py`
- Test: `tests/unit/test_schemas_remote_targets.py`, `tests/unit/test_schemas.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_schemas.py`:

```python
def test_node_kube_only_allows_empty_remote_targets():
    from tunstrap.schemas import InputSchema
    schema = InputSchema.model_validate({
        "nodes": {"n": {
            "host": "h", "user": "u", "ssh_pkey": "k",
            "remote_targets": {},
            "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
        }},
    })
    assert schema.nodes["n"].remote_targets == {}
    assert "k3s" in schema.nodes["n"].kube_targets


def test_node_doing_nothing_is_rejected():
    import pytest
    from pydantic import ValidationError
    from tunstrap.schemas import InputSchema
    with pytest.raises(ValidationError, match="at least one of"):
        InputSchema.model_validate({
            "nodes": {"n": {"host": "h", "user": "u", "ssh_pkey": "k", "remote_targets": {}}},
        })
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_schemas.py -k "kube_only or doing_nothing" -v`
Expected: FAIL (empty remote_targets currently raises "at least 1 entry required").

- [ ] **Step 3: Edit `schemas.py`**

1. Add `model_validator` to the imports: `from pydantic import ..., model_validator` (keep existing imports).
2. Change the field declaration:

```python
    remote_targets: dict[str, RemoteTarget] = Field(default_factory=dict)
```

3. In `_validate_remote_targets`, handle `None` and DELETE the empty-dict raise:

```python
    @field_validator("remote_targets", mode="before")
    @classmethod
    def _validate_remote_targets(cls, value: object) -> dict[str, RemoteTarget]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("remote_targets must be a dict")
        if len(value) > 16:
            raise ValueError("remote_targets: at most 16 entries per node")
        parsed: dict[str, RemoteTarget] = {}
        for handle, raw in value.items():
            # ... unchanged body (key validation + host:port / dict / RemoteTarget parsing) ...
        return parsed
```

(Keep the per-entry parsing exactly as-is; only `None` handling and the removal of the `len(value) == 0` raise change.)

4. Add a model validator after the field validators in `NodeInput`:

```python
    @model_validator(mode="after")
    def _validate_node_does_something(self) -> "NodeInput":
        if not self.remote_targets and not self.kube_targets and not self.fetch_files:
            raise ValueError(
                "node must define at least one of remote_targets, kube_targets, fetch_files"
            )
        return self
```

- [ ] **Step 4: Update any test asserting empty remote_targets is rejected**

In `tests/unit/test_schemas_remote_targets.py`, find any test expecting empty `remote_targets` to raise "at least 1 entry" and update it: empty is now valid **only** with kube/fetch; empty-and-nothing-else raises "at least one of". Run `.venv/bin/pytest tests/unit/test_schemas_remote_targets.py -v` and fix until green.

- [ ] **Step 5: Run unit suite**

Run: `.venv/bin/pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tunstrap/schemas.py tests/unit/test_schemas.py tests/unit/test_schemas_remote_targets.py
git commit -m "feat(schemas): allow kube-only/fetch-only nodes (empty remote_targets)"
```

---

### Task B1: `cli_input.py` — flags → single-node `InputSchema`

**Files:**
- Create: `tunstrap/cli_input.py`
- Test: `tests/unit/test_cli_input.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_cli_input.py
import pytest
from tunstrap.cli_input import parse_endpoint, parse_named, build_single_node_schema
from tunstrap.exceptions import SchemaValidationError
from tunstrap.schemas import DaemonOptions


def test_parse_endpoint_defaults_port():
    assert parse_endpoint("root@host") == ("root", "host", 22)

def test_parse_endpoint_explicit_port():
    assert parse_endpoint("u@h:2222") == ("u", "h", 2222)

def test_parse_endpoint_ipv6():
    assert parse_endpoint("u@[2001:db8::1]:6443") == ("u", "2001:db8::1", 6443)

def test_parse_endpoint_missing_user():
    with pytest.raises(SchemaValidationError):
        parse_endpoint("host:22")

def test_parse_endpoint_bad_port():
    with pytest.raises(SchemaValidationError):
        parse_endpoint("u@h:99999")

def test_parse_named_ok():
    assert parse_named(("api=127.0.0.1:6443",), "target") == {"api": "127.0.0.1:6443"}

def test_parse_named_missing_eq():
    with pytest.raises(SchemaValidationError):
        parse_named(("noeq",), "target")

def test_parse_named_dup():
    with pytest.raises(SchemaValidationError):
        parse_named(("a=1", "a=2"), "target")

def test_build_kube_only(tmp_path):
    key = tmp_path / "id"
    key.write_text("PEMDATA")
    schema = build_single_node_schema(
        connection="root@h:22", ssh_key=str(key), ssh_key_passphrase=None,
        ssh_password=None, targets=(), kube=("k3s=/etc/rancher/k3s/k3s.yaml",),
        fetch=(), daemon_opts=DaemonOptions(),
    )
    node = schema.nodes["h"]
    assert node.user == "root" and node.port == 22
    assert node.ssh_pkey == "PEMDATA"
    assert node.remote_targets == {}
    assert node.kube_targets["k3s"].kubeconfig_path == "/etc/rancher/k3s/k3s.yaml"

def test_build_target_and_password():
    schema = build_single_node_schema(
        connection="u@h", ssh_key=None, ssh_key_passphrase=None,
        ssh_password="secret", targets=("db=127.0.0.1:5432",), kube=(), fetch=(),
        daemon_opts=DaemonOptions(),
    )
    node = schema.nodes["h"]
    assert node.ssh_password == "secret"
    assert node.remote_targets["db"].port == 5432
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_cli_input.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Create `tunstrap/cli_input.py`**

```python
"""Build a single-node InputSchema from CLI flags (issue #6)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from tunstrap.exceptions import SchemaValidationError
from tunstrap.schemas import DaemonOptions, InputSchema


def parse_endpoint(endpoint: str) -> tuple[str, str, int]:
    """Parse ``USER@HOST[:PORT]`` into (user, host, port); default port 22."""
    user, sep, hostpart = endpoint.partition("@")
    if not sep or not user:
        raise SchemaValidationError(
            "connection must be USER@HOST[:PORT]", {"value": endpoint}
        )
    host, port = _split_host_port(hostpart, endpoint)
    return user, host, port


def _split_host_port(hostpart: str, original: str) -> tuple[str, int]:
    if hostpart.startswith("["):  # IPv6 literal: [addr] or [addr]:port
        end = hostpart.find("]")
        if end == -1:
            raise SchemaValidationError("malformed IPv6 host (missing ']')", {"value": original})
        host = hostpart[1:end]
        rest = hostpart[end + 1 :]
        if rest == "":
            return host, 22
        if not rest.startswith(":"):
            raise SchemaValidationError("expected ':PORT' after ']'", {"value": original})
        return host, _parse_port(rest[1:], original)
    if ":" in hostpart:
        host, _, raw_port = hostpart.rpartition(":")
        if not host:
            raise SchemaValidationError("connection missing host", {"value": original})
        return host, _parse_port(raw_port, original)
    if not hostpart:
        raise SchemaValidationError("connection missing host", {"value": original})
    return hostpart, 22


def _parse_port(raw: str, original: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise SchemaValidationError("port must be an integer", {"value": original}) from exc
    if not 1 <= port <= 65535:
        raise SchemaValidationError("port out of range 1-65535", {"value": original})
    return port


def parse_named(items: tuple[str, ...], label: str) -> dict[str, str]:
    """Parse repeated ``NAME=VALUE`` flags; reject empty/missing/duplicate."""
    out: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise SchemaValidationError(f"--{label} must be NAME=VALUE", {"value": item})
        if not name:
            raise SchemaValidationError(f"--{label} has empty NAME", {"value": item})
        if not value:
            raise SchemaValidationError(f"--{label} has empty VALUE", {"value": item})
        if name in out:
            raise SchemaValidationError(f"--{label} duplicate name {name!r}", {"value": item})
        out[name] = value
    return out


def build_single_node_schema(
    *,
    connection: str,
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password: str | None,
    targets: tuple[str, ...],
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    daemon_opts: DaemonOptions,
) -> InputSchema:
    """Assemble a one-node InputSchema from parsed CLI inputs."""
    user, host, port = parse_endpoint(connection)

    pkey: str | None = None
    if ssh_key is not None:
        try:
            pkey = Path(ssh_key).read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaValidationError(
                "cannot read --ssh-key file", {"path": ssh_key, "error": str(exc)}
            ) from exc

    remote_targets = parse_named(targets, "target")
    kube_named = parse_named(kube, "kube")
    fetch_named = parse_named(fetch, "fetch")

    node: dict[str, object] = {
        "host": host,
        "port": port,
        "user": user,
        "ssh_pkey": pkey,
        "ssh_password": ssh_password,
        "ssh_pkey_passphrase": ssh_key_passphrase,
        "remote_targets": remote_targets,
    }
    if kube_named:
        node["kube_targets"] = {k: {"kubeconfig_path": v} for k, v in kube_named.items()}
    if fetch_named:
        node["fetch_files"] = {k: {"path": v} for k, v in fetch_named.items()}

    try:
        return InputSchema.model_validate(
            {"nodes": {host: node}, "daemon": daemon_opts.model_dump()}
        )
    except ValidationError as exc:
        raise SchemaValidationError(
            "CLI input does not satisfy the schema", {"errors": json.loads(exc.json())}
        ) from exc
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/unit/test_cli_input.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add tunstrap/cli_input.py tests/unit/test_cli_input.py
git commit -m "feat(cli_input): build single-node InputSchema from CLI flags (#6)"
```

---

### Task B2: `envrender.py` — OutputSchema → TUNSTRAP_* env

**Files:**
- Create: `tunstrap/envrender.py`
- Test: `tests/unit/test_envrender.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_envrender.py
import pytest
from tunstrap.envrender import render_env, format_exports
from tunstrap.schemas import OutputSchema, NodeOutput, KubeTargetOutput


def _kube_out(port, path):
    return KubeTargetOutput(
        cluster_name="c", context_name="ctx", local_port=port,
        endpoint=f"https://127.0.0.1:{port}", tls_server_name="c",
        certificate_authority_data="", client_certificate_data="",
        client_key_data="", content_b64="", path=path,
    )


def test_render_ports_and_session():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db-1": 5432})},
        pid=42, session_dir="/run/s", started_at="now",
    )
    env = render_env(out)
    assert env["TUNSTRAP_SESSION_DIR"] == "/run/s"
    assert env["TUNSTRAP_PID"] == "42"
    assert env["TUNSTRAP_DB_1_PORT"] == "5432"
    assert env["TUNSTRAP_DB_1_ENDPOINT"] == "127.0.0.1:5432"
    assert "KUBECONFIG" not in env


def test_render_kube_sets_kubeconfig():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/run/s/tunnel-data/k3s")})},
        pid=1, session_dir="/run/s", started_at="now",
    )
    env = render_env(out)
    assert env["TUNSTRAP_K3S_KUBECONFIG"] == "/run/s/tunnel-data/k3s"
    assert env["KUBECONFIG"] == "/run/s/tunnel-data/k3s"
    assert env["TUNSTRAP_K3S_ENDPOINT"] == "https://127.0.0.1:7000"


def test_render_kube_not_materialized_raises():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, None)})},
        pid=1, session_dir="/run/s", started_at="now",
    )
    with pytest.raises(ValueError, match="not materialized"):
        render_env(out)


def test_render_requires_single_node():
    out = OutputSchema(connections={}, pid=1, session_dir="/s", started_at="now")
    with pytest.raises(ValueError, match="exactly one node"):
        render_env(out)


def test_format_exports_quotes_safely():
    txt = format_exports({"A": "x'y", "B": "z"})
    assert "export A='x'\\''y'" in txt
    assert "export B='z'" in txt
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_envrender.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Create `tunstrap/envrender.py`**

```python
"""Render an OutputSchema into TUNSTRAP_* environment variables (#6/#5)."""

from __future__ import annotations

import re

from tunstrap.schemas import OutputSchema

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _key(name: str) -> str:
    """Sanitise a target/kube name into an env-var segment (upper, _-joined)."""
    return _NON_ALNUM.sub("_", name.upper())


def render_env(output: OutputSchema) -> dict[str, str]:
    """Build the TUNSTRAP_* env mapping for a single-node OutputSchema."""
    if len(output.connections) != 1:
        raise ValueError("render_env requires exactly one node")
    (node,) = output.connections.values()

    env: dict[str, str] = {
        "TUNSTRAP_SESSION_DIR": output.session_dir,
        "TUNSTRAP_PID": str(output.pid),
    }

    def put(key: str, value: str) -> None:
        if key in env:
            raise ValueError(f"env key collision: {key}")
        env[key] = value

    for tname, port in node.ports.items():
        base = _key(tname)
        put(f"TUNSTRAP_{base}_HOST", "127.0.0.1")
        put(f"TUNSTRAP_{base}_PORT", str(port))
        put(f"TUNSTRAP_{base}_ENDPOINT", f"127.0.0.1:{port}")

    kube_paths: list[str] = []
    for kname, target in node.kube_targets.items():
        base = _key(kname)
        if target.path is None:
            raise ValueError(f"kube target {kname!r} not materialized; cannot set KUBECONFIG")
        put(f"TUNSTRAP_{base}_KUBECONFIG", target.path)
        put(f"TUNSTRAP_{base}_ENDPOINT", target.endpoint)
        kube_paths.append(target.path)

    if kube_paths:
        put("KUBECONFIG", ":".join(kube_paths))
    return env


def format_exports(env: dict[str, str]) -> str:
    """Render an env mapping as POSIX-safe ``export K='V'`` lines."""
    lines = [f"export {key}='{_shell_single_quote(value)}'" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def _shell_single_quote(value: str) -> str:
    """Escape a value for inclusion inside single quotes in POSIX sh."""
    return value.replace("'", "'\\''")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/unit/test_envrender.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/envrender.py tests/unit/test_envrender.py
git commit -m "feat(envrender): OutputSchema -> TUNSTRAP_* env + KUBECONFIG (#6)"
```

---

### Task B3: Wire flag input + `--output` into `start`, add conflict validation

**Files:**
- Modify: `tunstrap/cli.py`
- Test: `tests/unit/test_cli_runner.py`, `tests/unit/test_cli_parsing.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_cli_runner.py` (uses `click.testing.CliRunner`; `monkeypatch` `tunstrap.cli.spawn_daemon` to avoid real SSH):

```python
def test_start_flag_mode_builds_schema(monkeypatch):
    from tunstrap import cli as cli_mod
    captured = {}
    def fake_spawn(schema, session_dir=None):
        captured["schema"] = schema
        return {"kind": "success", "payload": {"connections": {}, "pid": 1, "session_dir": "/s", "started_at": "now"}}
    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    from tunstrap.cli import main
    from click.testing import CliRunner
    res = CliRunner().invoke(main, ["start", "root@h:22", "--target", "db=127.0.0.1:5432"])
    assert res.exit_code == 0
    assert captured["schema"].nodes["h"].user == "root"


def test_start_rejects_trailing_command():
    from tunstrap.cli import main
    from click.testing import CliRunner
    res = CliRunner().invoke(main, ["start", "root@h", "--", "helm", "list"])
    assert res.exit_code == 64
    assert "run" in res.output.lower()


def test_start_connection_plus_stdin_rejected(monkeypatch):
    from tunstrap.cli import main
    from click.testing import CliRunner
    res = CliRunner().invoke(main, ["start", "root@h", "--target", "a=192.0.2.1:1"], input='{"nodes":{}}')
    assert res.exit_code == 64


def test_start_conn_flag_without_connection_rejected():
    from tunstrap.cli import main
    from click.testing import CliRunner
    res = CliRunner().invoke(main, ["start", "--target", "a=192.0.2.1:1"])
    assert res.exit_code == 64


def test_start_output_env(monkeypatch):
    from tunstrap import cli as cli_mod
    def fake_spawn(schema, session_dir=None):
        return {"kind": "success", "payload": {
            "connections": {"h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}},
            "pid": 7, "session_dir": "/s", "started_at": "now"}}
    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    from tunstrap.cli import main
    from click.testing import CliRunner
    res = CliRunner().invoke(main, ["start", "u@h", "--target", "db=127.0.0.1:5432", "--output", "env"])
    assert res.exit_code == 0
    assert "export TUNSTRAP_DB_PORT='5432'" in res.output
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_cli_runner.py -k "flag_mode or trailing or stdin_rejected or without_connection or output_env" -v`
Expected: FAIL.

- [ ] **Step 3: Implement in `cli.py`**

Add imports near the top:

```python
from tunstrap.cli_input import build_single_node_schema
from tunstrap.envrender import format_exports, render_env
from tunstrap.schemas import DaemonOptions, InputSchema, OutputSchema
```

Add a shared connection-options decorator (place above `start_command`):

```python
def _connection_options(func):
    """Attach the shared single-node connection flags to a command."""
    decorators = [
        click.option("--ssh-key", "ssh_key", default=None, help="Path to a private key file."),
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


def _conn_flags_present(*, ssh_key, ssh_key_passphrase, ssh_password_stdin, targets, kube, fetch):
    return any([ssh_key, ssh_key_passphrase, ssh_password_stdin, targets, kube, fetch])


def _schema_from_flags(connection, *, ssh_key, ssh_key_passphrase, ssh_password_stdin,
                       targets, kube, fetch, auto_stop_idle_seconds, materialize, log_file,
                       force_materialize=False):
    ssh_password = None
    if ssh_password_stdin:
        ssh_password = sys.stdin.readline().rstrip("\n")
    daemon = DaemonOptions(
        auto_stop_idle_seconds=auto_stop_idle_seconds,
        materialize=materialize or force_materialize,
        log_file=log_file,
    )
    return build_single_node_schema(
        connection=connection, ssh_key=ssh_key, ssh_key_passphrase=ssh_key_passphrase,
        ssh_password=ssh_password, targets=targets, kube=kube, fetch=fetch, daemon_opts=daemon,
    )
```

Replace `start_command` with the connection-aware version:

```python
@main.command("start")
@click.argument("connection", required=False)
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@_connection_options
@click.option("--output", "output_fmt", type=click.Choice(["json", "env"]), default="json", show_default=True)
@click.option("--session-dir", "session_dir", default=None)
def start_command(connection, extra, ssh_key, ssh_key_passphrase, ssh_password_stdin,
                  targets, kube, fetch, auto_stop_idle_seconds, materialize, log_file,
                  output_fmt, session_dir):
    """Open tunnels and daemonize. Input: USER@HOST[:PORT] flags, or JSON on stdin."""
    try:
        if extra:
            raise click.UsageError("`--` invokes a child command; use `tunstrap run ... -- CMD`")
        conn_flags = _conn_flags_present(
            ssh_key=ssh_key, ssh_key_passphrase=ssh_key_passphrase,
            ssh_password_stdin=ssh_password_stdin, targets=targets, kube=kube, fetch=fetch,
        )
        if connection is None and conn_flags:
            raise click.UsageError("connection flags require a USER@HOST[:PORT] argument")

        if connection is not None:
            schema = _schema_from_flags(
                connection, ssh_key=ssh_key, ssh_key_passphrase=ssh_key_passphrase,
                ssh_password_stdin=ssh_password_stdin, targets=targets, kube=kube, fetch=fetch,
                auto_stop_idle_seconds=auto_stop_idle_seconds, materialize=materialize,
                log_file=log_file, force_materialize=(output_fmt == "env"),
            )
        else:
            raw = sys.stdin.read()
            if not raw.strip():
                raise SchemaValidationError("no input: provide USER@HOST[:PORT] or JSON on stdin", {})
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SchemaValidationError("stdin is not valid JSON", {"position": exc.pos}) from exc
            try:
                schema = InputSchema.model_validate(payload)
            except ValidationError as exc:
                raise SchemaValidationError("input does not satisfy the InputSchema contract",
                                            {"errors": json.loads(exc.json())}) from exc

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
    except click.UsageError:
        raise
    except TunstrapError as exc:
        sys.stdout.write(json.dumps(exc.to_error_output()) + "\n")
        sys.stdout.flush()
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        sys.stdout.write(json.dumps(DaemonError("unexpected failure during start",
                                                {"type": type(exc).__name__}).to_error_output()) + "\n")
        sys.stdout.flush()
        sys.exit(4)
```

Note: `click.UsageError` raised inside the command propagates to `_UsageExit64.main` → exit 64.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/unit/test_cli_runner.py tests/unit/test_cli_parsing.py -v`
Expected: PASS (update any pre-existing `start` parsing test that assumed no positional/flags).

- [ ] **Step 5: Commit**

```bash
git add tunstrap/cli.py tests/unit/test_cli_runner.py tests/unit/test_cli_parsing.py
git commit -m "feat(cli): start flag mode + --output env + conflict validation (#6)"
```

---

### Task C1: `run` command — foreground wrapper with teardown

**Files:**
- Modify: `tunstrap/cli.py`
- Test: `tests/unit/test_cli_run.py`

- [ ] **Step 1: Write failing tests** (mock the daemon + child)

```python
# tests/unit/test_cli_run.py
import json
from click.testing import CliRunner
from tunstrap import cli as cli_mod
from tunstrap.cli import main


def _success_payload():
    return {"kind": "success", "payload": {
        "connections": {"h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}},
        "pid": 99, "session_dir": "/s", "started_at": "now"}}


def test_run_injects_env_and_propagates_exit(monkeypatch):
    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload())
    stops = []
    monkeypatch.setattr(cli_mod, "_teardown_run", lambda sd, gs: stops.append(sd))
    seen = {}
    class R:
        returncode = 7
    def fake_run(cmd, env=None):
        seen["cmd"] = cmd
        seen["db_port"] = env.get("TUNSTRAP_DB_PORT")
        return R()
    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    res = CliRunner().invoke(main, ["run", "u@h", "--target", "db=127.0.0.1:5432", "--", "echo", "hi"])
    assert res.exit_code == 7
    assert seen["cmd"] == ["echo", "hi"]
    assert seen["db_port"] == "5432"
    assert stops, "teardown must run"


def test_run_requires_command():
    res = CliRunner().invoke(main, ["run", "u@h", "--target", "db=192.0.2.1:1"])
    assert res.exit_code == 64


def test_run_teardown_on_child_exception(monkeypatch):
    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload())
    stops = []
    monkeypatch.setattr(cli_mod, "_teardown_run", lambda sd, gs: stops.append(sd))
    def boom(cmd, env=None):
        raise OSError("no such binary")
    monkeypatch.setattr(cli_mod.subprocess, "run", boom)
    res = CliRunner().invoke(main, ["run", "u@h", "--target", "db=192.0.2.1:1", "--", "nope"])
    assert res.exit_code != 0
    assert stops, "teardown must run even when child fails to launch"


def test_run_session_active_exit3(monkeypatch):
    monkeypatch.setattr(cli_mod, "spawn_daemon",
                        lambda schema, session_dir=None: {"kind": "session_active", "payload": {"error": "SessionActive"}})
    res = CliRunner().invoke(main, ["run", "u@h", "--target", "db=192.0.2.1:1", "--session-dir", "/x", "--", "echo"])
    assert res.exit_code == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_cli_run.py -v`
Expected: FAIL (`run` command missing).

- [ ] **Step 3: Implement `run` in `cli.py`**

Add `import subprocess` to the imports. Add the command + helper:

```python
@main.command("run", context_settings={"allow_interspersed_args": False})
@click.argument("connection", required=True)
@_connection_options
@click.option("--session-dir", "session_dir", default=None)
@click.option("--grace-seconds", "grace_seconds", type=int, default=10, show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run_command(connection, ssh_key, ssh_key_passphrase, ssh_password_stdin, targets, kube,
                fetch, auto_stop_idle_seconds, materialize, log_file, session_dir,
                grace_seconds, command):
    """Open a tunnel, run CMD with TUNSTRAP_*/KUBECONFIG injected, then tear down."""
    cmd = list(command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise click.UsageError("run requires a command: tunstrap run USER@HOST ... -- CMD [ARGS]")

    try:
        schema = _schema_from_flags(
            connection, ssh_key=ssh_key, ssh_key_passphrase=ssh_key_passphrase,
            ssh_password_stdin=ssh_password_stdin, targets=targets, kube=kube, fetch=fetch,
            auto_stop_idle_seconds=auto_stop_idle_seconds, materialize=materialize,
            log_file=log_file, force_materialize=True,
        )
        message = spawn_daemon(schema, session_dir=session_dir)
    except click.UsageError:
        raise
    except TunstrapError as exc:
        sys.stderr.write(json.dumps(exc.to_error_output()) + "\n")
        sys.exit(exit_code_for(exc))

    kind = message["kind"]
    if kind != "success":
        sys.stderr.write(json.dumps(message["payload"]) + "\n")
        sys.exit({"required_failure": 2, "session_active": 3, "daemon_error": 4}.get(kind, 4))

    out = OutputSchema.model_validate(message["payload"])
    child_env = {**os.environ, **render_env(out)}
    resolved_session_dir = out.session_dir

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    try:
        proc = subprocess.Popen(cmd, env=child_env)  # noqa: SIM115  # pylint: disable=consider-using-with
        def _forward(signum, _frame):
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass
        signal.signal(signal.SIGINT, _forward)
        signal.signal(signal.SIGTERM, _forward)
        returncode = proc.wait()
    except OSError as exc:
        sys.stderr.write(f"run: failed to launch command: {exc}\n")
        returncode = 127
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        _teardown_run(resolved_session_dir, grace_seconds)
    sys.exit(returncode)


def _teardown_run(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon for session_dir and remove its tunnel-data. Best-effort."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError:
        SessionDir.cleanup_path(session_dir)
        return
    _kill_with_identity(pid, grace_seconds, force=True, session_dir=session_dir)
    SessionDir.cleanup_path(session_dir)
```

Notes for the implementer:
- `allow_interspersed_args=False` + `nargs=-1 UNPROCESSED` lets Click collect everything after the connection/flags (including after `--`) into `command`; the leading `--` is stripped above.
- The unit test monkeypatches `subprocess.run`; the implementation uses `subprocess.Popen(...).wait()` for signal forwarding. Adjust the test to patch `cli_mod.subprocess.Popen` instead, OR keep a thin `subprocess.run` path. To keep the provided tests valid, implement the child call via `subprocess.run` when no signal forwarding is needed is NOT acceptable (we need forwarding). **Update the tests to patch `subprocess.Popen`** returning a fake object with `.wait()`/`.send_signal()`/`.returncode`. Reconcile tests and code so both use `Popen`.

- [ ] **Step 4: Reconcile + run tests**

Update `tests/unit/test_cli_run.py` to patch `cli_mod.subprocess.Popen` with a fake:

```python
class FakePopen:
    def __init__(self, returncode=0):
        self.returncode = returncode
    def wait(self):
        return self.returncode
    def send_signal(self, signum):
        pass
```

Run: `.venv/bin/pytest tests/unit/test_cli_run.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/cli.py tests/unit/test_cli_run.py
git commit -m "feat(cli): run wrapper with env injection + guaranteed teardown (#5)"
```

---

### Task C2: Integration tests + README + final gates

**Files:**
- Test: `tests/integration/test_cli_modes.py` (new)
- Modify: `README.md`

- [ ] **Step 1: Integration tests (Docker)**

Create `tests/integration/test_cli_modes.py` following the patterns in the existing `tests/integration/` files (reuse their docker fixtures/conftest for an SSH container + a forwardable service). Cover:
- `start USER@HOST --target NAME=... --output env` → parse `export` lines → connect to `TUNSTRAP_<NAME>_PORT` succeeds; then `stop --session-dir $TUNSTRAP_SESSION_DIR`.
- `run USER@HOST --target NAME=... -- sh -c 'nc -z 127.0.0.1 $TUNSTRAP_<NAME>_PORT'` → exits 0; afterwards the session dir is gone (teardown ran).
- `run USER@HOST --target ... -- sh -c 'exit 7'` → process exits 7.

Mark with the `integration` marker like the other files. Read an existing integration test first to match fixture names and host/credentials.

- [ ] **Step 2: README**

Add a section documenting: flag mode (`tunstrap start root@host --target api=127.0.0.1:6443`), `--output env` with `eval "$(...)"`, and `run` (`tunstrap run root@host --kube k3s=/etc/rancher/k3s/k3s.yaml -- helm list`). Note exit-code semantics (child code wins; 3=session active).

- [ ] **Step 3: Full gates**

Run:

```bash
.venv/bin/ruff check . && .venv/bin/mypy --strict tunstrap/ && .venv/bin/pylint tunstrap/
.venv/bin/vulture tunstrap/ vulture_whitelist.py
.venv/bin/pytest tests/unit -q
.venv/bin/pytest tests/integration -m integration -q
```

Expected: ruff/mypy clean; pylint no new regressions (baseline ~9.95, only pre-existing kube/manager warnings); vulture clean (add `_teardown_run`/new public funcs to `vulture_whitelist.py` only if vulture flags them as unused); unit + integration green.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_cli_modes.py README.md vulture_whitelist.py
git commit -m "test+docs: CLI run modes integration + README (#6/#5)"
```

---

## Self-Review

**Spec coverage:**
- Schema relaxation (kube-only) → Task B0. ✓
- `cli_input.py` (USER@HOST:PORT, NAME=VALUE, full-parity flags) → Task B1. ✓
- `envrender.py` (TUNSTRAP_* no NODE segment, KUBECONFIG colon-join, sanitization, collision) → Task B2. ✓
- `start` flag mode + `--output json|env` + force-materialize on env → Task B3. ✓
- Conflict validation (start+`--`; conn-flags need connection; connection XOR stdin) → Task B3. ✓ (run requires `--` → Task C1.)
- `run` (env inject, child, signal forwarding, finally teardown, exit-code propagation, session_active→3) → Task C1. ✓
- KUBECONFIG works for kubectl+helm; env consumed via eval → Task C2 integration + README. ✓

**Placeholder scan:** New-module code is complete. Integration tests (Task C2 Step 1) are specified as a contract against existing fixtures rather than transcribed, because they must reuse the repo's Docker fixtures (read an existing integration test first) — gated by the Step 3 green run. A known reconcile point is flagged in Task C1 Step 3/4: tests and code must agree on `subprocess.Popen` (not `run`); the plan instructs fixing both.

**Type consistency:** `parse_endpoint(str)->(str,str,int)`, `parse_named(tuple,str)->dict`, `build_single_node_schema(**kw)->InputSchema`, `render_env(OutputSchema)->dict[str,str]`, `format_exports(dict)->str`, `_schema_from_flags(...)->InputSchema`, `_teardown_run(str,int)->None`, `_kill_with_identity(pid, grace_seconds, *, force, session_dir)` (matches Task A signature). ✓
```
