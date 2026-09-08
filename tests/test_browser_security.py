"""Real Chrome regressions with synthetic public responses and local tripwires."""
import asyncio
import base64
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chrome_web_mcp import server

PUBLIC_FIXTURE = "https://93.184.215.14/fixture"


@pytest.fixture
def browser_fixture(tmp_path, monkeypatch):
    if server.platform_runtime.platform_key() == "linux":
        if not shutil.which("Xvfb") or not shutil.which("xdpyinfo"):
            pytest.skip("Xvfb and xdpyinfo required on Linux")
        display_mode = "xvfb"
    else:
        display_mode = "headless"
    try:
        server.BrowserRuntime._chrome_executable()
    except RuntimeError:
        pytest.skip("Chrome required")
    profile = tmp_path / "profile"
    monkeypatch.setenv("CW_PROFILE_DIR", str(profile))
    monkeypatch.setenv("CW_DISPLAY_MODE", display_mode)
    monkeypatch.setattr(server, "PROFILE_DIR", profile)
    monkeypatch.setattr(server, "LOCK_PATH", profile / ".instance.lock")
    monkeypatch.setattr(server, "DEVTOOLS_FILE", profile / "DevToolsActivePort")
    runtime = server.BrowserRuntime()
    monkeypatch.setattr(server, "_RUNTIME", runtime)
    monkeypatch.setattr(server, "_ENSURE_LOCK", asyncio.Lock())
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>Local tripwire</body></html>")
        def log_message(self, *args): pass
    local = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=local.serve_forever, daemon=True).start()
    original = server._cdp_call
    response = {"html": "", "redirect": None}
    async def intercept(conn, method, params=None):
        if method != "Page.navigate" or params.get("url") != PUBLIC_FIXTURE:
            return await original(conn, method, params)
        await original(conn, "Fetch.enable", {"patterns": [{"urlPattern": PUBLIC_FIXTURE, "requestStage": "Request"}]})
        request_id = next(server._CDP_IDS)
        await conn.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            event = json.loads(await asyncio.wait_for(conn.recv(), 15))
            if event.get("method") == "Fetch.requestPaused":
                break
        reply = {
            "requestId": event["params"]["requestId"], "responseCode": 200,
            "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
            "body": base64.b64encode(response["html"].encode()).decode(),
        }
        if response["redirect"]:
            reply["responseCode"] = 302
            reply["responseHeaders"].append({"name": "Location", "value": response["redirect"]})
        await original(conn, "Fetch.fulfillRequest", reply)
        await original(conn, "Fetch.disable")
        return {}
    monkeypatch.setattr(server, "_cdp_call", intercept)
    try:
        yield response, hits, f"http://127.0.0.1:{local.server_port}"
    finally:
        runtime.cleanup()
        local.shutdown()
        local.server_close()


def test_redirect_never_reaches_private_server(browser_fixture):
    response, hits, local = browser_fixture
    response["redirect"] = local + "/redirect"
    async def run():
        with pytest.raises((ValueError, RuntimeError, TimeoutError)):
            await server._fetch_page(PUBLIC_FIXTURE, 500, "markdown")
    asyncio.run(run())
    assert hits == []


def test_private_subresources_and_websockets_never_connect(browser_fixture):
    response, hits, local = browser_fixture
    response["html"] = f'''<html><body><p>Public fixture content.</p>
      <img src="{local}/image"><iframe src="{local}/frame"></iframe>
      <script>fetch("{local}/fetch").catch(()=>{{}});
      new WebSocket("{local.replace('http:', 'ws:')}/socket");</script></body></html>'''
    result = asyncio.run(server._fetch_page(PUBLIC_FIXTURE, 500, "markdown"))
    assert "Public fixture" in result["markdown"]
    assert hits == []


def test_large_html_with_short_body_is_extracted(browser_fixture):
    response, _, _ = browser_fixture
    response["html"] = '<html><head><script>/*' + ('x' * 2200000) + '*/</script></head><body><p>Small readable body.</p></body></html>'
    result = asyncio.run(server._fetch_page(PUBLIC_FIXTURE, 500, "markdown"))
    assert "Small readable body." in result["markdown"]


def test_chunk_boundaries_preserve_unicode(browser_fixture):
    response, _, _ = browser_fixture
    body = "x" * 65535 + "😀日本語" + "y" * 65536
    response["html"] = '<html><body><p>' + body + '</p></body></html>'
    result = asyncio.run(server._fetch_page(PUBLIC_FIXTURE, 200000, "text"))
    assert result["text"] == body
