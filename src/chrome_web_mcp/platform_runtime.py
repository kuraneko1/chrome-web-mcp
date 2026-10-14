"""Small platform boundary for Chrome discovery and process identity.

The browser/network logic is intentionally kept out of this module.  It only
contains the pieces that differ materially between Linux and macOS so the main
server does not need scattered ``if sys.platform`` checks.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CHROME_DIR = PROJECT_ROOT / ".local-chrome"


def platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def platform_description() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def browser_mode(show_browser: bool, override: str = "") -> str:
    """Map the existing show_browser interface to a platform-native backend."""
    requested = override.strip().lower()
    if platform_key() == "macos":
        aliases = {"xephyr": "native", "xvfb": "headless"}
        requested = aliases.get(requested, requested)
        if requested:
            if requested not in {"native", "headless"}:
                raise ValueError("CW_DISPLAY_MODE on macOS must be native or headless")
            return requested
        return "native" if show_browser else "headless"
    if platform_key() == "linux":
        aliases = {"native": "xephyr", "headless": "xvfb"}
        requested = aliases.get(requested, requested)
        if requested:
            if requested not in {"xephyr", "xvfb"}:
                raise ValueError("CW_DISPLAY_MODE on Linux must be xephyr or xvfb")
            return requested
        return "xephyr" if show_browser else "xvfb"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _mac_app_binary(app_name: str) -> str:
    return f"/Applications/{app_name}.app/Contents/MacOS/{app_name}"


def _user_mac_app_binary(app_name: str) -> str:
    return str(Path.home() / "Applications" / f"{app_name}.app" / "Contents" / "MacOS" / app_name)


def bundled_chrome_for_testing_candidates() -> list[str]:
    """Find project-local Chrome for Testing builds without touching /Applications.

    ``.local-chrome`` is intentionally git-ignored.  Keeping CfT there makes a
    checkout self-contained on machines where the user does not want to install
    a normal Chrome app globally.
    """
    if not LOCAL_CHROME_DIR.is_dir():
        return []
    relative = Path("Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    direct = LOCAL_CHROME_DIR / relative
    candidates: list[Path] = [direct] if direct.is_file() else []
    try:
        discovered = sorted(
            LOCAL_CHROME_DIR.glob(f"**/{relative}"),
            key=lambda path: str(path),
        )
    except OSError:
        discovered = []
    seen = {str(path) for path in candidates}
    for path in discovered:
        if str(path) not in seen:
            candidates.append(path)
            seen.add(str(path))
    return [str(path) for path in candidates]


def chrome_candidates() -> list[str]:
    """Return executable candidates in fail-closed preference order."""
    configured = os.environ.get("CW_CHROME", "").strip()
    if configured:
        return [str(Path(configured).expanduser())]

    if platform_key() == "macos":
        # macOS port priority: explicit override, project-local Chrome for
        # Testing, stable Chrome, Chromium, then prerelease Chrome.
        bundled = bundled_chrome_for_testing_candidates()
        direct = [
            _mac_app_binary("Google Chrome"),
            _user_mac_app_binary("Google Chrome"),
        ]
        path_stable = [shutil.which("google-chrome")]
        chromium = [
            _mac_app_binary("Chromium"),
            _user_mac_app_binary("Chromium"),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
        ]
        prerelease = [
            _mac_app_binary("Google Chrome Beta"),
            _user_mac_app_binary("Google Chrome Beta"),
            _mac_app_binary("Google Chrome Canary"),
            _user_mac_app_binary("Google Chrome Canary"),
        ]
        return [
            str(item)
            for item in bundled + direct + path_stable + chromium + prerelease
            if item
        ]

    if platform_key() == "linux":
        direct = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        path_names = [shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser")]
        return [str(item) for item in direct + path_names if item]

    return []


def discover_chrome() -> str:
    configured = os.environ.get("CW_CHROME", "").strip()
    candidates = chrome_candidates()
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    if configured:
        raise RuntimeError(f"CW_CHROME is not an executable Chrome/Chromium binary: {configured}")
    raise RuntimeError(f"No supported Chrome/Chromium executable found for {platform_key()}")


def chrome_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not query Chrome version: {exc}") from exc
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    if completed.returncode != 0 or not text:
        raise RuntimeError("Chrome version query failed")
    return text[0][:300]


def process_identity(pid: int) -> dict:
    """Return a same-user PID birth identity that survives PID reuse checks."""
    if pid <= 1:
        raise ValueError("Invalid process id")
    try:
        proc = psutil.Process(pid)
        uids = proc.uids()
        if uids.real != os.getuid() and uids.effective != os.getuid():
            raise ValueError("Process is owned by another user")
        created = proc.create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise ProcessLookupError(pid) from exc
    except psutil.AccessDenied as exc:
        raise ValueError("Process identity is not accessible") from exc
    # String form avoids platform float/json comparison noise while retaining
    # microsecond precision from psutil's native process APIs.
    return {"pid": pid, "create_time": f"{created:.6f}"}


def process_summary(pid: int) -> tuple[bool, str]:
    """Return (alive, short command) for a same-user process."""
    try:
        proc = psutil.Process(pid)
        uids = proc.uids()
        if uids.real != os.getuid() and uids.effective != os.getuid():
            return True, "another-user process"
        cmdline = proc.cmdline()
        name = cmdline[0] if cmdline else proc.name()
        return True, name[:120]
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False, ""
    except psutil.AccessDenied:
        return True, "inaccessible process"


def processes_with_exact_arg(argument: str) -> list[dict]:
    """Find same-user processes carrying one exact argv token.

    Used only as a conservative legacy-profile recovery path.  New sessions use
    recorded PID/create_time identities instead.
    """
    records: list[dict] = []
    for proc in psutil.process_iter(["pid", "uids", "cmdline"]):
        try:
            info = proc.info
            uids = info.get("uids")
            if uids and uids.real != os.getuid() and uids.effective != os.getuid():
                continue
            if argument in (info.get("cmdline") or []):
                records.append(process_identity(int(info["pid"])))
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, ValueError, ProcessLookupError):
            continue
    return records
