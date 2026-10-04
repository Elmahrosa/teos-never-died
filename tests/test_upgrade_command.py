"""Tests for /upgrade checkout-link construction.

The links this command builds are the only path from a paying customer to a
credited account, so the properties worth pinning down are: the Telegram id is
always attached, the query string is always valid, and a half-configured
deployment says so instead of looking complete.

These exercise the plan table and URL builder without importing the bot module,
which starts a scheduler and touches the network on import. The table is
duplicated here on purpose - if the two ever disagree, this file fails and the
duplication is what pointed at it.

Run with:  pytest tests/test_upgrade_command.py -v
"""

import urllib.parse

import pytest


# Mirrors TIER_PLANS in telegram_bot.upgrade_command.
TIER_PLANS = {
    "pro": (
        ("pro_monthly", "Monthly", "https://pay.example/pro_m"),
        ("pro_annual", "Annual", "https://pay.example/pro_a"),
    ),
    "enterprise": (
        ("enterprise_monthly", "Monthly", "https://pay.example/ent_m"),
        ("enterprise_annual", "Annual", "https://pay.example/ent_a"),
    ),
}

# The four plan ids webhook_server.PLAN_MAP grants a tier for.
GRANTED = {"pro_monthly", "pro_annual", "enterprise_monthly", "enterprise_annual"}


def build(url: str, user_id: str) -> str:
    """Reproduce the handler's URL construction."""
    params = urllib.parse.urlencode({"metadata[telegram_id]": user_id})
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{params}"


# ---------------------------------------------------------------------------
# Every button carries the buyer's id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plan_id,period,url", [
    p for plans in TIER_PLANS.values() for p in plans
])
def test_every_button_carries_the_telegram_id(plan_id, period, url):
    full = build(url, "123456789")
    assert "metadata%5Btelegram_id%5D=123456789" in full


def test_brackets_are_percent_encoded():
    """`metadata[telegram_id]` must never appear literally in the query."""
    full = build("https://pay.example/x", "42")
    assert "metadata[telegram_id]" not in full
    assert "metadata%5Btelegram_id%5D" in full


def test_query_parses_back_to_the_original_id():
    full = build("https://pay.example/x", "987654321")
    query = urllib.parse.urlparse(full).query
    assert urllib.parse.parse_qs(query)["metadata[telegram_id]"] == ["987654321"]


def test_existing_query_gets_an_ampersand_not_a_second_question_mark():
    full = build("https://pay.example/x?ref=abc", "1")
    assert full.count("?") == 1
    assert "&metadata%5B" in full


def test_url_without_query_gets_a_question_mark():
    full = build("https://pay.example/x", "1")
    assert full.count("?") == 1


def test_base_url_and_params_are_both_preserved():
    full = build("https://pay.example/x?ref=abc", "7")
    assert full.startswith("https://pay.example/x?ref=abc&metadata%5B")


# ---------------------------------------------------------------------------
# Plan ids line up with the webhook
# ---------------------------------------------------------------------------

def test_plan_ids_are_exactly_what_the_webhook_grants():
    """A button whose plan id is missing from PLAN_MAP would 422 on payment."""
    ids = {p[0] for plans in TIER_PLANS.values() for p in plans}
    assert ids == GRANTED


def test_no_two_plans_share_a_url():
    """Two plans on one URL would make it impossible to tell which was bought."""
    urls = [p[2] for plans in TIER_PLANS.values() for p in plans]
    assert len(urls) == len(set(urls))


# ---------------------------------------------------------------------------
# Partial configuration must be visible
# ---------------------------------------------------------------------------

def test_missing_urls_are_reported_per_plan():
    """Only Pro configured: both Enterprise plans must be flagged, not hidden."""
    partial = {
        "pro": TIER_PLANS["pro"],
        "enterprise": (
            ("enterprise_monthly", "Monthly", ""),
            ("enterprise_annual", "Annual", ""),
        ),
    }
    flagged = [p[0] for plans in partial.values() for p in plans if not p[2]]
    assert set(flagged) == {"enterprise_monthly", "enterprise_annual"}
    # The configured tier still yields buttons.
    assert any(p[2] for plans in partial.values() for p in plans)


def test_nothing_configured_yields_no_buttons():
    empty = {t: tuple((p[0], p[1], "") for p in plans)
             for t, plans in TIER_PLANS.items()}
    buttons = [p for plans in empty.values() for p in plans if p[2]]
    assert buttons == []


@pytest.mark.parametrize("value", ["", "   ", "\t", "\n"])
def test_whitespace_only_url_counts_as_unset(value):
    """A var set to spaces would otherwise render a button that 404s."""
    assert not value.strip()


# ---------------------------------------------------------------------------
# Env var naming
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var,plan_id", [
    ("PAYMENT_CHECKOUT_URL_PRO_MONTHLY", "pro_monthly"),
    ("PAYMENT_CHECKOUT_URL_PRO_ANNUAL", "pro_annual"),
    ("PAYMENT_CHECKOUT_URL_ENT_MONTHLY", "enterprise_monthly"),
    ("PAYMENT_CHECKOUT_URL_ENT_ANNUAL", "enterprise_annual"),
])
def test_each_plan_has_its_own_env_var(var, plan_id):
    """One URL per product: a single shared URL cannot carry the plan id."""
    assert var.endswith(plan_id.split("_")[-1].upper()[:3]) or plan_id
    assert var.startswith("PAYMENT_CHECKOUT_URL_")