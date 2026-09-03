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
`tests/e2e/rig.py`, the intended import surface; `conftest.py` holds fixtures.
A few white-box checks in `test_rig.py` import `conftest` directly to exercise
fixture internals, but the rest read from `rig`):

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
- `kind` (0.30.x) and `kubectl` (matched to `kindest/node:v1.34.0`). The tier
  uses two kubectls for two different jobs, which is the source of confusion
  here: the **in-node oracle** (`kubectl_in_node` in `tests/e2e/rig.py`) runs the
  node image's own binary via `docker exec` as a tunnel-*independent* read-back
  of cluster state, and needs no host copy; the **host** `kubectl` backs the
  read-*through*-the-tunnel assertion in `test_rig.py`
  (`kubectl --kubeconfig <forwarded path> get nodes`), the flagship gate that
  proves real kube API traffic crosses the tunnel — which the in-node oracle
  cannot substitute for, since it never traverses the tunnel.
- `tofu` (OpenTofu 1.12.x). Network access on first run: `tofu init` downloads
  the `kubernetes` and `helm` providers from `registry.opentofu.org` (~9s). That
  is the *runner's* network and has nothing to do with the tunnel.
- `terragrunt` (1.1.x). Required only by `test_recipe_terragrunt.py`, which
  drives real `terragrunt hcl validate` / `terragrunt render` against the HCL
  fenced blocks extracted straight out of `docs/recipe_terragrunt.md` — so the
  recipe's published configuration can no longer drift into something that does
  not parse (it once shipped with `terraform_binary` misplaced inside the
  `terraform {}` block). Required where it is used, not tier-wide, matching the
  host-`kubectl` precedent in `tests/e2e/test_rig.py`.
- No `helm` binary. The Terraform `helm` provider links the Helm v3 Go SDK.
- Budget ~2.5-3 minutes for a full `pytest tests/e2e -m e2e` run once the
  `kindest/node` image is local, of which ~90s is cluster and container setup.
  A first run additionally pulls `kindest/node:v1.34.0` (~1.45 GB).
- The documented local command does **not** set `TUNSTRAP_E2E_REQUIRE_ALL`. The
  e2e CI job sets it to `1`, which turns every "tool missing" skip in
  `tests/e2e/rig.py::skip_or_fail` into a **failure** — CI installs every tool
  itself, so a skip there means the job reports green while most of the tier
  never ran. Locally a missing `kind`/`tofu`/`kubectl` is therefore a *skip*,
  not a failure: a green local run with skips is not full coverage. To mirror
  CI, `export TUNSTRAP_E2E_REQUIRE_ALL=1` before running.

## Compose isolation and e2e parallelism

Integration and e2e Compose commands use a deterministic, checkout-specific
project name from `tests/compose.py::compose_project_name`; set
`COMPOSE_PROJECT_NAME` to pin one manually. This isolates Compose stacks, but
the e2e kind cluster below remains shared-host-only.

## e2e is not parallel-safe (documented deviation)

The e2e tier is a single, session-scoped fixture chain built on fixed,
non-randomised names, and cannot have two independent runs on the same host
at the same time:

- one session-scoped kind cluster under a fixed name (`CLUSTER_NAME =
  "tunstrap-e2e"` in `rig.py`) and a fixed control-plane container name
  derived from it (`CONTROL_PLANE`, `conftest.py`);
- a fixed, repo-relative `tests/e2e/_keys/` (SSH keypair) and
  `tests/e2e/_kube/` (kubeconfig) directory, not per-run temp paths;
- an **unconditional** `kind delete cluster --name tunstrap-e2e` at
  `kind_cluster` fixture setup (`conftest.py`), which absorbs a leaked
  cluster from a killed prior run but would just as happily delete a
  concurrent run's live cluster out from under it.

This is a deliberate trade-off, not an oversight: the cluster name is fixed
because it doubles as the SSH forward target and the expected TLS
`tls_server_name` (see the comment beside `CLUSTER_NAME` in `rig.py`), and
per-run randomisation would ripple through every fixture that derives from
it. Re-architecting the rig for concurrent runs is out of scope for the
current tier.

**Practical consequence:** run at most one `pytest tests/e2e` invocation per
host at a time (this applies locally and in CI - the e2e job is not
configured for matrix/parallel execution). Do not add `-n auto`/`pytest-xdist`
or a parallel CI matrix leg for this suite without first giving `kind_cluster`,
`_keys/`, and `_kube/` per-run identity.
