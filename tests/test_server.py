import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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


class _FakeProxy:
    """Stands in for PublicNetworkProxy when only its URL is needed."""

    url = "http://127.0.0.1:12345"


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


def test_google_results_ask_the_structural_probe_instead_of_the_body(monkeypatch):
    """F1: challenge detection must not scan ordinary result text."""
    expressions = []

    async def fake_evaluate(conn, expr):
        expressions.append(expr)
        if expr == "location.href":
            return "https://www.google.com/search?q=captcha"
        return ""

    async def fake_read(conn, expr):
        return "[]"

    monkeypatch.setattr(server, "_evaluate", fake_evaluate)
    monkeypatch.setattr(server, "_read_page_string", fake_read)

    assert run_async(server._read_google_results(object())) == []
    assert server._PAGE_CHALLENGE_PROBE_JS in expressions
    assert "document.body?.innerText || ''" not in expressions


def test_structural_probe_hit_raises_captcha_required(monkeypatch):
    async def fake_evaluate(conn, expr):
        if expr == "location.href":
            return "https://www.google.com/search?q=x"
        return "Our systems have detected unusual traffic from your computer network"

    monkeypatch.setattr(server, "_evaluate", fake_evaluate)

    with pytest.raises(server.CaptchaRequired):
        run_async(server._read_google_results(object()))


def test_engine_challenge_check_uses_the_structural_probe(monkeypatch):
    """F1: fallback engines must not classify their rendered body text either."""
    expressions = []
    blocked_args = []

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
        return {}

    async def fake_evaluate(conn, expr):
        expressions.append(expr)
        return "" if expr == server._PAGE_CHALLENGE_PROBE_JS else "not a robot"

    async def fake_search_page(browser):
        return FakeConn(), "tab", False

    async def fake_wait(conn):
        return None

    async def fake_to_thread(fn, *a, **k):
        result = fn(*a, **k)
        if asyncio.iscoroutine(result):
            return await result
        return result

    runtime = server.BrowserRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    monkeypatch.setattr(runtime, "ensure", lambda: "ws://127.0.0.1:1/devtools/browser/x")
    monkeypatch.setattr(server.websockets, "connect", fake_connect)
    monkeypatch.setattr(server, "_cdp_call", fake_cdp)
    monkeypatch.setattr(server, "_evaluate", fake_evaluate)
    monkeypatch.setattr(server, "_wait_ready", fake_wait)
    monkeypatch.setattr(server, "_ensure_search_page", fake_search_page)
    monkeypatch.setattr(server, "_read_page_html", lambda page: _async_value("<html></html>"))
    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        server.engines, "looks_blocked",
        lambda engine, challenge_text: blocked_args.append(challenge_text) or False,
    )
    monkeypatch.setattr(server.engines, "parse_results", lambda engine, html, limit: [])

    assert run_async(server._fetch_engine_candidates("ddg", "q", 5, "ja", "jp")) == []
    assert server._PAGE_CHALLENGE_PROBE_JS in expressions
    assert "document.body?.innerText || ''" not in expressions
    assert blocked_args == [""]


async def _async_value(value):
    return value


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


def test_captcha_error_is_marked_for_the_mcp_client(monkeypatch, tmp_path):
    """Google-only config: a CAPTCHA still surfaces captcha_required=True."""
    async def fake_google(query, limit, hl="ja", gl="jp"):
        raise server.CaptchaRequired("Google CAPTCHA detected")

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_ENGINE_STATE", server.SearchEngineState(tmp_path / "state.sqlite3"))
    monkeypatch.setitem(server.CONFIG, "search_engines", ["google"])
    content = run_async(_call_tool("google_search", {"query": "test", "limit": 1}))
    payload = json.loads(content[0].text)

    assert payload["success"] is False
    assert payload["captcha_required"] is True
    assert "captcha" in payload["error"].lower()
    assert payload["attempts"] == [
        {"engine": "google", "ok": False, "error": "captcha",
         "cooldown_s": 300.0, "detail": "Google CAPTCHA detected"}
    ]


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


