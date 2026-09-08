"""Run inside the built image; see the Docker job in verify.yml."""
import asyncio
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import websockets
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    assert os.geteuid() != 0, "The image must run as a non-root user"
    parent_display = None
    with tempfile.TemporaryDirectory() as temp:
        profile = Path(temp) / "AI Agent"
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Preferences").write_text(json.dumps({"profile": {"name": "AI Agent"}}))
        os.environ["CW_PROFILE_DIR"] = str(profile)
        os.environ["CW_RATE_LIMIT_DB"] = str(Path(temp) / "rate.sqlite3")
        os.environ["CW_DISPLAY_MODE"] = "xvfb"
        try:
            if "--xephyr" in sys.argv:
                # An authenticated parent X server keeps the visible-mode test
                # independent of the CI host desktop and its real auth cookie.
                auth = Path(temp) / ".Xauthority"
                fields = [b"", b"99", b"MIT-MAGIC-COOKIE-1", os.urandom(16)]
                auth.write_bytes(struct.pack("!H", 65535) + b"".join(struct.pack("!H", len(x)) + x for x in fields))
                auth.chmod(0o600)
                os.environ.update(DISPLAY=":99", XAUTHORITY=str(auth), CW_DISPLAY_MODE="xephyr")
                parent_display = subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1365x900x24", "-nolisten", "tcp", "-auth", str(auth)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                for _ in range(50):
                    ready = subprocess.run(["xdpyinfo", "-display", ":99"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                    if ready.returncode == 0:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Authenticated parent X display did not start")

            from chrome_web_mcp import server

            runtime = server._RUNTIME
            try:
                ws = await asyncio.to_thread(runtime.ensure)
                assert runtime.chrome is not None
                assert "--no-sandbox" not in runtime.chrome.args
                assert server._SEARCH_LIMITER.reserve_slot() >= 0
                async with websockets.connect(ws) as browser:
                    target = await server._cdp_call(browser, "Target.createTarget", {"url": "chrome://sandbox"})
                    async with websockets.connect(f"ws://127.0.0.1:{runtime.port}/devtools/page/{target['targetId']}") as page:
                        await server._wait_ready(page)
                        status = await server._evaluate(page, "document.body.innerText")
                        for expected in ("Layer 1 Sandbox\tNamespace", "PID namespaces\tYes", "Network namespaces\tYes", "Seccomp-BPF sandbox\tYes"):
                            assert expected in status, status
                        await server._cdp_call(page, "Page.navigate", {"url": "data:text/html,<body><script>document.body.innerText='rendered fixture'</script>"})
                        await server._wait_ready(page)
                        assert await server._evaluate(page, "document.body.innerText") == "rendered fixture"
                children = [proc for proc in (runtime.chrome, runtime.xvfb, runtime.xephyr) if proc is not None]
            finally:
                runtime.cleanup()
            assert all(proc.poll() is not None for proc in children)

            params = StdioServerParameters(command="chrome-web-mcp", env=dict(os.environ))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    assert {tool.name for tool in (await client.list_tools()).tools} == {"google_search", "fetch_url", "health_check"}
                    result = await client.call_tool("health_check", {})
                    assert json.loads(result.content[0].text)["success"] is True
            print(f"Docker smoke passed: uid={os.geteuid()}, mode={runtime.display_mode}, sandbox active, JS rendered, MCP ready, children stopped")
        finally:
            if parent_display is not None:
                parent_display.terminate()
                parent_display.wait(timeout=5)


asyncio.run(main())
