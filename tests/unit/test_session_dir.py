"""Session-directory resolution, tunnel-data creation, and cleanup.

Validates: a generated dir is removed wholesale; a supplied dir keeps
only tunnel-data removed; tunnel-data is 0700; an existing/symlinked
tunnel-data is rejected.
Code: tunstrap/session.py
Assertion: directory existence/mode after open/cleanup matches the rules;
SessionError is raised on a hostile tunnel-data.
Method: drive SessionDir against tmp_path with crafted preconditions.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path

import pytest

from tunstrap.session import SessionDir, SessionError, SessionIdentityUnreadable

pytestmark = pytest.mark.unit


def test_generated_dir_cleanup_removes_whole_dir(tmp_path: Path) -> None:
    """A daemon-generated session dir is removed entirely on cleanup."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    root = Path(sd.session_dir)
    assert (root / "tunnel-data").is_dir()
    sd.cleanup()
    assert not root.exists()


def test_supplied_dir_cleanup_keeps_dir(tmp_path: Path) -> None:
    """A supplied session dir keeps the dir; only tunnel-data is removed."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    assert (supplied / "tunnel-data").is_dir()
    sd.cleanup()
    assert supplied.exists()
    assert not (supplied / "tunnel-data").exists()
    # Tightening is the fix: a bare mkdir is 0o775 under umask 0o002, and create
    # must clear the group/other write bits rather than refuse the dir.
    assert stat.S_IMODE(supplied.stat().st_mode) & (stat.S_IWGRP | stat.S_IWOTH) == 0


def test_tunnel_data_is_0700(tmp_path: Path) -> None:
    """tunnel-data is created with mode 0700."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    mode = stat.S_IMODE(os.stat(Path(sd.session_dir) / "tunnel-data").st_mode)
    assert mode == 0o700


def test_reclaims_existing_tunnel_data(tmp_path: Path) -> None:
    """A pre-existing owned tunnel-data (orphan) is wiped and recreated fresh.

    With the single session.lock held exclusively, any leftover tunnel-data
    belongs to a dead session and is safe to reclaim.
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    data = supplied / "tunnel-data"
    data.mkdir()
    (data / "leftover").write_text("stale\n")
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    assert (supplied / "tunnel-data").is_dir()
    assert not (supplied / "tunnel-data" / "leftover").exists()
    sd.cleanup()


def test_rejects_symlink_tunnel_data(tmp_path: Path) -> None:
    """A symlinked tunnel-data is rejected (no symlink-following).

    The bare ``mkdir()`` is deliberate: under umask 0o002 it yields 0o775, which
    create tightens to 0o755 and then proceeds to ``_reclaim_data_slot`` -- so
    the ``match`` proves this trips the tunnel-data guard, not the root guard
    (a previous cycle shipped it raising "group- or world-writable" here while
    staying green).
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (supplied / "tunnel-data").symlink_to(target)
    with pytest.raises(SessionError, match="tunnel-data is a symlink"):
        SessionDir.create(supplied=str(supplied), base=tmp_path)


def test_write_identity_and_materialize(tmp_path: Path) -> None:
    """Identity files and a materialized file land in tunnel-data, mode 0600."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    sd.write_identity(pid=4321)
    path = sd.materialize("hub-k3s", b"kubeconfig-bytes")
    data_dir = Path(sd.session_dir) / "tunnel-data"
    assert (data_dir / "daemon.pid").read_text().strip() == "4321"
    assert Path(path).read_bytes() == b"kubeconfig-bytes"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_file_rejects_traversal_name(tmp_path: Path) -> None:
    """materialize() with a traversal name is rejected (defense in depth)."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    with pytest.raises(SessionError, match="unsafe materialized file name"):
        sd.materialize("../escaped", b"x")