@pytest.mark.parametrize(
    "value",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "gho_0123456789abcdefghijklmnopqrstuvwxyz",
        "ghs_0123456789abcdefghijklmnopqrstuvwxyz",
        "ghu_0123456789abcdefghijklmnopqrstuvwxyz",
        "ghr_0123456789abcdefghijklmnopqrstuvwxyz",
        "ya29.a0AfH6SMBx-0123456789abcdef",
        "sk_live_0123456789abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
    ],
)
def test_secret_gate_covers_new_credential_shapes(value):
    """F2: the extended pattern must block these in URLs and in search queries."""
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_public_url(f"https://example.com/?x={value}")
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_search_query(f"look this up: {value}")


def test_secret_gate_checks_percent_decoded_values():
    encoded = "https://example.com/?token=sk-abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_public_url(encoded.replace("sk-", "sk%2D"))
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_search_query("query%3Dsk-abcdefghijklmnopqrstuvwxyz")


def test_secret_gate_uses_own_environment_values_as_needles(monkeypatch):
    """F2: a private token with no vendor prefix is caught via the process env."""
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "private-value-0123456789")
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_search_query("search for private-value-0123456789")
    with pytest.raises(ValueError, match="Blocked"):
        server._validate_public_url("https://example.com/?q=private-value-0123456789")

    # A short value, or one whose variable name is not credential-like, is not a needle.
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "short")
    monkeypatch.setenv("INTERNAL_SERVICE_LABEL", "private-value-0123456789")
    server._validate_search_query("search for private-value-0123456789")


def test_secret_gate_never_echoes_the_offending_value(monkeypatch):
    secret = "AKIAIOSFODNN7EXAMPLE"
    with pytest.raises(ValueError) as exc:
        server._validate_public_url(f"https://example.com/?k={secret}")
    assert secret not in str(exc.value)
    with pytest.raises(ValueError) as exc:
        server._validate_search_query(secret)
    assert secret not in str(exc.value)


def test_google_search_tool_blocks_queries_that_carry_secrets(monkeypatch, tmp_path):
    """F2: the gate runs before any engine is asked, so nothing leaves the process."""
    called = []

    async def fake_google(query, limit, hl="ja", gl="jp"):
        called.append(query)
        return []

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_ENGINE_STATE", server.SearchEngineState(tmp_path / "state.sqlite3"))
    monkeypatch.setitem(server.CONFIG, "search_engines", ["google"])
    payload = json.loads(
        run_async(_call_tool("google_search", {"query": "AKIAIOSFODNN7EXAMPLE"}))[0].text
    )
    assert payload["success"] is False
    assert "credential" in payload["error"]
    assert "AKIA" not in payload["error"]
    assert called == []


def test_call_tool_returns_single_structured_json_layer(monkeypatch, tmp_path):
    async def fake_google(query, limit, hl="ja", gl="jp"):
        assert query == "Hermes Agent"
        assert limit == 2
        return [{"title": "Hermes", "href": "https://example.com/", "description": "Agent"}]

    async def fake_resolve(href):
        return href

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_resolve_candidate_url", fake_resolve)
    monkeypatch.setattr(server, "_ENGINE_STATE", server.SearchEngineState(tmp_path / "state.sqlite3"))
    monkeypatch.setitem(server.CONFIG, "search_engines", ["google"])
    content = run_async(_call_tool("google_search", {"query": "Hermes Agent", "limit": 2}))
    payload = json.loads(content[0].text)
    assert payload["success"] is True
    assert payload["data"]["web"] == [
        {"title": "Hermes", "url": "https://example.com/", "description": "Agent", "position": 1}
    ]
    assert payload["data"]["engine"] == "google"
    assert payload["data"]["captcha_avoided"] is False
    assert payload["data"]["waited_ms"] >= 0
    assert payload["data"]["attempts"] == [{"engine": "google", "ok": True, "results": 1}]


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


def test_download_policy_is_denied_on_the_browser_session(monkeypatch):
    """F3: every browser-level CDP session must refuse downloads."""
    calls = []

    async def fake_cdp(conn, method, params=None):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(server, "_cdp_call", fake_cdp)

    run_async(server._deny_downloads(object()))

    assert calls == [
        ("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": False})
    ]


def test_unsupported_download_policy_is_not_fatal(monkeypatch):
    """An older Chrome that lacks the method must not break fetch_url."""
    async def fake_cdp(conn, method, params=None):
        raise RuntimeError("CDP failed: 'Browser.setDownloadBehavior' wasn't found")

    monkeypatch.setattr(server, "_cdp_call", fake_cdp)

    run_async(server._deny_downloads(object()))


