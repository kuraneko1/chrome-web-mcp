"""Deterministic tests for the macOS browser/process platform boundary."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from chrome_web_mcp import platform_runtime, server


def test_platform_detection(monkeypatch):
    monkeypatch.setattr(platform_runtime.sys, "platform", "darwin")
    assert platform_runtime.platform_key() == "macos"
    monkeypatch.setattr(platform_runtime.sys, "platform", "linux")
    assert platform_runtime.platform_key() == "linux"


def test_mac_chrome_discovery(tmp_path, monkeypatch):
    chrome = tmp_path / "Google Chrome"
    chrome.write_text("#!/bin/sh\necho 'Google Chrome 999.0'\n", encoding="utf-8")
    chrome.chmod(0o755)
    monkeypatch.setattr(platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.setenv("CW_CHROME", str(chrome))
    assert platform_runtime.discover_chrome() == str(chrome)
    assert platform_runtime.chrome_version(str(chrome)) == "Google Chrome 999.0"


def test_mac_chrome_candidate_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.delenv("CW_CHROME", raising=False)
    local = tmp_path / ".local-chrome"
    bundled = local / "chrome-mac-arm64" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled.chmod(0o755)
    monkeypatch.setattr(platform_runtime, "LOCAL_CHROME_DIR", local)
    monkeypatch.setattr(platform_runtime.shutil, "which", lambda name: f"/path/{name}")
    candidates = platform_runtime.chrome_candidates()
    bundled_index = candidates.index(str(bundled))
    stable = candidates.index("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    chromium = candidates.index("/Applications/Chromium.app/Contents/MacOS/Chromium")
    beta = candidates.index("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta")
    assert bundled_index < stable < chromium < beta


def test_cw_chrome_still_beats_bundled_cft(tmp_path, monkeypatch):
    override = tmp_path / "override-chrome"
    override.write_text("#!/bin/sh\n", encoding="utf-8")
    override.chmod(0o755)
    monkeypatch.setattr(platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.setenv("CW_CHROME", str(override))
    monkeypatch.setattr(platform_runtime, "LOCAL_CHROME_DIR", tmp_path / ".local-chrome")
    assert platform_runtime.chrome_candidates() == [str(override)]
    assert platform_runtime.discover_chrome() == str(override)


def test_mac_visible_backend(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.delenv("CW_DISPLAY_MODE", raising=False)
    config = dict(server._CONFIG_DEFAULTS)
    config["show_browser"] = True
    monkeypatch.setattr(server, "CONFIG", config)
    assert server.BrowserRuntime().display_mode == "native"


def test_mac_headless_backend(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.delenv("CW_DISPLAY_MODE", raising=False)
    config = dict(server._CONFIG_DEFAULTS)
    config["show_browser"] = False
    monkeypatch.setattr(server, "CONFIG", config)
    assert server.BrowserRuntime().display_mode == "headless"


def test_process_identity():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        first = platform_runtime.process_identity(child.pid)
        second = platform_runtime.process_identity(child.pid)
        assert first == second
        assert first["pid"] == child.pid
        assert "create_time" in first
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_process_cleanup():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    server._terminate_owned_process(child, 2)
    assert child.poll() is not None


def test_profile_isolation():
    assert str(os.getpid()) in str(server.PROFILE_DIR)
    assert server.LOCK_PATH == server.PROFILE_DIR / ".instance.lock"


class _FakeProxy:
    url = "http://127.0.0.1:12345"


@pytest.mark.parametrize("mode,expects_headless", [("native", False), ("headless", True)])
def test_no_x11_flags_on_mac(monkeypatch, mode, expects_headless):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.setattr(server.BrowserRuntime, "_chrome_executable", staticmethod(lambda: "/bin/echo"))
    runtime = server.BrowserRuntime()
    runtime.display_mode = mode
    runtime.proxy = _FakeProxy()
    command = runtime._chrome_command()
    assert "--ozone-platform=x11" not in command
    assert "--disable-gpu" not in command
    assert "--disable-dev-shm-usage" not in command
    assert ("--headless=new" in command) is expects_headless


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.com/",
        "https://example.com/?key=sk-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_public_url_security(url):
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_public_url(url)


def test_mcp_tools():
    tools = asyncio.run(server.list_tools())
    assert sorted(tool.name for tool in tools) == ["fetch_url", "google_search", "health_check"]


def test_health_check_mac(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "macos")
    monkeypatch.setattr(server.platform_runtime, "platform_description", lambda: "macOS test (arm64)")
    monkeypatch.setattr(server.BrowserRuntime, "_chrome_executable", staticmethod(lambda: "/tmp/Google Chrome"))
    monkeypatch.setattr(server.platform_runtime, "chrome_version", lambda binary: "Google Chrome 999.0")
    monkeypatch.setattr(server._RUNTIME, "display_mode", "headless")
    payload = json.loads(asyncio.run(server.call_tool("health_check", {}))[0].text)
    assert payload["success"] is True
    data = payload["data"]
    assert data["platform"] == "macOS test (arm64)"
    assert data["chrome_binary"] == "/tmp/Google Chrome"
    assert data["chrome_version"] == "Google Chrome 999.0"
    assert data["display_mode"] == "headless"


def test_macos_stealth_does_not_spoof_windows_or_intel(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "macos")
    script = server.BrowserRuntime._stealth_init_js()
    assert "Win32" not in script
    assert "Intel Iris OpenGL Engine" not in script
    assert "webdriver" in script