def test_write_file_rejects_slash_name(tmp_path: Path) -> None:
    """materialize() with a nested path is rejected."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    with pytest.raises(SessionError, match="unsafe materialized file name"):
        sd.materialize("sub/dir", b"x")


def test_rejects_relative_supplied_dir(tmp_path: Path) -> None:
    """A relative --session-dir is rejected before resolution."""
    with pytest.raises(SessionError):
        SessionDir.create(supplied="relative-session", base=tmp_path)


def test_accepts_absolute_supplied_dir(tmp_path: Path) -> None:
    """An absolute --session-dir is accepted (regression guard)."""
    abs_dir = tmp_path / "work"
    sd = SessionDir.create(supplied=str(abs_dir), base=tmp_path)
    assert Path(sd.session_dir) == abs_dir.resolve()


def test_cleanup_path_returns_empty_on_success(tmp_path: Path) -> None:
    """A successful tunnel-data removal reports no survivors."""
    data = tmp_path / "tunnel-data"
    data.mkdir()
    (data / "daemon.pid").write_text("1\n")
    assert SessionDir.cleanup_path(str(tmp_path)) == []
    assert not data.exists()


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory write permission")
def test_cleanup_path_reports_survivor_when_removal_fails(tmp_path: Path) -> None:
    """An unremovable tunnel-data is reported, not silently swallowed."""
    data = tmp_path / "tunnel-data"
    data.mkdir()
    (data / "stuck").write_text("x")
    # Drop write permission on the parent so the child entry cannot be unlinked.
    os.chmod(data, 0o500)
    try:
        survivors = SessionDir.cleanup_path(str(tmp_path))
    finally:
        os.chmod(data, 0o700)
    assert survivors == [str(data.resolve())]
    assert data.exists()


def test_cleanup_path_missing_dir_is_not_a_failure(tmp_path: Path) -> None:
    """A tunnel-data that was never created reports no survivors."""
    assert SessionDir.cleanup_path(str(tmp_path)) == []


def test_remove_root_removes_everything(tmp_path: Path) -> None:
    """remove_root deletes the whole minted root, not just tunnel-data."""
    root = tmp_path / "minted"
    (root / "tunnel-data").mkdir(parents=True)
    (root / "session.lock").write_text("1\n")
    assert SessionDir.remove_root(str(root)) == []
    assert not root.exists()


def test_remove_root_missing_is_not_a_failure(tmp_path: Path) -> None:
    """remove_root on an already-gone root reports no survivors and never raises."""
    assert SessionDir.remove_root(str(tmp_path / "gone")) == []


def test_remove_root_resolves_its_argument(tmp_path: Path) -> None:
    """remove_root normalises the path like cleanup_path and read_identity do.

    Reached through a symlink, an unresolved ``rmtree`` refuses to descend
    (a symlink is not a directory), the error is swallowed by
    ``ignore_errors=True``, and the follow-through ``stat()`` then finds the
    target alive and reports the root as an unremovable survivor -- so ``run``
    prints "could not remove session root" for a root it never actually tried
    to delete.

    The only caller passes a ``tempfile.mkdtemp`` path, which is always a real
    directory, so this is a consistency fix rather than a live bug; the test
    exists because a symlink is the one input that can tell the two
    implementations apart.

    Fails with ``[<symlink>]`` and a surviving target if ``.resolve()`` is
    removed.
    """
    real = tmp_path / "real-root"
    real.mkdir()
    (real / "tunnel-data").mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)

    assert SessionDir.remove_root(str(link)) == []
    assert not real.exists(), "the resolved root was not removed"


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory execute permission")
def test_rmtree_reporting_reports_unstatable_survivor(tmp_path: Path) -> None:
    """An unstatable leaf is reported rather than letting exists() raise."""
    middle = tmp_path / "parent" / "middle"
    leaf = middle / "leaf"
    leaf.mkdir(parents=True)
    os.chmod(middle, 0o600)
    try:
        survivors = SessionDir._rmtree_reporting(leaf)
    finally:
        os.chmod(middle, 0o700)
    assert survivors == [str(leaf)]
    assert leaf.exists()


def test_missing_identity_is_not_reported_as_unreadable(tmp_path: Path) -> None:
    """A file that was never written means nothing was ever recorded.

    This is the only one of the three ``read_identity`` failures that lets a
    caller conclude no daemon is running, so it must stay distinguishable from
    the other two.
    """
    (tmp_path / "tunnel-data").mkdir()

    with pytest.raises(SessionError) as caught:
        SessionDir.read_identity(str(tmp_path))

    blocks_cleanup = "a missing identity was reported as unreadable, which blocks cleanup forever"
    assert not isinstance(caught.value, SessionIdentityUnreadable), blocks_cleanup


def test_unreadable_identity_is_distinguishable_from_a_missing_one(tmp_path: Path) -> None:
    """An identity we cannot read at all leaves the daemon's state unknown.

    Uses a directory where the file belongs, so the ``OSError`` is
    ``IsADirectoryError`` — deterministic for any uid, unlike a chmod-based
    setup which a root test runner would sail straight through.
    """
    (tmp_path / "tunnel-data").mkdir()
    (tmp_path / "tunnel-data" / "daemon.pid").mkdir()

    with pytest.raises(SessionIdentityUnreadable):
        SessionDir.read_identity(str(tmp_path))


def test_malformed_identity_is_distinguishable_from_a_missing_one(tmp_path: Path) -> None:
    """A daemon recorded *something*; we just cannot turn it into a pid.

    A truncated write is the realistic shape, and it says the opposite of
    "nothing is running": a daemon got far enough to open the file.
    """
    (tmp_path / "tunnel-data").mkdir()
    (tmp_path / "tunnel-data" / "daemon.pid").write_text("not-a-pid\n")

    with pytest.raises(SessionIdentityUnreadable):
        SessionDir.read_identity(str(tmp_path))


@pytest.mark.parametrize(
    "body",
    ["0\n", "-1\n", "  -7 \n"],
    ids=["zero", "minus-one", "negative-with-whitespace"],
)
def test_non_positive_identity_is_unreadable(tmp_path: Path, body: str) -> None:
    """A non-positive pid is corrupt state, not a stop target.

    Under ``kill(2)`` a pid of 0 means the caller's own process group and a
    negative pid a process group — with ``-1`` meaning *every* process the
    caller can signal, a broadcast rather than a single group — so handing such
    a value to ``os.kill`` widens a signal far beyond the recorded daemon, the
    exact hazard ``_has_exited`` already guards ``waitpid`` against (where the
    same encodings select a child group, or for ``-1`` any child).
    ``read_identity`` is the gate that keeps a corrupt ``daemon.pid`` (or a
    hostile ``--session-dir`` whose body is ``-1``) off the kill path entirely,
    so 0 and negatives are ``SessionIdentityUnreadable``: the corrupt-state
    answer that makes both ``run`` and ``stop`` preserve rather than signal or
    clean up.

    Whitespace is part of the case because the reader does ``int(raw.strip())``,
    which would otherwise turn ``"  -7 \\n"`` into a perfectly valid, perfectly
    dangerous ``-7``.
    """
    (tmp_path / "tunnel-data").mkdir()
    (tmp_path / "tunnel-data" / "daemon.pid").write_text(body)

    with pytest.raises(SessionIdentityUnreadable):
        SessionDir.read_identity(str(tmp_path))


# --- issue #25: caller-supplied root validation (mirror _reclaim_data_slot) ---


def test_create_clears_group_write_on_supplied_root(tmp_path: Path) -> None:
    """A group-writable supplied root is tightened, not refused.

    Directory write is authority over *entries* (unlink/rename a planted
    ``session.lock`` or ``tunnel-data``), which no fd-level inode check reaches;
    the root guard is therefore not redundant given ``O_NOFOLLOW`` + ``fstat``.
    But refusal is the wrong enforcement -- the mode cannot tell a user-private
    group from a shared one -- so create clears the write bit instead. The final
    mode is exactly 0o755: only the write bits are cleared, read/exec preserved.
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    supplied.chmod(0o775)
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    sd.cleanup()
    assert stat.S_IMODE(supplied.stat().st_mode) == 0o755


