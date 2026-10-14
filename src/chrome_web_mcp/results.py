"""Search-result shaping shared by the Google and fallback paths.

This module owns ``_normalize_candidates``, which decodes DuckDuckGo/Bing redirect
wrappers into direct hrefs before validation, and ``_build_results``, which turns
raw engine candidates into the numbered result payload (title/description
trimming, URL resolution through an injected resolver, de-duplication, limit).
Resolution is injected so the caller keeps its patchable ``_resolve_candidate_url``
and this module stays free of server state.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import httpx

from chrome_web_mcp import engines


async def _build_results(
    candidates: list[dict],
    limit: int,
    *,
    resolve_url: Callable[[str], Awaitable[str | None]],
) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        title = " ".join(str(candidate.get("title", "")).split())
        href = str(candidate.get("href", ""))
        description = " ".join(str(candidate.get("description", "")).split())
        if not title or not href:
            continue
        try:
            url = await resolve_url(href)
        except (ValueError, OSError, httpx.HTTPError):
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": title[:300],
                "url": url,
                "description": description[:1000],
                "position": len(results) + 1,
            }
        )
        if len(results) >= limit:
            break
    return results


def _normalize_candidates(engine: str, candidates: list[dict]) -> list[dict]:
    """Decode engine redirect wrappers before the shared URL validation runs.

    Google result wrappers are resolved later by ``_resolve_candidate_url`` (it
    also follows ``/goto``-style links), but DuckDuckGo and Bing return
    ``//duckduckgo.com/l/?uddg=`` and ``bing.com/ck/a?...u=a1`` links that must be
    decoded first - otherwise their scheme-less / engine-host URLs are dropped as
    unusable and every fallback search looks empty.
    """
    if engine == "google":
        return candidates
    normalized: list[dict] = []
    for candidate in candidates:
        href = engines.normalize_href(engine, str(candidate.get("href", "")))
        if not href:
            continue
        item = dict(candidate)
        item["href"] = href
        normalized.append(item)
    return normalized
