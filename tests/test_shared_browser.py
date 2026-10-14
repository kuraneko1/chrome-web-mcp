"""Shared-browser owner/attach: registry validation, attach, and idle reaper.

The unit tests never start Xvfb/Chrome and never touch the network: the CDP
endpoint is a local websocket that answers ``Browser.getVersion``, and the lock
holder is a short-lived subprocess holding the real ``flock``. The integration
test at the end spawns two real MCP servers against one profile and a local
tripwire server (no Internet); it skips cleanly when Xvfb/Chrome are missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.server import serve

from chrome_web_mcp import registry, server

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
BOOTSTRAP = ROOT / "tests" / "fixtures" / "shared_browser_server.py"
FIXTURE_URL = "https://93.184.215.14/fixture"
FIXTURE_BODY = "<html><body><p>shared tripwire fetch</p></body></html>"
FIXTURE_TEXT = "shared tripwire fetch"

# Hold the real profile flock from another process (no browser involved).
_HOLD_LOCK = (
    "import fcntl, os, sys, time;"
    "f = os.fdopen(os.open(sys.argv[1], os.O_RDWR | os.O_CREAT), 'w+');"
    "fcntl.flock(f, fcntl.LOCK_EX);"
    "f.write('pid=' + str(os.getpid()) + ' start=' + format(time.time(), '.6f') + chr(10));"
    "f.flush(); print('held', flush=True); time.sleep(120)"
)


def _birth(pid: int) -> str:
    return str(server._process_identity(pid)["create_time"])


def _profile(tmp_path: Path, monkeypatch) -> Path:
    """Point the module globals at a throwaway profile and return it."""
    profile = tmp_path / "profile"
    monkeypatch.setattr(server, "PROFILE_DIR", profile)
    monkeypatch.setattr(server, "LOCK_PATH", profile / ".instance.lock")
    monkeypatch.setattr(server, "DEVTOOLS_FILE", profile / "DevToolsActivePort")
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _write_registry(profile: Path, **overrides) -> Path:
    """Write a registry directly so a test can inject untrusted values."""
    payload = {
        "version": registry.REGISTRY_VERSION,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "started": time.monotonic(),
        "port": 9450,
        "browser_ws": "ws://127.0.0.1:9450/devtools/browser/x",
        "create_time": _birth(os.getpid()),
    }
    payload.update(overrides)
    path = registry.registry_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _write_raw(profile: Path, name: str, text: str, mode: int = 0o600) -> Path:
    directory = registry.clients_dir(profile)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _LockHolder:
    """A separate process that holds the profile flock for the test's lifetime."""

    def __init__(self, lock_path: Path) -> None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [str(PYTHON), "-c", _HOLD_LOCK, str(lock_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "held"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.fixture
def fake_cdp():
    """A loopback websocket that answers ``Browser.getVersion`` like Chrome."""

    def handler(connection):
        try:
            while True:
                message = json.loads(connection.recv())
                if message.get("method") == "Browser.getVersion":
                    connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "result": {"product": "Chrome/test", "protocolVersion": "1.3"},
                            }
                        )
                    )
        except Exception:
            return

    endpoint = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    port = int(endpoint.socket.getsockname()[1])
    try:
        yield f"ws://127.0.0.1:{port}/devtools/browser/shared-test", port
    finally:
        endpoint.shutdown()


@pytest.fixture
def silent_cdp():
    """A websocket that accepts connections and never answers."""

    def handler(connection):
        time.sleep(10)

    endpoint = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    port = int(endpoint.socket.getsockname()[1])
    try:
        yield f"ws://127.0.0.1:{port}/devtools/browser/silent", port
    finally:
        endpoint.shutdown()


# --------------------------------------------------------------------------- #
# Registry format and validation
# --------------------------------------------------------------------------- #


def test_registry_round_trip_writes_a_private_file(tmp_path):
    profile = tmp_path / "profile"
    registry.write_registry(
        profile,
        pid=os.getpid(),
        port=9450,
        browser_ws="ws://127.0.0.1:9450/devtools/browser/x",
        create_time=_birth(os.getpid()),
    )

    path = registry.registry_path(profile)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob("*.tmp")) == []  # atomic replace leaves no partial file
    entry = registry.load_registry(profile)
    assert entry["pid"] == os.getpid()
    assert entry["uid"] == os.getuid()
    assert entry["port"] == 9450
    assert entry["browser_ws"] == "ws://127.0.0.1:9450/devtools/browser/x"
    assert entry["create_time"] == _birth(os.getpid())