def test_create_clears_world_write_on_supplied_root(tmp_path: Path) -> None:
    """A world-writable supplied root is tightened to 0o755, not refused."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    supplied.chmod(0o777)
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    sd.cleanup()
    assert stat.S_IMODE(supplied.stat().st_mode) == 0o755


def test_create_refuses_foreign_owned_supplied_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supplied root owned by another uid is refused.

    Mirrors ``_reclaim_data_slot``'s ownership check. The root is created 0o700
    so the write-bit guard does NOT fire, and only its *reported* owner is forged
    foreign -- that is what independently pins the ownership guard: remove it and
    ``SessionDir.create`` sails through to success (a ``getuid``-patch would
    instead be caught by ``acquire_session_lock``'s own uid check and mask the
    missing root guard, which is the false-pass shape a prior cycle shipped).

    Stand-in: an unprivileged runner cannot ``chown`` to another uid, so the
    foreign owner is reported by patching ``os.fstat`` for the root's inode --
    mechanism-stable now that the guard is fd-based (``os.fstat`` rather than
    ``Path.stat``).
    """
    supplied = tmp_path / "work"
    supplied.mkdir(mode=0o700)
    real_fstat = os.fstat
    root_ino = supplied.stat().st_ino
    foreign_uid = os.getuid() + 1

    def fake_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if st.st_ino == root_ino:
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    foreign_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(SessionError, match="not owned by the current user"):
        SessionDir.create(supplied=str(supplied), base=tmp_path)


