"""Agent engine tests.

The whole loop is exercised with monkeypatched model calls and searches - no
real Ollama, no real DuckDuckGo. What gets pinned: the planner Q: parsing,
the [DONE]/[NEED_MORE] verdict split, the auto-trigger heuristic, the
happy-path one-round flow, the multi-round refine loop, the max-rounds cap,
the non-stop contract (search or model failure never raises - it degrades
to a real message), the freshness insurance (latest/current questions also
search the verbatim question), the citation-block dedup, and the claim
verification layer (claims that cannot be traced to the returned evidence
are marked unverified, not asserted as fact).
"""
import datetime

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


# ── planner system prompt & freshness rules ────────────────────────────
def test_plan_system_includes_current_date_and_freshness_rules():
    system = agent_engine._plan_system()
    assert datetime.date.today().isoformat() in system
    assert "LATEST" in system
    assert "latest release date" in system
    assert "release notes" in system


@pytest.mark.parametrize("text", [
    "what is the latest python release and its new features",
    "current stock price",
    "show me recent earthquakes",
    "what happened today",
    "newest react version",
    "breaking news on the election",
])
def test_freshness_question_detection_positive(text):
    assert agent_engine._is_freshness_question(text), text


@pytest.mark.parametrize("text", [
    "explain recursion",
    "when was python 3.13 released",
    "how does a b-tree work",
    "hello",
    "",
])
def test_freshness_question_detection_negative(text):
    assert not agent_engine._is_freshness_question(text), text


def test_freshness_question_appends_verbatim_query(monkeypatch):
    searched = []

    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: Python 3.10 features"  # stale drift the guard must survive
        if "claim verifier" in system:
            return "[1] SUPPORTED | ok"
        return "Answer.\n[DONE]"

    def fake_search(q):
        searched.append(q)
        return [{"title": "T " + q,
                 "url": "https://e.com/" + str(len(searched)),
                 "snippet": "s"}]

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(web_search, "search", fake_search)

    q = "what is the latest python release and its new features"
    out = agent_engine.run_agent(q, max_rounds=1)
    # even when the planner drifts to a stale version, the verbatim question
    # is searched too, so current evidence can reach the answerer
    assert "Python 3.10 features" in searched
    assert q in searched
    assert out["claims_flagged"] == []


def test_non_freshness_question_does_not_append_verbatim(monkeypatch):
    searched = []

    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: python 3.13 release date"
        if "claim verifier" in system:
            return "[1] SUPPORTED | ok"
        return "Answer.\n[DONE]"

    def fake_search(q):
        searched.append(q)
        return [{"title": "T " + q,
                 "url": "https://e.com/" + str(len(searched)),
                 "snippet": "s"}]

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(web_search, "search", fake_search)

    agent_engine.run_agent("When was python 3.13 released?", max_rounds=1)
    assert searched == ["python 3.13 release date"]


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


# ── citation-block dedup ───────────────────────────────────────────────
def test_strip_model_citation_block_removes_trailing_url_list():
    a = ("Python 3.13 shipped in October 2024.\n\n"
         "[1] https://docs.python.org/3/whatsnew/3.13.html\n"
         "[2] https://www.python.org/downloads/releases/")
    cleaned = agent_engine._strip_model_citation_block(a)
    assert "Python 3.13 shipped" in cleaned
    assert "https://docs.python.org" not in cleaned
    assert "https://www.python.org" not in cleaned
    assert "[1]" not in cleaned


def test_strip_model_citation_block_keeps_inline_markers_and_lists():
    a = "In 2024 [1] release notes.\n\n1. First point\n2. Second point"
    assert agent_engine._strip_model_citation_block(a) == a


def test_strip_model_citation_block_keeps_plain_answers():
    a = "Just prose, no citations at all."
    assert agent_engine._strip_model_citation_block(a) == a


