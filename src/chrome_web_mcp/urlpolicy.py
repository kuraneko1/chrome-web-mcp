"""Public-URL and Google-host policy checks.

This module owns the fail-closed URL validator plus the host/challenge helpers it
and the search layer share: ``_SECRET_RE``/``_BLOCKED_HOSTS`` define the deny
patterns, ``_is_google_host``/``_is_google_challenge`` classify Google pages, and
``_normalize_candidate_href`` unwraps direct/legacy Google result links.
``_validate_public_url`` and ``_validate_search_query`` apply the same
credential gate to fetch targets and to search queries. It has no dependencies
on server state, so it can be imported freely; ``server`` re-exports the names
that its call sites and tests use.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.parse
from typing import Mapping


# Credential shapes with a recognizable vendor prefix. Anything else is caught
# by the per-process environment needles below; the list cannot be complete, so
# it errs toward the shapes agents most often paste back into a URL.
_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{10,}"
    r"|sk_live_[A-Za-z0-9]{10,}"
    r"|ghp_[A-Za-z0-9]{10,}"
    r"|gho_[A-Za-z0-9]{10,}"
    r"|ghs_[A-Za-z0-9]{10,}"
    r"|ghu_[A-Za-z0-9]{10,}"
    r"|ghr_[A-Za-z0-9]{10,}"
    r"|github_pat_[A-Za-z0-9_]{10,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{10,}"
    r"|ya29\.[A-Za-z0-9_.-]{10,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|hf_[A-Za-z0-9]{10,}"
    r"|pplx-[A-Za-z0-9]{10,}"
    r"|tvly-[A-Za-z0-9]{10,}"
    r"|exa_[A-Za-z0-9]{10,}"
    r"|xai-[A-Za-z0-9]{10,}"
    r"|eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
    r"|-----BEGIN"
    r")",
    re.IGNORECASE,
)

# Environment values are used as literal needles when the variable *name* looks
# like a credential holder: an agent repeating such a value back into a URL or
# query is the leak being blocked, whatever its vendor prefix.
_SECRET_ENV_NAME_RE = re.compile(r"(?i)(key|token|secret|passwd|password|credential)")
_SECRET_ENV_MIN_LEN = 16


def _env_secret_needles(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return this process's own secret-looking environment values."""
    env = os.environ if environ is None else environ
    needles: list[str] = []
    for name, value in env.items():
        if not _SECRET_ENV_NAME_RE.search(name):
            continue
        text = (value or "").strip()
        if len(text) >= _SECRET_ENV_MIN_LEN:
            needles.append(text)
    return needles


def _contains_secret(value: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a URL or query carries a credential-like value, raw or decoded."""
    needles = _env_secret_needles(environ)
    for candidate in (value, urllib.parse.unquote(value)):
        if _SECRET_RE.search(candidate):
            return True
        if any(needle in candidate for needle in needles):
            return True
    return False


_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata.goog"}


def _is_google_challenge(url: str, challenge_text: str) -> bool:
    """Detect Google's challenge page from the URL and the challenge UI's text.

    ``challenge_text`` must come from the structural DOM probe
    (``cdp._PAGE_CHALLENGE_PROBE_JS``), never from whole-page text: a normal
    result page for the query "captcha" contains that word in a snippet, so
    scanning the body marked ordinary searches as challenges (F1). The phrase
    list below only disambiguates text that already came out of a challenge
    container, which is why it cannot be tripped by a result page.
    """
    parsed = urllib.parse.urlparse(url)
    if not _is_google_host(parsed.hostname):
        return False
    path = parsed.path.lower()
    if path == "/sorry" or path.startswith("/sorry/"):
        return True
    normalized = " ".join(challenge_text.lower().split())
    return any(
        phrase in normalized
        for phrase in (
            "our systems have detected unusual traffic",
            "unusual traffic from your computer network",
            "automated queries",
            "not a robot",
            "captcha",
        )
    )


def _is_google_host(host: str | None) -> bool:
    normalized = (host or "").lower().rstrip(".")
    return normalized == "google.com" or normalized.endswith(".google.com")


def _validate_public_url(url: str) -> str:
    """Return a public HTTP(S) URL or raise a fail-closed validation error."""
    if _contains_secret(url):
        raise ValueError("Blocked: URL contains a credential-like value")
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ValueError("Blocked: malformed URL") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("Blocked: only public HTTP(S) URLs without credentials are allowed")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Blocked: local or metadata hostname")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise ValueError("Blocked: hostname could not be resolved safely") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Blocked: URL resolves to a non-public address")
    return urllib.parse.urlunparse(parsed)


def _validate_search_query(query: str) -> str:
    """Return a search query or raise if it carries a credential-like value.

    The query is sent to whichever engine answers (and SearXNG forwards it to a
    third-party host), so it gets the same gate as a fetch URL. The offending
    value is never echoed back.
    """
    if _contains_secret(query):
        raise ValueError("Blocked: query contains a credential-like value")
    return query


def _normalize_candidate_href(href: str) -> str | None:
    """Normalize direct and legacy Google result links without fetching them."""
    try:
        parsed = urllib.parse.urlparse(href)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if _is_google_host(parsed.hostname) and parsed.path == "/url":
        target = urllib.parse.parse_qs(parsed.query).get("q", [None])[0]
        return _normalize_candidate_href(target) if target else None
    return href
