# Tests

This directory contains three suites:

- `tests/unit/` — pure-Python unit tests. Marked with
  `pytestmark = pytest.mark.unit`. Run with `pytest tests/unit -q`.
- `tests/integration/` — Linux + Docker integration tests. Marked with
  `pytestmark = pytest.mark.integration`. Run with
  `PATH="$PWD/.venv/bin:$PATH" pytest tests/integration -m integration -q`.
- `tests/e2e/` — Linux + Docker + kind + OpenTofu end-to-end tests. Marked with
  `pytestmark = [pytest.mark.e2e]`. Run with
  `PATH="$PWD/.venv/bin:$PATH" pytest tests/e2e -m e2e -q`. Excluded from the
  default selection by `addopts` and from the coverage combine by design, so a
  cluster flake cannot take down the `--fail-under=80` gate.

## Conventions

- Every test file starts with a module docstring describing what behaviour
  the file covers.
- Every test function has a one-line docstring stating the assertion.
- Test names are imperative: `test_<subject>_does_<behaviour>`.
- Fakes live alongside the tests that need them (no shared mocks module).
- No real network IO in unit tests. Integration tests use dockerized
  `openssh-server` containers from `linuxserver/openssh-server`.

## Fixtures

Defined in `tests/integration/conftest.py`:

- `tunstrap_it_dir` — session-scoped; pre-creates
  `/tmp/tunstrap-it/` with mode `0o1777` so that a docker bind-mount
  cannot lock it to root.
- `ssh_keypair` — generates an ed25519 keypair into
  `tests/integration/_keys/`.
- `ssh_test_cluster` — runs `docker compose up -d --wait` and returns
  the per-service exposed ports.
- `prepared_files` — populates `/tmp/tunstrap-it/{kubeconfig,
  big.bin,no-perm.txt}` for `fetch_files` scenarios.
- `started_daemons` — collects `session_dir` strings from successful start
  invocations so the suite teardown can stop them by `--session-dir`.

Defined in `tests/e2e/conftest.py` (constants and helpers live in
`tests/e2e/rig.py`, which the tests import from — never from `conftest`):

- `e2e_preflight` — session-scoped; requires Linux and `tunstrap` on PATH. A
  missing `tunstrap` is a hard **failure**, not a skip: it means the venv is not
  on `PATH` and a skip would report a green tier that tested nothing.
- `e2e_ssh_keypair` — generates this suite's **own** ed25519 keypair into
  `tests/e2e/_keys/`. It does not share `tests/integration/_keys/`, which is
  gitignored and created only by a fixture pytest never loads here.
- `kind_cluster` — creates `tunstrap-e2e` from `kindest/node:v1.34.0`, deleting
  any stale cluster of that name first, and always deletes it on teardown.
- `node_kubeconfig` — copies the control plane's `/etc/kubernetes/admin.conf`
  into `tests/e2e/_kube/admin.conf` for the compose mount.
- `kube_rig` — brings `sshd-kube` up on kind's external `kind` network, waits
  for a real authenticated SSH exec (not a TCP connect), discovers the random
  published port, and returns the connection facts.
- `tofu_plugin_cache` — one shared provider download per session.
- `tofu_module` — a private copy of `tests/e2e/module/` per test, so no test can
  pass because of a neighbour's `.terraform/` or `terraform.tfstate`.

## Local prerequisites for integration

- Linux host (macOS works but is slower; CI uses ubuntu-latest).
- Docker Compose v2.
- Python 3.10+ with the project venv installed: `pip install -e ".[dev]"`.

## Local prerequisites for e2e

- Everything the integration suite needs, plus:
- `kind` (0.30.x). No host `kubectl`: the oracle runs the version-matched binary
  *inside* the node image via `docker exec` (`kubectl_in_node` in
  `tests/e2e/rig.py`), and `kind` itself shells out to no kubectl of its own.
- `tofu` (OpenTofu 1.12.x). Network access on first run: `tofu init` downloads
  the `kubernetes` and `helm` providers from `registry.opentofu.org` (~9s). That
  is the *runner's* network and has nothing to do with the tunnel.
- No `helm` binary. The Terraform `helm` provider links the Helm v3 Go SDK.
- Budget ~2.5-3 minutes for a full `pytest tests/e2e -m e2e` run, of which
  ~90s is cluster and container setup.
