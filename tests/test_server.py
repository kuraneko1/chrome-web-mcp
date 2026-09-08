import asyncio
import json
import os
import subprocess
import sys
import tempfile

import pytest

from typing import Any

# Use a throwaway Chrome profile/lock so the in-process live test does not
# collide with a live chrome-web-v2 server holding the default profile lock.
_SANDBOX = tempfile.mkdtemp(prefix="cw-v2-test-")
os.environ["CW_PROFILE_DIR"] = os.path.join(_SANDBOX, "profile")
os.environ["CW_LOCK_PATH"] = os.path.join(_SANDBOX, ".instance.lock")
os.environ["CW_RATE_LIMIT_DB"] = os.path.join(_SANDBOX, "rate-limit.sqlite3")

from chrome_web_mcp import server


# MCP's @app.list_tools() / @app.call_tool() decorators leak wrapper signatures
# into static typing, so direct calls trip checkers even though they work at
# runtime. These aliases document that and keep call sites clean.
_list_tools: Any = server.list_tools
_call_tool: Any = server.call_tool


def run_async(awaitable):
    """Run an async call in a sync test."""
    return asyncio.run(awaitable)


def test_exposes_google_search_and_fetch_url_tool_names():
    tools = run_async(_list_tools())
    assert sorted(tool.name for tool in tools) == ["fetch_url", "google_search", "health_check"]


def test_browser_environment_cannot_fall_back_to_user_wayland_session(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

    env = server.BrowserRuntime._browser_environment(":77")

    assert env["DISPLAY"] == ":77"
    assert env["XDG_SESSION_TYPE"] == "x11"
    assert "WAYLAND_DISPLAY" not in env


def test_shared_rate_limiter_reserves_slots_across_processes(tmp_path):
    db_path = tmp_path / "search-rate-limit.sqlite3"
    code = (
        "import sys; "
        "from chrome_web_mcp.server import SharedSearchRateLimiter; "
        "print(SharedSearchRateLimiter(sys.argv[1], min_delay=1.0, max_delay=1.0).reserve_slot(), flush=True)"
    )
    env = dict(os.environ)
    first = subprocess.Popen([sys.executable, "-c", code, str(db_path)], stdout=subprocess.PIPE, text=True, env=env)
    second = subprocess.Popen([sys.executable, "-c", code, str(db_path)], stdout=subprocess.PIPE, text=True, env=env)
    slots = sorted([float(first.communicate(timeout=10)[0]), float(second.communicate(timeout=10)[0])])

    # >=0.5 (not the full 1.0 delay): process spawn skew on loaded hosts eats
    # into the spacing, so assert ordering + substantial gap instead.
    assert slots[1] - slots[0] >= 0.5


def test_google_challenge_is_detected_from_url_or_rendered_text():
    assert server._is_google_challenge("https://www.google.com/sorry/index", "")
    assert server._is_google_challenge("https://www.google.com/search?q=x", "Our systems have detected unusual traffic")
    assert not server._is_google_challenge("https://www.google.com/search?q=x", "Normal search results")


def test_human_display_environment_uses_real_x11_display(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

    env = server.BrowserRuntime()._human_display_environment()

    assert env["DISPLAY"] == ":0"
    assert env["XDG_SESSION_TYPE"] == "x11"
    assert "WAYLAND_DISPLAY" not in env


def test_human_display_environment_discovers_mutter_xauthority(tmp_path, monkeypatch):
    xauth = tmp_path / ".mutter-Xwaylandauth.test"
    xauth.write_bytes(b"cookie")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    env = server.BrowserRuntime()._human_display_environment()

    assert env["XAUTHORITY"] == str(xauth)


def test_human_display_environment_prefers_explicit_xauthority(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit-xauth"
    explicit.write_bytes(b"cookie")
    discovered = tmp_path / ".mutter-Xwaylandauth.test"
    discovered.write_bytes(b"cookie")
    monkeypatch.setenv("XAUTHORITY", str(explicit))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    env = server.BrowserRuntime()._human_display_environment()

    assert env["XAUTHORITY"] == str(explicit)


def test_captcha_error_is_marked_for_the_mcp_client(monkeypatch):
    async def fake_search(query, limit, hl="ja", gl="jp"):
        raise server.CaptchaRequired("Google CAPTCHA detected")

    monkeypatch.setattr(server, "_search_google", fake_search)
    content = run_async(_call_tool("google_search", {"query": "test", "limit": 1}))
    payload = json.loads(content[0].text)

    assert payload == {"success": False, "error": "Google CAPTCHA detected", "captcha_required": True}


def test_expose_for_human_starts_shadow_and_attach(monkeypatch):
    calls = []
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/xpra")
    monkeypatch.setattr(server.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)) or FakeProcess())
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda command, **kwargs: type("Result", (), {"stdout": "LIVE session at :77"})(),
    )
    runtime = server.BrowserRuntime()
    runtime.display = ":77"
    runtime.user_display = ":0"

    runtime.expose_for_human()

    assert calls[0][0][:3] == ["/usr/bin/xpra", "shadow", ":77"]
    assert calls[1][0][:3] == ["/usr/bin/xpra", "attach", ":77"]
    assert calls[1][1]["env"]["DISPLAY"] == ":0"


