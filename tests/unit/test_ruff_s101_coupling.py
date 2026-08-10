"""Guard the wiring of the S101 (``assert``) production ban in ruff.

``[tool.ruff.lint]`` opts into S101 via ``extend-select`` so production modules
can never reintroduce an ``assert``: the codebase documents twice
(``tunstrap/cli.py``, ``tunstrap/daemon.py``) that an ``assert`` raises
``AssertionError`` outside the ``TunstrapError`` handler -- so it escapes as a
traceback -- and that ``python -O`` erases the check altogether, leaving a bare
``TypeError``/``AttributeError`` in its place. The ban is relaxed for tests,
which are themselves built on ``assert``, via the ``tests/**/*.py``
per-file-ignores entry.

Nothing enforced the coupling: a comment above the per-file-ignores table says
S101 stays enabled for production, but comments do not enforce anything, and a
contributor appending ``"S101"`` to any production per-file-ignores entry would
silently punch a hole in the ban for that file. ``"tunstrap/kube.py" =
["SIM117"]`` is the obvious place someone might do it. A test is the only
enforcement that fails loudly.

Lives in the unit tier (not e2e) on purpose: the e2e job ``needs: unit``, so a
divergence fails the unit job early. Reads ``pyproject.toml`` as TEXT rather
than importing ``tomllib``: the CI matrix runs Python 3.10
(``.github/workflows/test.yml``: ``python-version: ["3.10", "3.11", "3.12",
"3.13"]``) and ``tomllib`` is 3.11+, so a ``tomllib``-based guard would error on
collection on the 3.10 leg -- exactly the leg it must run on. A plain-text scan
is the same approach ``tests/unit/test_ci_version_coupling.py`` takes and is
precise here because TOML rule codes are quoted strings (``"S101"``), so a
quoted-substring match cannot false-positive on a longer code such as
``"S1010"``. Tables are resolved sectionally (a ``[tool.ruff.lint]`` body ends
at the next ``[`` header) so the extend-select check is not confused by the
``per-file-ignores`` sub-table that follows it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PYPROJECT = REPO_ROOT / "pyproject.toml"

# A TOML table header: ``[tool.ruff.lint]`` -> group(1) = ``tool.ruff.lint``.
_TABLE_HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# extend-select = [...] inside the [tool.ruff.lint] body. Single-line array
# (black/ruff-format keep short arrays unwrapped); a multi-line array fails the
# match loudly so the guard is updated to the new shape rather than passing
# silently.
_EXTEND_SELECT = re.compile(r"^extend-select\s*=\s*\[([^\]]*)\]", re.MULTILINE)
# A per-file-ignores entry: "glob" = [ ... ]. group(2) = glob, group(3) = array
# body (single-line, same rationale as above).
_PF_ENTRY = re.compile(r'^"([^"]+)"\s*=\s*\[([^\]]*)\]')


def _table_body(text: str, table: str) -> str:
    """Return the body of a TOML ``[table]``, up to the next table header.

    pyproject.toml tables are ``[a.b]`` headers; a sub-table ``[a.b.c]`` ends
    the parent's body, so the [tool.ruff.lint] body excludes the
    [tool.ruff.lint.per-file-ignores] entries that follow it. Fails the test
    (not silently returns empty) if the table is absent.
    """
    lines = text.splitlines()
    in_table = False
    out: list[str] = []
    for line in lines:
        m = _TABLE_HEADER.match(line)
        if m:
            if in_table:
                break  # next header ends this table's body
            if m.group(1) == table:
                in_table = True
            continue
        if in_table:
            out.append(line)
    if not in_table:
        pytest.fail(f"could not find [{table}] table in {_PYPROJECT}")
    return "\n".join(out)


def test_s101_is_in_ruff_extend_select() -> None:
    """S101 (the assert rule) is explicitly opted into via extend-select.

    ``extend-select`` (not ``select``) is used so the rest of the bandit S-group
    stays off and only the production assert ban is pulled in. Pinning
    ``"S101"`` here pins the ban's on-switch: removing it would silently
    re-allow ``assert`` across every production module.

    Fails-when-broken verbatim red recorded in the task report: removed
    ``"S101"`` from ``extend-select = ["S101"]`` (leaving ``extend-select =
    []``); this test failed with the missing-from-extend-select message below.
    """
    body = _table_body(_PYPROJECT.read_text(), "tool.ruff.lint")
    m = _EXTEND_SELECT.search(body)
    assert m is not None, (
        f"could not parse extend-select = [...] from [tool.ruff.lint] in "
        f"{_PYPROJECT}; the key has been renamed, removed, or the array wrapped "
        f"across lines. Update this guard to the new shape."
    )
    assert '"S101"' in m.group(1), (
        f'"S101" is missing from extend-select in {_PYPROJECT}. The production '
        f"assert ban depends on S101 being opted in here; without it ruff "
        f"silently accepts `assert` in production modules."
    )


def test_no_production_per_file_ignore_lists_s101() -> None:
    """No per-file-ignores entry other than tests/** may relax S101.

    The ``tests/**`` entry is the sanctioned escape hatch: the test suite is
    built on ``assert``. Every other glob in
    ``[tool.ruff.lint.per-file-ignores]`` targets production code, and any of
    them silently appending ``"S101"`` would punch a hole in the ban for that
    file -- the exact regression this guard exists to catch.
    ``"tunstrap/kube.py" = ["SIM117"]`` is the obvious place someone might do
    it.

    Fails-when-broken verbatim red recorded in the task report: appended
    ``"S101"`` to ``"tunstrap/kube.py" = ["SIM117"]`` (making it
    ``["SIM117", "S101"]``); this test failed naming ``tunstrap/kube.py`` as
    the offender.
    """
    body = _table_body(_PYPROJECT.read_text(), "tool.ruff.lint.per-file-ignores")
    offenders: list[str] = []
    for line in body.splitlines():
        m = _PF_ENTRY.match(line.strip())
        if m is None:
            continue
        glob = m.group(1)
        array = m.group(2)
        if glob.startswith("tests"):
            continue  # sanctioned escape hatch for the assert-based test suite
        if '"S101"' in array:
            offenders.append(f'"{glob}" = [{array}]')
    assert not offenders, (
        f"production per-file-ignores entries in {_PYPROJECT} relax S101 (the "
        f"assert ban): {offenders}. The S101 ignore belongs only under the "
        f"tests/** entry; remove it from the production glob(s) above."
    )
