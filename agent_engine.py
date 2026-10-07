"""RAVEN's think -> search -> answer agent loop.

Satisfies the "answer, think, search, non-stop" requirement:
  * think    - a fast local model plans 1-3 real web queries before answering
  * search   - DuckDuckGo via web_search.py (no API key), plus deep page
               reads when the loop needs to dig deeper for a missing fact
  * answer   - the command model composes a cited answer from that context
  * non-stop - every failure is contained: a dead search falls back to
               knowledge, a dead model call degrades gracefully, and the loop
               always returns a real message - never a bare timeout error.

The loop is deliberately synchronous (blocking model + network calls) so the
bot can run it inside asyncio.to_thread. progress_cb is invoked from the
worker thread and the caller must marshal it back to the event loop.
"""

import logging
import os
import re

import raven
import web_search

logger = logging.getLogger("raven.agent")

DEFAULT_FAST_MODEL = (os.getenv("RAVEN_MODEL", "llama3.2:3b").strip()
                      or "llama3.2:3b")
DEFAULT_ANSWER_MODEL = (
    os.getenv("RAVEN_COMMAND_MODEL") or os.getenv("RAVEN_MODEL")
    or "dolphin-llama3:8b"
).strip()
DEFAULT_MAX_ROUNDS = int(os.getenv("RAVEN_AGENT_MAX_ROUNDS", "3").strip() or "3")
DEFAULT_TOP_RESULTS = int(os.getenv("RAVEN_AGENT_TOP_N", "6").strip() or "6")
DEFAULT_PAGE_READS = int(os.getenv("RAVEN_AGENT_PAGE_READS", "2").strip() or "2")

AGENT_IDENTITY = os.getenv(
    "RAVEN_AGENT_IDENTITY",
    "You are RAVEN, a precise research assistant running on a CPU-only host "
    "with live web search. Base your answer on the SEARCH CONTEXT provided; "
    "cite sources inline as [1], [2], ... Never present a guess as a "
    "verified fact, and never claim to have opened a page you were not given.",
).strip()

PLAN_SYSTEM = (
    "You are RAVEN's search planner. Given a question, decide what web "
    "lookups would actually help and list 1-3 short, specific search queries, "
    "one per line, each starting with 'Q:'. Only ask for lookups that need "
    "fresh or current information. If the question can be fully answered from "
    "general knowledge, reply with exactly: NONE"
)

ANSWER_SYSTEM = (
    AGENT_IDENTITY + "\n\n"
    "Answer the question using ONLY the SEARCH CONTEXT and PAGE EXCERPTS "
    "given in the user message. If the context is enough, end your reply with "
    "a single final line: [DONE].\n"
    "If a specific fact is missing and one more targeted search would find "
    "it, say what is missing and end with a single final line: "
    "[NEED_MORE: <one refined search query>].\n"
    "Be complete but tight: bullets over prose, no preamble."
)