@pytest.mark.parametrize(
    "name, kind",
    [("pid", "text"), ("uid", "text")],
)
def test_registry_documented_shape_is_stable(tmp_path, name, kind):
    """The published keys are the documented format (regression guard)."""
    profile = tmp_path / "profile"
    registry.write_registry(
        profile,
        pid=os.getpid(),
        port=9450,
        browser_ws="ws://127.0.0.1:9450/devtools/browser/x",
        create_time=_birth(os.getpid()),
    )
    entry = registry.load_registry(profile)
    assert set(entry) == {
        "version",
        "pid",
        "uid",
        "started",
        "port",
        "browser_ws",
        "create_time",
    }
    assert isinstance(entry[name], (int, str))


def test_load_registry_rejects_a_missing_file(tmp_path):
    with pytest.raises(registry.RegistryError):
        registry.load_registry(tmp_path / "absent")


def test_load_registry_rejects_malformed_json(tmp_path):
    profile = tmp_path / "profile"
    path = registry.registry_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(registry.RegistryError, match="not valid JSON"):
        registry.load_registry(profile)


def test_load_registry_rejects_a_world_readable_file(tmp_path):
    profile = tmp_path / "profile"
    path = _write_registry(profile)
    os.chmod(path, 0o644)
    with pytest.raises(registry.RegistryError, match="not private"):
        registry.load_registry(profile)


def test_load_registry_rejects_an_implausibly_large_file(tmp_path):
    profile = tmp_path / "profile"
    path = registry.registry_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * (9 * 1024), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(registry.RegistryError, match="large"):
        registry.load_registry(profile)


def test_load_registry_rejects_a_symlink(tmp_path):
    profile = tmp_path / "profile"
    real = _write_registry(tmp_path / "elsewhere")
    path = registry.registry_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(real)
    with pytest.raises(registry.RegistryError):
        registry.load_registry(profile)


def test_load_registry_rejects_a_foreign_uid(tmp_path):
    profile = tmp_path / "profile"
    _write_registry(profile, uid=os.getuid() + 1)
    with pytest.raises(registry.RegistryError, match="uid"):
        registry.load_registry(profile)


def test_load_registry_rejects_an_unknown_version(tmp_path):
    profile = tmp_path / "profile"
    _write_registry(profile, version=registry.REGISTRY_VERSION + 1)
    with pytest.raises(registry.RegistryError, match="version"):
        registry.load_registry(profile)


@pytest.mark.parametrize("bad_pid", [0, 1, -5, "1234", True, None])
def test_load_registry_rejects_an_invalid_pid(tmp_path, bad_pid):
    profile = tmp_path / "profile"
    _write_registry(profile, pid=bad_pid)
    with pytest.raises(registry.RegistryError, match="pid"):
        registry.load_registry(profile)


@pytest.mark.parametrize("bad_port", [0, 65536, "9450", True])
def test_load_registry_rejects_an_invalid_port(tmp_path, bad_port):
    profile = tmp_path / "profile"
    _write_registry(profile, port=bad_port, browser_ws="ws://127.0.0.1:9450/devtools/browser/x")
    with pytest.raises(registry.RegistryError, match="port"):
        registry.load_registry(profile)


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://10.0.0.5:9450/devtools/browser/x",  # not loopback
        "ws://127.0.0.1:9451/devtools/browser/x",  # port disagrees with the registry
        "http://127.0.0.1:9450/devtools/browser/x",  # not a websocket
        "ws://127.0.0.1/devtools/browser/x",  # no port
        "not-a-url",
    ],
)
def test_load_registry_rejects_a_non_loopback_endpoint(tmp_path, endpoint):
    profile = tmp_path / "profile"
    _write_registry(profile, browser_ws=endpoint)
    with pytest.raises(registry.RegistryError):
        registry.load_registry(profile)


