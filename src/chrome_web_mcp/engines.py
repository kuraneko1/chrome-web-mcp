"""Search-engine adapters for the fallback path.

Google remains the primary engine, but it is the only engine that issues an
interactive CAPTCHA. When Google challenges (or is inside a cooldown), the
server asks the next engine in this list for the same query, so a tool call
still returns results instead of failing.

Two families are supported:

* ``browser`` engines are rendered in the shared Chrome tab and parsed from the
  rendered HTML (so JavaScript-only result layouts still work).
* ``http`` engines are called directly with httpx - used for a self-hosted
  SearXNG instance, which lives on a private address the browser proxy
  deliberately refuses to reach.

Parsing lives here in Python rather than in per-engine page JavaScript so every
selector can be unit-tested offline against saved fixtures (``tests/fixtures``).
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Iterable

import lxml.html

# Order matters: the first engine that returns usable results wins.
DEFAULT_ENGINE_ORDER = ("google", "ddg", "brave", "bing")
BROWSER_ENGINES = ("google", "ddg", "brave", "bing")
HTTP_ENGINES = ("searxng",)

# Block pages that mean "this engine will not answer right now".
_BLOCK_PHRASES = (
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    "automated queries",
    "not a robot",
    "captcha",
)

_SPACE_RE = re.compile(r"\s+")


def clean_text(value: str | None) -> str:
    """Collapse whitespace, matching how the existing tools normalize strings."""
    return _SPACE_RE.sub(" ", value or "").strip()


def engine_url(engine: str, query: str, hl: str = "ja", gl: str = "jp",
               count: int | None = None) -> str:
    """Build the results URL for a browser engine.

    ``count`` is a hint for engines that accept one (Google's ``num``); it is
    ignored elsewhere because the other engines' page size is not contractual.
    """
    quoted = urllib.parse.quote_plus(query)
    if engine == "google":
        return "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": query, "num": min(max(int(count or 10), 1), 25), "udm": "14",
             "hl": hl, "gl": gl}
        )
    if engine == "ddg":
        return f"https://html.duckduckgo.com/html/?q={quoted}&kl={gl}-{hl}"
    if engine == "brave":
        return f"https://search.brave.com/search?q={quoted}&source=web"
    if engine == "bing":
        return f"https://www.bing.com/search?q={quoted}&setlang={hl}"
    raise ValueError(f"unsupported browser engine: {engine}")


def google_home_url(hl: str = "ja", gl: str = "jp") -> str:
    return "https://www.google.com/?" + urllib.parse.urlencode({"hl": hl, "gl": gl})


# --------------------------------------------------------------------------- #
# URL normalization: unwrap engine-specific redirects so the caller gets the
# real destination. Anything still pointing at the engine itself is dropped.
# --------------------------------------------------------------------------- #

def _decode_bing_href(href: str) -> str | None:
    """Decode a ``bing.com/ck/a?...&u=a1<base64url>`` redirect wrapper."""
    match = re.search(r"[?&]u=a1([A-Za-z0-9_\-=]+)", href)
    if not match:
        return None
    raw = match.group(1)
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return None


def _decode_ddg_href(href: str) -> str | None:
    """Decode a ``//duckduckgo.com/l/?uddg=<urlencoded>`` redirect wrapper."""
    parsed = urllib.parse.urlparse(href if "//" not in href[:2] else "https:" + href)
    target = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
    if not target:
        return None
    return urllib.parse.unquote(target)


_ENGINE_HOSTS = {
    "ddg": ("duckduckgo.com",),
    "brave": ("brave.com",),
    "bing": ("bing.com", "microsoft.com", "msn.com"),
    "google": ("google.com",),
}


def _is_engine_host(engine: str, host: str | None) -> bool:
    normalized = (host or "").lower().rstrip(".")
    return any(normalized == suffix or normalized.endswith("." + suffix)
               for suffix in _ENGINE_HOSTS.get(engine, ()))


def normalize_href(engine: str, href: str) -> str | None:
    """Return the external destination for a result link, or None.

    Only the wrapper decoding happens here; private/credential-bearing targets
    are still rejected later by the server's public-URL validator.
    """
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if engine in ("ddg", "bing") and _is_engine_host(engine, urllib.parse.urlparse(href).hostname):
        decoded = _decode_ddg_href(href) if engine == "ddg" else _decode_bing_href(href)
        if not decoded:
            return None
        href = decoded.strip()
    try:
        parsed = urllib.parse.urlparse(href)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if _is_engine_host(engine, parsed.hostname):
        return None
    return href


# --------------------------------------------------------------------------- #
# HTML parsing (one function per engine; selectors verified against fixtures)
# --------------------------------------------------------------------------- #

def _parse_html(html_text: str) -> lxml.html.HtmlElement:
    """Parse CDP-provided HTML deterministically as UTF-8.

    The text arrives as a JavaScript string (already decoded), so encoding it
    back to bytes and pinning the parser encoding avoids lxml re-sniffing a
    ``<meta charset>`` and mangling non-ASCII titles.
    """
    parser = lxml.html.HTMLParser(encoding="utf-8", recover=True)
    return lxml.html.fromstring(html_text.encode("utf-8", "replace"), parser=parser)


def _nodes(root: lxml.html.HtmlElement, xpath: str) -> list:
    try:
        return root.xpath(xpath)
    except Exception:
        return []


_DDG_RESULT_XP = "//div[contains(concat(' ',normalize-space(@class),' '),' result ')]"
_BRAVE_RESULT_XP = "//div[@data-type='web']"
_BING_RESULT_XP = "//li[contains(concat(' ',normalize-space(@class),' '),' b_algo ')]"


def _first_href(node) -> str:
    for anchor in node.xpath(".//a[@href]"):
        href = anchor.get("href") or ""
        if href.startswith("http") or href.startswith("//"):
            return href
    return ""


def parse_results(engine: str, html_text: str, limit: int) -> list[dict]:
    """Extract ``{title, href, description}`` candidates from a results page."""
    root = _parse_html(html_text)
    out: list[dict] = []

    if engine == "ddg":
        for block in _nodes(root, _DDG_RESULT_XP):
            title_nodes = block.xpath(".//a[contains(@class,'result__a')]")
            if not title_nodes:
                continue
            anchor = title_nodes[0]
            snippet = block.xpath(".//*[contains(@class,'result__snippet')]")
            out.append({
                "title": clean_text(anchor.text_content()),
                "href": anchor.get("href") or "",
                "description": clean_text(snippet[0].text_content()) if snippet else "",
            })
            if len(out) >= limit:
                break
        return out

    if engine == "brave":
        for block in _nodes(root, _BRAVE_RESULT_XP):
            title_nodes = block.xpath(".//*[contains(@class,'search-snippet-title')]")
            if not title_nodes:
                continue
            body = block.xpath(".//*[contains(@class,'generic-snippet')]")
            out.append({
                "title": clean_text(title_nodes[0].text_content()),
                "href": _first_href(block),
                "description": clean_text(body[0].text_content()) if body else "",
            })
            if len(out) >= limit:
                break
        return out

    if engine == "bing":
        for block in _nodes(root, _BING_RESULT_XP):
            headings = block.xpath("./h2//a[@href]")
            if not headings:
                continue
            caption = block.xpath(".//*[contains(@class,'b_caption')]//p")
            title = clean_text(headings[0].text_content())
            if not title or "{" in title:  # carousel/style blocks leak CSS into text
                continue
            out.append({
                "title": title,
                "href": headings[0].get("href") or "",
                "description": clean_text(caption[0].text_content()) if caption else "",
            })
            if len(out) >= limit:
                break
        return out

    raise ValueError(f"no parser for engine: {engine}")


def parse_searxng(payload: dict, limit: int) -> list[dict]:
    """Convert a SearXNG JSON response into result candidates."""
    out: list[dict] = []
    for item in (payload.get("results") or [])[:limit]:
        title = clean_text(item.get("title"))
        url = (item.get("url") or "").strip()
        if not title or not url:
            continue
        out.append({
            "title": title,
            "href": url,
            "description": clean_text(item.get("content")),
        })
    return out


def looks_blocked(engine: str, page_text: str) -> bool:
    """Report whether a rendered page is a bot challenge rather than results."""
    normalized = " ".join((page_text or "").lower().split())
    if not normalized:
        return False
    return any(phrase in normalized for phrase in _BLOCK_PHRASES)


def available_engines(configured: Iterable[str], searxng_url: str = "") -> list[str]:
    """Filter a configured engine list down to what this deployment can run."""
    valid = set(BROWSER_ENGINES) | set(HTTP_ENGINES)
    out: list[str] = []
    for name in configured:
        candidate = str(name).strip().lower()
        if candidate not in valid or candidate in out:
            continue
        if candidate in HTTP_ENGINES and not searxng_url:
            continue
        out.append(candidate)
    return out
