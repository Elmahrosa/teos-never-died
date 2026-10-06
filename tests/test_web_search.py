"""Web search tests.

Everything network-y is monkeypatched - the suite never touches the real
DuckDuckGo. The point is to pin the parsing (DuckDuckGo's /l/ redirect
decode), the instant-answer/HTML merge order, the dedupe, the cache, and the
"never claim success when every provider failed" contract.
"""
import gzip
import json

import pytest

import web_search
from web_search import SearchError

# A faithful slice of html.duckduckgo.com's results markup.
DDG_PAGE = """<!DOCTYPE html><html><head><title>DuckDuckGo</title></head>
<body>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle%3Futm_source%3Dddg&amp;rut=abc">Example Article: The Facts</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle">A short factual snippet about the article.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.org%2Fpage">Other Org Page</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.org%2Fpage">Another snippet.</a>
  </div>
</div>
</body></html>"""


# ── parsing ────────────────────────────────────────────────────────────
def test_parse_html_results_extracts_titles_urls_snippets():
    results = web_search._parse_html_results(DDG_PAGE)
    assert len(results) == 2
    assert results[0]["title"] == "Example Article: The Facts"
    assert results[0]["url"] == "https://example.com/article?utm_source=ddg"
    assert results[0]["snippet"] == "A short factual snippet about the article."
    assert results[1]["title"] == "Other Org Page"
    assert results[1]["url"].startswith("https://other.org/")


def test_parse_html_results_empty_page():
    assert web_search._parse_html_results("<html><body>no results</body></html>") == []
    assert web_search._parse_html_results("") == []
    assert web_search._parse_html_results(None) == []


def test_real_url_decodes_uddg():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x"
    assert web_search._real_url(href) == "https://example.com/a?b=1"
    assert web_search._real_url("https://direct.example.com/x") == "https://direct.example.com/x"
    assert web_search._real_url("/not-a-real-url") is None
    assert web_search._real_url("//duckduckgo.com/l/?rut=x") is None


# ── search() merge behaviour ───────────────────────────────────────────
def test_search_uses_instant_answer_first(monkeypatch):
    calls = []

    def fake_instant(q):
        calls.append("instant")
        return {"title": "Python 3.13",
                "url": "https://python.org",
                "snippet": "released October 2024", "_fact": True}

    def fake_html(q):
        calls.append("html")
        return [{"title": "T", "url": "https://example.com/x", "snippet": "s"}]

    monkeypatch.setattr(web_search, "_instant_answer", fake_instant)
    monkeypatch.setattr(web_search, "_html_search", fake_html)
    results = web_search.search("python 3.13 release")
    assert results[0]["title"] == "Python 3.13"
    assert results[1]["url"] == "https://example.com/x"
    assert calls == ["instant", "html"]


def test_search_falls_back_to_html_when_instant_empty(monkeypatch):
    monkeypatch.setattr(web_search, "_instant_answer", lambda q: None)
    monkeypatch.setattr(
        web_search, "_html_search",
        lambda q: [{"title": "T", "url": "https://e.com", "snippet": "s"}])
    results = web_search.search("anything")
    assert len(results) == 1
    assert results[0]["url"] == "https://e.com"


def test_search_keeps_fact_only_when_html_fails(monkeypatch):
    monkeypatch.setattr(
        web_search, "_instant_answer",
        lambda q: {"title": "F", "url": "https://f.com", "snippet": "fact"})

    def boom(q):
        raise SearchError("provider refused")

    monkeypatch.setattr(web_search, "_html_search", boom)
    results = web_search.search("capitals")
    assert results[0]["title"] == "F"


def test_search_raises_when_every_source_fails(monkeypatch):
    web_search._CACHE.clear()

    def boom(q):
        raise SearchError("network down")

    monkeypatch.setattr(web_search, "_instant_answer", boom)
    monkeypatch.setattr(web_search, "_html_search", boom)
    with pytest.raises(SearchError):
        web_search.search("anything")


def test_search_dedupes_by_url(monkeypatch):
    monkeypatch.setattr(web_search, "_instant_answer", lambda q: None)
    monkeypatch.setattr(web_search, "_html_search", lambda q: [
        {"title": "T", "url": "https://e.com/a?utm=1", "snippet": "s"},
        {"title": "T2", "url": "https://e.com/a?utm=2", "snippet": "s2"},
        {"title": "T3", "url": "https://e.com/b", "snippet": "s3"},
    ])
    results = web_search.search("dedupe me")
    assert [r["url"] for r in results] == ["https://e.com/a?utm=1",
                                           "https://e.com/b"]


def test_search_respects_n_limit(monkeypatch):
    monkeypatch.setattr(web_search, "_instant_answer", lambda q: None)
    monkeypatch.setattr(web_search, "_html_search", lambda q: [
        {"title": f"T{i}", "url": f"https://e.com/{i}",
         "snippet": "s"} for i in range(10)])
    assert len(web_search.search("many", n=3)) == 3


def test_search_empty_query_raises():
    with pytest.raises(SearchError):
        web_search.search("   ")


# ── cache ──────────────────────────────────────────────────────────────
def test_search_serves_second_call_from_cache(monkeypatch):
    count = {"n": 0}

    def fake_html(q):
        count["n"] += 1
        return [{"title": "T", "url": "https://e.com", "snippet": "s"}]

    monkeypatch.setattr(web_search, "_instant_answer", lambda q: None)
    monkeypatch.setattr(web_search, "_html_search", fake_html)
    key = "cached query"
    first = web_search.search(key)
    hits_before = count["n"]
    second = web_search.search(key)
    assert count["n"] == hits_before
    assert second == first
    web_search._CACHE.pop(key.lower(), None)


# ── fetch_page ─────────────────────────────────────────────────────────
def test_fetch_page_strips_tags_and_scripts(monkeypatch):
    payload = (b"<html><body><script>var x=1;</script>"
               b"<p>Hello <b>World</b>!</p></body></html>")
    monkeypatch.setattr(web_search, "_get", lambda url, timeout: payload)
    text = web_search.fetch_page("https://e.com/x")
    assert "Hello World !" in text
    assert "var x=" not in text


def test_fetch_page_truncates_to_max_chars(monkeypatch):
    payload = ("<p>" + "word " * 500 + "</p>").encode()
    monkeypatch.setattr(web_search, "_get", lambda url, timeout: payload)
    text = web_search.fetch_page("https://e.com/x", max_chars=100)
    assert len(text) <= 101
    assert "\u2026" in text


def test_get_gunzips_gzip_responses(monkeypatch):
    """gzip decompression lives inside _get, before callers see bytes."""
    import urllib.request
    payload = gzip.compress(b"<html><body>compressed content</body></html>")

    class FakeResp:
        status = 200
        headers = {"Content-Encoding": "gzip"}

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout: FakeResp())
    raw = web_search._get("https://e.com/gz", 10)
    assert b"compressed content" in raw