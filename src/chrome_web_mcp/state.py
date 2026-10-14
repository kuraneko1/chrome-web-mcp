"""Cross-process search pacing and engine-health state.

This module owns the shared rate limiter and the engine-health store:
``_env_float``/``_env_flag`` read environment overrides, ``SharedSearchRateLimiter``
reserves globally spaced Google-search start slots through a SQLite table, and
``SearchEngineState`` tracks per-engine CAPTCHA cooldowns/strikes plus an hourly
search budget so separate MCP processes agree. Both classes fall back to
``_CONFIG_DEFAULTS`` when constructed without a ``config`` mapping; ``server``
passes its live ``CONFIG`` in and keeps the ``_SEARCH_LIMITER`` / ``_ENGINE_STATE``
instances themselves, because tests replace those module attributes.
"""

from __future__ import annotations

import os
import random
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from chrome_web_mcp.config import _CONFIG_DEFAULTS


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
        config: Mapping[str, Any] | None = None,
    ) -> None:
        defaults = _CONFIG_DEFAULTS if config is None else config
        if min_delay is None:
            min_delay = _env_float("CW_MIN_DELAY", float(defaults["min_delay"]))
        if max_delay is None:
            max_delay = _env_float("CW_MAX_DELAY", float(defaults["max_delay"]))
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


class SearchEngineState:
    """Cross-process engine health: CAPTCHA cooldowns and an hourly budget.

    Stored next to the pacing table so separate MCP processes agree on which
    engines are temporarily unusable. Only Google is throttled - the fallback
    engines exist precisely to keep searches working while Google cools down.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        cooldown_s: float | None = None,
        cooldown_max_s: float | None = None,
        budget: int | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        defaults = _CONFIG_DEFAULTS if config is None else config
        default_path = Path(tempfile.gettempdir()) / "chrome-web-mcp" / "search-rate-limit.sqlite3"
        configured = os.environ.get("CW_RATE_LIMIT_DB")
        self.db_path = Path(db_path or configured or default_path)
        if cooldown_s is None:
            cooldown_s = _env_float("CW_CAPTCHA_COOLDOWN", float(defaults["captcha_cooldown_s"]))
        if cooldown_max_s is None:
            cooldown_max_s = _env_float(
                "CW_CAPTCHA_COOLDOWN_MAX", float(defaults["captcha_cooldown_max_s"])
            )
        if budget is None:
            raw = os.environ.get("CW_MAX_SEARCHES_PER_HOUR", "").strip()
            try:
                budget = int(raw) if raw else int(defaults["max_searches_per_hour"])
            except ValueError:
                budget = int(defaults["max_searches_per_hour"])
        self.cooldown_s = max(0.0, cooldown_s)
        self.cooldown_max_s = max(self.cooldown_s, cooldown_max_s)
        self.budget = max(0, budget)

    # Cap the persisted strike count: ``2 ** (strikes - 1)`` is unbounded for a
    # corrupted or hostile row, and only burns CPU (the cooldown itself is
    # already capped by cooldown_max_s).
    _MAX_STRIKES = 32

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS engine_state ("
            "engine TEXT PRIMARY KEY, strikes INTEGER NOT NULL DEFAULT 0, "
            "cooldown_until REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS search_log ("
            "ts REAL NOT NULL, engine TEXT NOT NULL)"
        )
        return connection

    def note_challenge(self, engine: str) -> float:
        """Record a challenge; return the cooldown applied (seconds).

        Repeated challenges back off exponentially so an aggressive client stops
        hammering Google instead of walking into a longer ban.
        """
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT strikes FROM engine_state WHERE engine = ?", (engine,)
            ).fetchone()
            strikes = int(row[0]) + 1 if row else 1
            strikes = min(strikes, self._MAX_STRIKES)
            cooldown = min(self.cooldown_s * (2 ** (strikes - 1)), self.cooldown_max_s)
            connection.execute(
                "INSERT INTO engine_state(engine, strikes, cooldown_until, updated_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(engine) DO UPDATE SET "
                "strikes=excluded.strikes, cooldown_until=excluded.cooldown_until, "
                "updated_at=excluded.updated_at",
                (engine, strikes, now + cooldown, now),
            )
            connection.commit()
            return cooldown
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def note_success(self, engine: str) -> None:
        """Clear the cooldown and the strike count after a clean search."""
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO engine_state(engine, strikes, cooldown_until, updated_at) "
                "VALUES(?, 0, 0, ?) ON CONFLICT(engine) DO UPDATE SET "
                "strikes=0, cooldown_until=0, updated_at=excluded.updated_at",
                (engine, time.time()),
            )
        finally:
            connection.close()

    def cooldown_remaining(self, engine: str) -> float:
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT cooldown_until FROM engine_state WHERE engine = ?", (engine,)
                ).fetchone()
            finally:
                connection.close()
        except Exception:
            return 0.0
        if not row:
            return 0.0
        return max(0.0, float(row[0]) - time.time())

    def note_search(self, engine: str) -> None:
        connection = self._connect()
        try:
            connection.execute("INSERT INTO search_log(ts, engine) VALUES(?, ?)", (time.time(), engine))
            connection.execute("DELETE FROM search_log WHERE ts < ?", (time.time() - 7200,))
        finally:
            connection.close()

    def searches_last_hour(self) -> int:
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM search_log WHERE ts >= ?", (time.time() - 3600,)
                ).fetchone()
            finally:
                connection.close()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def budget_exhausted(self) -> bool:
        if self.budget <= 0:
            return False
        return self.searches_last_hour() >= self.budget

    def snapshot(self) -> dict:
        return {
            "cooldowns": {"google": round(self.cooldown_remaining("google"), 1)},
            "searches_last_hour": self.searches_last_hour(),
            "max_searches_per_hour": self.budget,
            "budget_exhausted": self.budget_exhausted(),
        }


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env override (1/0, true/false, yes/no, on/off)."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default
