"""``tunstrap/__init__.py`` lazy ``__version__`` resolution.

Covers the two branches the lazy PEP 562 ``__getattr__`` adds and the prior
``test_version_flag`` could not reach: the ``PackageNotFoundError`` fallback
(when the distribution metadata is absent) and the ``AttributeError`` for any
other attribute name. These pay no cost at import time — that is guarded
separately by ``test_tofu_proxy.test_importing_proxy_does_not_pull_in_cli_or_heavy_deps``.
Code: tunstrap/__init__.py
"""

from __future__ import annotations

import importlib.metadata

import pytest

import tunstrap

pytestmark = pytest.mark.unit


def test_version_falls_back_when_distribution_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing distribution yields ``0.0.0+unknown``, not a traceback.

    Reaches the ``PackageNotFoundError`` branch that the lazy getter handles:
    a source checkout with no installed distribution metadata must still import
    and report a placeholder. Without the branch, ``tunstrap.__version__`` would
    propagate the ``PackageNotFoundError``.
    """

    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("tunstrap")

    # The getter does ``from importlib.metadata import version`` on each access,
    # so patching the attribute on the module is picked up. Module __dict__ may
    # already hold a cached ``__version__`` from a prior access; drop it so the
    # getter re-runs.
    monkeypatch.setattr(importlib.metadata, "version", _raise)
    monkeypatch.delitem(tunstrap.__dict__, "__version__", raising=False)
    assert tunstrap.__version__ == "0.0.0+unknown"


def test_unknown_attribute_raises_attribute_error() -> None:
    """Any name other than ``__version__`` is rejected, not silently faked.

    PEP 562 ``__getattr__`` is only consulted for missing attributes; the getter
    must raise ``AttributeError`` for everything it does not provide, so a typo
    surfaces normally instead of returning ``None`` or the version.
    """
    with pytest.raises(AttributeError, match="no attribute"):
        _ = tunstrap.does_not_exist  # type: ignore[attr-defined]