def test_remove_registry_never_deletes_another_owners_file(tmp_path):
    profile = tmp_path / "profile"
    _write_registry(profile, pid=os.getpid() + 1)
    registry.remove_registry(profile, owner_pid=os.getpid())
    assert registry.registry_path(profile).exists()
    registry.remove_registry(profile, owner_pid=os.getpid() + 1)
    assert not registry.registry_path(profile).exists()


# --------------------------------------------------------------------------- #
# Client heartbeats (the owner's idle-reaper input)
# --------------------------------------------------------------------------- #


def test_heartbeat_is_fresh_below_the_idle_timeout_and_stale_above_it(tmp_path):
    profile = tmp_path / "profile"
    registry.write_heartbeat(
        profile, pid=os.getpid(), create_time=_birth(os.getpid()), last_used=1000.0
    )
    path = registry.heartbeat_path(profile, os.getpid())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert registry.live_client_pids(profile, now=1400.0, idle_s=300.0) == []
    assert not path.exists()  # stale entries are pruned, not merely ignored

    registry.write_heartbeat(
        profile, pid=os.getpid(), create_time=_birth(os.getpid()), last_used=1000.0
    )
    assert registry.live_client_pids(profile, now=1200.0, idle_s=300.0) == [os.getpid()]


def test_dead_client_heartbeat_is_pruned(tmp_path):
    profile = tmp_path / "profile"
    child = subprocess.Popen([str(PYTHON), "-c", "import time; time.sleep(60)"])
    try:
        birth = _birth(child.pid)
    finally:
        child.kill()
        child.wait(timeout=5)
    registry.write_heartbeat(profile, pid=child.pid, create_time=birth)

    assert registry.live_client_pids(profile, now=time.time(), idle_s=300.0) == []
    assert not registry.heartbeat_path(profile, child.pid).exists()


def test_reused_pid_heartbeat_is_pruned(tmp_path):
    profile = tmp_path / "profile"
    registry.write_heartbeat(profile, pid=os.getpid(), create_time="0.000000")
    assert registry.live_client_pids(profile) == []
    assert not registry.heartbeat_path(profile, os.getpid()).exists()


@pytest.mark.parametrize("mode", [0o644, 0o604])
def test_world_readable_heartbeat_is_pruned(tmp_path, mode):
    profile = tmp_path / "profile"
    _write_raw(
        profile,
        f"{os.getpid()}.json",
        json.dumps(
            {
                "pid": os.getpid(),
                "uid": os.getuid(),
                "create_time": _birth(os.getpid()),
                "last_used": time.time(),
            }
        ),
        mode=mode,
    )
    assert registry.live_client_pids(profile) == []
    assert not registry.heartbeat_path(profile, os.getpid()).exists()


def test_malformed_heartbeat_is_pruned(tmp_path):
    profile = tmp_path / "profile"
    _write_raw(profile, f"{os.getpid()}.json", "not json at all")
    assert registry.live_client_pids(profile) == []
    assert not registry.heartbeat_path(profile, os.getpid()).exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"pid": os.getpid(), "uid": os.getuid() + 1, "create_time": "0", "last_used": 1.0},
        {"pid": os.getpid(), "uid": os.getuid(), "last_used": 1.0},  # no birth stamp
        {"pid": os.getpid(), "uid": os.getuid(), "create_time": "0"},  # no heartbeat time
        {"pid": os.getpid(), "uid": os.getuid(), "create_time": "0", "last_used": "soon"},
        ["not", "an", "object"],
    ],
)
def test_invalid_heartbeat_fields_are_pruned(tmp_path, payload):
    profile = tmp_path / "profile"
    _write_raw(profile, f"{os.getpid()}.json", json.dumps(payload))
    assert registry.live_client_pids(profile, now=10.0, idle_s=300.0) == []
    assert not registry.heartbeat_path(profile, os.getpid()).exists()


def test_heartbeat_filename_must_match_the_recorded_pid(tmp_path):
    profile = tmp_path / "profile"
    _write_raw(
        profile,
        "999999.json",
        json.dumps(
            {
                "pid": os.getpid(),
                "uid": os.getuid(),
                "create_time": _birth(os.getpid()),
                "last_used": time.time(),
            }
        ),
    )
    assert registry.live_client_pids(profile) == []
    assert not (registry.clients_dir(profile) / "999999.json").exists()