# ── claim verification / annotation ────────────────────────────────────
def test_parse_verdicts():
    reply = ("[1] SUPPORTED | snippet confirms\n"
             "[2] UNSUPPORTED | contradicts\n"
             "[3] NO_EVIDENCE | not present\n"
             "UNNAMED NO_EVIDENCE | 'launched in 2021'\n"
             "UNNAMED NO_EVIDENCE | 'used by NASA'")
    flagged, unnamed = agent_engine._parse_verdicts(reply)
    assert flagged == {2, 3}
    assert unnamed == 2


def test_parse_verdicts_empty_or_garbage():
    assert agent_engine._parse_verdicts(None) == (set(), 0)
    assert agent_engine._parse_verdicts("") == (set(), 0)
    assert agent_engine._parse_verdicts("I can't verify this run.") == (set(), 0)


def test_out_of_range_citations():
    assert agent_engine._out_of_range_citations("Facts [1] and [4] here.", 3) == {4}
    assert agent_engine._out_of_range_citations("Facts [1] and [2].", 3) == set()
    assert agent_engine._out_of_range_citations("No citations.", 3) == set()


def test_annotate_answer_marks_unverified():
    out, notes = agent_engine._annotate_answer("Claim text.", {2}, 1)
    assert "\u26a0" in out
    assert "[2]" in out
    assert "1 uncited factual claim" in out
    assert len(notes) == 2
    assert "unverified" in out


def test_annotate_answer_no_flags_unchanged():
    out, notes = agent_engine._annotate_answer("Claim text.", set(), 0)
    assert out == "Claim text."
    assert notes == []


def test_run_agent_flags_unsupported_claim(monkeypatch):
    # Reproduction of the failing scenario: the model answers about a "latest"
    # release with a version/date that no returned result actually supports.
    used = [{"title": "What's New In Python 3.10",
             "url": "https://docs.python.org/3/whatsnew/3.10.html",
             "snippet": "Python 3.10 was released on October 4, 2021."}]

    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: python 3.10 features"
        if "claim verifier" in system:
            return ("[1] NO_EVIDENCE | answer claims 3.14.8 in Sept 2026 but "
                    "the evidence only covers Python 3.10")
        return ("The latest release is Python 3.14.8, released 30 Sept 2026 "
                "[1].\n[DONE]")

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(web_search, "search", lambda q: [dict(r) for r in used])

    out = agent_engine.run_agent(
        "what is the latest python release and its new features", max_rounds=1)
    assert "\u26a0" in out["answer"]
    assert "unverified" in out["answer"]
    assert out["claims_flagged"]
    assert "Python 3.14.8" in out["answer"]


def test_verifier_failure_degrades_gracefully(monkeypatch):
    def fake_model(model, system, user):
        if "claim verifier" in system:
            raise RuntimeError("verifier down")
        if "search planner" in system:
            return "Q: q"
        return "Answer [1].\n[DONE]"

    monkeypatch.setattr(agent_engine, "_model_call", fake_model)
    monkeypatch.setattr(
        web_search, "search",
        lambda q: [{"title": "t", "url": "https://e.com", "snippet": "s"}])

    out = agent_engine.run_agent("q", max_rounds=1)
    assert out["answer"].startswith("Answer")
    assert out["claims_flagged"] == []


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
    calls = {"plan": 0, "answer": 0, "search": 0, "verify": 0}
    results = [{"title": "Python 3.13 release",
                "url": "https://python.org/3.13",
                "snippet": "Released October 2024."}]

    def fake_model(model, system, user):
        if "search planner" in system:
            calls["plan"] += 1
            return "Q: python 3.13 release date"
        if "claim verifier" in system:
            calls["verify"] += 1
            return "[1] SUPPORTED | the snippet states the release date"
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
    assert calls == {"plan": 1, "answer": 1, "search": 1, "verify": 1}
    assert any("python.org" in s for s in out["sources"])
    assert out["warning"] is None
    assert out["claims_flagged"] == []


# ── multi-round refine loop ────────────────────────────────────────────
def test_run_agent_refines_when_need_more(monkeypatch):
    model_calls = []

    def fake_model(model, system, user):
        if "search planner" in system:
            return "Q: first query"
        if "claim verifier" in system:
            return "[1] SUPPORTED | ok\n[2] SUPPORTED | ok"
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