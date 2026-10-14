"""Public-URL and Google-host policy checks.

This module owns the fail-closed URL validator plus the host/challenge helpers it
and the search layer share: ``_SECRET_RE``/``_BLOCKED_HOSTS`` define the deny
patterns, ``_is_google_host``/``_is_google_challenge`` classify Google pages, and
``_normalize_candidate_href`` unwraps direct/legacy Google result links. It has no
dependencies on server state, so it can be imported freely; ``server`` re-exports
the names that its call sites and tests use.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse


_SECRET_RE = re.compile(
    r"(?:sk-|ghp_|github_pat_|xox[baprs]-|AIza|hf_|pplx-|tvly-|exa_|xai-)[A-Za-z0-9_-]{10,}",
    re.IGNORECASE,
)


_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata.goog"}


def _is_google_challenge(url: str, text: str) -> bool:
    """Detect Google's challenge page without treating ordinary result text as one."""
    parsed = urllib.parse.urlparse(url)
    if not _is_google_host(parsed.hostname):
        return False
    path = parsed.path.lower()
    if path == "/sorry" or path.startswith("/sorry/"):
        return True
    normalized = " ".join(text.lower().split())
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
    if _SECRET_RE.search(url) or _SECRET_RE.search(urllib.parse.unquote(url)):
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
