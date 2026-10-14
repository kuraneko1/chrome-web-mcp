"""Configuration defaults and JSON config-file loading.

This module owns the ``_CONFIG_DEFAULTS`` mapping, the stderr warning helper and
``_load_config()``: it merges an optional JSON file (``CW_CONFIG`` or
``~/.config/chrome-web-mcp/config.json``) over the defaults, validating each key
individually and warning while keeping the default on bad input. The live
``CONFIG`` dict stays in ``server`` (which calls ``_load_config()`` at import),
because other modules and tests observe and replace that module attribute.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from chrome_web_mcp import engines


_CONFIG_DEFAULTS = {
    "show_browser": True,  # Linux: Xephyr/Xvfb | macOS: native/headless
    "hl": "ja",
    "gl": "jp",
    "limit": 5,
    "char_limit": 15000,
    "format": "markdown",
    "min_delay": 1.0,
    "max_delay": 2.5,
    # Profile lifetime: "persistent" keeps cookies/consent between runs (fewer
    # CAPTCHAs), "ephemeral" restores the throwaway per-pid profile.
    "profile": "persistent",
    # Engine order. Google is first, but the rest absorb a CAPTCHA so a search
    # still returns results instead of failing.
    "search_engines": ["google", "ddg", "brave", "bing"],
    # Self-hosted SearXNG base URL (enables the "searxng" engine when listed).
    "searxng_url": "",
    # Type the query into the Google box instead of navigating to /search?q=.
    "human_input": True,
    # Google-only safety valves: hourly budget and CAPTCHA cooldown.
    "max_searches_per_hour": 60,  # 0 disables the budget
    "captcha_cooldown_s": 300,
    "captcha_cooldown_max_s": 1800,
    # Platform fingerprint: "native" keeps Linux UA/platform consistent;
    # "windows" applies a matching Windows UA + platform + client hints.
    "spoof_platform": "native",
    # Keep Chrome's software rendering (stable on headless/CI hosts). Turn off
    # to let Chrome use the host GPU.
    "disable_gpu": True,
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
    if "profile" in data:
        value = str(data["profile"]).strip().lower()
        if value in ("persistent", "ephemeral"):
            cfg["profile"] = value
        else:
            _warn_config("profile must be persistent or ephemeral; using default")
    if "search_engines" in data:
        value = data["search_engines"]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            valid = set(engines.BROWSER_ENGINES) | set(engines.HTTP_ENGINES)
            picked = [item.strip().lower() for item in value]
            unknown = sorted({item for item in picked if item not in valid})
            if unknown:
                _warn_config(f"unknown search_engines entries ignored: {unknown}")
            usable = [item for item in picked if item in valid]
            if usable:
                cfg["search_engines"] = usable
            else:
                _warn_config("search_engines listed no usable engine; using default")
        else:
            _warn_config("search_engines must be a list of engine names; using default")
    if "searxng_url" in data:
        value = data["searxng_url"]
        if isinstance(value, str) and (
            not value.strip() or value.strip().startswith(("http://", "https://"))
        ):
            cfg["searxng_url"] = value.strip().rstrip("/")
        else:
            _warn_config("searxng_url must be an http(s) URL; using default")
    for key in ("human_input", "disable_gpu"):
        if key in data:
            if isinstance(data[key], bool):
                cfg[key] = data[key]
            else:
                _warn_config(f"{key} must be true or false; using default")
    if "spoof_platform" in data:
        value = str(data["spoof_platform"]).strip().lower()
        if value in ("native", "windows"):
            cfg["spoof_platform"] = value
        else:
            _warn_config("spoof_platform must be native or windows; using default")
    if "max_searches_per_hour" in data:
        value = data["max_searches_per_hour"]
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000:
            cfg["max_searches_per_hour"] = value
        else:
            _warn_config("max_searches_per_hour must be an integer from 0 to 10000; using default")
    cooldowns: dict[str, float] = {}
    for key in ("captcha_cooldown_s", "captcha_cooldown_max_s"):
        if key in data:
            value = data[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                cooldowns[key] = float(value)
            else:
                _warn_config(f"{key} must be a non-negative number; using default")
    if cooldowns:
        lo = cooldowns.get("captcha_cooldown_s", float(cfg["captcha_cooldown_s"]))
        hi = cooldowns.get("captcha_cooldown_max_s", float(cfg["captcha_cooldown_max_s"]))
        if hi >= lo:
            cfg.update(cooldowns)
        else:
            _warn_config("captcha_cooldown_max_s must be >= captcha_cooldown_s; using defaults")
    return cfg
