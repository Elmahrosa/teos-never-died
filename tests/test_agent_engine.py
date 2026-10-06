"""Agent engine tests.

The whole loop is exercised with monkeypatched model calls and searches - no
real Ollama, no real DuckDuckGo. What gets pinned: the planner Q: parsing,
the [DONE]/[NEED_MORE] verdict split, the auto-trigger heuristic, the
happy-path one-round flow, the multi-round refine loop, the max-rounds cap,
and the non-stop contract (search or model failure never raises - it
degrades to a real message).
"""
import pytest

import agent_engine
import web_search


# ── planner output parsing ─────────────────────────────────────────────
def test_extract_queries_parses_q_lines():
    reply = ("We need facts.\n"
             "Q: python 3.13 release date\n"
             "Q: python 3.13 changelog\n"
             "Q: python 3.13 performance vs 3.12")
    assert agent_engine._extract_queries(reply) == [
        "python 3.13 release date",
        "python 3.13 changelog",
        "python 3.13 performance vs 3.12",
    ]


def test_extract_queries_ignores_garbage_and_none():
    assert agent_engine._extract_queries("No lookups needed here.\nNONE") == []
    assert agent_engine._extract_queries("Q: one\ngarbage\nQ: two") == ["one", "two"]
    assert agent_engine._extract_queries("Q: none") == []
    assert agent_engine._extract_queries(None) == []
    assert agent_engine._extract_queries("") == []


# ── verdict parsing ────────────────────────────────────────────────────
def test_extract_verdict_done():
    text, need = agent_engine._extract_verdict("Bullets here.\n[DONE]")
    assert "Bullets here." in text
    assert need is None


def test_extract_verdict_need_more():
    text, need = agent_engine._extract_verdict(
        "I found the date.\n[NEED_MORE: python 3.13 install guide]")
    assert "I found the date." in text
    assert need == "python 3.13 install guide"


def test_extract_verdict_plain_answer_without_markers():
    text, need = agent_engine._extract_verdict("Just an answer, no markers.")
    assert text == "Just an answer, no markers."
    assert need is None


def test_extract_verdict_empty():
    assert agent_engine._extract_verdict(None) == ("", None)
    assert agent_engine._extract_verdict("") == ("", None)


# ── auto-trigger heuristic ─────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "what is the latest python release",
    "search python 3.13 news",
    "how much is bitcoin today",
    "check https://example.com/report",
    "what happened in 2024",
    "fact check: did the moon landing happen",
    "what is the exchange rate usd to eur",
])
def test_needs_web_agent_triggers(text):
    assert agent_engine.needs_web_agent(text), text


@pytest.mark.parametrize("text", [
    "hello",
    "how are you doing",
    "thanks raven",
    "",
    "short",
    "x" * 700,  # too long to be a search request
])
def test_needs_web_agent_ignores_normal_chat(text):
    assert not agent_engine.needs_web_agent(text), text


# ── one-round happy path ───────────────────────────────────────────────
def test_run_agent_happy_path(monkeypatch):
    calls = {"plan": 0, "answer": 0, "search": 0}
    results = [{"title": "Python 3.13 release",
                "url": "https://python.org/3.13",
                "snippet": "Released October 2024."}]

    def fake_model(model, system, user):
        if "search planner" in system:
            calls["plan"] += 1
            return "Q: python 3.13 release date"
        calls["answer"] += 1
        return "Python 3.13 shipped on 7 October 2024 [1].\n[DONE]"

    def fake_search(q):
        calls["search"] += 1
        return results

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(web_search, "search", fake_search)

    out = agent_engine.run_agent("When was python 3.13 released?", max_rounds=3)
    assert out["answer"].startswith("Python 3.13 shipped")
    assert out["web_used"] is True
    assert out["rounds"] == 1
    assert calls == {"plan": 1, "answer": 1, "search": 1}
    assert any("python.org" in s for s in out["sources"])
    assert out["warning"] is None


# ── multi-round refine loop ────────────────────────────────────────────
def test_run_agent_refines_when_need_more(monkeypatch):
    model_calls = []

    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: first query"
        model_calls.append(user)
        if len(model_calls) == 1:
            return "Not enough detail yet.\n[NEED_MORE: second query]"
        return "Now complete, citing [2].\n[DONE]"

    searched = []

    def fake_search(q):
        searched.append(q)
        return [{"title": "Result " + q,
                 "url": "https://e.com/" + q.replace(" ", "-"),
                 "snippet": "snip"}]

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(web_search, "search", fake_search)

    out = agent_engine.run_agent("question", max_rounds=2)
    assert out["rounds"] == 2
    assert out["answer"].startswith("Now complete")
    assert searched == ["first query", "second query"]


def test_run_agent_stops_at_max_rounds(monkeypatch):
    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: q"
        return "Still partial.\n[NEED_MORE: again]"

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(
        web_search, "search",
        lambda q: [{"title": "t", "url": "https://e.com/" + q,
                    "snippet": "s"}])

    out = agent_engine.run_agent("q", max_rounds=2)
    assert out["rounds"] == 2
    assert "Still partial." in out["answer"]


# ── non-stop: failures never raise ─────────────────────────────────────
def test_run_agent_survives_search_failure(monkeypatch):
    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: q"
        return "I can answer this from general knowledge.\n[DONE]"

    def boom(q):
        raise web_search.SearchError("the search provider refused the request")

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(web_search, "search", boom)

    out = agent_engine.run_agent("q")
    assert out["web_used"] is False
    assert out["warning"] is not None
    assert "general knowledge" in out["answer"]


def test_run_agent_survives_model_failure(monkeypatch):
    def boom(model, system, user):
        raise RuntimeError("ollama crashed")

    monkeypatch.setattr(agent_engine, "_model_call", boom)
    monkeypatch.setattr(
        web_search, "search",
        lambda q: [{"title": "t", "url": "https://e.com", "snippet": "s"}])

    out = agent_engine.run_agent("q", max_rounds=2)
    assert out["warning"]
    assert "could not produce" in out["answer"]


def test_run_agent_empty_query():
    out = agent_engine.run_agent("   ")
    assert "empty" in out["answer"].lower()
    assert out["rounds"] == 0


# ── reply formatting ───────────────────────────────────────────────────
def test_agent_reply_text_formats_answer_and_sources():
    text = agent_engine._agent_reply_text({
        "answer": "Answer text",
        "sources": ["[1] Title - https://e.com\n    snippet"],
        "warning": None,
    })
    assert "Answer text" in text
    assert "Sources:" in text
    assert "https://e.com" in text


def test_agent_reply_text_includes_warning():
    text = agent_engine._agent_reply_text({
        "answer": "a", "sources": [], "warning": "planning failed (x)"})
    assert "planning failed (x)" in text