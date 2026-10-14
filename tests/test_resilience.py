"""CAPTCHA resilience: shared cooldowns, hourly budget, and engine fallback.

Google is the only engine that can issue an interactive CAPTCHA, so these tests
drive the orchestrator with fake engines and assert that a challenged Google
never fails the call while any other configured engine still answers.
"""

import asyncio
import time

import pytest

from chrome_web_mcp import server


def run(coro):
    return asyncio.run(coro)


async def _identity(href):
    return href


@pytest.fixture
def engines_only(monkeypatch):
    """Return a helper that pins the configured engine list."""
    def configure(names, searxng_url=""):
        monkeypatch.setitem(server.CONFIG, "search_engines", list(names))
        monkeypatch.setitem(server.CONFIG, "searxng_url", searxng_url)
    return configure


@pytest.fixture
def state(monkeypatch, tmp_path):
    """An isolated engine-state database for one test."""
    fresh = server.SearchEngineState(
        tmp_path / "state.sqlite3", cooldown_s=10.0, cooldown_max_s=40.0, budget=3
    )
    monkeypatch.setattr(server, "_ENGINE_STATE", fresh)
    return fresh


# --------------------------------------------------------------------------- #
# Shared engine state
# --------------------------------------------------------------------------- #

def test_cooldown_grows_exponentially_and_is_capped(state):
    assert state.cooldown_remaining("google") == 0.0
    assert state.note_challenge("google") == 10.0
    assert state.note_challenge("google") == 20.0
    assert state.note_challenge("google") == 40.0
    assert state.note_challenge("google") == 40.0  # capped at cooldown_max_s
    assert state.cooldown_remaining("google") > 30.0


def test_success_clears_the_cooldown(state):
    state.note_challenge("google")
    state.note_success("google")
    assert state.cooldown_remaining("google") == 0.0
    assert state.note_challenge("google") == 10.0  # strikes reset


def test_expired_cooldown_reports_zero(state, monkeypatch):
    state.note_challenge("google")
    future = time.time() + 3600
    monkeypatch.setattr(server.time, "time", lambda: future)
    assert state.cooldown_remaining("google") == 0.0


def test_hourly_budget_counts_recent_searches(state):
    assert state.budget_exhausted() is False
    for _ in range(3):
        state.note_search("google")
    assert state.searches_last_hour() == 3
    assert state.budget_exhausted() is True


def test_budget_of_zero_means_unlimited(monkeypatch, tmp_path):
    unlimited = server.SearchEngineState(tmp_path / "s.sqlite3", budget=0)
    for _ in range(50):
        unlimited.note_search("google")
    assert unlimited.budget_exhausted() is False


def test_snapshot_reports_health_fields(state):
    state.note_challenge("google")
    snap = state.snapshot()
    assert snap["cooldowns"]["google"] > 0
    assert snap["max_searches_per_hour"] == 3
    assert snap["budget_exhausted"] is False


# --------------------------------------------------------------------------- #
# Skip decisions and engine order
# --------------------------------------------------------------------------- #

def test_only_google_is_skipped_when_throttled(state):
    state.note_challenge("google")
    assert server._engine_skip_reason("google") is not None
    assert server._engine_skip_reason("ddg") is None


def test_hourly_budget_skips_google_only(state):
    for _ in range(3):
        state.note_search("google")
    reason = server._engine_skip_reason("google")
    assert reason is not None and "budget" in reason
    assert server._engine_skip_reason("brave") is None


def test_engine_order_follows_configuration(engines_only):
    engines_only(["google", "ddg", "brave"])
    assert server._engine_order("auto") == ["google", "ddg", "brave"]


def test_pinned_engine_is_tried_first(engines_only):
    engines_only(["google", "ddg", "brave"])
    assert server._engine_order("brave") == ["brave", "google", "ddg"]


def test_pinned_engine_must_be_enabled(engines_only):
    engines_only(["google", "ddg"])
    with pytest.raises(ValueError, match="not enabled"):
        server._engine_order("bing")
    with pytest.raises(ValueError, match="unknown engine"):
        server._engine_order("yandex")


def test_searxng_needs_a_configured_url(engines_only):
    engines_only(["google", "searxng"])
    assert server._engine_order("auto") == ["google"]
    engines_only(["google", "searxng"], "http://10.0.0.1:4000")
    assert server._engine_order("auto") == ["google", "searxng"]


# --------------------------------------------------------------------------- #
# Fallback orchestration
# --------------------------------------------------------------------------- #