def test_create_accepts_0755_supplied_root(tmp_path: Path) -> None:
    """A 0755 root (group read+exec, no write) is accepted unchanged at 0o755.

    Regression guard on two sides: several existing tests mint ``tmp_path/"work"``
    under a 022 umask, which yields exactly 0755, so checking anything beyond the
    write bits would break that legitimate shape; and only the write bits are
    cleared, so an implementation that force-chmods to 0700 fails the equality.
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    supplied.chmod(0o755)
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    sd.cleanup()
    assert supplied.exists()
    assert stat.S_IMODE(supplied.stat().st_mode) == 0o755


def test_create_accepts_0700_supplied_root(tmp_path: Path) -> None:
    """A 0700 supplied root is accepted (regression guard)."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    supplied.chmod(0o700)
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    sd.cleanup()
    assert supplied.exists()


def test_create_fresh_root_under_zero_umask_does_not_self_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly created supplied root must not fail its own write-bits check.

    Under umask 0 the default ``mkdir`` mode (0777) would produce a group- and
    world-writable root that validation would then refuse -- a self-inflicted
    denial of service. ``SessionDir.create`` therefore creates the root with an
    explicit ``0o700`` so a directory it just minted can never carry group/other
    write bits regardless of the inherited umask. The fix's own constraint: a
    freshly created root must not be able to fail its own validation.
    """
    fresh = tmp_path / "fresh-root"
    assert not fresh.exists()
    old_umask = os.umask(0)
    try:
        SessionDir.create(supplied=str(fresh), base=tmp_path).cleanup()
    finally:
        os.umask(old_umask)
    assert fresh.exists()
    mode = stat.S_IMODE(fresh.stat().st_mode)
    assert mode & (stat.S_IWGRP | stat.S_IWOTH) == 0, oct(mode)


def test_create_refuses_symlink_lock_and_preserves_victim(tmp_path: Path) -> None:
    """End-to-end: a symlinked session.lock surfaces as SessionError, victim intact.

    Drives the real entry point (``SessionDir.create``, as ``_worker.main``
    calls it) so the OSError ``acquire_session_lock`` raises is shown to be
    translated to the domain ``SessionError`` every other session refusal uses,
    and the victim file -- the actual security property -- survives unchanged.
    """
    victim = tmp_path / "victim"
    payload = b"do-not-truncate-this-file\n"
    victim.write_bytes(payload)
    work = tmp_path / "work"
    work.mkdir()
    (work / "session.lock").symlink_to(victim)
    with pytest.raises(SessionError, match="cannot acquire session lock"):
        SessionDir.create(supplied=str(work), base=tmp_path)
    assert victim.read_bytes() == payload


def test_create_refuses_root_that_cannot_be_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root whose write bits cannot be cleared is refused, not accepted.

    Two failure modes that both must surface as ``could not be tightened``:

    (a) ``fchmod`` itself raises (a read-only or ACL-locked filesystem). The
    write bits must still be set afterwards -- a partial in-place change would
    be a quiet security regression.

    (b) ``fchmod`` returns success but the bits survive (an ACL mask or exotic
    filesystem that silently no-ops the call). This pins the re-stat: an
    implementation that drops it would see the no-op ``fchmod`` succeed and
    accept the still-writable root, shipping a guard that tightens nothing.
    """
    # --- (a) fchmod raises; bits unchanged afterwards -----------------------
    supplied = tmp_path / "raise-root"
    supplied.mkdir()
    supplied.chmod(0o775)
    root_ino = supplied.stat().st_ino
    real_fchmod = os.fchmod

    def raising_fchmod(fd: int, mode: int) -> None:
        if os.fstat(fd).st_ino == root_ino:
            raise PermissionError(errno.EPERM, "simulated fchmod refusal")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", raising_fchmod)
    with pytest.raises(SessionError, match="could not be tightened"):
        SessionDir.create(supplied=str(supplied), base=tmp_path)
    # Write bits must still be set: a partial in-place change would be a quiet
    # security regression.
    leftover = stat.S_IMODE(supplied.stat().st_mode)
    assert leftover & (stat.S_IWGRP | stat.S_IWOTH)
    monkeypatch.undo()  # restore os.fchmod before variant (b) reuses the tree

    # --- (b) fchmod no-ops; forged re-fstat still reports the write bits -----
    supplied_b = tmp_path / "silent-root"
    supplied_b.mkdir()
    supplied_b.chmod(0o775)
    root_ino_b = supplied_b.stat().st_ino
    real_fstat = os.fstat

    def lying_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if st.st_ino == root_ino_b:
            # Always report the group/other write bits as set, even after the
            # real fchmod has cleared them on disk.
            forced = st.st_mode | stat.S_IWGRP | stat.S_IWOTH
            return os.stat_result(
                (
                    forced,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "fstat", lying_fstat)
    with pytest.raises(SessionError, match="could not be tightened"):
        SessionDir.create(supplied=str(supplied_b), base=tmp_path)


def test_validated_path_rejects_symlinked_tunnel_data(tmp_path: Path) -> None:
    """Materialization refuses a ``tunnel-data`` swapped for a symlink.

    ``_validated_path``'s ``path.resolve().parent != self._data.resolve()``
    check is a no-op when ``tunnel-data`` itself is the symlink (both sides
    resolve through it), so the explicit ``is_symlink`` guard is what actually
    keeps a patched kubeconfig out of attacker-controlled space. The property
    under test is not just that an exception is raised but that nothing is
    written into the symlink target -- a refusal that still wrote the file would
    be no fix at all.
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    data = Path(sd.session_dir) / "tunnel-data"
    assert data.is_dir()
    target = tmp_path / "attacker-sink"
    target.mkdir()
    shutil.rmtree(data)
    data.symlink_to(target)

    with pytest.raises(SessionError, match="tunnel-data is a symlink"):
        sd.materialize("hub-k3s", b"patched-kubeconfig-with-client_key_data")

    # The sink must still be empty: the patched kubeconfig never landed.
    assert list(target.iterdir()) == [], "materialized bytes reached the symlink target"
    sd.cleanup()
