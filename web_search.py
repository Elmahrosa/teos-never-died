"""Live web search for RAVEN - DuckDuckGo, no API key.

Two endpoints, both free and keyless:
  * api.duckduckgo.com  - Instant Answer JSON (fast facts, infoboxes)
  * html.duckduckgo.com - organic results as plain HTML

Everything here is pure stdlib (urllib + html.parser) so the module keeps
working even when the optional `requests` package is missing. `search()` is
synchronous and can block for seconds; the bot runs it through
asyncio.to_thread like every other blocking call on this CPU-only host.

Errors are raised as :class:`SearchError` so callers can distinguish "the
search provider is down" from "no results", and can fall back without ever
surfacing a timeout string to the user.
"""

import gzip
import html
import html.parser
import json
import os
import re
import urllib.parse
import urllib.request

SEARCH_TIMEOUT = float(os.getenv("RAVEN_SEARCH_TIMEOUT", "15").strip() or "15")
PAGE_TIMEOUT = float(os.getenv("RAVEN_SEARCH_PAGE_TIMEOUT", "20").strip() or "20")
CACHE_SIZE = int(os.getenv("RAVEN_SEARCH_CACHE", "64").strip() or "64")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

# Small query -> results cache. An agent loop can issue the same question
# twice; gaining one network round trip per repeat keeps it fast.
_CACHE = {}  # ordered dict, capped at CACHE_SIZE


class SearchError(Exception):
    """Raised when a search provider cannot be reached or refuses the query."""


def _get(url, timeout):
    """GET `url` and return the raw bytes, transparently decompressing gzip."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status in (403, 429):
                raise SearchError(
                    "the search provider refused the request (rate limit?)")
            raw = resp.read()
            if (resp.headers.get("Content-Encoding", "").lower() == "gzip"):
                raw = gzip.decompress(raw)
            return raw
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError(f"network error while searching: {exc}") from exc


def _ddg_link(query):
    return "https://duckduckgo.com/?q=" + urllib.parse.quote(query)


def _instant_answer(query):
    """DuckDuckGo Instant Answer API. Returns one dict or None."""
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"})
    raw = _get("https://api.duckduckgo.com/?" + params, SEARCH_TIMEOUT)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    abstract = (data.get("AbstractText") or "").strip()
    if not abstract:
        return None
    return {
        "title": (data.get("AbstractTitle") or query).strip(),
        "url": (data.get("AbstractURL") or _ddg_link(query)).strip(),
        "snippet": abstract,
        "_fact": True,
    }


_DDG_REDIRECT = re.compile(r"^//duckduckgo\.com/l/\?")


def _real_url(href):
    """Resolve a DuckDuckGo /l/ redirect to the underlying URL."""
    href = (href or "").strip()
    if _DDG_REDIRECT.match(href):
        parsed = urllib.parse.urlparse("https:" + href)
        values = urllib.parse.parse_qs(parsed.query).get("uddg")
        if values and values[0]:
            return urllib.parse.unquote(values[0])
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return None


class _ResultParser(html.parser.HTMLParser):
    """Extract (title, url, snippet) tuples from the DuckDuckGo HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._pairs = []  # (title, url) pending a snippet
        self._title = None
        self._href = None
        self._in_snippet = False
        self._snippet_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            self._title = []
            self._href = attrs.get("href", "")
        elif tag == "a" and "result__snippet" in cls:
            self._in_snippet = True
            self._snippet_parts = []

    def handle_data(self, data):
        if self._title is not None:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag):
        if tag != "a":
            return
        if self._title is not None:
            title = " ".join("".join(self._title).split()).strip()
            url = _real_url(self._href)
            if title and url:
                self._pairs.append((title, url))
            self._title = None
            self._href = None
        elif self._in_snippet and self._pairs:
            snippet = " ".join("".join(self._snippet_parts).split()).strip()
            self._pairs[-1] = (self._pairs[-1][0], self._pairs[-1][1], snippet)
            self._in_snippet = False
            self._snippet_parts = []  # keep pairs uniform once completed

    def close(self):
        super().close()
        for item in self._pairs:
            if len(item) == 3:
                self.results.append({
                    "title": item[0], "url": item[1], "snippet": item[2]})
            else:
                self.results.append({
                    "title": item[0], "url": item[1], "snippet": ""})


def _parse_html_results(page_text):
    """Parse the DuckDuckGo HTML results page into result dicts."""
    parser = _ResultParser()
    try:
        parser.feed(page_text or "")
        parser.close()
    except Exception:
        return []
    return parser.results


def _html_search(query):
    """DuckDuckGo HTML endpoint - organic results."""
    params = urllib.parse.urlencode({"q": query})
    raw = _get("https://html.duckduckgo.com/html/?" + params, SEARCH_TIMEOUT)
    return _parse_html_results(raw.decode("utf-8", "replace"))


def _dedupe(items):
    """Order-preserving dedupe by bare URL (fragment/query stripped)."""
    seen = set()
    out = []
    for r in items:
        key = ((r.get("url") or "").split("#")[0].split("?")[0]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def search(query, n=5):
    """Search the web. Returns a list of {title, url, snippet} dicts.

    Instant-answer facts (when present) come first, then organic results.
    Raises SearchError only when *every* source fails.
    """
    q = (query or "").strip()
    if not q:
        raise SearchError("empty search query")
    key = q.lower()
    if key in _CACHE:
        return [dict(r) for r in _CACHE[key]][:n]

    results = []
    try:
        fact = _instant_answer(q)
        if fact:
            results.append(fact)
    except SearchError:
        pass  # instant answer is best-effort; organic results may still work

    try:
        results.extend(_html_search(q))
    except SearchError as exc:
        if not results:
            raise exc
        # A fact-only answer is still a usable answer; do not fail the search.

    results = _dedupe(results)[:n]
    if len(_CACHE) >= CACHE_SIZE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = results
    return [dict(r) for r in results]


_TAG_SKIP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_STRIP_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _page_text(raw):
    """HTML bytes -> clean, whitespace-collapsed text."""
    text = raw.decode("utf-8", "replace")
    text = _TAG_SKIP.sub(" ", text)
    text = _STRIP_TAGS.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def fetch_page(url, max_chars=4000, timeout=None):
    """Fetch `url` and return readable text, truncated to max_chars."""
    raw = _get(url, timeout or PAGE_TIMEOUT)
    text = _page_text(raw)
    if len(text) > max_chars:
        cut = text[:max_chars]
        cut = cut[: cut.rfind(" ")] if " " in cut else cut
        text = cut.rstrip() + " …"
    return text