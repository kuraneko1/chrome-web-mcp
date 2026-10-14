"""Shared-browser registry: owner publication and attached-client heartbeats.

Several MCP server processes must be able to use ONE Chrome/Xvfb/CDP pair
(~600 MB each, measured) instead of starting one browser per process. The
profile ``flock`` already elects exactly one owner per profile, so this module
adds only the small file protocol on top of that election - no broker process:

  * ``PROFILE_DIR/.browser.json`` (mode 0600, atomic tmp+replace) publishes the
    owner's CDP endpoint, so a process that lost the election can attach.
  * ``PROFILE_DIR/clients/<pid>.json`` (mode 0600) is a heartbeat each attached
    client refreshes on every browser tool call, so the owner's idle reaper
    knows the browser is still in use by another process.

Everything here is fail-closed: a file that is not a private, same-user regular
file is reported as unusable (``RegistryError``) instead of trusted, and the
caller keeps today's behaviour (its own throwaway profile). ``server`` owns the
policy (who may attach, when to fall back); this module owns the file format,
its validation, and the CDP liveness probe.
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.parse
from pathlib import Path

from websockets.sync.client import connect as _ws_connect

from chrome_web_mcp import platform_runtime

REGISTRY_NAME = ".browser.json"
CLIENTS_DIRNAME = "clients"
# Bump when the payload gains a key the reader must understand.
REGISTRY_VERSION = 1
# These files only ever hold a handful of JSON fields; anything larger is not
# ours and must not be read into memory.
_REGISTRY_MAX_BYTES = 8 * 1024
_HEARTBEAT_MAX_BYTES = 4 * 1024
# A CDP browser endpoint must be loopback. Anything else could point this
# process at a stranger's DevTools port, so it is rejected before any connect.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RegistryError(RuntimeError):
    """The shared-browser file exists but cannot be trusted."""


def registry_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / REGISTRY_NAME


def clients_dir(profile_dir: Path) -> Path:
    return Path(profile_dir) / CLIENTS_DIRNAME


def heartbeat_path(profile_dir: Path, pid: int) -> Path:
    return clients_dir(profile_dir) / f"{pid}.json"


def _read_private_json(path: Path, *, max_bytes: int) -> dict:
    """Read a private, same-user, regular JSON object or raise RegistryError."""
    try:
        # O_NOFOLLOW plus fstat closes the symlink-swap window that a separate
        # lstat/open pair would leave open.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RegistryError(f"{path.name} could not be opened ({exc.strerror or exc})") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RegistryError(f"{path.name} is not a regular file")
        if info.st_uid != os.getuid():
            raise RegistryError(f"{path.name} is owned by another user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise RegistryError(
                f"{path.name} mode {stat.S_IMODE(info.st_mode):03o} is not private"
            )
        if info.st_size > max_bytes:
            raise RegistryError(f"{path.name} is implausibly large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            try:
                data = json.load(handle)
            except ValueError as exc:
                raise RegistryError(f"{path.name} is not valid JSON") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(data, dict):
        raise RegistryError(f"{path.name} is not a JSON object")
    return data


def _write_private_json(path: Path, payload: dict) -> None:
    """Atomically write 0600 JSON so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # The tmp name ends in .tmp, so a heartbeat scan never mistakes a partial
        # write for a client.
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        # Be explicit instead of trusting the process umask.
        os.chmod(pending, 0o600)
        pending.replace(path)
    except BaseException:
        try:
            pending.unlink()
        except OSError:
            pass
        raise


def write_registry(
    profile_dir: Path, *, pid: int, port: int, browser_ws: str, create_time: str
) -> None:
    """Publish the owner's CDP endpoint for sibling processes to attach to."""
    _write_private_json(
        registry_path(profile_dir),
        {
            "version": REGISTRY_VERSION,
            "pid": int(pid),
            "uid": os.getuid(),
            "started": time.monotonic(),
            "port": int(port),
            "browser_ws": str(browser_ws),
            "create_time": str(create_time),
        },
    )


def load_registry(profile_dir: Path) -> dict:
    """Return the validated owner registry, or raise RegistryError.

    Validation is deliberately paranoid: the caller is about to send its browser
    traffic to whatever endpoint this file names, so a malformed, world-readable,
    foreign, non-loopback or version-mismatched registry is refused outright.
    """
    data = _read_private_json(registry_path(profile_dir), max_bytes=_REGISTRY_MAX_BYTES)
    if data.get("version") != REGISTRY_VERSION:
        raise RegistryError("unsupported shared-browser registry version")
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise RegistryError("invalid owner pid")
    if data.get("uid") != os.getuid():
        raise RegistryError("registry uid does not match this user")
    if not isinstance(data.get("started"), (int, float)) or isinstance(data.get("started"), bool):
        raise RegistryError("invalid owner start time")
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RegistryError("invalid CDP port")
    browser_ws = data.get("browser_ws")
    if not isinstance(browser_ws, str):
        raise RegistryError("invalid browser_ws")
    try:
        parsed = urllib.parse.urlparse(browser_ws)
        ws_port = parsed.port
    except ValueError as exc:
        raise RegistryError("invalid browser_ws URL") from exc
    if parsed.scheme != "ws" or parsed.hostname not in _LOOPBACK_HOSTS or ws_port != port:
        raise RegistryError("browser_ws is not the loopback CDP endpoint on the registered port")
    create_time = data.get("create_time")
    if not isinstance(create_time, str) or not create_time:
        raise RegistryError("invalid owner birth stamp")
    return data