def test_call_tool_fetch_url_validates_and_delegates(monkeypatch):
    captured = {}

    async def fake_fetch(url, char_limit, format="markdown"):
        captured["url"] = url
        captured["char_limit"] = char_limit
        captured["format"] = format
        return {"url": url, "title": "Example", "text": "hello", "truncated": False}

    monkeypatch.setattr(server, "_fetch_page", fake_fetch)
    content = run_async(_call_tool("fetch_url", {"url": "https://example.com", "char_limit": 500}))
    payload = json.loads(content[0].text)
    assert captured == {"url": "https://example.com", "char_limit": 500, "format": "markdown"}
    assert payload["success"] is True
    assert payload["data"]["title"] == "Example"


def test_call_tool_fetch_url_rejects_bad_args():
    payload = json.loads(
        run_async(_call_tool("fetch_url", {"url": ""}))[0].text
    )
    assert payload["success"] is False
    assert "url is required" in payload["error"]
    payload2 = json.loads(
        run_async(_call_tool("fetch_url", {"url": "https://example.com", "char_limit": 10}))[0].text
    )
    assert payload2["success"] is False


def test_normalizes_direct_and_legacy_google_result_links():
    direct = "https://example.com/article"
    legacy = "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Farticle&sa=U"
    assert server._normalize_candidate_href(direct) == direct
    assert server._normalize_candidate_href(legacy) == direct


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://10.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com/?token=sk-abcdefghijklmnopqrstuvwxyz",
        "file:///etc/passwd",
    ],
)
def test_rejects_non_public_or_secret_result_urls(url):
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_public_url(url)


def test_call_tool_returns_single_structured_json_layer(monkeypatch):
    async def fake_search(query, limit, hl="ja", gl="jp"):
        assert query == "Hermes Agent"
        assert limit == 2
        return [{"title": "Hermes", "url": "https://example.com/", "description": "Agent", "position": 1}], 0.0, None

    monkeypatch.setattr(server, "_search_google", fake_search)
    content = run_async(_call_tool("google_search", {"query": "Hermes Agent", "limit": 2}))
    payload = json.loads(content[0].text)
    assert payload == {
        "success": True,
        "data": {"web": [{"title": "Hermes", "url": "https://example.com/", "description": "Agent", "position": 1}], "waited_ms": 0, "pace_warning": None},
    }