def test_live_client_count_without_a_clients_directory_is_zero(tmp_path):
    assert registry.live_client_count(tmp_path / "absent") == 0


# --------------------------------------------------------------------------- #
# CDP probe
# --------------------------------------------------------------------------- #


def test_probe_browser_accepts_a_real_cdp_answer(fake_cdp):
    endpoint, _port = fake_cdp
    assert registry.probe_browser(endpoint, timeout=2.0) is True


def test_probe_browser_rejects_a_refused_endpoint():
    port = _free_port()
    assert (
        registry.probe_browser(f"ws://127.0.0.1:{port}/devtools/browser/x", timeout=1.0) is False
    )


def test_probe_browser_times_out_on_a_silent_endpoint(silent_cdp):
    endpoint, _port = silent_cdp
    started = time.monotonic()
    assert registry.probe_browser(endpoint, timeout=0.5) is False
    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------------------- #
# Attach path
# --------------------------------------------------------------------------- #


def _unexpected_start(*_args, **_kwargs):
    raise AssertionError("an attaching process must not start its own browser")


class _StubProxy:
    url = "http://127.0.0.1:0"

    def close(self) -> None:
        pass


def _stub_browser_start(monkeypatch) -> str:
    endpoint = "ws://127.0.0.1:4321/devtools/browser/stub"
    monkeypatch.setenv(
        "CW_DISPLAY_MODE",
        "xvfb" if server.platform_runtime.platform_key() == "linux" else "headless",
    )
    monkeypatch.setattr(server.BrowserRuntime, "_start_xvfb", lambda self: ":99")
    monkeypatch.setattr(
        server.BrowserRuntime, "_start_chrome", lambda self, display: (4321, endpoint)
    )
    monkeypatch.setattr(server, "PublicNetworkProxy", _StubProxy)
    return endpoint


def test_attach_uses_the_owner_endpoint_without_starting_a_browser(
    tmp_path, monkeypatch, fake_cdp
):
    profile = _profile(tmp_path, monkeypatch)
    endpoint, port = fake_cdp
    holder = _LockHolder(server.LOCK_PATH)
    try:
        registry.write_registry(
            profile,
            pid=holder.proc.pid,
            port=port,
            browser_ws=endpoint,
            create_time=_birth(holder.proc.pid),
        )
        monkeypatch.setattr(server.BrowserRuntime, "_start_xvfb", _unexpected_start)
        monkeypatch.setattr(server.BrowserRuntime, "_start_chrome", _unexpected_start)
        monkeypatch.setattr(server, "PublicNetworkProxy", _unexpected_start)

        runtime = server.BrowserRuntime()
        assert runtime.ensure() == endpoint
        assert runtime.shared is True
        assert runtime.owner_pid == holder.proc.pid
        assert runtime.attach_error is None
        assert runtime.chrome is None
        assert runtime.xvfb is None
        assert runtime.xephyr is None
        assert runtime.proxy is None
        assert runtime.lock_file is None  # an attached client holds no lock

        heartbeat = registry.heartbeat_path(profile, os.getpid())
        assert heartbeat.is_file()
        assert stat.S_IMODE(heartbeat.stat().st_mode) == 0o600

        # cleanup() must only drop our own heartbeat: the owner's registry,
        # lock and browser all survive.
        runtime.cleanup()
        assert not heartbeat.exists()
        assert registry.registry_path(profile).exists()
        assert registry.load_registry(profile)["pid"] == holder.proc.pid
        assert server.LOCK_PATH.exists()
        assert holder.proc.poll() is None
    finally:
        holder.stop()