_Q_LINE = re.compile(r"^Q[:\-\u2013]\s*(.+)$", re.IGNORECASE)
_NEED_MORE = re.compile(r"\[NEED_MORE:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_DONE_LINE = re.compile(r"\s*\[DONE\]\s*$", re.IGNORECASE)

# Phrases that strongly hint the question needs fresh, current information.
_WEB_WORDS = (
    "search", "look up", "lookup", "google", "find out",
    "latest", "breaking", "news", "current", "today", "tomorrow",
    "price of", "how much is", "value of", "exchange rate",
    "fact check", "verify", "is this real", "this week", "this year",
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _model_call(model, system, user):
    """Blocking local-model call. Indirection point for tests."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return raven.get_reply(model, messages)


def _extract_queries(reply):
    """Pull 'Q: ...' lines out of the planner's reply."""
    queries = []
    if not reply:
        return queries
    for line in reply.splitlines():
        line = line.strip()
        m = _Q_LINE.match(line)
        if not m:
            continue
        q = " ".join(m.group(1).split()).strip().strip("?.")
        if (q and len(q) <= 200
                and q.lower() not in ("none", "nothing", "n/a")):
            queries.append(q)
        if len(queries) >= 3:
            break
    return queries


def _extract_verdict(answer):
    """Split a model answer into (clean_text, refined_query_or_None).

    A trailing [DONE] is removed. A [NEED_MORE: query] marker is removed from
    the text and its query returned so the loop can search it next round.
    """
    if not answer:
        return "", None
    m = _NEED_MORE.search(answer)
    if m:
        query = " ".join(m.group(1).split()).strip().strip("?")
        clean = _NEED_MORE.sub("", answer)
        clean = _DONE_LINE.sub("", clean).strip()
        return clean or "", query or None
    clean = _DONE_LINE.sub("", answer).strip()
    return clean or "", None


def _search_all(queries):
    """Run every query, merging results. Never raises."""
    results = []
    err = None
    for q in queries:
        try:
            results.extend(web_search.search(q))
        except Exception as exc:
            if not err:
                err = str(exc).replace("\u274c", "").strip()
    return results, err


def _merge_results(used, fresh, cap=None):
    """Append fresh results to used, deduped by bare URL, capped."""
    cap = cap or DEFAULT_TOP_RESULTS
    seen = {r.get("url", "").split("#")[0].split("?")[0].lower()
            for r in used}
    out = list(used)
    for r in fresh:
        key = (r.get("url") or "").split("#")[0].split("?")[0].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= cap:
            break
    return out


def _format_context(used):
    if not used:
        return "SEARCH CONTEXT:\n(no web results were available)"
    lines = ["SEARCH CONTEXT:"]
    for i, r in enumerate(used, 1):
        lines.append(f"[{i}] {(r.get('title') or '').strip()}")
        lines.append(f"    {(r.get('url') or '').strip()}")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)


def _fetch_pages(used, limit=None):
    """Read a few of the top results into plain text. Never raises."""
    limit = limit or DEFAULT_PAGE_READS
    pages = {}
    for r in used[:limit]:
        url = (r.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        try:
            text = web_search.fetch_page(url)
        except Exception:
            continue
        if text:
            pages[url] = text
    return pages


def _format_pages(pages):
    if not pages:
        return ""
    chunks = ["PAGE EXCERPTS:"]
    for url, text in pages.items():
        chunk = text[:1400]
        chunks.append(f"<{url}>\n{chunk}")
    return "\n\n".join(chunks)


def _compose_user(query, used, pages, previous_answers):
    parts = [f"QUESTION:\n{query}", "", _format_context(used)]
    page_block = _format_pages(pages)
    if page_block:
        parts.extend(["", page_block])
    if previous_answers:
        parts.extend([
            "",
            "PREVIOUS PARTIAL ANSWER (do not repeat it - improve on it):",
            previous_answers[-1],
        ])
    parts.extend(["", "Answer the QUESTION now, citing [1]..[n] inline."])
    return "\n".join(parts)


def run_agent(query, max_rounds=None, progress_cb=None,
              fast_model=None, answer_model=None):
    """Think -> search -> answer -> refine, up to max_rounds.

    Returns a dict: {answer, sources, rounds, web_used, warning}.
    Guaranteed to return a message rather than raising for model/network
    failures (the non-stop contract).
    """
    query = (query or "").strip()
    if not query:
        return {
            "answer": "\u274c Nothing to think about: the query was empty.",
            "sources": [], "rounds": 0, "web_used": False, "warning": None,
        }
    max_rounds = max(1, int(max_rounds or DEFAULT_MAX_ROUNDS))
    fast_model = fast_model or DEFAULT_FAST_MODEL
    answer_model = answer_model or DEFAULT_ANSWER_MODEL
    progress = progress_cb or (lambda _m: None)

    warning = None
    used = []
    pages = {}
    answers = []
    rounds_done = 0

    progress("\U0001f9e0 planning the search\u2026")
    try:
        plan = _model_call(fast_model, PLAN_SYSTEM, f"QUESTION:\n{query}")
    except Exception as exc:
        plan = ""
        warning = f"planning failed ({exc})"
    queries = _extract_queries(plan)[:3] or [query]
    logger.info("agent plan query=%r -> %d queries: %s",
                query[:120], len(queries), queries)

    pending = None
    for rnd in range(1, max_rounds + 1):
        rounds_done = rnd
        round_queries = [pending] if pending else queries
        progress(f"\U0001f50d searching the web (round {rnd}/{max_rounds})\u2026")
        fresh, serr = _search_all(round_queries)
        if serr and not warning:
            warning = serr
        used = _merge_results(used, fresh)
        logger.info("agent round %d searched %s -> %d fresh, %d used total%s",
                    rnd, round_queries, len(fresh), len(used),
                    f" (warning: {serr})" if serr else "")

        if rnd > 1 or pending:
            progress("\U0001f4c4 reading source pages\u2026")
            pages.update(_fetch_pages(used))

        progress(f"\U0001f9e0 composing the analysis (round {rnd}/{max_rounds})\u2026")
        user = _compose_user(query, used, pages, answers)
        try:
            raw = _model_call(answer_model, ANSWER_SYSTEM, user)
        except Exception as exc:
            warning = (f"{warning}; " if warning else "") + f"model error ({exc})"
            break
        clean, need = _extract_verdict(raw)
        if clean:
            answers.append(clean)
        if not need or rnd >= max_rounds:
            break
        pending = need
        progress(f"\U0001f50e refining the search: {need}")

    if answers:
        final = answers[-1]
    else:
        final = (
            "I could not produce an answer this time. The web search and the "
            "local model both failed, so there is nothing verified to report "
            "- try again in a moment."
        )
        if warning:
            final += f"\nDetail: {warning}"

    logger.info("agent finished query=%r rounds=%d web_used=%s warning=%r",
                query[:120], rounds_done, bool(used), warning)

    sources = []
    for i, r in enumerate(used, 1):
        line = (f"[{i}] {(r.get('title') or '').strip()} - "
                f"{(r.get('url') or '').strip()}")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            line += f"\n    {snippet}"
        sources.append(line)

    return {
        "answer": final,
        "sources": sources,
        "rounds": rounds_done,
        "web_used": bool(used),
        "warning": warning,
    }


def needs_web_agent(text):
    """Heuristic: does this chat message need live web data?

    Conservative on purpose - casual chat stays on the fast ~16s path and
    only questions that mention URLs, search verbs, fresh-time words, prices
    or a year are routed into the (much slower) agent loop.
    """
    t = (text or "").strip()
    if not t or len(t) < 12 or len(t) > 600:
        return False
    low = t.lower()
    if "http://" in low or "https://" in low:
        return True
    if any(w in low for w in _WEB_WORDS):
        return True
    if _YEAR_RE.search(t):
        return True
    return False


def _agent_reply_text(result):
    """Shape a run_agent result into a ready-to-send Telegram message."""
    parts = [(result.get("answer") or "").strip()]
    sources = result.get("sources") or []
    if sources:
        parts.append("")
        parts.append("\U0001f4ce Sources:")
        parts.extend(sources)
    warning = (result.get("warning") or "").strip()
    if warning:
        parts.append("")
        parts.append(f"\u26a0\ufe0f Note: {warning}")
    return "\n".join(parts)