def test_build_results_deduplicates_and_honors_limit():
    candidates = [
        {"title": "One", "href": "https://one.example/a", "description": "first"},
        {"title": "One duplicate", "href": "https://one.example/a", "description": "duplicate"},
        {"title": "Two", "href": "https://two.example/b", "description": "second"},
        {"title": "Three", "href": "https://three.example/c", "description": "third"},
    ]

    async def resolve(href):
        return href

    results = asyncio.run(server._build_results(candidates, 2, resolve_url=resolve))
    assert results == [
        {"title": "One", "url": "https://one.example/a", "description": "first", "position": 1},
        {"title": "Two", "url": "https://two.example/b", "description": "second", "position": 2},
    ]


def test_fetch_page_runs_concurrent_calls_on_separate_tabs(monkeypatch):
    navigated = []
    in_section = {"active": 0, "max": 0}

    async def fake_ensure():
        return "ws://127.0.0.1:1/devtools/browser/x"

    class FakeConn:
        async def send(self, msg):
            pass

        async def recv(self):
            return '{"id": 1, "result": {}}'

        async def close(self):
            pass

    async def fake_connect(*args, **kwargs):
        return FakeConn()

    async def fake_cdp(conn, method, params=None):
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "fetch-tab"}}
        if method == "Target.createTarget":
            return {"targetId": f"fetch-tab-{len(navigated)}"}
        if method == "Target.closeTarget":
            return {}
        if method == "Page.navigate":
            url = (params or {}).get("url", "")
            navigated.append(url)
            nav_by_conn[id(conn)] = url
            in_section["active"] += 1
            in_section["max"] = max(in_section["max"], in_section["active"])
            await asyncio.sleep(0.05)
            in_section["active"] -= 1
            return {}
        return {}

    nav_by_conn: dict = {}

    async def fake_evaluate(conn, expr):
        if "location.href" in expr:
            return nav_by_conn.get(id(conn), "https://example.com/")
        if "outerHTML" in expr:
            return "<html><head><title>t</title></head><body><p>hello</p></body></html>"
        if "clone" in expr:  # _FETCH_TEXT_JS body clone
            return '{"title": "t", "text": "hello"}'
        if "document.title" in expr:
            return "t"
        return "[]"

    async def fake_wait(conn):
        await asyncio.sleep(0.02)

    monkeypatch.setattr(server, "_RUNTIME", server.BrowserRuntime())
    server._RUNTIME.fetch_target_id = "fetch-tab"
    server._RUNTIME.port = 1
    async def fake_to_thread(fn, *a, **k):
        res = fn(*a, **k)
        if asyncio.iscoroutine(res):
            return await res
        return res
    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(server._RUNTIME, "ensure", fake_ensure)
    monkeypatch.setattr(server.websockets, "connect", fake_connect)
    monkeypatch.setattr(server, "_cdp_call", fake_cdp)
    monkeypatch.setattr(server, "_evaluate", fake_evaluate)
    monkeypatch.setattr(server, "_read_page_string", fake_evaluate)
    monkeypatch.setattr(server, "_wait_ready", fake_wait)
    monkeypatch.setattr(server, "_validate_public_url", lambda u: u)

    async def run_two():
        return await asyncio.gather(
            server._fetch_page("https://one.example/a", 500),
            server._fetch_page("https://two.example/b", 500),
        )

    first, second = asyncio.run(run_two())
    assert {first["final_url"], second["final_url"]} == {"https://one.example/a", "https://two.example/b"}
    assert first["requested_url"] == "https://one.example/a"
    assert first["redirected"] is False
    assert first["total_chars"] == len("hello")
    assert in_section["max"] == 2


