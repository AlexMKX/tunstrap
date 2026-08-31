"""Worker-only argument handling for parent-minted session roots."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from tunstrap import _worker as worker_mod

pytestmark = pytest.mark.unit


def test_worker_marks_parent_minted_session_root_as_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal ownership flag reaches ``SessionDir.create`` unchanged."""
    captured: dict[str, object] = {}

    class FakeSession:
        """Minimal session accepted by the worker entry point."""

        def write_identity(self, *, pid: int) -> None:
            captured["pid"] = pid

    def fake_create(*, supplied: str | None, owns_supplied_root: bool = False) -> FakeSession:
        captured["supplied"] = supplied
        captured["owns_supplied_root"] = owns_supplied_root
        return FakeSession()

    def fake_run(coro: Coroutine[Any, Any, int]) -> int:
        coro.close()
        return 0

    def fake_exit(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(worker_mod.SessionDir, "create", fake_create)
    monkeypatch.setattr(worker_mod.asyncio, "run", fake_run)
    monkeypatch.setattr(worker_mod.os, "_exit", fake_exit)

    with pytest.raises(SystemExit) as caught:
        worker_mod.main(
            [
                "--ipc-fd=9",
                "--session-dir=/tmp/tunstrap-parent-minted",
                "--generated-session-dir",
            ]
        )

    assert caught.value.code == 0
    assert captured["supplied"] == "/tmp/tunstrap-parent-minted"
    assert captured["owns_supplied_root"] is True


def test_generated_flag_without_a_path_is_refused() -> None:
    """Claiming ownership of a root nobody named would delete a generated dir.

    ``owns_supplied_root`` only reaches ``SessionDir.create`` on the ``supplied
    is not None`` branch, so the flag alone is silently inert — and a silently
    inert ownership flag is how a generated root stops being cleaned up.
    """
    with pytest.raises(SystemExit) as caught:
        worker_mod._parse_args(  # pylint: disable=protected-access
            ["--ipc-fd=9", "--generated-session-dir"]
        )

    assert caught.value.code == 2
