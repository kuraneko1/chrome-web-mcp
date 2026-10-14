"""Lock and orphan-sweep behavior tests for the chrome-web server.

Verifies the foolproofing added so multiple Hermes sessions can no longer wedge
each other over a shared Chrome profile:
  - a second instance contending for the SAME profile gets a diagnostic error
    naming the live holder (not a bare "owns this profile");
  - a holder that dies (fd released by the kernel) lets the next acquirer
    through after the short retry window;
  - the startup orphan sweep removes per-pid profile dirs whose owner pid is
    dead and keeps ones whose owner is alive.
Run: .venv/bin/pytest tests/test_locking.py -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import fcntl
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = ROOT / "src" / "chrome_web_mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("chrome_web_server", SERVER_PY)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

BrowserRuntime = server.BrowserRuntime


def test_default_profile_is_persistent_and_ephemeral_is_opt_in():
    """The default profile is a warm shared one; CW_PROFILE_MODE=ephemeral opts out."""
    saved = {k: os.environ.pop(k) for k in ("CW_PROFILE_DIR", "CW_LOCK_PATH") if k in os.environ}
    saved_mode = os.environ.pop("CW_PROFILE_MODE", None)

    def fresh_server():
        spec = importlib.util.spec_from_file_location(
            "chrome_web_server_fresh", SERVER_PY
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    try:
        fresh = fresh_server()
        assert fresh._PROFILE_EPHEMERAL is False
        assert str(fresh.PROFILE_DIR).endswith("chrome-web-mcp/profile")
        assert str(os.getpid()) not in str(fresh.PROFILE_DIR)
        assert fresh.LOCK_PATH == fresh.PROFILE_DIR / ".instance.lock"

        os.environ["CW_PROFILE_MODE"] = "ephemeral"
        fresh = fresh_server()
        assert fresh._PROFILE_EPHEMERAL is True
        assert str(os.getpid()) in str(fresh.PROFILE_DIR)
        assert fresh.LOCK_PATH == fresh.PROFILE_DIR / ".instance.lock"
    finally:
        os.environ.update(saved)
        os.environ.pop("CW_PROFILE_MODE", None)
        if saved_mode is not None:
            os.environ["CW_PROFILE_MODE"] = saved_mode


def test_explicit_profile_dir_wins_over_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_PROFILE_MODE", "ephemeral")
    monkeypatch.setattr(server, "_PROFILE_EPHEMERAL", False)
    server._init_profile()
    try:
        assert server.PROFILE_DIR == Path(os.environ["CW_PROFILE_DIR"]).expanduser()
        assert server._PROFILE_EPHEMERAL is False
    finally:
        server._init_profile()


def test_busy_shared_profile_falls_back_to_ephemeral(monkeypatch, tmp_path):
    """A live sibling holds the shared profile: this process must not fail."""
    monkeypatch.setattr(server, "_activate_profile", server._activate_profile)
    monkeypatch.setattr(server, "_PROFILE_EPHEMERAL", False)
    monkeypatch.setattr(server, "_PROFILE_FELL_BACK", False)
    monkeypatch.delenv("CW_PROFILE_DIR", raising=False)
    monkeypatch.delenv("CW_LOCK_PATH", raising=False)
    shared = tmp_path / "shared"
    server._activate_profile(shared, ephemeral=False)

    holder = (shared / ".instance.lock")
    holder.parent.mkdir(parents=True, exist_ok=True)
    held = holder.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert server._can_fall_back_to_ephemeral() is True
        server._fall_back_to_ephemeral_profile()
        assert server._PROFILE_FELL_BACK is True
        assert server._PROFILE_EPHEMERAL is True
        assert str(os.getpid()) in str(server.PROFILE_DIR)
    finally:
        held.close()
        server._init_profile()
        setattr(server, "_PROFILE_FELL_BACK", False)


def test_no_fallback_when_profile_dir_is_explicit(monkeypatch, tmp_path):
    """An explicit CW_PROFILE_DIR/CW_LOCK_PATH is never silently swapped out."""
    monkeypatch.setenv("CW_PROFILE_DIR", str(tmp_path / "explicit"))
    server._activate_profile(tmp_path / "explicit", ephemeral=False)
    try:
        assert server._can_fall_back_to_ephemeral() is False
    finally:
        server._init_profile()


def test_live_holder_yields_diagnostic_error(tmp_path, monkeypatch):
    lock_path = tmp_path / ".instance.lock"
    monkeypatch.setattr(server, "LOCK_PATH", lock_path)
    # A live holder: this process takes the flock and records our (alive) pid.
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder.seek(0)
    holder.truncate()
    holder.write(f"pid={os.getpid()} start={time.time():.6f}\n")
    holder.flush()

    rt = BrowserRuntime()
    with pytest.raises(RuntimeError) as exc:
        rt._acquire_lock()
    message = str(exc.value)
    assert str(lock_path) in message
    assert "ALIVE" in message
    assert str(os.getpid()) in message
    holder.close()


def test_dead_holder_lock_is_reacquired(tmp_path, monkeypatch):
    lock_path = tmp_path / ".instance.lock"
    monkeypatch.setattr(server, "LOCK_PATH", lock_path)
    lock_path.touch()
    # A dead holder: record a pid that no longer exists and hold the flock in a
    # subprocess that we kill (the kernel releases the flock with the fd).
    dead_pid_holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys,time;"
                "f=os.fdopen(os.open(sys.argv[1], os.O_RDWR), 'w');"
                "fcntl.flock(f, fcntl.LOCK_EX);"
                "f.write(f'pid={sys.argv[2]} start=0\\n');f.flush();"
                "print('held', flush=True);"
                "time.sleep(60)"
            ),
            str(lock_path),
            "99999999",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert "held" in dead_pid_holder.stdout.readline()
    dead_pid_holder.kill()
    dead_pid_holder.wait()
    # flock is released with the dead process's fd.

    rt = BrowserRuntime()
    rt._acquire_lock()
    try:
        assert rt.lock_file is not None
        # Our own pid must now be recorded as holder.
        content = lock_path.read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in content
    finally:
        rt.cleanup()


def test_ephemeral_profile_is_created_atomically_0700(tmp_path, monkeypatch):
    """F7: the throwaway profile must be private from the moment it exists."""
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    path = server._ephemeral_profile_dir()

    assert path == tmp_path / "chrome-web-v2-profile" / str(os.getpid())
    assert (path.stat().st_mode & 0o777) == 0o700
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    # A stale directory from an earlier run of this pid is ours and is reused.
    assert server._ephemeral_profile_dir() == path


def test_ephemeral_profile_refuses_an_open_or_foreign_base(tmp_path, monkeypatch):
    """F7: a pre-created shared-/tmp parent must never be used."""
    base = tmp_path / "chrome-web-v2-profile"
    base.mkdir()
    os.chmod(base, 0o777)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="refusing"):
        server._ephemeral_profile_dir()


def test_ephemeral_profile_refuses_a_symlinked_base(tmp_path, monkeypatch):
    """F7: a symlinked parent is refused, never followed."""
    target = tmp_path / "attacker"
    target.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "chrome-web-v2-profile").symlink_to(target)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(root))

    with pytest.raises(RuntimeError, match="refusing"):
        server._ephemeral_profile_dir()


def test_ephemeral_profile_refuses_a_precreated_pid_directory(tmp_path, monkeypatch):
    """F7: the per-pid leaf is the actual profile; it must not be pre-planted."""
    base = tmp_path / "chrome-web-v2-profile"
    leaf = base / str(os.getpid())
    leaf.mkdir(parents=True)
    os.chmod(leaf, 0o777)
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="refusing"):
        server._ephemeral_profile_dir()


def test_sweep_orphans_removes_dead_keeps_alive(tmp_path, monkeypatch):
    base = tmp_path / "chrome-web-v2-profile"
    base.mkdir()
    dead_dir = base / "1234567"  # an unlikely-live pid
    dead_dir.mkdir()
    (dead_dir / "marker").write_text("x")
    alive_dir = base / str(os.getpid())
    alive_dir.mkdir()

    # _sweep_orphans hardcodes tempfile.gettempdir(); point it at our tmp_path.
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
    BrowserRuntime._sweep_orphans()
    assert not dead_dir.exists(), "dead-owner profile dir should be removed"
    assert alive_dir.exists(), "alive-owner profile dir must be kept"


def test_contender_cleanup_preserves_owners_devtools_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(server, "LOCK_PATH", tmp_path / ".instance.lock")
    monkeypatch.setattr(server, "DEVTOOLS_FILE", tmp_path / "DevToolsActivePort")
    owner = BrowserRuntime()
    contender = BrowserRuntime()
    owner._acquire_lock()
    server.DEVTOOLS_FILE.write_text("12345\n/devtools/browser/owner\n")
    try:
        with pytest.raises(RuntimeError, match="owns this profile"):
            contender.ensure()
        contender.cleanup()
        assert server.DEVTOOLS_FILE.read_text() == "12345\n/devtools/browser/owner\n"
    finally:
        owner.cleanup()


def test_stale_process_identity_does_not_signal_reused_pid(monkeypatch):
    monkeypatch.setattr(server, "_process_identity", lambda pid: {"pid": pid, "start_ticks": "new"})
    monkeypatch.setattr(server.os, "killpg", lambda *args: pytest.fail("must not signal a reused PID"))
    server._stop_recorded_process({"pid": 1234, "start_ticks": "old"})


def test_sweep_recovers_recorded_display_and_browser(tmp_path, monkeypatch):
    base = tmp_path / "chrome-web-v2-profile"
    dead = base / "99999999"
    dead.mkdir(parents=True)
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True) for _ in range(2)]
    try:
        records = [server._process_identity(child.pid) for child in children]
        (dead / ".owned-processes.json").write_text(json.dumps(records))
        monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
        BrowserRuntime._sweep_orphans()
        assert all(child.wait(timeout=5) < 0 for child in children)
        assert not dead.exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_cleanup_removes_owned_ephemeral_profile(tmp_path, monkeypatch):
    profile = tmp_path / "default-profile"
    monkeypatch.delenv("CW_PROFILE_DIR", raising=False)
    monkeypatch.setattr(server, "PROFILE_DIR", profile)
    monkeypatch.setattr(server, "LOCK_PATH", profile / ".instance.lock")
    monkeypatch.setattr(server, "DEVTOOLS_FILE", profile / "DevToolsActivePort")
    monkeypatch.setattr(server, "_PROFILE_EPHEMERAL", True)
    runtime = BrowserRuntime()
    runtime._acquire_lock()
    (profile / "fixture").write_text("temporary profile")
    runtime.cleanup()
    assert not profile.exists()


def test_cleanup_keeps_persistent_profile(tmp_path, monkeypatch):
    """A persistent profile must survive cleanup: that is the whole point of it."""
    profile = tmp_path / "persistent-profile"
    monkeypatch.setattr(server, "PROFILE_DIR", profile)
    monkeypatch.setattr(server, "LOCK_PATH", profile / ".instance.lock")
    monkeypatch.setattr(server, "DEVTOOLS_FILE", profile / "DevToolsActivePort")
    monkeypatch.setattr(server, "_PROFILE_EPHEMERAL", False)
    runtime = BrowserRuntime()
    runtime._acquire_lock()
    (profile / "Cookies").write_text("session state")
    runtime.cleanup()
    assert (profile / "Cookies").read_text() == "session state"