def test_google_captcha_is_absorbed_by_the_next_engine(monkeypatch, engines_only, state):
    engines_only(["google", "ddg"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)
    calls = []

    async def fake_google(query, limit, hl="ja", gl="jp"):
        calls.append("google")
        raise server.CaptchaRequired("Google CAPTCHA detected")

    async def fake_ddg(engine, query, limit, hl, gl):
        calls.append(engine)
        return [{"title": "Fallback", "href": "https://example.com/f", "description": "d"}]

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_fetch_engine_candidates", fake_ddg)

    data = run(server._search_with_fallback("query", 3))

    assert calls == ["google", "ddg"]
    assert data["engine"] == "ddg"
    assert data["captcha_avoided"] is True
    assert [item["url"] for item in data["web"]] == ["https://example.com/f"]
    assert data["attempts"][0]["engine"] == "google"
    assert data["attempts"][0]["error"] == "captcha"
    assert data["attempts"][1] == {"engine": "ddg", "ok": True, "results": 1}
    # Google is now on cooldown for every process sharing the state database.
    assert state.cooldown_remaining("google") > 0


def test_google_is_skipped_while_the_cooldown_is_active(monkeypatch, engines_only, state):
    engines_only(["google", "ddg"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)
    state.note_challenge("google")
    touched = []

    async def fake_google(query, limit, hl="ja", gl="jp"):
        touched.append("google")
        return []

    async def fake_ddg(engine, query, limit, hl, gl):
        touched.append(engine)
        return [{"title": "Fallback", "href": "https://example.com/f", "description": "d"}]

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_fetch_engine_candidates", fake_ddg)

    data = run(server._search_with_fallback("query", 1))

    assert touched == ["ddg"], "Google must not be called during its cooldown"
    assert data["attempts"][0]["skipped"].startswith("cooldown active")
    assert data["engine"] == "ddg"


def test_engine_errors_advance_to_the_next_engine(monkeypatch, engines_only, state):
    engines_only(["google", "ddg", "brave"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_google(query, limit, hl="ja", gl="jp"):
        raise RuntimeError("Google rendered no usable external search results")

    async def fake_ddg(engine, query, limit, hl, gl):
        return []

    async def fake_brave(engine, query, limit, hl, gl):
        return [{"title": "Brave", "href": "https://example.com/b", "description": ""}]

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_fetch_engine_candidates",
                        lambda engine, *a: fake_ddg(engine, *a) if engine == "ddg"
                        else fake_brave(engine, *a))

    data = run(server._search_with_fallback("query", 2))

    assert data["engine"] == "brave"
    assert data["captcha_avoided"] is False
    assert [item["ok"] for item in data["attempts"]] == [False, False, True]


def test_all_engines_failing_raises_with_attempt_log(monkeypatch, engines_only, state):
    engines_only(["google", "ddg"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_google(query, limit, hl="ja", gl="jp"):
        raise server.CaptchaRequired("Google CAPTCHA detected")

    async def fake_ddg(engine, query, limit, hl, gl):
        raise server.CaptchaRequired("ddg returned a bot challenge page instead of results.")

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_fetch_engine_candidates", fake_ddg)

    with pytest.raises(server.SearchFailed) as exc:
        run(server._search_with_fallback("query", 1))

    assert exc.value.captcha is True
    assert [item["engine"] for item in exc.value.attempts] == ["google", "ddg"]
    assert "No engine returned results" in str(exc.value)


def test_search_failed_is_reported_with_captcha_flag(monkeypatch, engines_only, state):
    engines_only(["google"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_google(query, limit, hl="ja", gl="jp"):
        raise server.CaptchaRequired("Google CAPTCHA detected")

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)

    content = run(server.call_tool("google_search", {"query": "q", "limit": 1}))
    import json

    payload = json.loads(content[0].text)
    assert payload["success"] is False
    assert payload["captcha_required"] is True
    assert payload["attempts"][0]["error"] == "captcha"


def test_success_clears_a_stale_google_cooldown(monkeypatch, engines_only, state):
    engines_only(["google"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_google(query, limit, hl="ja", gl="jp"):
        return [{"title": "T", "href": "https://example.com/ok", "description": ""}]

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    state.note_challenge("google")
    state.note_success("google")  # simulate the cooldown having expired

    data = run(server._search_with_fallback("query", 1))
    assert data["engine"] == "google"
    assert state.cooldown_remaining("google") == 0.0


def test_engine_redirect_wrappers_are_decoded_before_validation(monkeypatch, engines_only, state):
    """DDG/Bing wrappers must be decoded or every fallback search looks empty.

    Regression: the first end-to-end run returned "no usable results" for a
    pinned DuckDuckGo search because ``//duckduckgo.com/l/?uddg=`` hrefs reached
    the URL validator still wrapped.
    """
    engines_only(["ddg"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_ddg(engine, query, limit, hl, gl):
        return [{
            "title": "Hermes",
            "href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freal&rut=abc",
            "description": "d",
        }]

    monkeypatch.setattr(server, "_fetch_engine_candidates", fake_ddg)

    data = run(server._search_with_fallback("query", 1))
    assert [item["url"] for item in data["web"]] == ["https://example.com/real"]


def test_engine_internal_links_are_dropped(monkeypatch, engines_only, state):
    engines_only(["ddg"])
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_ddg(engine, query, limit, hl, gl):
        return [
            {"title": "Settings", "href": "https://duckduckgo.com/settings", "description": ""},
            {"title": "Real", "href": "https://example.com/ok", "description": ""},
        ]

    monkeypatch.setattr(server, "_fetch_engine_candidates", fake_ddg)

    data = run(server._search_with_fallback("query", 5))
    assert [item["url"] for item in data["web"]] == ["https://example.com/ok"]


def test_searxng_engine_is_used_when_configured(monkeypatch, engines_only, state):
    engines_only(["google", "searxng"], "http://searx.local:4000")
    monkeypatch.setattr(server, "_resolve_candidate_url", _identity)

    async def fake_google(query, limit, hl="ja", gl="jp"):
        raise server.CaptchaRequired("Google CAPTCHA detected")

    async def fake_searxng(query, limit, hl):
        return [{"title": "S", "href": "https://example.com/s", "description": ""}]

    monkeypatch.setattr(server, "_extract_google_candidates", fake_google)
    monkeypatch.setattr(server, "_searxng_candidates", fake_searxng)

    data = run(server._search_with_fallback("query", 1))
    assert data["engine"] == "searxng"