def remove_registry(profile_dir: Path, *, owner_pid: int) -> None:
    """Unlink the registry only while it still names ``owner_pid``.

    A client - or an owner whose registry a newer owner already replaced - must
    never delete a file it does not own.
    """
    try:
        data = _read_private_json(registry_path(profile_dir), max_bytes=_REGISTRY_MAX_BYTES)
    except (RegistryError, OSError):
        return
    if data.get("pid") != owner_pid:
        return
    try:
        registry_path(profile_dir).unlink()
    except OSError:
        pass


def write_heartbeat(
    profile_dir: Path, *, pid: int, create_time: str, last_used: float | None = None
) -> None:
    """Announce this attached client to the owner's idle reaper."""
    _write_private_json(
        heartbeat_path(profile_dir, pid),
        {
            "pid": int(pid),
            "uid": os.getuid(),
            "create_time": str(create_time),
            "last_used": time.time() if last_used is None else float(last_used),
        },
    )


def remove_heartbeat(profile_dir: Path, pid: int) -> None:
    """Drop our own heartbeat (missing is fine)."""
    try:
        heartbeat_path(profile_dir, pid).unlink()
    except OSError:
        pass


def live_client_pids(
    profile_dir: Path, *, now: float | None = None, idle_s: float | None = None
) -> list[int]:
    """Return the pids of attached clients, pruning entries that are not live.

    A heartbeat counts only when the file is a private same-user regular file,
    the recorded pid is still that exact process (birth stamp), and - when
    ``idle_s`` is given - it was refreshed less than ``idle_s`` seconds ago.
    Dead, reused, foreign, malformed and (with ``idle_s``) stale entries are
    removed, so a crashed client can never pin the owner's browser forever.
    """
    try:
        entries = sorted(clients_dir(profile_dir).glob("*.json"))
    except OSError:
        return []
    pids: list[int] = []
    for entry in entries:
        try:
            data = _read_private_json(entry, max_bytes=_HEARTBEAT_MAX_BYTES)
            pid = data.get("pid")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 1
                or entry.stem != str(pid)
            ):
                raise RegistryError("invalid client pid")
            if data.get("uid") != os.getuid():
                raise RegistryError("client belongs to another user")
            create_time = data.get("create_time")
            if not isinstance(create_time, str) or not create_time:
                raise RegistryError("invalid client birth stamp")
            identity = platform_runtime.process_identity(pid)
            if identity.get("create_time") != create_time:
                raise RegistryError("client pid was reused")
            if idle_s is not None and idle_s > 0:
                last_used = data.get("last_used")
                if not isinstance(last_used, (int, float)) or isinstance(last_used, bool):
                    raise RegistryError("invalid client heartbeat time")
                age = (time.time() if now is None else now) - float(last_used)
                if age >= idle_s:
                    raise RegistryError("stale client heartbeat")
        except (RegistryError, OSError, ValueError):
            # Prune so one bad file cannot slow every later scan down.
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        pids.append(pid)
    return pids


def live_client_count(
    profile_dir: Path, *, now: float | None = None, idle_s: float | None = None
) -> int:
    return len(live_client_pids(profile_dir, now=now, idle_s=idle_s))


def probe_browser(browser_ws: str, timeout: float = 2.0) -> bool:
    """True when the published CDP endpoint answers ``Browser.getVersion``.

    ``BrowserRuntime.ensure()`` runs in a worker thread (``asyncio.to_thread``),
    so the probe must be synchronous; ``websockets.sync`` keeps it short and
    self-contained without adding a dependency. A dead owner or a dead Chrome
    fails here instead of failing the tool call later.
    """
    try:
        # proxy=None: a loopback DevTools socket must never be routed through a
        # configured HTTP proxy.
        with _ws_connect(browser_ws, proxy=None, open_timeout=timeout, close_timeout=1) as connection:
            connection.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    message = json.loads(connection.recv(timeout=remaining))
                except TimeoutError:
                    return False
                if isinstance(message, dict) and message.get("id") == 1:
                    return isinstance(message.get("result"), dict)
    except Exception:
        return False
