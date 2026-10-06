"""Credly badge verification tests.

The whole point of badge_verify is that the bot reports Credly's own
public record instead of whatever the chat model guessed, so these tests
pin the three things that matter: strict UUID extraction (a URL, a
/twitter share link and a bare UUID must all resolve; garbage must not),
the og:title parsing ('X was issued by Y to Z.'), and the HTML fallback
when the JSON API is unavailable. All network access is monkeypatched -
no real requests in the suite.
"""
import pytest

import badge_verify
from badge_verify import BadgeVerifyError

UUID = "f14c9ae0-c594-4cf8-af37-398545f5b75f"

# A faithful slice of Credly's public page for that badge.
FAKE_PAGE = f"""<!DOCTYPE html><html><head>
<meta property="og:title" content="Claude Partner Badge - Claude Code was issued by Anthropic to Ayman Seif.">
<meta data-rh name="description" content="The Claude Partner Badge validates that consultants are ready to scope, deploy, configure, and run Claude Code activations end-to-end.">
<meta property="og:description" content="The Claude Partner Badge validates that consultants are ready to scope, deploy, configure, and run Claude Code activations end-to-end.">
<meta property="og:url" content="https://www.credly.com/badges/{UUID}">
<meta property="og:type" content="og:object">
<title>Claude Partner Badge - Claude Code - Credly</title>
</head><body></body></html>"""


# ── UUID extraction ───────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    f"https://www.credly.com/badges/{UUID}",
    f"https://www.credly.com/badges/{UUID}/twitter",
    f"https://www.credly.com/badges/{UUID}?trk=public_profile",
    UUID,
    f"here is my badge {UUID} thanks",
])
def test_extract_uuid_accepts_common_forms(text):
    assert badge_verify.extract_uuid(text) == UUID


@pytest.mark.parametrize("text", [
    "", None, "not a badge", "https://example.com/foo",
    "f14c9ae0c5944cf8af37398545f5b75f",  # no hyphens
    "x" * 36,
])
def test_extract_uuid_rejects_garbage(text):
    assert badge_verify.extract_uuid(text) is None


# ── og:title parsing ──────────────────────────────────────────────────
def test_parse_title_splits_issued_by_to():
    parts = badge_verify._parse_title(
        "Claude Partner Badge - Claude Code was issued by Anthropic to Ayman Seif."
    )
    assert parts == {
        "badge": "Claude Partner Badge - Claude Code",
        "issuer": "Anthropic",
        "recipient": "Ayman Seif",
    }


def test_parse_title_wrong_shape_keeps_badge_name():
    parts = badge_verify._parse_title("Some Other Badge")
    assert parts["badge"] == "Some Other Badge"
    assert parts["issuer"] == "" and parts["recipient"] == ""


# ── Fetching: HTML fallback when the API is not public ────────────────
class _FakeResponse:
    def __init__(self, status, body="", json_data=None):
        self.status_code = status
        self._body = body
        self._json = json_data
        self.ok = 200 <= status < 300

    @property
    def text(self):
        return self._body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _fake_get(url, timeout=None, headers=None):
    if url.startswith(badge_verify.API_URL.rsplit("{", 1)[0]):
        # API is not publicly served for this issuer: 404.
        return _FakeResponse(404)
    assert url == badge_verify.PAGE_URL.format(uuid=UUID)
    return _FakeResponse(200, body=FAKE_PAGE)


def test_verify_falls_back_to_page_when_api_404(monkeypatch):
    monkeypatch.setattr(badge_verify.requests, "get", _fake_get)
    info = badge_verify.verify_badge(f"https://www.credly.com/badges/{UUID}/twitter")
    assert info["badge"] == "Claude Partner Badge - Claude Code"
    assert info["issuer"] == "Anthropic"
    assert info["recipient"] == "Ayman Seif"
    assert info["uuid"] == UUID
    assert info["source"] == "page"
    assert f"/badges/{UUID}" in info["url"]
    assert info["description"].startswith("The Claude Partner Badge")


def test_verify_uses_api_when_available(monkeypatch):
    def _api_get(url, timeout=None, headers=None):
        if url.startswith(badge_verify.API_URL.rsplit("{", 1)[0]):
            return _FakeResponse(200, json_data={"data": {
                "badge_template": {"name": "Claude Partner Badge - Claude Code",
                                   "issuer": {"name": "Anthropic"},
                                   "description": "desc"},
                "issued_to": {"first_name": "Ayman"},
                "issued_at": "2026-07-10T00:00:00Z",
                "expires_at": "2027-01-10T00:00:00Z",
                "state": "issued",
            }})
        pytest.fail("API path should have answered")
    monkeypatch.setattr(badge_verify.requests, "get", _api_get)
    info = badge_verify.verify_badge(UUID)
    assert info["source"] == "api"
    assert info["issuer"] == "Anthropic"
    assert info["issued_at"].startswith("2026-07-10")
    assert info["expires_at"].startswith("2027-01-10")


def test_verify_rejects_non_badge_input(monkeypatch):
    monkeypatch.setattr(badge_verify.requests, "get", _fake_get)
    with pytest.raises(BadgeVerifyError, match="No Credly badge id"):
        badge_verify.verify_badge("love my job")


def test_verify_reports_unverifiable_badge(monkeypatch):
    def _not_found(url, timeout=None, headers=None):
        return _FakeResponse(404)
    monkeypatch.setattr(badge_verify.requests, "get", _not_found)
    with pytest.raises(BadgeVerifyError, match="could not be verified"):
        badge_verify.verify_badge(UUID)


# ── Renderer ──────────────────────────────────────────────────────────
def test_format_badge_mentions_key_facts():
    text = badge_verify.format_badge({
        "badge": "Claude Partner Badge - Claude Code",
        "issuer": "Anthropic",
        "recipient": "Ayman Seif",
        "description": "The Claude Partner Badge.",
        "issued_at": "2026-07-10T00:00:00Z",
        "expires_at": "2027-01-10T00:00:00Z",
        "url": badge_verify.PAGE_URL.format(uuid=UUID),
        "source": "api",
    })
    for needle in ("Claude Partner Badge", "Anthropic", "Ayman Seif",
                   "2026-07-10", "2027-01-10", "BADGE VERIFIED"):
        assert needle in text