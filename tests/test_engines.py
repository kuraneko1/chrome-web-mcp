"""Engine adapters: URL building, redirect unwrapping, and offline parsing.

The parsing tests run against saved result pages (``tests/fixtures``) captured
from the real engines, so selector drift is caught without touching the network.
"""

from pathlib import Path

import pytest

from chrome_web_mcp import engines

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# URL building
# --------------------------------------------------------------------------- #

def test_engine_url_encodes_query():
    assert engines.engine_url("ddg", "a b&c").startswith("https://html.duckduckgo.com/html/?q=a+b%26c")
    assert "q=a+b%26c" in engines.engine_url("brave", "a b&c")
    assert "q=a+b%26c" in engines.engine_url("bing", "a b&c")


def test_google_url_keeps_language_hints():
    url = engines.engine_url("google", "test", hl="en", gl="us")
    assert url.startswith("https://www.google.com/search?")
    assert "hl=en" in url and "gl=us" in url


def test_unknown_browser_engine_rejected():
    with pytest.raises(ValueError):
        engines.engine_url("yandex", "test")


def test_google_home_url_carries_hints():
    url = engines.google_home_url(hl="ja", gl="jp")
    assert url.startswith("https://www.google.com/?") and "hl=ja" in url


# --------------------------------------------------------------------------- #
# Redirect unwrapping
# --------------------------------------------------------------------------- #

def test_ddg_redirect_is_unwrapped():
    href = ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1"
            "&rut=deadbeef")
    assert engines.normalize_href("ddg", href) == "https://example.com/a?b=1"


def test_ddg_internal_link_is_dropped():
    assert engines.normalize_href("ddg", "https://duckduckgo.com/settings") is None


def test_bing_redirect_is_unwrapped():
    # base64url("https://example.com/page") with Bing's "a1" prefix.
    import base64

    payload = base64.urlsafe_b64encode(b"https://example.com/page").decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{payload}&ntb=1"
    assert engines.normalize_href("bing", href) == "https://example.com/page"


def test_bing_redirect_without_target_is_dropped():
    assert engines.normalize_href("bing", "https://www.bing.com/ck/a?!&&p=abc") is None


def test_non_http_schemes_are_dropped():
    for href in ("javascript:alert(1)", "mailto:a@b.c", "", "ftp://example.com/x"):
        assert engines.normalize_href("ddg", href) is None


# --------------------------------------------------------------------------- #
# Parsing real markup
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("engine", ["ddg", "brave", "bing"])
def test_fixture_parsing_returns_usable_results(engine):
    items = engines.parse_results(engine, fixture(engine), 5)
    assert len(items) == 5
    for item in items:
        assert item["title"], f"{engine}: empty title"
        url = engines.normalize_href(engine, item["href"])
        assert url and url.startswith("http"), f"{engine}: unusable href {item['href']!r}"
    # Every engine must yield the project's own site for this query.
    joined = " ".join(item["title"] + item["href"] for item in items)
    assert "hermes" in joined.lower() or "nous" in joined.lower()


def test_fixture_titles_keep_non_ascii():
    """Encoding pinning: UTF-8 titles must survive parsing (no mojibake)."""
    items = engines.parse_results("bing", fixture("bing"), 10)
    assert any("—" in item["title"] or "·" in item["description"] for item in items)


def test_ddg_descriptions_are_extracted():
    items = engines.parse_results("ddg", fixture("ddg"), 5)
    assert all(item["description"] for item in items)


def test_limit_is_respected():
    assert len(engines.parse_results("ddg", fixture("ddg"), 1)) == 1
    assert len(engines.parse_results("bing", fixture("bing"), 3)) == 3


def test_unknown_engine_parser_rejected():
    with pytest.raises(ValueError):
        engines.parse_results("yandex", "<html></html>", 3)


def test_empty_document_returns_no_results():
    assert engines.parse_results("ddg", "<html><body>nothing here</body></html>", 5) == []


def test_searxng_json_is_mapped():
    payload = {"results": [
        {"title": "Example", "url": "https://example.com", "content": "  spaced\ncontent "},
        {"title": "", "url": "https://ignored.example"},
    ]}
    items = engines.parse_searxng(payload, 5)
    assert items == [{"title": "Example", "href": "https://example.com",
                      "description": "spaced content"}]


# --------------------------------------------------------------------------- #
# Challenge/block detection and engine selection
# --------------------------------------------------------------------------- #

def test_google_sorry_page_is_recognized_as_blocked():
    assert engines.looks_blocked("google", "Our systems have detected unusual traffic from your computer network")
    assert engines.looks_blocked("ddg", "Please complete the following to continue: not a robot")


def test_normal_results_are_not_blocked():
    assert not engines.looks_blocked("brave", "Hermes Agent - open source AI agent by Nous Research")
    assert not engines.looks_blocked("ddg", "")


def test_available_engines_filters_and_dedupes():
    configured = ["google", "ddg", "google", "yandex", "searxng"]
    assert engines.available_engines(configured) == ["google", "ddg"]
    assert engines.available_engines(configured, "http://10.0.0.1:4000") == [
        "google", "ddg", "searxng"]
