"""Idle browser release: decision logic and CW_IDLE_RELEASE/CONFIG plumbing.

The decision function is driven with a fake runtime and an explicit clock, so
these tests never start a browser and never sleep.
"""

import asyncio

import pytest

from chrome_web_mcp import server


class FakeChrome:
    """Stand-in for ``subprocess.Popen``; ``poll() is None`` means alive."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def poll(self):
        return None if self.alive else 1


class FakeRuntime:
    """The subset of ``BrowserRuntime`` the release decision touches."""

    def __init__(self, *, chrome=None, active_calls=0, last_used=None, releases=0):
        self.chrome = chrome
        self.active_calls = active_calls
        self.last_used = last_used
        self.browser_releases = releases
        self.cleanups = 0

    def cleanup(self):
        self.cleanups += 1
        self.chrome = None


def test_release_if_idle_releases_after_timeout():
    runtime = FakeRuntime(chrome=FakeChrome(), last_used=100.0)

    assert server._release_if_idle(runtime, 400.0, 300.0) is True
    assert runtime.cleanups == 1
    assert runtime.browser_releases == 1
    assert runtime.chrome is None


def test_release_if_idle_waits_for_the_full_timeout():
    runtime = FakeRuntime(chrome=FakeChrome(), last_used=100.0)

    assert server._release_if_idle(runtime, 399.999, 300.0) is False
    assert runtime.cleanups == 0
    assert runtime.browser_releases == 0


def test_release_if_idle_keeps_a_busy_runtime():
    runtime = FakeRuntime(chrome=FakeChrome(), active_calls=1, last_used=0.0)

    assert server._release_if_idle(runtime, 10_000.0, 300.0) is False
    assert runtime.cleanups == 0


def test_release_if_idle_keeps_a_runtime_without_a_browser():
    never_started = FakeRuntime(chrome=None, last_used=0.0)
    assert server._release_if_idle(never_started, 10_000.0, 300.0) is False

    exited = FakeRuntime(chrome=FakeChrome(alive=False), last_used=0.0)
    assert server._release_if_idle(exited, 10_000.0, 300.0) is False

    never_used = FakeRuntime(chrome=FakeChrome(), last_used=None)
    assert server._release_if_idle(never_used, 10_000.0, 300.0) is False

    assert never_started.cleanups == exited.cleanups == never_used.cleanups == 0


@pytest.mark.parametrize("idle_s", [0.0, -1.0])
def test_release_if_idle_is_a_noop_when_disabled(idle_s):
    runtime = FakeRuntime(chrome=FakeChrome(), last_used=0.0)

    assert server._release_if_idle(runtime, 10_000.0, idle_s) is False
    assert runtime.cleanups == 0


def test_idle_release_env_overrides_config(monkeypatch):
    monkeypatch.setattr(server, "CONFIG", {"idle_release_s": 42})
    monkeypatch.delenv("CW_IDLE_RELEASE", raising=False)
    assert server._idle_release_s() == 42.0

    monkeypatch.setenv("CW_IDLE_RELEASE", "7.5")
    assert server._idle_release_s() == 7.5

    # 0 disables idle release, and a bad value falls back to the config.
    monkeypatch.setenv("CW_IDLE_RELEASE", "0")
    assert server._idle_release_s() == 0.0
    monkeypatch.setenv("CW_IDLE_RELEASE", "soon")
    assert server._idle_release_s() == 42.0
    monkeypatch.setenv("CW_IDLE_RELEASE", "-3")
    assert server._idle_release_s() == 0.0


def test_idle_release_defaults_to_300_seconds(monkeypatch):
    monkeypatch.delenv("CW_IDLE_RELEASE", raising=False)
    monkeypatch.setattr(server, "CONFIG", dict(server._CONFIG_DEFAULTS))
    assert server._idle_release_s() == 300.0


def test_idle_reaper_interval_is_capped_at_30s():
    assert server._idle_reaper_interval(300.0) == 30.0
    assert server._idle_reaper_interval(5.0) == 5.0


def test_reaper_tick_uses_the_clock_and_swallows_errors(monkeypatch):
    seen = []

    def fake_release(runtime, now, idle_s):
        seen.append((runtime, now, idle_s))
        return True

    runtime = FakeRuntime(chrome=FakeChrome())
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    monkeypatch.setattr(server, "_release_if_idle", fake_release)
    assert server._reaper_tick(5.0) is True
    assert seen[0][0] is runtime and seen[0][2] == 5.0

    def boom(runtime, now, idle_s):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(server, "_release_if_idle", boom)
    assert server._reaper_tick(5.0) is False  # swallowed, reaper keeps running


def test_idle_reaper_task_wakes_and_ticks(monkeypatch):
    ticks = []
    monkeypatch.setattr(server, "_reaper_tick", lambda idle_s: ticks.append(idle_s))

    async def run():
        task = asyncio.create_task(server._idle_reaper_task(5.0, 0))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert ticks
    assert set(ticks) == {5.0}


def test_main_schedules_and_cancels_the_reaper_only_when_enabled(monkeypatch):
    started = []

    async def fake_reaper(idle_s, interval_s):
        started.append((idle_s, interval_s))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            started.append("cancelled")
            raise

    class FakeStdio:
        async def __aenter__(self):
            return None, None

        async def __aexit__(self, *exc):
            return False

    async def fake_run(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "_idle_reaper_task", fake_reaper)
    monkeypatch.setattr(server, "stdio_server", lambda: FakeStdio())
    monkeypatch.setattr(server.app, "run", fake_run)
    monkeypatch.setattr(server, "_RUNTIME", FakeRuntime())

    monkeypatch.setattr(server, "_idle_release_s", lambda: 5.0)
    asyncio.run(server.main())
    assert started[0] == (5.0, 5.0)
    assert "cancelled" in started  # cancelled on the normal EOF exit path

    started.clear()
    monkeypatch.setattr(server, "_idle_release_s", lambda: 0.0)
    asyncio.run(server.main())
    assert started == []  # idle_release_s=0 restores today's behaviour