def test_display_mode_defaults_to_xephyr(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.delenv("CW_DISPLAY_MODE", raising=False)
    monkeypatch.setattr(server, "CONFIG", dict(server._CONFIG_DEFAULTS))
    assert server.BrowserRuntime().display_mode == "xephyr"


def test_show_browser_false_selects_xvfb(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.delenv("CW_DISPLAY_MODE", raising=False)
    config = dict(server._CONFIG_DEFAULTS)
    config["show_browser"] = False
    monkeypatch.setattr(server, "CONFIG", config)
    assert server.BrowserRuntime().display_mode == "xvfb"


def test_xephyr_mode_requires_user_display(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.setenv("CW_DISPLAY_MODE", "xephyr")
    runtime = server.BrowserRuntime()
    runtime.user_display = None
    try:
        runtime.ensure()
    except RuntimeError as exc:
        assert "xephyr" in str(exc).lower()
    else:
        raise AssertionError("xephyr without DISPLAY should fail")


def test_xpra_expose_is_opt_in(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.delenv("CW_XPRA_EXPOSE", raising=False)
    assert server._xpra_expose_enabled() is False
    monkeypatch.setenv("CW_XPRA_EXPOSE", "1")
    assert server._xpra_expose_enabled() is True


@pytest.mark.live
def test_live_google_search_returns_real_external_results():
    try:
        results, waited_ms, pace = asyncio.run(server._search_google("Hermes Agent Nous Research", 3))
    except server.CaptchaRequired:
        pytest.skip("Google requires human CAPTCHA; challenge handling is tested separately")
    finally:
        server._RUNTIME.cleanup()
    assert isinstance(waited_ms, float)
    assert len(results) == 3
    assert [item["position"] for item in results] == [1, 2, 3]
    assert all(item["title"] for item in results)
    assert all(item["url"].startswith(("http://", "https://")) for item in results)
    assert all("google.com/goto" not in item["url"] for item in results)
    assert any("hermes-agent.nousresearch.com" in item["url"] for item in results)


def test_smart_cut_keeps_short_text():
    cut, truncated = server._smart_cut("hello", 100)
    assert (cut, truncated) == ("hello", False)


def test_smart_cut_prefers_sentence_boundary():
    cut, truncated = server._smart_cut("First sentence. Second sentence here", 20)
    assert truncated is True
    assert cut == "First sentence."


def test_pace_warning_fires_after_burst():
    server._SEARCH_TIMES.clear()
    for _ in range(14):
        assert server._pace_warning(server._note_search_start()) is None
    assert "slow down" in (server._pace_warning(server._note_search_start()) or "")
    assert server._peek_search_count() == 15
    server._SEARCH_TIMES.clear()


def test_call_tool_google_search_rejects_bad_hl_gl():
    payload = json.loads(
        run_async(_call_tool("google_search", {"query": "x", "hl": "!!"}))[0].text
    )
    assert payload["success"] is False
    assert "hl" in payload["error"]


def test_call_tool_fetch_url_rejects_bad_format():
    payload = json.loads(
        run_async(_call_tool("fetch_url", {"url": "https://example.com", "format": "pdf"}))[0].text
    )
    assert payload["success"] is False
    assert "format" in payload["error"]


def test_health_check_reports_status(monkeypatch):
    monkeypatch.setattr(server, "_LAST_CAPTCHA_TS", None)
    monkeypatch.setattr(server, "CONFIG", dict(server._CONFIG_DEFAULTS))
    monkeypatch.setattr(server._RUNTIME, "display_mode", "xvfb")
    payload = json.loads(run_async(_call_tool("health_check", {}))[0].text)
    assert payload["success"] is True
    data = payload["data"]
    assert data["display_mode"] == "xvfb"
    assert isinstance(data["chrome_alive"], bool)
    assert data["last_captcha_at"] is None
    assert data["rate_limit_min_delay_s"] == 1.0
    assert data["rate_limit_max_delay_s"] == 2.5


def test_rate_limiter_honors_env_delays(monkeypatch):
    monkeypatch.setenv("CW_MIN_DELAY", "0.1")
    monkeypatch.setenv("CW_MAX_DELAY", "0.2")
    limiter = server.SharedSearchRateLimiter()
    assert limiter.min_delay == 0.1
    assert limiter.max_delay == 0.2