def test_fetch_page_sets_the_download_policy_before_navigating(monkeypatch):
    methods = []

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
        methods.append(method)
        if method == "Target.createTarget":
            return {"targetId": "fetch-tab"}
        return {}

    async def fake_evaluate(conn, expr):
        if "location.href" in expr:
            return "https://example.com/"
        if "clone" in expr:
            return '{"title": "t", "text": "hello"}'
        return "[]"

    async def fake_wait(conn):
        return None

    async def fake_to_thread(fn, *a, **k):
        result = fn(*a, **k)
        if asyncio.iscoroutine(result):
            return await result
        return result

    runtime = server.BrowserRuntime()
    runtime.port = 1
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    monkeypatch.setattr(server, "_cdp_call", fake_cdp)
    monkeypatch.setattr(server, "_evaluate", fake_evaluate)
    monkeypatch.setattr(server, "_read_page_string", fake_evaluate)
    monkeypatch.setattr(server, "_wait_ready", fake_wait)
    monkeypatch.setattr(server, "_validate_public_url", lambda u: u)
    monkeypatch.setattr(server.websockets, "connect", fake_connect)
    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(runtime, "ensure", fake_ensure)

    result = run_async(server._fetch_page("https://example.com/", 500))

    assert result["text"] == "hello"
    assert methods.index("Browser.setDownloadBehavior") < methods.index("Target.createTarget")


def test_chrome_command_bounds_the_disk_cache(monkeypatch):
    """F3: an unbounded cache is the other half of the download problem."""
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.setattr(server.BrowserRuntime, "_chrome_executable", staticmethod(lambda: "/bin/echo"))
    runtime = server.BrowserRuntime()
    runtime.proxy = _FakeProxy()

    assert "--disk-cache-size=67108864" in runtime._chrome_command()


def _root_runtime(monkeypatch):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.setattr(server.BrowserRuntime, "_chrome_executable", staticmethod(lambda: "/bin/echo"))
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)
    runtime = server.BrowserRuntime()
    runtime.proxy = _FakeProxy()
    return runtime


def test_refuses_to_render_as_root_without_the_opt_in(monkeypatch):
    """F6: --no-sandbox as root would make a renderer escape host root."""
    monkeypatch.delenv("CW_ALLOW_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="CW_ALLOW_ROOT"):
        _root_runtime(monkeypatch)._chrome_command()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_root_opt_in_restores_no_sandbox(monkeypatch, value):
    monkeypatch.setenv("CW_ALLOW_ROOT", value)

    assert "--no-sandbox" in _root_runtime(monkeypatch)._chrome_command()


def test_health_check_reports_running_as_root(monkeypatch):
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)

    payload = json.loads(run_async(_call_tool("health_check", {}))[0].text)

    assert payload["data"]["running_as_root"] is True


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


def test_xephyr_mode_requires_user_display(monkeypatch, tmp_path):
    monkeypatch.setattr(server.platform_runtime, "platform_key", lambda: "linux")
    monkeypatch.setenv("CW_DISPLAY_MODE", "xephyr")
    # Keep the profile lock inside tmp_path so the display check is what fails,
    # not contention with another runtime in this test session.
    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "LOCK_PATH", tmp_path / "profile" / ".instance.lock")
    monkeypatch.setattr(server, "DEVTOOLS_FILE", tmp_path / "profile" / "DevToolsActivePort")
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


class _FakeDisplayProcess:
    """Stands in for Xvfb/Xephyr: already "exited" so teardown never signals a pid."""

    pid = 4242

    def poll(self):
        return 0


def _patch_x_display_seams(monkeypatch, tmp_path, real_xauth, launched):
    """Fake the display server process but let the real xauth write the cookie."""
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(server.shutil, "which", lambda name: real_xauth if name == "xauth" else None)
    monkeypatch.setattr(
        server.BrowserRuntime, "_free_display_number", staticmethod(lambda: "150")
    )
    real_popen = server.subprocess.Popen

    def fake_popen(command, **kwargs):
        if command[0] in ("Xvfb", "Xephyr"):
            launched.append((command, kwargs))
            return _FakeDisplayProcess()
        return real_popen(command, **kwargs)

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    real_run = server.subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == "xdpyinfo":
            return type("Check", (), {"returncode": 0})()
        return real_run(command, **kwargs)

    monkeypatch.setattr(server.subprocess, "run", fake_run)