def test_attach_is_cached_across_calls_and_revalidated_from_the_registry(
    tmp_path, monkeypatch, fake_cdp
):
    """An attached endpoint is reused, and dropped once the registry changes."""
    profile = _profile(tmp_path, monkeypatch)
    endpoint, port = fake_cdp
    holder = _LockHolder(server.LOCK_PATH)
    try:
        registry.write_registry(
            profile,
            pid=holder.proc.pid,
            port=port,
            browser_ws=endpoint,
            create_time=_birth(holder.proc.pid),
        )
        probes = []
        monkeypatch.setattr(
            registry, "probe_browser", lambda url, timeout=2.0: probes.append(url) or True
        )
        runtime = server.BrowserRuntime()
        assert runtime.ensure() == endpoint
        assert runtime.ensure() == endpoint  # cheap re-check, no second probe
        assert probes == [endpoint]

        # The owner republished a different endpoint: the cached one is stale.
        registry.write_registry(
            profile,
            pid=holder.proc.pid,
            port=port,
            browser_ws=f"ws://127.0.0.1:{port}/devtools/browser/other",
            create_time=_birth(holder.proc.pid),
        )
        assert runtime._shared_still_current() is False
        assert runtime.ensure() == f"ws://127.0.0.1:{port}/devtools/browser/other"
    finally:
        holder.stop()


