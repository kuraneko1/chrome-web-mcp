#!/usr/bin/env python3
"""Focused DS4-derived Google search + URL fetch MCP server.

Runs Chrome through a platform browser backend (X11 virtual displays on Linux,
native/headless Chrome on macOS), renders pages with JavaScript through CDP, and
exposes a stateless Google search tool plus a public-URL fetch tool. Fetch
targets and their post-redirect final URLs are validated fail-closed; arbitrary
page-context JavaScript is never exposed.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import fcntl
import ipaddress
import itertools
import json
import os
import random
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import mcp.types as types
import websockets
from mcp.server import Server
from mcp.server.stdio import stdio_server
from chrome_web_mcp.network import PublicNetworkProxy
from chrome_web_mcp import platform_runtime

try:
    import trafilatura
except ImportError:  # pragma: no cover - dependency declared in pyproject
    trafilatura = None  # type: ignore[assignment]
try:
    import html2text
except ImportError:  # pragma: no cover - dependency declared in pyproject
    html2text = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
# Each MCP server process (one per Hermes session) gets its OWN throwaway Chrome
# profile under /tmp, so sessions never contend for a shared profile: a live
# second session can no longer wedge this one with "owns this profile". The
# shared on-disk profile (HERE/chrome-profile) is still supported via the
# CW_PROFILE_DIR override, for environments that intentionally share one.
# The instance lock (flock) still guards whichever profile directory is in use;
# its holder pid/starttime are embedded so a DEAD holder's stale lock can be
# recovered automatically instead of blocking forever.
PROFILE_DIR = (
    Path(os.environ["CW_PROFILE_DIR"])
    if os.environ.get("CW_PROFILE_DIR")
    else Path(tempfile.gettempdir()) / "chrome-web-v2-profile" / str(os.getpid())
)
LOCK_PATH = Path(os.environ["CW_LOCK_PATH"]) if os.environ.get("CW_LOCK_PATH") else PROFILE_DIR / ".instance.lock"
DEVTOOLS_FILE = PROFILE_DIR / "DevToolsActivePort"
MAX_CDP_MESSAGE = 2 * 1024 * 1024
MAX_EXTRACTION_CHARS = 16 * 1024 * 1024

app = Server("chrome-web")

_SECRET_RE = re.compile(
    r"(?:sk-|ghp_|github_pat_|xox[baprs]-|AIza|hf_|pplx-|tvly-|exa_|xai-)[A-Za-z0-9_-]{10,}",
    re.IGNORECASE,
)
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata.goog"}
_CDP_IDS = itertools.count(1)


class CaptchaRequired(RuntimeError):
    """The Google page requires a human interaction before search can continue."""


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


async def _resolve_candidate_url(href: str) -> str | None:
    """Resolve Google wrappers without following a redirect to the destination."""
    current = _normalize_candidate_href(href)
    if not current:
        return None
    for _ in range(3):
        parsed = urllib.parse.urlparse(current)
        if not _is_google_host(parsed.hostname):
            return _validate_public_url(current)
        if parsed.path == "/url":
            current = _normalize_candidate_href(current)
            if not current:
                return None
            continue
        if parsed.path != "/goto":
            return None
        # The only fetched host is Google. The external Location is validated but
        # is never requested here, preventing redirect-based SSRF.
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            if platform_runtime.platform_key() == "macos"
            else "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/150 Safari/537.36"
        )
        async with httpx.AsyncClient(follow_redirects=False, timeout=8.0) as client:
            response = await client.get(
                current,
                headers={"User-Agent": user_agent},
            )
        location = response.headers.get("location")
        if not location or response.status_code not in {301, 302, 303, 307, 308}:
            return None
        current = urllib.parse.urljoin(current, location)
    return None


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


def _terminate_owned_process(proc: subprocess.Popen | None, timeout: float) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=timeout)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _process_identity(pid: int) -> dict:
    """Identify a same-user process without relying on a reusable PID alone."""
    return platform_runtime.process_identity(pid)


def _stop_recorded_process(record: dict) -> None:
    """Stop only a same-user process group whose leader identity still matches."""
    try:
        pid = int(record["pid"])
        if pid <= 1 or _process_identity(pid) != record or os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if _process_identity(pid) != record:
                return
            time.sleep(0.05)
        if _process_identity(pid) == record:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return


_CONFIG_DEFAULTS = {
    "show_browser": True,  # Linux: Xephyr/Xvfb | macOS: native/headless
    "hl": "ja",
    "gl": "jp",
    "limit": 5,
    "char_limit": 15000,
    "format": "markdown",
    "min_delay": 1.0,
    "max_delay": 2.5,
}


def _warn_config(message: str) -> None:
    try:
        print(f"chrome-web-mcp: config: {message}", file=sys.stderr)
    except Exception:
        pass


def _load_config() -> dict:
    """Load the optional JSON config file; fall back to defaults per key.

    Path from CW_CONFIG, else ~/.config/chrome-web-mcp/config.json when it
    exists. Invalid keys/values warn on stderr and keep the default value.
    """
    raw_path = os.environ.get("CW_CONFIG", "").strip()
    path = Path(raw_path).expanduser() if raw_path else (
        Path.home() / ".config" / "chrome-web-mcp" / "config.json"
    )
    cfg: dict[str, Any] = dict(_CONFIG_DEFAULTS)
    if not path.is_file():
        if raw_path:
            _warn_config(f"{path} not found; using defaults")
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _warn_config(f"cannot parse {path} ({exc}); using defaults")
        return cfg
    if not isinstance(data, dict):
        _warn_config(f"{path} must be a JSON object; using defaults")
        return cfg
    for key in data:
        if key not in _CONFIG_DEFAULTS:
            _warn_config(f"unknown key {key!r} ignored")
    if "show_browser" in data:
        value = data["show_browser"]
        if isinstance(value, bool):
            cfg["show_browser"] = value
        else:
            _warn_config("show_browser must be true or false; using default")
    for key in ("hl", "gl"):
        if key in data:
            value = data[key]
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z-]{2,8}", value.strip()):
                cfg[key] = value.strip()
            else:
                _warn_config(f"{key} must be a 2-8 letter code; using default")
    if "limit" in data:
        value = data["limit"]
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 20:
            cfg["limit"] = value
        else:
            _warn_config("limit must be an integer from 1 to 20; using default")
    if "char_limit" in data:
        value = data["char_limit"]
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 200000:
            cfg["char_limit"] = value
        else:
            _warn_config("char_limit must be an integer from 100 to 200000; using default")
    if "format" in data:
        if data["format"] in ("text", "markdown", "links"):
            cfg["format"] = data["format"]
        else:
            _warn_config("format must be text, markdown, or links; using default")
    delays: dict[str, float] = {}
    for key in ("min_delay", "max_delay"):
        if key in data:
            value = data[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                delays[key] = float(value)
            else:
                _warn_config(f"{key} must be a non-negative number; using default")
    if delays:
        lo = delays["min_delay"] if "min_delay" in delays else float(cfg["min_delay"])
        hi = delays["max_delay"] if "max_delay" in delays else float(cfg["max_delay"])
        if hi >= lo:
            cfg.update(delays)
        else:
            _warn_config("max_delay must be >= min_delay; using defaults")
    return cfg


CONFIG = _load_config()


class BrowserRuntime:
    """Own one isolated browser session and its platform display backend."""

    def __init__(self) -> None:
        self.xvfb: subprocess.Popen | None = None
        self.xephyr: subprocess.Popen | None = None
        self.chrome: subprocess.Popen | None = None
        self.lock_file: Any = None
        self.display: str | None = None
        self.port: int | None = None
        self.browser_ws: str | None = None
        self.proxy: PublicNetworkProxy | None = None
        self.xpra_server: subprocess.Popen | None = None
        self.xpra_client: subprocess.Popen | None = None
        self.user_display = os.environ.get("DISPLAY")
        # CW_DISPLAY_MODE is an advanced environment override. The JSON
        # show_browser boolean is the user-facing switch.
        env_display_mode = os.environ.get("CW_DISPLAY_MODE", "").strip().lower()
        self.display_mode = platform_runtime.browser_mode(
            bool(CONFIG["show_browser"]), env_display_mode
        )
        self.gpu_info: dict[str, Any] = {
            "gpu_device": None,
            "gpu_renderer": None,
            "gpu_backend": "UNKNOWN",
            "metal_active": None,
        }
        # A long-lived background search tab, reused across queries so we do not
        # repeatedly open/close targets (which looks like bot activity to Google).
        self.search_target_id: str | None = None
        # Fetch calls use per-call tabs (created and closed per request), so a
        # fetch never steals the Google search tab and concurrent fetches are
        # parallel-safe.
        self.fetch_target_id: str | None = None

    # Fingerprint hardening, injected into every new document before scripts run.
    # Linux keeps the upstream spoof for compatibility.  macOS intentionally
    # avoids pretending to be Windows/Intel because that contradicts native
    # Chrome's own UA/WebGL/ANGLE fingerprint and can make detection worse.
    _STEALTH_INIT_JS_LINUX = r"""
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    const _qp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return _qp.call(this, p);
    };
    """

    _STEALTH_INIT_JS_MAC = r"""
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
    """

    @staticmethod
    def _stealth_init_js() -> str:
        return (
            BrowserRuntime._STEALTH_INIT_JS_MAC
            if platform_runtime.platform_key() == "macos"
            else BrowserRuntime._STEALTH_INIT_JS_LINUX
        )

    @staticmethod
    def _browser_environment(display: str) -> dict[str, str]:
        """Force Chrome onto the private Xvfb display, never the user Wayland session."""
        env = os.environ.copy()
        env["DISPLAY"] = display
        env["XDG_SESSION_TYPE"] = "x11"
        for key in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET"):
            env.pop(key, None)
        return env

    @staticmethod
    def _discover_xauthority() -> str | None:
        """Find the Xauthority file needed to connect to a desktop Xwayland."""
        configured = os.environ.get("XAUTHORITY", "").strip()
        if configured and Path(configured).is_file():
            return configured

        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if runtime_dir:
            candidates = list(Path(runtime_dir).glob(".mutter-Xwaylandauth.*"))
            candidates = [path for path in candidates if path.is_file()]
            if candidates:
                candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
                return str(candidates[0])

        home_xauthority = Path.home() / ".Xauthority"
        if home_xauthority.is_file():
            return str(home_xauthority)
        return None

    def _human_display_environment(self) -> dict[str, str]:
        """Prepare an X11 client environment for the user's desktop display."""
        env = os.environ.copy()
        env["DISPLAY"] = self.user_display or ""
        env["XDG_SESSION_TYPE"] = "x11"
        xauthority = self._discover_xauthority()
        if xauthority:
            env["XAUTHORITY"] = xauthority
        for key in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET"):
            env.pop(key, None)
        return env

    def hide_for_human(self) -> None:
        """Stop the temporary Xpra shadow server and its visible client."""
        _terminate_owned_process(self.xpra_client, 3)
        _terminate_owned_process(self.xpra_server, 3)
        self.xpra_client = None
        self.xpra_server = None

    def expose_for_human(self) -> None:
        """Attach the private Xvfb display to the user's desktop via Xpra."""
        if platform_runtime.platform_key() == "macos":
            if self.display_mode == "native":
                return
            raise RuntimeError("CAPTCHA is in headless Chrome; restart with show_browser=true")
        if not self.display:
            raise RuntimeError("CAPTCHA browser display is not ready")
        if not self.user_display:
            raise RuntimeError("CAPTCHA display is unavailable: user DISPLAY is not set")
        if self.xpra_client and self.xpra_client.poll() is None:
            return
        xpra = shutil.which("xpra")
        if not xpra:
            raise RuntimeError("Xpra is not installed; install the xpra package to solve CAPTCHA")
        self.hide_for_human()
        server_env = self._browser_environment(self.display)
        self.xpra_server = subprocess.Popen(
            [
                xpra,
                "shadow",
                self.display,
                "--daemon=no",
                "--mdns=no",
                "--notifications=no",
                "--bell=no",
                "--system-tray=no",
            ],
            env=server_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        client_env = self._human_display_environment()
        ready = False
        for _ in range(40):
            if self.xpra_server.poll() is not None:
                break
            status = subprocess.run(
                [xpra, "list"],
                env=client_env,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if self.display in status.stdout:
                ready = True
                break
            time.sleep(0.25)
        if not ready:
            self.hide_for_human()
            raise RuntimeError("Xpra shadow session did not become ready")
        self.xpra_client = subprocess.Popen(
            [
                xpra,
                "attach",
                self.display,
                "--opengl=no",
                "--clipboard=no",
                "--notifications=no",
                "--bell=no",
                "--system-tray=no",
            ],
            env=client_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.25)
        if self.xpra_client.poll() is not None:
            self.hide_for_human()
            raise RuntimeError("Xpra attach client exited before showing the browser")

    @staticmethod
    def _chrome_executable() -> str:
        return platform_runtime.discover_chrome()

    def _acquire_lock(self) -> None:
        """Acquire the profile lock, embedding our pid/starttime for forensics.

        On contention we wait a few seconds (a dying holder releases its fd
        asynchronously) and then raise an error that names the live holder, so
        a real conflict with another running session is diagnosable at a glance
        instead of a bare 'owns this profile'.
        """
        if self.lock_file is not None:
            return
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        start = time.monotonic()
        while True:
            lock_file = LOCK_PATH.open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                if time.monotonic() - start > 4.0:
                    raise RuntimeError(
                        "Another chrome-web MCP instance owns this profile "
                        f"({LOCK_PATH}). {self._holder_diagnosis()} A live second "
                        "session is expected to hold its own lock independently; "
                        "if this persists, check for a stale chrome-web-v2 server "
                        "process (ps aux | grep chrome-web-v2/server.py)."
                    ) from None
                time.sleep(0.25)
                continue
            # Record who holds the lock (best-effort, never fatal).
            try:
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(f"pid={os.getpid()} start={time.time():.6f}\n")
                lock_file.flush()
            except OSError:
                pass
            self.lock_file = lock_file
            return

    @staticmethod
    def _holder_diagnosis() -> str:
        """Read the embedded holder pid from the lock file and report its state."""
        try:
            content = LOCK_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            return "No holder recorded in the lock file."
        pid = None
        for field in content.split():
            if field.startswith("pid="):
                try:
                    pid = int(field.split("=", 1)[1])
                except ValueError:
                    pid = None
        if pid is None:
            return f"Lock content: {content[:80]!r} (no pid recorded)."
        alive, command = platform_runtime.process_summary(pid)
        if alive:
            return (
                f"Holder pid {pid} is ALIVE ({command or 'unknown'}); "
                "it owns the lock legitimately and will release it on exit."
            )
        return (
            f"Holder pid {pid} is DEAD — the flock released automatically; "
            "this instance is acquiring the freed lock."
        )

    @staticmethod
    def _sweep_orphans() -> None:
        """Kill orphaned Chrome/Xvfb and remove per-session profile dirs whose
        owning MCP server process is dead (crash / kill -9 leftovers)."""
        base = Path(tempfile.gettempdir()) / "chrome-web-v2-profile"
        if not base.is_dir():
            return
        for entry in base.iterdir():
            try:
                eligible = not entry.is_symlink() and entry.is_dir() and entry.stat().st_uid == os.getuid()
            except OSError:
                # Another starting server may already have reaped this entry.
                continue
            if not eligible:
                continue
            try:
                pid = int(entry.name)
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                # New profiles record Chrome AND any private display, including
                # process birth identity to avoid terminating an unrelated reused PID.
                try:
                    records = json.loads((entry / ".owned-processes.json").read_text())
                    if isinstance(records, list):
                        for record in records:
                            if isinstance(record, dict):
                                _stop_recorded_process(record)
                except (OSError, ValueError):
                    pass
                # Upgrade path for old profiles: exact argv matching, never
                # pkill regexes or option-like patterns. Old display processes
                # cannot be safely identified without a manifest, so leave them.
                for record in platform_runtime.processes_with_exact_arg(
                    f"--user-data-dir={entry}"
                ):
                    _stop_recorded_process(record)
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    pass
            except OSError:
                # EPERM: owner alive (or a kernel pid we cannot signal) — keep it.
                pass

    def _record_processes(self) -> None:
        if self.lock_file is None:
            return
        PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        records = []
        for proc in (self.chrome, self.xephyr, self.xvfb):
            if proc is not None and proc.poll() is None:
                records.append(_process_identity(proc.pid))
        pending = PROFILE_DIR / ".owned-processes.tmp"
        pending.write_text(json.dumps(records))
        pending.replace(PROFILE_DIR / ".owned-processes.json")

    def _start_xvfb(self) -> str:
        proc = subprocess.Popen(
            ["Xvfb", "-displayfd", "1", "-screen", "0", "1365x900x24", "-nolisten", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.xvfb = proc
        self._record_processes()
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            raise RuntimeError("Xvfb did not allocate a display within 10 seconds")
        number = proc.stdout.readline().strip()
        if not number.isdigit():
            raise RuntimeError("Xvfb returned an invalid display number")
        display = f":{number}"
        for _ in range(30):
            check = subprocess.run(
                ["xdpyinfo", "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if check.returncode == 0:
                return display
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("Xvfb failed its readiness check")

    def _start_xephyr(self, host_display: str) -> str:
        """Start a nested Xephyr window on the user's desktop and return its display.

        Xephyr is a plain X client: it appears as one ordinary window the user
        can minimize, move to another workspace, or close. Chrome runs *inside*
        it, so tool calls can never pop a window to the front of the desktop
        the way running Chrome directly on DISPLAY would.
        """
        env = self._human_display_environment()
        env["DISPLAY"] = host_display
        # Arm a minimize helper BEFORE Xephyr maps its window: `xdotool search
        # --sync` blocks until the window appears, then minimizes it within
        # milliseconds. Starting minimized this way leaves (almost) no visible
        # flash, unlike sleep-then-minimize after the fact.
        try:
            minimizer = subprocess.Popen(
                ["xdotool", "search", "--sync", "--onlyvisible", "--name", "chrome-web-mcp", "windowminimize"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            minimizer = None
        try:
            proc = subprocess.Popen(
                [
                    "Xephyr",
                    "-displayfd",
                    "1",
                    "-screen",
                    "1365x900x24",
                    "-title",
                    "chrome-web-mcp",
                    "-nolisten",
                    "tcp",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except Exception:
            _terminate_owned_process(minimizer, 2)
            raise
        self.xephyr = proc
        self._record_processes()
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            _terminate_owned_process(minimizer, 2)
            raise RuntimeError("Xephyr did not allocate a display within 10 seconds")
        number = proc.stdout.readline().strip()
        if not number.isdigit():
            _terminate_owned_process(minimizer, 2)
            raise RuntimeError(
                "Xephyr could not allocate a nested display. "
                "Check DISPLAY and XAUTHORITY access to the desktop X server."
            )
        display = f":{number}"
        for _ in range(30):
            check = subprocess.run(
                ["xdpyinfo", "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if check.returncode == 0:
                if minimizer is not None:
                    try:
                        minimizer.wait(timeout=10)
                    except subprocess.SubprocessError:
                        _terminate_owned_process(minimizer, 2)
                return display
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        _terminate_owned_process(minimizer, 2)
        raise RuntimeError("Xephyr failed its readiness check")

    def _chrome_command(self) -> list[str]:
        if self.proxy is None:
            raise RuntimeError("Public-network proxy is not ready")
        command = [
            self._chrome_executable(),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--proxy-server={self.proxy.url}",
            "--proxy-bypass-list=<-loopback>",
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--mute-audio",
            "--disable-blink-features=AutomationControlled",
            "--lang=ja-JP",
            "--window-size=1365,900",
            "--window-position=0,0",
            "about:blank",
        ]
        if platform_runtime.platform_key() == "linux":
            command[1:1] = [
                "--ozone-platform=x11",
                "--password-store=basic",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ]
        elif platform_runtime.platform_key() == "macos":
            command.insert(1, "--use-mock-keychain")
            if self.display_mode == "headless":
                command.insert(1, "--headless=new")
        if os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        return command

    def _start_chrome(self, display: str | None) -> tuple[int, str]:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        DEVTOOLS_FILE.unlink(missing_ok=True)
        if platform_runtime.platform_key() == "linux":
            if not display:
                raise RuntimeError("Linux Chrome needs a private X11 display")
            env = self._browser_environment(display)
        else:
            env = os.environ.copy()
        command = self._chrome_command()
        proc = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.chrome = proc
        self._record_processes()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Chrome exited before CDP was ready ({proc.returncode})")
            try:
                lines = DEVTOOLS_FILE.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                path = lines[1]
                return port, f"ws://127.0.0.1:{port}{path}"
            except (FileNotFoundError, IndexError, ValueError):
                time.sleep(0.1)
        raise RuntimeError("Chrome did not expose CDP within 20 seconds")

    def ensure(self) -> str:
        if self.chrome and self.chrome.poll() is None and self.browser_ws:
            return self.browser_ws
        self.cleanup()
        self._acquire_lock()
        try:
            self.proxy = PublicNetworkProxy()
            if platform_runtime.platform_key() == "macos":
                self.display = None
            elif self.display_mode == "xephyr":
                if not self.user_display:
                    raise RuntimeError(
                        "CW_DISPLAY_MODE=xephyr needs a user DISPLAY, but none is set"
                    )
                self.display = self._start_xephyr(self.user_display)
            else:
                self.display = self._start_xvfb()
            self.port, self.browser_ws = self._start_chrome(self.display)
            return self.browser_ws
        except Exception:
            self.cleanup()
            raise

    def cleanup(self) -> None:
        self.hide_for_human()
        _terminate_owned_process(self.chrome, 5)
        _terminate_owned_process(self.xephyr, 3)
        _terminate_owned_process(self.xvfb, 3)
        self.chrome = None
        self.xephyr = None
        self.xvfb = None
        self.display = None
        self.port = None
        self.browser_ws = None
        self.search_target_id = None
        self.fetch_target_id = None
        self.gpu_info = {
            "gpu_device": None,
            "gpu_renderer": None,
            "gpu_backend": "UNKNOWN",
            "metal_active": None,
        }
        if self.proxy is not None:
            self.proxy.close()
            self.proxy = None
        if self.lock_file is not None:
            try:
                DEVTOOLS_FILE.unlink(missing_ok=True)
                (PROFILE_DIR / ".owned-processes.json").unlink(missing_ok=True)
                if not os.environ.get("CW_PROFILE_DIR"):
                    # SIGTERM uses os._exit, so atexit alone cannot remove it.
                    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self.lock_file.close()
                self.lock_file = None


def _env_float(name: str, default: float) -> float:
    """Read a float env override, falling back to default on unset/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class SharedSearchRateLimiter:
    """Reserve globally spaced Google-search start slots across processes."""

    _BUCKET = "google_search"

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        min_delay: float | None = None,
        max_delay: float | None = None,
    ) -> None:
        if min_delay is None:
            min_delay = _env_float("CW_MIN_DELAY", float(CONFIG["min_delay"]))
        if max_delay is None:
            max_delay = _env_float("CW_MAX_DELAY", float(CONFIG["max_delay"]))
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("invalid search rate-limit delay range")
        default_path = Path(tempfile.gettempdir()) / "chrome-web-mcp" / "search-rate-limit.sqlite3"
        configured = os.environ.get("CW_RATE_LIMIT_DB")
        self.db_path = Path(db_path or configured or default_path)
        self.min_delay = min_delay
        self.max_delay = max_delay

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS rate_limit ("
            "bucket TEXT PRIMARY KEY, next_at REAL NOT NULL)"
        )
        return connection

    def reserve_slot(self) -> float:
        """Atomically reserve a slot and return seconds to wait before starting."""
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_at FROM rate_limit WHERE bucket = ?", (self._BUCKET,)
            ).fetchone()
            previous = float(row[0]) if row else now
            slot = max(now, previous)
            next_at = slot + random.uniform(self.min_delay, self.max_delay)
            connection.execute(
                "INSERT INTO rate_limit(bucket, next_at) VALUES(?, ?) "
                "ON CONFLICT(bucket) DO UPDATE SET next_at=excluded.next_at",
                (self._BUCKET, next_at),
            )
            connection.commit()
            return max(0.0, slot - now)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


_RUNTIME = BrowserRuntime()
# Reap crash/kill -9 leftovers from previous sessions (dead per-pid profile
# dirs and their orphaned Chrome/display processes) before this session needs a browser.
BrowserRuntime._sweep_orphans()

# Serialize searches within this process and reserve a shared inter-process slot.
_SEARCH_LOCK = asyncio.Lock()
_SEARCH_LIMITER = SharedSearchRateLimiter()
# Serialize browser lifecycle (ensure/cleanup/lock) across search AND fetch:
# ensure() is blocking and not reentrant, so a search racing a fetch would
# cleanup() the other's starting browser and then fail on the profile lock.
_ENSURE_LOCK = asyncio.Lock()
# Last time Google served a CAPTCHA (epoch seconds), for health_check.
_LAST_CAPTCHA_TS: float | None = None
# Sliding window of recent search starts, for burst warnings to agents.
_PACE_WINDOW_S = 60.0
_PACE_WARN_N = 15
_SEARCH_TIMES: collections.deque[float] = collections.deque()


def _note_search_start() -> int:
    """Record a search start; return starts within the pace window."""
    now = time.monotonic()
    _SEARCH_TIMES.append(now)
    while _SEARCH_TIMES and _SEARCH_TIMES[0] <= now - _PACE_WINDOW_S:
        _SEARCH_TIMES.popleft()
    return len(_SEARCH_TIMES)


def _peek_search_count() -> int:
    """Count starts within the pace window without recording."""
    now = time.monotonic()
    while _SEARCH_TIMES and _SEARCH_TIMES[0] <= now - _PACE_WINDOW_S:
        _SEARCH_TIMES.popleft()
    return len(_SEARCH_TIMES)


def _pace_warning(count: int) -> str | None:
    if count >= _PACE_WARN_N:
        return (
            f"{count} searches in the last 60s; slow down or batch queries "
            "to avoid a Google CAPTCHA"
        )
    return None


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


async def _collect_gpu_info(browser: Any) -> None:
    """Best-effort Chrome GPU diagnostics from the browser-level CDP socket."""
    if _RUNTIME.gpu_info.get("gpu_renderer") or _RUNTIME.gpu_info.get("gpu_backend") != "UNKNOWN":
        return
    try:
        result = await _cdp_call(browser, "SystemInfo.getInfo")
        gpu = result.get("gpu", {}) if isinstance(result, dict) else {}
        devices = gpu.get("devices", []) if isinstance(gpu, dict) else []
        aux = gpu.get("auxAttributes", {}) if isinstance(gpu, dict) else {}
        device = devices[0] if devices and isinstance(devices[0], dict) else {}
        renderer = str(
            aux.get("glRenderer")
            or aux.get("renderer")
            or device.get("deviceString")
            or ""
        ).strip()
        vendor = str(device.get("vendorString") or aux.get("glVendor") or "").strip()
        implementation = str(aux.get("glImplementationParts") or "").strip()
        display_type = str(aux.get("displayType") or "").strip()
        blob = " ".join([renderer, vendor, implementation, display_type]).lower()
        if "swiftshader" in blob or "software" in blob:
            backend = "SOFTWARE"
            metal_active: bool | None = False
        elif "metal" in blob and "angle" in blob:
            backend = "ANGLE_METAL"
            metal_active = True
        elif "metal" in blob:
            backend = "METAL"
            metal_active = True
        else:
            backend = "UNKNOWN"
            metal_active = None
        _RUNTIME.gpu_info = {
            "gpu_device": str(device.get("deviceString") or "").strip() or None,
            "gpu_renderer": renderer or None,
            "gpu_backend": backend,
            "metal_active": metal_active,
        }
    except Exception:
        # Diagnostics must never make browser/search/fetch unavailable.
        return


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


_EXTRACT_RESULTS_JS = r"""(() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const output = [];
  for (const anchor of document.querySelectorAll('a[href]')) {
    const heading = anchor.querySelector('h3');
    if (!heading) continue;
    const title = clean(heading.innerText || heading.textContent);
    if (!title) continue;
    const block = anchor.closest('[data-hveid]') || anchor.parentElement?.parentElement || anchor.parentElement;
    const blockText = clean(block?.innerText || '');
    let description = blockText;
    if (description.startsWith(title)) description = clean(description.slice(title.length));
    output.push({title, href: anchor.href || '', description: description.slice(0, 1000)});
    if (output.length >= 40) break;
  }
  return JSON.stringify(output);
})()"""


async def _ensure_reused_tab(browser: Any, which: str) -> Any:
    """Return a connected page socket for the long-lived search tab.

    Reuses the existing target when it is still alive, otherwise creates a
    background tab. Fetch calls use per-call tabs instead, so concurrent
    fetches never share a navigation target.
    """
    attr = "search_target_id" if which == "search" else "fetch_target_id"
    target_id = getattr(_RUNTIME, attr)
    if target_id:
        try:
            alive = await _cdp_call(
                browser, "Target.getTargetInfo", {"targetId": target_id}
            )
            if not alive.get("targetInfo", {}).get("targetId"):
                target_id = None
        except Exception:
            target_id = None
    if not target_id:
        created = await _cdp_call(
            browser,
            "Target.createTarget",
            {"url": "about:blank", "background": True, "newWindow": False},
        )
        target_id = created.get("targetId")
        if not target_id or _RUNTIME.port is None:
            raise RuntimeError(f"Chrome did not create a {which} tab")
        setattr(_RUNTIME, attr, target_id)
    port = _RUNTIME.port
    return await websockets.connect(
        f"ws://127.0.0.1:{port}/devtools/page/{target_id}",
        max_size=MAX_CDP_MESSAGE,
    )


async def _ensure_search_page(browser: Any) -> tuple[Any, str, bool]:
    """Return (page_ws, target_id, created) for the long-lived search tab.

    The target is created once per browser session and reused across searches so
    we are not constantly opening/closing targets. The browser-level socket is
    ephemeral (re-connected per search), so we identify the target by its ID and
    simply verify it is still alive before reconnecting the page socket.
    """
    previous = _RUNTIME.search_target_id
    page = await _ensure_reused_tab(browser, "search")
    current = _RUNTIME.search_target_id
    assert current is not None
    return page, current, previous != current


async def _extract_google_candidates(
    query: str, limit: int, hl: str = "ja", gl: str = "jp"
) -> list[dict]:
    async with _ENSURE_LOCK:
        browser_ws = await asyncio.to_thread(_RUNTIME.ensure)
    browser = await websockets.connect(browser_ws, max_size=MAX_CDP_MESSAGE)
    await _collect_gpu_info(browser)
    page: Any = None
    try:
        try:
            page, _tid, _reused = await _ensure_search_page(browser)
        except Exception:
            # Browser-level connection was fine but the page socket may have been
            # dropped; fall back to a fresh target.
            page, _tid, _reused = await _ensure_search_page(browser)
        await _cdp_call(page, "Page.enable")
        await _cdp_call(page, "Runtime.enable")
        # Register fingerprint-hardening JS before any document runs. This is
        # per-DevTools-session (not per-target), so it must be (re)added on every
        # fresh page socket connection, reused tab or not.
        try:
            await _cdp_call(
                page,
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": BrowserRuntime._stealth_init_js()},
            )
        except Exception:
            pass
        search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": query, "num": min(max(limit + 5, 10), 25), "udm": "14", "hl": hl, "gl": gl}
        )
        await _cdp_call(page, "Page.navigate", {"url": search_url})
        await _wait_ready(page)
        if await _click_google_consent(page):
            await asyncio.sleep(1)
            await _wait_ready(page)
        final_url = str(await _evaluate(page, "location.href"))
        if not _is_google_host(urllib.parse.urlparse(final_url).hostname):
            raise RuntimeError("Google navigation left the allowed origin")
        body_text = str(await _evaluate(page, "document.body?.innerText || ''"))
        if _is_google_challenge(final_url, body_text):
            global _LAST_CAPTCHA_TS
            _LAST_CAPTCHA_TS = time.time()
            if _xpra_expose_enabled():
                try:
                    await asyncio.to_thread(_RUNTIME.expose_for_human)
                    detail = "Xpra has shown the CAPTCHA browser window; solve it, then retry the same search."
                except Exception as exc:
                    detail = f"CAPTCHA detected, but the browser could not be shown: {exc}"
            else:
                detail = _captcha_detail_for_mode()
            raise CaptchaRequired(detail)
        raw = await _read_page_string(page, _EXTRACT_RESULTS_JS)
        candidates = json.loads(raw or "[]")
        if not isinstance(candidates, list):
            raise RuntimeError("Google result extraction returned an invalid payload")
        return candidates
    finally:
        # Keep the page socket for reuse, but never leak the browser socket.
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await browser.close()


def _xpra_expose_enabled() -> bool:
    """Xpra auto-attach is opt-in: it once crashed the desktop session."""
    if platform_runtime.platform_key() == "macos":
        return False
    return os.environ.get("CW_XPRA_EXPOSE", "").strip() == "1"


def _captcha_detail_for_mode() -> str:
    if _RUNTIME.display_mode == "native":
        return (
            "Google CAPTCHA detected. Solve it in the native Chrome window "
            "on your desktop, then retry the same search."
        )
    if _RUNTIME.display_mode == "xephyr":
        return (
            "Google CAPTCHA detected. Solve it in the chrome-web-mcp window "
            "on your desktop, then retry the same search."
        )
    return "Google CAPTCHA detected. Wait a while, then retry the same search."


async def _search_google(
    query: str, limit: int, hl: str = "ja", gl: str = "jp"
) -> tuple[list[dict], float, str | None]:
    """Run one Google search with process-wide pacing and a shared slot queue.

    Returns (results, waited_seconds, pace_warning) so callers can expose
    queue waits and burst warnings.
    """
    async with _SEARCH_LOCK:
        wait_for = await asyncio.to_thread(_SEARCH_LIMITER.reserve_slot)
        if wait_for:
            await asyncio.sleep(wait_for)
        pace_count = _note_search_start()
        candidates = await _extract_google_candidates(query, limit, hl, gl)
    results = await _build_results(candidates, limit, resolve_url=_resolve_candidate_url)
    if not results:
        raise RuntimeError("Google rendered no usable external search results")
    await asyncio.to_thread(_RUNTIME.hide_for_human)
    return results, wait_for, _pace_warning(pace_count)


# Extract clean, readable text from a rendered page (no scripts/styles/nav).
_FETCH_TEXT_JS = r"""(() => {
  const clone = document.body ? document.body.cloneNode(true) : document;
  for (const sel of ['script','style','noscript','svg','canvas','iframe','nav','footer','header','[aria-hidden="true"]']) {
    clone.querySelectorAll(sel).forEach(el => el.remove());
  }
  const title = (document.title || '').trim();
  const text = (clone.innerText || '').replace(/ /g, ' ');
  return JSON.stringify({title, text});
})()"""


# Extract lightweight markdown (headings/paragraphs/lists) plus page links.
_FETCH_MD_JS = r"""(() => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  const title = (document.title || '').trim();
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const t = clean(a.innerText || a.textContent);
    let h = '';
    try { h = new URL(a.href, location.href).href; } catch (e) { continue; }
    if (!/^https?:\/\//i.test(h)) continue;
    if (t) links.push({text: t.slice(0, 200), url: h});
    if (links.length >= 200) break;
  }
  const parts = [];
  for (const el of document.querySelectorAll('h1,h2,h3,p,li,pre')) {
    if (!el.isConnected) continue;
    const t = clean(el.innerText || el.textContent);
    if (!t) continue;
    const tag = el.tagName;
    if (tag === 'H1') parts.push('# ' + t);
    else if (tag === 'H2') parts.push('## ' + t);
    else if (tag === 'H3') parts.push('### ' + t);
    else if (tag === 'LI') parts.push('- ' + t);
    else if (tag === 'PRE') parts.push('```\n' + t.slice(0, 2000) + '\n```');
    else parts.push(t);
    if (parts.join('\n\n').length > 300000) break;
  }
  let markdown = parts.join('\n\n');
  if (!markdown) markdown = clean(document.body ? document.body.innerText : '');
  return JSON.stringify({title, markdown, links});
})()"""


# Page links as follow-up crawl targets (used with shaped markdown).
_FETCH_LINKS_JS = r"""(() => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const t = clean(a.innerText || a.textContent);
    let h = '';
    try { h = new URL(a.href, location.href).href; } catch (e) { continue; }
    if (!/^https?:\/\//i.test(h)) continue;
    if (t) out.push({text: t.slice(0, 200), url: h});
    if (out.length >= 200) break;
  }
  return JSON.stringify(out);
})()"""


def _shape_markdown(html: str, url: str) -> tuple[str, str]:
    """Shape rendered HTML into boilerplate-free markdown.

    Returns (markdown, extraction): trafilatura first, html2text fallback,
    dom-walk last resort. An empty markdown with extraction "none" means the
    shaper saw nothing usable (caller: retry with format "text").
    """
    if trafilatura is not None:
        try:
            shaped = trafilatura.extract(
                html,
                output_format="markdown",
                include_links=True,
                include_images=False,
                url=url,
                deduplicate=True,
            )
        except Exception:
            shaped = None
        if shaped and len(shaped.strip()) > 200:
            return shaped.strip(), "trafilatura"
    if html2text is not None:
        try:
            conv = html2text.HTML2Text()
            conv.body_width = 0
            conv.ignore_images = True
            shaped = conv.handle(html)
        except Exception:
            shaped = None
        if shaped and shaped.strip():
            return shaped.strip(), "html2text"
    return "", "none"


def _smart_cut(text: str, limit: int) -> tuple[str, bool]:
    """Cut text at a sentence/word boundary; return (cut, truncated)."""
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    boundary = -1
    for match in re.finditer(r"[。.!?！？]", window):
        boundary = match.end()
    if boundary > limit * 0.5:
        return window[:boundary].rstrip(), True
    space = window.rfind(" ")
    if space > limit * 0.5:
        return window[:space], True
    return window, True


async def _fetch_page(url: str, char_limit: int, format: str = "text") -> dict:
    """Render a public URL in a per-call tab and return its readable content.

    Each call gets its own tab (created and closed here), so concurrent fetches
    are parallel-safe. The requested URL and its post-redirect final URL are
    both validated as public HTTP(S). The browser's mandatory public-network
    proxy blocks non-public connections before sending any upstream bytes,
    including redirects, subresources, WebSockets, and DNS rebinding.
    """
    # Validate the requested URL up front (fail-closed).
    validated = _validate_public_url(url)
    async with _ENSURE_LOCK:
        browser_ws = await asyncio.to_thread(_RUNTIME.ensure)
    browser = await websockets.connect(browser_ws, max_size=MAX_CDP_MESSAGE)
    await _collect_gpu_info(browser)
    page: Any = None
    target_id: str | None = None
    try:
        created = await _cdp_call(
            browser,
            "Target.createTarget",
            {"url": "about:blank", "background": True, "newWindow": False},
        )
        target_id = created.get("targetId")
        if not target_id or _RUNTIME.port is None:
            raise RuntimeError("Chrome did not create a fetch tab")
        page = await websockets.connect(
            f"ws://127.0.0.1:{_RUNTIME.port}/devtools/page/{target_id}",
            max_size=MAX_CDP_MESSAGE,
        )
        await _cdp_call(page, "Page.enable")
        await _cdp_call(page, "Runtime.enable")
        await _cdp_call(page, "Page.navigate", {"url": validated})
        await _wait_ready(page)
        # Output policy; the proxy enforces network policy before connection.
        final_url = str(await _evaluate(page, "location.href"))
        _validate_public_url(final_url)
        payload: dict = {
            "url": final_url,  # kept for backward compatibility
            "requested_url": validated,
            "final_url": final_url,
            "redirected": validated != final_url,
        }
        if format == "text":
            raw = await _read_page_string(page, _FETCH_TEXT_JS)
            data = json.loads(raw or "{}")
            full = " ".join(str(data.get("text", "")).split())
            title = " ".join(str(data.get("title", "")).split())
            cut, was_cut = _smart_cut(full, char_limit)
            payload.update(
                {
                    "title": title[:300],
                    "text": cut,
                    "total_chars": len(full),
                    "truncated": was_cut,
                    "format": "text",
                    "formatted": False,
                }
            )
        else:
            html = str(
                await _read_page_string(
                    page,
                    "document.documentElement ? document.documentElement.outerHTML : ''",
                )
            )
            shaped, method = await asyncio.to_thread(_shape_markdown, html, final_url)
            links_raw = await _read_page_string(page, _FETCH_LINKS_JS)
            try:
                links = json.loads(links_raw or "[]")
            except (ValueError, TypeError):
                links = []
            if not isinstance(links, list):
                links = []
            if not shaped:
                # Shaper saw nothing usable: fall back to the in-page DOM walk
                # so the agent still gets something (extraction says "dom").
                raw = await _read_page_string(page, _FETCH_MD_JS)
                try:
                    data = json.loads(raw or "{}")
                except (ValueError, TypeError):
                    data = {}
                shaped = str(data.get("markdown", "") or "")
                if not links:
                    maybe = data.get("links", [])
                    links = maybe if isinstance(maybe, list) else []
                method = "dom" if shaped.strip() else "none"
            title = " ".join(
                str(await _evaluate(page, "document.title || ''")).split()
            )
            full = shaped
            cut, was_cut = _smart_cut(full, char_limit)
            entry: dict = {
                "title": title[:300],
                "total_chars": len(full),
                "truncated": was_cut,
                "format": format,
                "formatted": True,
                "extraction": method,
                "links": links[:200],
            }
            if format == "markdown":
                entry["markdown"] = cut
            else:  # links: plain text plus follow-up crawl targets
                entry["text"] = cut
            payload.update(entry)
        return payload
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if target_id is not None:
            try:
                await _cdp_call(browser, "Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        try:
            await browser.close()
        except Exception:
            pass


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="google_search",
            description=(
                "Search Google in a JavaScript-rendering Chrome browser running "
                "in the platform browser backend. Returns structured search results. "
                "Workflow: first google_search, then fetch_url on interesting "
                "result URLs for full text. Pace calls: bursts of 15+ searches "
                "per minute raise a pace_warning and risk a Google CAPTCHA."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (1-20)",
                        "default": CONFIG["limit"],
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "hl": {
                        "type": "string",
                        "description": "Google UI language, e.g. ja or en",
                        "default": CONFIG["hl"],
                    },
                    "gl": {
                        "type": "string",
                        "description": "Google region, e.g. jp or us",
                        "default": CONFIG["gl"],
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="fetch_url",
            description=(
                "Fetch a public HTTP(S) URL with JavaScript-rendering Chrome "
                "and return shaped readable markdown plus requested/final URLs, "
                "redirect flag, and total_chars. The markdown is shaped "
                "(boilerplate removed, extraction method reported); if content "
                "looks missing, retry with format:text for the full rendered "
                "text. Use after google_search on result URLs, "
                "or for pages that need JS to render (SPAs, "
                "paywalled-after-consent layouts, etc.). Parallel calls are safe; "
                "each fetch uses its own tab. char_limit caps the returned text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public http(s) URL to fetch (no credentials, no localhost).",
                    },
                    "char_limit": {
                        "type": "integer",
                        "description": "Maximum characters of readable text to return.",
                        "default": CONFIG["char_limit"],
                        "minimum": 100,
                        "maximum": 200000,
                    },
                    "format": {
                        "type": "string",
                        "description": "markdown: shaped readable markdown, boilerplate removed. text: full rendered text, use when markdown looks incomplete. links: text plus follow-up link targets",
                        "default": CONFIG["format"],
                        "enum": ["text", "markdown", "links"],
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="health_check",
            description=(
                "Report server health: display mode, browser/process liveness, "
                "search rate-limiter queue, and last CAPTCHA time. No arguments."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
    ]


def _health_status() -> dict:
    """Collect display/browser/queue/CAPTCHA health without starting anything."""
    chrome_alive = _RUNTIME.chrome is not None and _RUNTIME.chrome.poll() is None
    xvfb_alive = _RUNTIME.xvfb is not None and _RUNTIME.xvfb.poll() is None
    xephyr_alive = _RUNTIME.xephyr is not None and _RUNTIME.xephyr.poll() is None
    queue_wait_s = 0.0
    try:
        connection = _SEARCH_LIMITER._connect()
        try:
            row = connection.execute(
                "SELECT next_at FROM rate_limit WHERE bucket = ?",
                (_SEARCH_LIMITER._BUCKET,),
            ).fetchone()
        finally:
            connection.close()
        if row:
            queue_wait_s = max(0.0, float(row[0]) - time.time())
    except Exception:
        queue_wait_s = -1.0
    pace_count = _peek_search_count()
    chrome_binary: str | None = None
    chrome_version: str | None = None
    chrome_detection_error: str | None = None
    try:
        chrome_binary = _RUNTIME._chrome_executable()
        chrome_version = platform_runtime.chrome_version(chrome_binary)
    except Exception as exc:
        chrome_detection_error = str(exc)
    return {
        "platform": platform_runtime.platform_description(),
        "display_mode": _RUNTIME.display_mode,
        "chrome_binary": chrome_binary,
        "chrome_version": chrome_version,
        "chrome_detection_error": chrome_detection_error,
        "chrome_alive": chrome_alive,
        "xvfb_alive": xvfb_alive,
        "xephyr_alive": xephyr_alive,
        **_RUNTIME.gpu_info,
        "rate_limiter_queue_wait_s": round(queue_wait_s, 3),
        "rate_limit_min_delay_s": _SEARCH_LIMITER.min_delay,
        "rate_limit_max_delay_s": _SEARCH_LIMITER.max_delay,
        "recent_searches_60s": pace_count,
        "pace_warning": _pace_warning(pace_count),
        "last_captcha_at": (
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(_LAST_CAPTCHA_TS))
            if _LAST_CAPTCHA_TS
            else None
        ),
    }


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "google_search":
            query = arguments.get("query", "")
            limit = arguments.get("limit", CONFIG["limit"])
            hl = arguments.get("hl", CONFIG["hl"])
            gl = arguments.get("gl", CONFIG["gl"])
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query is required")
            if len(query) > 512:
                raise ValueError("query is too long")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                raise ValueError("limit must be an integer from 1 to 20")
            for label, value in (("hl", hl), ("gl", gl)):
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z-]{2,8}", value.strip()):
                    raise ValueError(f"{label} must be a 2-8 letter language/region code")
            results, waited, pace = await _search_google(query.strip(), limit, hl.strip(), gl.strip())
            payload = {
                "success": True,
                "data": {
                    "web": results,
                    "waited_ms": int(waited * 1000),
                    "pace_warning": pace,
                },
            }
        elif name == "fetch_url":
            url = arguments.get("url", "")
            char_limit = arguments.get("char_limit", CONFIG["char_limit"])
            format = arguments.get("format", CONFIG["format"])
            if not isinstance(url, str) or not url.strip():
                raise ValueError("url is required")
            if len(url) > 2048:
                raise ValueError("url is too long")
            if isinstance(char_limit, bool) or not isinstance(char_limit, int) or not 100 <= char_limit <= 200000:
                raise ValueError("char_limit must be an integer from 100 to 200000")
            if format not in ("text", "markdown", "links"):
                raise ValueError("format must be one of text, markdown, links")
            payload = {"success": True, "data": await _fetch_page(url.strip(), char_limit, format)}
        elif name == "health_check":
            if arguments:
                raise ValueError("health_check takes no arguments")
            payload = {"success": True, "data": _health_status()}
        else:
            raise ValueError(f"Unknown tool: {name}")
    except CaptchaRequired as exc:
        payload = {"success": False, "error": str(exc), "captcha_required": True}
    except Exception as exc:
        payload = {"success": False, "error": str(exc)}
    return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main() -> None:
    loop = asyncio.get_running_loop()
    state: dict = {"task": None, "handling": False}

    def _teardown() -> None:
        """Synchronous best-effort teardown used from the signal callback."""
        task = state.get("task")
        if task is not None:
            task.cancel()
        _RUNTIME.cleanup()
        # The stdio transport's writer task blocks on open pipes; os._exit is
        # the deterministic way to leave without waiting on the task group.
        # All owned processes (Chrome and any display backend) are terminated above.
        os._exit(0)

    async def _on_stop(signum: int) -> None:
        if state["handling"]:
            return
        state["handling"] = True
        _teardown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(_on_stop(s)))
        except NotImplementedError:
            signal.signal(sig, lambda *_: _teardown())

    async with stdio_server() as (read_stream, write_stream):
        run_task = asyncio.create_task(
            app.run(read_stream, write_stream, app.create_initialization_options())
        )
        state["task"] = run_task
        await run_task
        # run_task finishes only when the client closes stdin (EOF): exit the
        # transport context so the reader/writer tasks wind down normally.


atexit.register(_RUNTIME.cleanup)
if not os.environ.get("CW_PROFILE_DIR"):
    # Throwaway per-session profile: remove it on process exit (the startup
    # sweep also reaps these if the process is killed hard).
    atexit.register(lambda: shutil.rmtree(PROFILE_DIR, ignore_errors=True))


def run() -> None:
    """Run the MCP stdio server for console-script and python -m users."""
    try:
        asyncio.run(main())
    finally:
        _RUNTIME.cleanup()


if __name__ == "__main__":
    run()
