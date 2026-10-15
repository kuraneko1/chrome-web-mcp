"""Low-level Chrome DevTools Protocol page driving.

This module owns the CDP primitives used by the search and fetch paths:
``_cdp_call`` frames requests against the shared ``_CDP_IDS`` counter and awaits
their replies, ``_evaluate``/``_read_page_string`` evaluate JavaScript (the latter
chunking large snapshots under ``MAX_EXTRACTION_CHARS``), ``_wait_ready`` polls
until a document settles, and ``_click_google_consent`` / ``_type_google_query`` /
``_read_page_html`` drive Google and fallback-engine pages. Browser lifecycle, GPU
diagnostics, CAPTCHA bookkeeping and all orchestration call sites stay in
``server``, which re-exports these names for patching.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import time
from typing import Any

from chrome_web_mcp import engines
from chrome_web_mcp.errors import CaptchaRequired
from chrome_web_mcp.extract import MAX_EXTRACTION_CHARS


_CDP_IDS = itertools.count(1)


async def _cdp_call(connection: Any, method: str, params: dict | None = None) -> dict:
    request_id = next(_CDP_IDS)
    await connection.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    while True:
        raw = await asyncio.wait_for(connection.recv(), timeout=25)
        message = json.loads(raw)
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})


async def _evaluate(connection: Any, expression: str) -> Any:
    result = await _cdp_call(
        connection,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    remote = result.get("result", {})
    if remote.get("subtype") == "error" or "exceptionDetails" in result:
        raise RuntimeError("JavaScript evaluation failed")
    return remote.get("value")


async def _read_page_string(connection: Any, expression: str) -> str:
    """Read a snapshot in bounded CDP messages, including large HTML/text pages."""
    result = await _cdp_call(connection, "Runtime.evaluate", {
        "expression": "({text: String(" + expression + ")})",
        "returnByValue": False,
    })
    object_id = result.get("result", {}).get("objectId")
    if not object_id or "exceptionDetails" in result:
        raise RuntimeError("Could not snapshot rendered content")
    async def call(function: str, arguments: list) -> Any:
        answer = await _cdp_call(connection, "Runtime.callFunctionOn", {
            "objectId": object_id, "functionDeclaration": function,
            "arguments": [{"value": value} for value in arguments],
            "returnByValue": True,
        })
        if "exceptionDetails" in answer:
            raise RuntimeError("Could not read rendered content")
        return answer.get("result", {}).get("value")
    try:
        length = await call("function(){return this.text.length}", [])
        if not isinstance(length, int) or length > MAX_EXTRACTION_CHARS:
            raise RuntimeError("Rendered content exceeds the 16Mi character extraction limit")
        chunks = []
        for start in range(0, length, 65536):
            chunks.append(await call("function(a,b){return this.text.slice(a,b)}", [start, start + 65536]))
        # Chunk boundaries can split a UTF-16 surrogate pair.
        return "".join(chunks).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    finally:
        await _cdp_call(connection, "Runtime.releaseObject", {"objectId": object_id})


async def _wait_ready(connection: Any) -> None:
    deadline = time.monotonic() + 25
    stable = 0
    previous = -1
    while time.monotonic() < deadline:
        try:
            state = await _evaluate(
                connection,
                "JSON.stringify({ready:document.readyState,href:location.href,n:(document.body?.innerText||'').length})",
            )
            parsed = json.loads(state)
            size = int(parsed.get("n", 0))
            stable = stable + 1 if size == previous and size > 0 else 0
            previous = size
            if parsed.get("ready") in {"interactive", "complete"} and stable >= 1:
                return
        except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError("Google page did not finish rendering")


async def _click_google_consent(connection: Any) -> bool:
    clicked = await _evaluate(
        connection,
        """(() => {
          const patterns=[/accept all/i,/i agree/i,/すべて同意/i];
          for(const el of document.querySelectorAll('button,[role=button],input[type=submit]')){
            const text=(el.innerText||el.value||el.textContent||'').trim();
            if(patterns.some(p=>p.test(text))){el.click();return true;}
          }
          return false;
        })()""",
    )
    return bool(clicked)


async def _type_google_query(page: Any, query: str, hl: str, gl: str) -> bool:
    """Open Google, type the query into the search box, press Enter.

    Returns False when the box or the navigation did not work out, so the caller
    can fall back to a direct results URL instead of failing the search.
    """
    try:
        await _cdp_call(
            page, "Page.navigate", {"url": engines.google_home_url(hl, gl)}
        )
        await _wait_ready(page)
        if await _click_google_consent(page):
            await asyncio.sleep(0.8)
            await _wait_ready(page)
        before = str(await _evaluate(page, "location.href"))
        focused = await _evaluate(
            page,
            """(() => {
              for (const sel of ['textarea[name="q"]', 'input[name="q"]', '#APjFqb']) {
                const el = document.querySelector(sel);
                if (el) { el.focus(); el.click(); return true; }
              }
              return false;
            })()""",
        )
        if not focused:
            return False
        await asyncio.sleep(random.uniform(0.25, 0.7))
        # insertText produces the same input events as typing without guessing
        # per-character key codes for non-ASCII queries.
        await _cdp_call(page, "Input.insertText", {"text": query})
        await asyncio.sleep(random.uniform(0.15, 0.45))
        for event in (
            {"type": "rawKeyDown", "key": "Enter", "code": "Enter",
             "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
            {"type": "char", "key": "Enter", "text": "\r"},
            {"type": "keyUp", "key": "Enter", "code": "Enter",
             "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
        ):
            await _cdp_call(page, "Input.dispatchKeyEvent", event)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            href = str(await _evaluate(page, "location.href"))
            if href != before and "/search" in href:
                await _wait_ready(page)
                return True
            await asyncio.sleep(0.25)
        return False
    except CaptchaRequired:
        raise
    except Exception:
        return False


# Cap the rendered-HTML capture used by the non-Google engine parsers. Real
# results pages measure 30KB-700KB; this only guards against pathological pages.
MAX_ENGINE_HTML_CHARS = 1_800_000


async def _read_page_html(page: Any) -> str:
    """Return the rendered HTML of the current page, bounded in size."""
    raw = await _read_page_string(
        page,
        f"document.documentElement.outerHTML.slice(0, {MAX_ENGINE_HTML_CHARS})",
    )
    return raw


# Structural bot-challenge probe (F1). The decision must come from the challenge
# UI itself, not from page text: a normal result page for the query "captcha"
# (or one that ranks a snippet containing "not a robot") would otherwise mark
# the engine as challenged for its whole cooldown. Returns the challenge
# container's own text, or "" when no container is present.
_PAGE_CHALLENGE_PROBE_JS = r"""(() => {
  const sel = 'form[action*="/sorry"], #captcha-form, #challenge-form, '
    + 'div.g-recaptcha, iframe[src*=recaptcha], iframe[src*=challenge], '
    + 'form[action*="challenge"]';
  const node = document.querySelector(sel);
  if (!node) return '';
  return String(node.innerText || node.textContent || node.tagName || 'challenge').slice(0, 2000);
})()"""