def test_xvfb_gets_a_private_cookie_file_and_chrome_uses_it(monkeypatch, tmp_path):
    """F5: the display must require our 0600 MIT-MAGIC-COOKIE-1 file."""
    xauth = shutil.which("xauth")
    if not xauth:
        pytest.skip("xauth is required for the X access-control test")
    launched = []
    _patch_x_display_seams(monkeypatch, tmp_path, xauth, launched)

    runtime = server.BrowserRuntime()
    display = runtime._start_xvfb()

    assert display == ":150"
    assert launched[0][0] == ["Xvfb", ":150", "-screen", "0", "1365x900x24", "-nolisten", "tcp", "-auth", runtime.xauth_file]
    auth_path = Path(runtime.xauth_file)
    assert auth_path.exists()
    assert (auth_path.stat().st_mode & 0o777) == 0o600
    listing = subprocess.run([xauth, "-f", str(auth_path), "list"], capture_output=True, text=True)
    assert "MIT-MAGIC-COOKIE-1" in listing.stdout
    assert runtime._browser_environment(display, runtime.xauth_file)["XAUTHORITY"] == runtime.xauth_file

    runtime.cleanup()
    assert not auth_path.exists(), "the cookie file must be removed with the display"


def test_start_xvfb_fails_closed_without_xauth(monkeypatch, tmp_path):
    """F5: no cookie available means no display, never an open one."""
    launched = []
    _patch_x_display_seams(monkeypatch, tmp_path, None, launched)

    with pytest.raises(RuntimeError, match="xauth"):
        server.BrowserRuntime()._start_xvfb()

    assert launched == [], "no display server may start without an auth cookie"


def test_private_display_socket_is_not_open_to_other_users():
    """F5 regression guard: the real Xvfb must refuse a client without the cookie.

    This runs the actual Xvfb/xauth pair when a display can be allocated, but it
    never touches the network and is skipped when the tools are unavailable.
    """
    if not (shutil.which("Xvfb") and shutil.which("xdpyinfo") and shutil.which("xauth")):
        pytest.skip("Xvfb, xdpyinfo and xauth are required")
    runtime = server.BrowserRuntime()
    if server.platform_runtime.platform_key() != "linux":
        pytest.skip("private X display is Linux-only")
    try:
        display = runtime._start_xvfb()
    except RuntimeError as exc:
        pytest.skip(f"cannot allocate a private display here: {exc}")
    try:
        authorized = subprocess.run(
            ["xdpyinfo", "-display", display],
            env=runtime._browser_environment(display, runtime.xauth_file),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        anonymous_env = dict(os.environ)
        anonymous_env.pop("XAUTHORITY", None)
        anonymous = subprocess.run(
            ["xdpyinfo", "-display", display],
            env=anonymous_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        assert authorized.returncode == 0
        assert anonymous.returncode != 0, "the private display must not accept cookie-less clients"
    finally:
        runtime.cleanup()


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


def test_markdown_fallback_accumulates_length_linearly():
    """F10: the DOM-walk fallback must not re-join the parts every iteration."""
    script = server._FETCH_MD_JS
    loop = script[script.index("for (const el of"):script.index("let markdown = parts.join")]

    assert "parts.join" not in loop
    assert "total +=" in loop
    assert "if (total > 300000) break;" in loop


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


def test_call_tool_tracks_browser_calls_for_idle_release(monkeypatch):
    runtime = server.BrowserRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    seen = {}

    async def fake_fetch(url, char_limit, format="markdown"):
        seen["active_calls"] = runtime.active_calls
        return {"url": url, "title": "t", "text": "x", "truncated": False}

    monkeypatch.setattr(server, "_fetch_page", fake_fetch)
    run_async(_call_tool("fetch_url", {"url": "https://example.com"}))

    assert seen["active_calls"] == 1
    assert runtime.active_calls == 0
    assert runtime.last_used is not None


def test_health_check_does_not_count_as_browser_use(monkeypatch):
    runtime = server.BrowserRuntime()
    runtime.last_used = 123.0
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    monkeypatch.setattr(server, "_LAST_CAPTCHA_TS", None)

    run_async(_call_tool("health_check", {}))

    assert runtime.active_calls == 0
    assert runtime.last_used == 123.0


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