def test_attach_refuses_a_missing_registry(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    runtime = server.BrowserRuntime()
    assert runtime._attach_shared() is False
    assert runtime.shared is False
    assert runtime.attach_error


def test_attached_client_reprobes_after_a_connect_failure(tmp_path, monkeypatch, fake_cdp):
    """The owner's Chrome dying must not leave a client trusting a dead endpoint."""
    profile = _profile(tmp_path, monkeypatch)
    endpoint, port = fake_cdp
    _write_registry(profile, pid=os.getpid(), port=port, browser_ws=endpoint)
    runtime = server.BrowserRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    runtime.shared = True
    runtime.owner_pid = os.getpid()
    runtime.port = port
    runtime.browser_ws = endpoint

    dead = f"ws://127.0.0.1:{_free_port()}/devtools/browser/dead"
    with pytest.raises(Exception):
        asyncio.run(server._connect_browser(dead))
    assert runtime.shared_recheck is True

    probes = []
    monkeypatch.setattr(
        registry, "probe_browser", lambda url, timeout=2.0: probes.append(url) or True
    )
    assert runtime.ensure() == endpoint  # re-probed and kept
    assert probes == [endpoint]
    assert runtime.shared_recheck is False
    assert runtime.ensure() == endpoint  # healthy again: no further probe
    assert probes == [endpoint]


def test_call_tool_refreshes_an_attached_clients_heartbeat(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    runtime = server.BrowserRuntime()
    runtime.shared = True
    runtime.owner_pid = 1234
    monkeypatch.setattr(server, "_RUNTIME", runtime)

    async def fake_fetch(url, char_limit, format="markdown"):
        return {"url": url, "title": "t", "text": "x", "truncated": False}

    monkeypatch.setattr(server, "_fetch_page", fake_fetch)
    before = time.time()
    asyncio.run(server.call_tool("fetch_url", {"url": "https://example.com"}))
    heartbeat = json.loads(
        registry.heartbeat_path(profile, os.getpid()).read_text(encoding="utf-8")
    )
    assert heartbeat["pid"] == os.getpid()
    assert heartbeat["create_time"] == _birth(os.getpid())
    assert heartbeat["last_used"] >= before

    # health_check is not a browser call, so it must not refresh the heartbeat.
    original = heartbeat["last_used"]
    asyncio.run(server.call_tool("health_check", {}))
    refreshed = json.loads(
        registry.heartbeat_path(profile, os.getpid()).read_text(encoding="utf-8")
    )
    assert refreshed["last_used"] == original


def test_attach_refuses_a_stale_birth_stamp(tmp_path, monkeypatch, fake_cdp):
    profile = _profile(tmp_path, monkeypatch)
    endpoint, port = fake_cdp
    _write_registry(
        profile,
        pid=os.getpid(),
        port=port,
        browser_ws=endpoint,
        create_time="0.000000",
    )
    runtime = server.BrowserRuntime()
    assert runtime._attach_shared() is False
    assert runtime.shared is False
    assert "reused" in (runtime.attach_error or "")


def test_attach_refuses_a_dead_owner(tmp_path, monkeypatch, fake_cdp):
    profile = _profile(tmp_path, monkeypatch)
    endpoint, port = fake_cdp
    child = subprocess.Popen([str(PYTHON), "-c", "import time; time.sleep(60)"])
    try:
        birth = _birth(child.pid)
    finally:
        child.kill()
        child.wait(timeout=5)
    _write_registry(profile, pid=child.pid, port=port, browser_ws=endpoint, create_time=birth)

    runtime = server.BrowserRuntime()
    assert runtime._attach_shared() is False
    assert runtime.shared is False
    assert "not alive" in (runtime.attach_error or "")


def test_attach_refuses_an_unreachable_cdp_endpoint(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    port = _free_port()
    _write_registry(
        profile,
        pid=os.getpid(),
        port=port,
        browser_ws=f"ws://127.0.0.1:{port}/devtools/browser/x",
    )
    runtime = server.BrowserRuntime()
    assert runtime._attach_shared() is False
    assert runtime.shared is False
    assert "did not answer" in (runtime.attach_error or "")


# --------------------------------------------------------------------------- #
# Owner election with an absent or unusable registry
# --------------------------------------------------------------------------- #


def test_ensure_becomes_the_owner_and_publishes_the_registry(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    endpoint = _stub_browser_start(monkeypatch)

    runtime = server.BrowserRuntime()
    try:
        assert runtime.ensure() == endpoint
        assert runtime.shared is False
        assert runtime.owner_pid is None
        assert runtime.lock_file is not None
        assert registry.load_registry(profile)["pid"] == os.getpid()
    finally:
        runtime.cleanup()
    # The owner unpublishes before releasing the lock.
    assert not registry.registry_path(profile).exists()


def test_ensure_falls_back_when_the_registry_cannot_be_trusted(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    endpoint = _stub_browser_start(monkeypatch)
    _write_registry(profile, create_time="0.000000")  # present but stale
    monkeypatch.setattr(server, "_ephemeral_profile_dir", lambda: tmp_path / "ephemeral")
    monkeypatch.setattr(server, "_can_fall_back_to_ephemeral", lambda: True)
    monkeypatch.setattr(server, "_PROFILE_FELL_BACK", False)

    holder = _LockHolder(server.LOCK_PATH)
    runtime = server.BrowserRuntime()
    try:
        assert runtime.ensure() == endpoint
        assert runtime.profile_fell_back is True
        assert runtime.shared is False
        assert server._PROFILE_FELL_BACK is True
        assert server._PROFILE_EPHEMERAL is True
        assert server.PROFILE_DIR == tmp_path / "ephemeral"
        assert runtime.lock_file is not None
    finally:
        holder.stop()
        runtime.cleanup()
        # _activate_profile() rebound the module globals; put them back.
        server._init_profile()


def test_attached_client_recovers_when_the_registry_disappears(tmp_path, monkeypatch):
    """Owner death: the client drops the attach and wins the freed election."""
    profile = _profile(tmp_path, monkeypatch)
    endpoint = _stub_browser_start(monkeypatch)
    _write_registry(
        profile,
        pid=os.getpid(),
        port=4321,
        browser_ws=endpoint,
        create_time=_birth(os.getpid()),
    )
    runtime = server.BrowserRuntime()
    monkeypatch.setattr(registry, "probe_browser", lambda url, timeout=2.0: True)
    runtime.shared = True
    runtime.owner_pid = os.getpid()
    runtime.port = 4321
    runtime.browser_ws = endpoint

    assert runtime._shared_still_current() is True
    registry.remove_registry(profile, owner_pid=os.getpid())
    assert runtime._shared_still_current() is False
    assert runtime.ensure() == endpoint  # now the owner of a fresh (stub) browser
    assert runtime.shared is False
    assert runtime.lock_file is not None
    runtime.cleanup()


# --------------------------------------------------------------------------- #
# Idle reaper with attached clients
# --------------------------------------------------------------------------- #


class _FakeChrome:
    def poll(self):
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self.chrome = _FakeChrome()
        self.active_calls = 0
        self.last_used = time.monotonic() - 10_000.0
        self.browser_releases = 0
        self.cleanups = 0

    def cleanup(self) -> None:
        self.cleanups += 1
        self.chrome = None


def test_reaper_keeps_the_browser_while_a_client_heartbeat_is_fresh(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    registry.write_heartbeat(profile, pid=os.getpid(), create_time=_birth(os.getpid()))
    runtime = _FakeRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)

    assert server._reaper_tick(300.0) is False
    assert runtime.cleanups == 0
    assert registry.heartbeat_path(profile, os.getpid()).exists()

    # Once every heartbeat is stale the browser is released as before.
    registry.write_heartbeat(
        profile,
        pid=os.getpid(),
        create_time=_birth(os.getpid()),
        last_used=time.time() - 400.0,
    )
    assert server._reaper_tick(300.0) is True
    assert runtime.cleanups == 1
    assert not registry.heartbeat_path(profile, os.getpid()).exists()


def test_reaper_releases_after_a_crashed_client(tmp_path, monkeypatch):
    """A dead client's heartbeat must not pin the browser forever."""
    profile = _profile(tmp_path, monkeypatch)
    child = subprocess.Popen([str(PYTHON), "-c", "import time; time.sleep(60)"])
    try:
        birth = _birth(child.pid)
    finally:
        child.kill()
        child.wait(timeout=5)
    registry.write_heartbeat(profile, pid=child.pid, create_time=birth)  # fresh timestamp
    runtime = _FakeRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)

    assert server._reaper_tick(300.0) is True
    assert runtime.cleanups == 1
    assert not registry.heartbeat_path(profile, child.pid).exists()


def test_reaper_is_unchanged_when_no_client_ever_attached(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    runtime = _FakeRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)

    assert server._reaper_tick(300.0) is True
    assert runtime.cleanups == 1


# --------------------------------------------------------------------------- #
# health_check reporting
# --------------------------------------------------------------------------- #


def _health_data() -> dict:
    content = asyncio.run(server.call_tool("health_check", {}))
    return json.loads(content[0].text)["data"]


def _health_runtime(monkeypatch, *, shared=False, owner_pid=None, lock_file=None):
    runtime = server.BrowserRuntime()
    runtime.display_mode = "xvfb"
    runtime.shared = shared
    runtime.owner_pid = owner_pid
    runtime.lock_file = lock_file
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    monkeypatch.setattr(server, "_LAST_CAPTCHA_TS", None)
    monkeypatch.setattr(server, "CONFIG", dict(server._CONFIG_DEFAULTS))
    return runtime


def test_health_check_reports_the_owner_role_and_its_clients(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    _health_runtime(monkeypatch, lock_file=object())

    data = _health_data()
    assert data["browser_role"] == "owner"
    assert data["owner_pid"] is None
    assert data["clients"] == 0
    assert data["shared_attach_error"] is None

    registry.write_heartbeat(profile, pid=os.getpid(), create_time=_birth(os.getpid()))
    assert _health_data()["clients"] == 1


def test_health_check_reports_the_shared_role_and_owner_pid(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    _health_runtime(monkeypatch, shared=True, owner_pid=4242)

    data = _health_data()
    assert data["browser_role"] == "shared"
    assert data["owner_pid"] == 4242
    assert data["clients"] is None


def test_health_check_reports_no_role_without_a_browser(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    _health_runtime(monkeypatch)

    data = _health_data()
    assert data["browser_role"] == "none"
    assert data["owner_pid"] is None
    assert data["clients"] is None


def test_health_check_explains_a_failed_attach(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    runtime = _health_runtime(monkeypatch)
    runtime.attach_error = "owner pid 1 is not alive (no such process)"
    assert _health_data()["shared_attach_error"] == "owner pid 1 is not alive (no such process)"


# --------------------------------------------------------------------------- #
# Two real processes, one browser, no Internet
# --------------------------------------------------------------------------- #


def _display_mode() -> str:
    return "xvfb" if server.platform_runtime.platform_key() == "linux" else "headless"


def _browser_available() -> bool:
    if server.platform_runtime.platform_key() == "linux":
        if not shutil.which("Xvfb") or not shutil.which("xdpyinfo"):
            return False
    try:
        server.BrowserRuntime._chrome_executable()
    except Exception:
        return False
    return True


def _chrome_processes(profile: Path) -> list[str]:
    """The one main Chrome process of the test profile (helpers excluded)."""
    listing = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    found = []
    for line in listing.splitlines():
        if (
            f"--user-data-dir={profile}" in line
            and "--remote-debugging-port=" in line
            and "--remote-debugging-address=" in line
        ):
            found.append(line)
    return found


class _McpServer:
    """The minimal stdio MCP client needed to drive one real server process."""

    def __init__(self, env: dict) -> None:
        self.proc = subprocess.Popen(
            [str(PYTHON), str(BOOTSTRAP)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._id = 0
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _next(self, timeout: float):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("MCP response timed out") from exc
        if not line:
            raise RuntimeError("server closed stdout")
        return json.loads(line)

    def rpc(self, method: str, params: dict | None = None, timeout: float = 120):
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self._next(max(0.001, deadline - time.monotonic()))
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method} error: {reply['error']}")
                return reply["result"]
        raise TimeoutError(f"{method} timed out")

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n"
        )
        self.proc.stdin.flush()

    def handshake(self) -> None:
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "shared-browser", "version": "0"},
            },
        )
        self.notify("notifications/initialized")

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return json.loads(result["content"][0]["text"])

    def health(self) -> dict:
        payload = self.tool("health_check")
        assert payload["success"] is True, payload
        return payload["data"]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def tripwire():
    """A local HTTP canary that a shared browser must never be able to reach."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(FIXTURE_BODY.encode())

        def log_message(self, *args):
            pass

    local = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=local.serve_forever, daemon=True).start()
    try:
        yield hits, f"http://127.0.0.1:{local.server_port}"
    finally:
        local.shutdown()
        local.server_close()


def test_two_processes_share_one_browser(tmp_path, tripwire):
    """Two real MCP servers, one profile: both fetch, exactly one Chrome runs.

    The fetch itself is fulfilled locally through CDP (the tripwire fixture), so
    the test needs no Internet and can assert the local canary was never hit.
    """
    hits, tripwire_url = tripwire
    if not _browser_available():
        pytest.skip("Xvfb/Chrome not available")

    profile = tmp_path / "profile"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"show_browser": False}), encoding="utf-8")
    env = dict(
        os.environ,
        CW_CONFIG=str(config),
        CW_PROFILE_DIR=str(profile),
        CW_LOCK_PATH=str(profile / ".instance.lock"),
        CW_RATE_LIMIT_DB=str(tmp_path / "rate-limit.sqlite3"),
        CW_DISPLAY_MODE=_display_mode(),
        CW_TEST_FIXTURE_URL=FIXTURE_URL,
        CW_TEST_FIXTURE_BODY=FIXTURE_BODY,
        PYTHONPATH=str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    owner = _McpServer(env)
    client = None
    try:
        owner.handshake()
        first = owner.tool("fetch_url", {"url": FIXTURE_URL, "format": "text"})
        assert first["success"] is True, first
        assert FIXTURE_TEXT in first["data"]["text"]
        assert first["data"]["requested_url"] == FIXTURE_URL
        owner_health = owner.health()
        assert owner_health["browser_role"] == "owner", owner_health
        assert owner_health["clients"] == 0
        assert len(_chrome_processes(profile)) == 1

        client = _McpServer(env)
        client.handshake()
        second = client.tool("fetch_url", {"url": FIXTURE_URL, "format": "text"})
        assert second["success"] is True, second
        assert FIXTURE_TEXT in second["data"]["text"]

        client_health = client.health()
        assert client_health["browser_role"] == "shared", client_health
        assert client_health["owner_pid"] == owner.proc.pid
        assert owner.health()["clients"] == 1
        # Still exactly one browser after the second process fetched.
        assert len(_chrome_processes(profile)) == 1

        # A client's cleanup must never touch the owner's browser or lock.
        client.close()
        client = None
        assert _chrome_processes(profile), "the client cleanup killed the owner's Chrome"
        again = owner.tool("fetch_url", {"url": FIXTURE_URL, "format": "text"})
        assert again["success"] is True, again
        assert owner.health()["clients"] == 0
    finally:
        if client is not None:
            client.close()
        owner.close()
        time.sleep(0.5)

    assert _chrome_processes(profile) == []
    assert hits == [], f"local tripwire was reached: {hits}"
    assert tripwire_url  # the canary existed even though nothing contacted it
