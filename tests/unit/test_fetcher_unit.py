"""Fetcher behaviour against a fake SFTP client.

Validates: tunstrap/fetcher.py::fetch_files happy paths, EFBIG cap
handling, error classification, and resource cleanup.
Code: tunstrap/fetcher.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import asyncssh
import pytest

from tunstrap.fetcher import fetch_files
from tunstrap.schemas import FileSpec

pytestmark = pytest.mark.unit


class _FakeStat:
    def __init__(self, size: int | None) -> None:
        self.size = size


class _FakeOpenedFile:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeOpenedFile":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def read(self, n: int) -> bytes:
        return self._payload[:n]


class _FakeSFTP:
    def __init__(
        self,
        files: dict[str, bytes | BaseException],
        sizes: dict[str, int] | None = None,
    ) -> None:
        self._files = files
        self._sizes = sizes or {}
        self.opened_paths: list[str] = []
        self.closed = False

    async def stat(self, path: str) -> _FakeStat:
        entry = self._files.get(path)
        if isinstance(entry, BaseException):
            raise entry
        if entry is None:
            raise asyncssh.SFTPError(code=2, reason="no such file")
        size = self._sizes.get(path, len(entry))
        return _FakeStat(size)

    def open(self, path: str, mode: str) -> _FakeOpenedFile:
        assert mode == "rb"
        entry = self._files.get(path)
        if isinstance(entry, BaseException):
            raise entry
        if entry is None:
            raise asyncssh.SFTPError(code=2, reason="no such file")
        self.opened_paths.append(path)
        assert isinstance(entry, bytes)
        return _FakeOpenedFile(entry)

    async def __aenter__(self) -> "_FakeSFTP":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


class _FakeConn:
    def __init__(self, sftp: _FakeSFTP | BaseException) -> None:
        self._sftp = sftp

    def start_sftp_client(self) -> Any:
        if isinstance(self._sftp, BaseException):
            raise self._sftp
        return self._sftp


@pytest.mark.asyncio
async def test_empty_specs_short_circuits() -> None:
    """Empty fetch_files dict short-circuits without opening SFTP."""
    conn = _FakeConn(asyncssh.ChannelOpenError(2, "should not be touched"))
    results, failures = await fetch_files(conn, {}, timeout=60)  # type: ignore[arg-type]
    assert results == {}
    assert failures == []


@pytest.mark.asyncio
async def test_happy_path_single_file() -> None:
    """A single readable file returns content_b64, size, and sha256."""
    payload = b"hello"
    sftp = _FakeSFTP({"/k.yaml": payload})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"kubeconfig": FileSpec(path="/k.yaml")},
        timeout=60,
    )
    assert failures == []
    ff = results["kubeconfig"]
    assert ff.content_b64 == base64.b64encode(payload).decode("ascii")
    assert ff.size == 5
    assert ff.sha256 == hashlib.sha256(payload).hexdigest()
    assert sftp.closed is True


@pytest.mark.asyncio
async def test_two_files_both_ok() -> None:
    """Two readable files are both returned with correct sizes."""
    sftp = _FakeSFTP({"/a": b"AAA", "/b": b"BBBB"})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"alpha": FileSpec(path="/a"), "beta": FileSpec(path="/b")},
        timeout=60,
    )
    assert failures == []
    assert results["alpha"].size == 3
    assert results["beta"].size == 4


@pytest.mark.asyncio
async def test_enoent_required_adds_failure() -> None:
    """ENOENT on a required file appends the key to failures."""
    sftp = _FakeSFTP({})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"k": FileSpec(path="/missing", required=True)},
        timeout=60,
    )
    assert failures == ["k"]
    assert results["k"].error == "SSH_FX_NO_SUCH_FILE"


@pytest.mark.asyncio
async def test_enoent_optional_does_not_fail_node() -> None:
    """ENOENT on an optional file is recorded as soft error (no failures)."""
    sftp = _FakeSFTP({})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"k": FileSpec(path="/missing", required=False)},
        timeout=60,
    )
    assert failures == []
    assert results["k"].error == "SSH_FX_NO_SUCH_FILE"


@pytest.mark.asyncio
async def test_efbig_via_stat_skips_open() -> None:
    """Oversized file detected via stat is rejected without opening it."""
    sftp = _FakeSFTP({"/big": b"x"}, sizes={"/big": (1 << 20) + 1})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"k": FileSpec(path="/big", required=True)},
        timeout=60,
    )
    assert failures == ["k"]
    assert results["k"].error == "EFBIG"
    assert sftp.opened_paths == []


@pytest.mark.asyncio
async def test_efbig_via_read_overflow() -> None:
    """File that grows between stat and read overflows the read-time cap."""
    payload = b"x" * ((1 << 20) + 1)
    sftp = _FakeSFTP({"/grew": payload}, sizes={"/grew": 1024})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"k": FileSpec(path="/grew", required=True)},
        timeout=60,
    )
    assert failures == ["k"]
    assert results["k"].error == "EFBIG"


@pytest.mark.asyncio
async def test_channel_open_failure_marks_all_files() -> None:
    """If SFTP channel open fails, every file is reported with that error."""
    conn = _FakeConn(asyncssh.ChannelOpenError(2, "denied"))
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {
            "a": FileSpec(path="/a", required=True),
            "b": FileSpec(path="/b", required=False),
        },
        timeout=60,
    )
    assert results["a"].error == "ChannelOpenError"
    assert results["b"].error == "ChannelOpenError"
    assert failures == ["a"]


@pytest.mark.asyncio
async def test_permission_denied_classified() -> None:
    """SFTP permission denied is surfaced as SSH_FX_PERMISSION_DENIED."""
    err = asyncssh.SFTPError(code=3, reason="denied")
    sftp = _FakeSFTP({"/secret": err})
    conn = _FakeConn(sftp)
    results, failures = await fetch_files(
        conn,  # type: ignore[arg-type]
        {"k": FileSpec(path="/secret", required=True)},
        timeout=60,
    )
    assert results["k"].error == "SSH_FX_PERMISSION_DENIED"
    assert failures == ["k"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled_stage", ["stat", "open", "read"])
async def test_each_file_timeout_covers_all_sftp_stages(stalled_stage: str) -> None:
    """Each-file timeout covers every generic SFTP stage.

    Removing the wait_for around the per-file operation leaves this coroutine
    pending until pytest cancels it instead of returning the TimeoutError result.
    """
    never = asyncio.Event()

    class _PendingFile:
        async def __aenter__(self) -> "_PendingFile":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def read(self, _size: int) -> bytes:
            await never.wait()
            return b""

    class _PendingSFTP:
        async def stat(self, _path: str) -> _FakeStat:
            if stalled_stage == "stat":
                await never.wait()
            return _FakeStat(1)

        def open(self, _path: str, _mode: str) -> _PendingFile:
            if stalled_stage == "open":
                return _PendingOpen()
            return _PendingFile()

        async def __aenter__(self) -> "_PendingSFTP":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _PendingOpen(_PendingFile):
        async def __aenter__(self) -> "_PendingOpen":
            await never.wait()
            return self

    try:
        results, failures = await asyncio.wait_for(
            fetch_files(
                _FakeConn(_PendingSFTP()),  # type: ignore[arg-type]
                {"file": FileSpec(path="/remote")},
                timeout=0.01,
            ),
            timeout=0.1,
        )
    except TimeoutError:
        pytest.fail("fetch_files did not enforce its configured timeout")

    assert failures == ["file"]
    assert results["file"].error == "TimeoutError"
