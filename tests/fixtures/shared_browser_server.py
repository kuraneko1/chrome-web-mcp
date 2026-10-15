"""Stdio MCP server used by the shared-browser two-process integration test.

This is the real ``chrome_web_mcp.server`` with one test-only shim: a navigation
to the fake public fixture URL is fulfilled locally through CDP's Fetch domain,
so two processes can drive one real Chrome without any Internet access. The
tripwire HTTP server the test also starts must never be contacted - it only
proves the network boundary still holds while the browser is shared.

The pattern (a local tripwire plus CDP ``Fetch.fulfillRequest``) is the one
``tests/test_browser_security.py`` already uses in-process; this file lifts it
into a subprocess so the owner/attach path can be exercised for real.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import chrome_web_mcp.server as server

FIXTURE_URL = os.environ["CW_TEST_FIXTURE_URL"]
FIXTURE_BODY = os.environ.get(
    "CW_TEST_FIXTURE_BODY", "<html><body><p>shared tripwire fetch</p></body></html>"
)

_original_cdp_call = server._cdp_call


async def _intercept(connection, method, params=None):
    if method != "Page.navigate" or (params or {}).get("url") != FIXTURE_URL:
        return await _original_cdp_call(connection, method, params)
    await _original_cdp_call(
        connection,
        "Fetch.enable",
        {"patterns": [{"urlPattern": FIXTURE_URL, "requestStage": "Request"}]},
    )
    request_id = next(server._CDP_IDS)
    await connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        event = json.loads(await asyncio.wait_for(connection.recv(), 15))
        if event.get("method") == "Fetch.requestPaused":
            break
    await _original_cdp_call(
        connection,
        "Fetch.fulfillRequest",
        {
            "requestId": event["params"]["requestId"],
            "responseCode": 200,
            "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
            "body": base64.b64encode(FIXTURE_BODY.encode()).decode(),
        },
    )
    await _original_cdp_call(connection, "Fetch.disable")
    return {}


server._cdp_call = _intercept
server.run()
