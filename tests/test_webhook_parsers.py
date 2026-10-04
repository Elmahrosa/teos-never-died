"""Parser tests for the payment webhook, driven by realistic provider payloads.

These assert on *parsed output* rather than HTTP status codes, because the bug
that motivated this file was invisible to status-code assertions: a settled
payment parsed a Telegram id but not a plan, so the handler returned 422
"unrecognised plan" and credited nobody while every other check passed.

`_find_telegram_id` recursed through wrapper keys and `_find_plan` did not, so
the two disagreed on how deep to look. Both now share one walker
(`_find_in_tree`), and the fixtures below cover the nesting each provider is
known to emit.

Run with:  pytest tests/test_webhook_parsers.py -v
"""

import pytest

from webhook_server import (
    PLAN_MAP,
    _find_in_tree,
    _find_plan,
    _find_telegram_id,
    _find_event_id,
    _is_paid,
)

# ---------------------------------------------------------------------------
# Realistic provider fixtures
# ---------------------------------------------------------------------------

# Dodo nests buyer metadata one level under data. This is the shape that broke.
DODO_NESTED_PAYLOAD = {
    "event_id": "evt_dodo_123",
    "type": "payment.succeeded",
    "data": {
        "payment_id": "pay_456",
        "status": "succeeded",
        "metadata": {
            "telegram_id": "987654321",
            "price_id": "pro_monthly",
        },
    },
}

# Flat shape, as a simpler provider or a hand-rolled test post emits it.
FLAT_PAYLOAD = {
    "id": "evt_flat_123",
    "event": "payment.paid",
    "telegramId": 123456789,
    "plan": "enterprise_annual",
    "status": "paid",
}

# Four wrapper layers deep: payload -> client_ref -> custom_data.
DEEPLY_NESTED_PAYLOAD = {
    "event": "checkout.completed",
    "payload": {
        "client_ref": {
            "custom_data": {
                "telegram_user_id": "1122334455",
                "tier": "pro",
            }
        }
    },
}

# A settled-then-refunded payment. The success words must not win here.
REFUNDED_PAYLOAD = {
    "event_id": "evt_refund_123",
    "type": "payment.succeeded",
    "data": {"status": "refunded", "price_id": "pro_monthly"},
    "metadata": {"telegram_id": "999"},
}

# Not a payment event at all.
UNRELATED_PAYLOAD = {
    "event_id": "evt_invoice_123",
    "type": "invoice.created",
    "data": {"status": "open"},
}


# ---------------------------------------------------------------------------
# _find_plan
# ---------------------------------------------------------------------------

def test_find_plan_nested_under_data_metadata():
    """Regression: the plan sat beside the id under data.metadata and was missed."""
    assert _find_plan(DODO_NESTED_PAYLOAD) == "pro_monthly"


def test_find_plan_flat_shape():
    assert _find_plan(FLAT_PAYLOAD) == "enterprise_annual"


def test_find_plan_deeply_nested():
    """Must traverse several wrapper layers, not just one."""
    assert _find_plan(DEEPLY_NESTED_PAYLOAD) == "pro"


@pytest.mark.parametrize("plan", sorted(PLAN_MAP))
def test_find_plan_every_mapped_plan_resolves(plan):
    """Every for-sale plan must be reachable, whatever key the provider used."""
    assert _find_plan({"data": {"metadata": {"price_id": plan}}}) == plan
    assert _find_plan({"metadata": {"plan": plan}}) == plan


def test_find_plan_unmapped_returns_empty_not_crash():
    """An unknown plan must resolve to "", never to a default tier."""
    assert _find_plan({"data": {"metadata": {"price_id": "enterprise_plus"}}}) == ""


def test_find_plan_empty_when_absent():
    assert _find_plan({"data": {"status": "succeeded"}}) == ""
    assert _find_plan({}) == ""


def test_find_plan_ignores_non_string_values():
    """A numeric or nested value under a plan key must not be coerced."""
    assert _find_plan({"price_id": 42}) == ""
    assert _find_plan({"price_id": {"nested": "pro_monthly"}}) == ""


def test_find_plan_is_case_insensitive():
    assert _find_plan({"plan": "PRO_MONTHLY"}) == "pro_monthly"


def test_find_plan_respects_depth_cap():
    """Past MAX_WALK_DEPTH the walker gives up rather than recursing forever."""
    deep: dict = {"price_id": "pro_monthly"}
    for _ in range(12):
        deep = {"metadata": deep}
    assert _find_plan(deep) == ""


def test_find_plan_does_not_loop_on_self_referential_payload():
    """A cyclic structure must not hang the webhook."""
    node: dict = {"price_id": "pro_monthly"}
    node["metadata"] = node
    assert _find_plan(node) == "pro_monthly"


# ---------------------------------------------------------------------------
# _find_telegram_id
# ---------------------------------------------------------------------------

def test_find_telegram_id_nested():
    assert _find_telegram_id(DODO_NESTED_PAYLOAD) == "987654321"


def test_find_telegram_id_flat_integer():
    """Integer ids must come back as strings."""
    result = _find_telegram_id(FLAT_PAYLOAD)
    assert result == "123456789"
    assert isinstance(result, str)


def test_find_telegram_id_deeply_nested():
    assert _find_telegram_id(DEEPLY_NESTED_PAYLOAD) == "1122334455"


def test_find_telegram_id_absent_returns_none():
    """None, so the handler returns 422 instead of crediting a guess."""
    assert _find_telegram_id({"data": {"status": "succeeded"}}) is None


def test_find_telegram_id_ignores_non_numeric_under_id_key():
    """A username under chat_id is not an id."""
    assert _find_telegram_id({"chat_id": "@someuser"}) is None


def test_find_telegram_id_ignores_nine_digit_decoy_outside_allow_list():
    """A phone number in an unlisted key must not be taken for a Telegram id.

    Crediting is irreversible, so the walker only descends ALLOWED_WRAPPERS
    rather than every key present.
    """
    assert _find_telegram_id({"data": {"order": {"phone": "5551234567"}}}) is None


def test_find_telegram_id_negative_id_accepted_as_string():
    """Telegram group ids are negative; the walker's digit check allows them."""
    assert _find_telegram_id({"chat_id": "-1001234567890"}) == "-1001234567890"


# ---------------------------------------------------------------------------
# The two parsers must agree on shape
# ---------------------------------------------------------------------------

def test_plan_and_id_are_found_from_the_same_subtree():
    """Both values live in the same metadata block; neither may be missed."""
    for payload, plan, telegram_id in (
        (DODO_NESTED_PAYLOAD, "pro_monthly", "987654321"),
        (FLAT_PAYLOAD, "enterprise_annual", "123456789"),
        (DEEPLY_NESTED_PAYLOAD, "pro", "1122334455"),
    ):
        assert _find_plan(payload) == plan
        assert _find_telegram_id(payload) == telegram_id


# ---------------------------------------------------------------------------
# _find_in_tree
# ---------------------------------------------------------------------------

def test_walker_returns_none_when_nothing_matches():
    assert _find_in_tree({"a": 1}, ("b",), lambda v: True) is None


def test_walker_stops_at_non_dict():
    assert _find_in_tree("just a string", ("a",), lambda v: True) is None
    assert _find_in_tree(None, ("a",), lambda v: True) is None
    assert _find_in_tree([1, 2, 3], ("a",), lambda v: True) is None


def test_walker_prefers_shallower_match():
    """A nearer value wins over one buried deeper in the tree."""
    node = {"plan": "pro", "metadata": {"plan": "enterprise_annual"}}
    assert _find_plan(node) == "pro"


# ---------------------------------------------------------------------------
# _find_event_id
# ---------------------------------------------------------------------------

def test_find_event_id_top_level():
    assert _find_event_id(DODO_NESTED_PAYLOAD) == "evt_dodo_123"


def test_find_event_id_falls_back_to_data_payment_id():
    payload = {"data": {"payment_id": "pay_456"}}
    assert _find_event_id(payload) == "pay_456"


def test_find_event_id_content_hash_when_unidentifiable():
    """An unidentifiable payload still dedupes on a stable content hash."""
    first = _find_event_id({"weird": {"shape": 1}})
    second = _find_event_id({"weird": {"shape": 1}})
    assert first == second
    assert first.startswith("hash:")


# ---------------------------------------------------------------------------
# _is_paid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [DODO_NESTED_PAYLOAD, FLAT_PAYLOAD])
def test_is_paid_true_for_settled_payments(payload):
    assert _is_paid(payload) is True


def test_is_paid_refund_beats_success_event_name():
    """type says succeeded but status says refunded: no credit."""
    assert _is_paid(REFUNDED_PAYLOAD) is False


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired", "disputed"])
def test_is_paid_false_for_failure_statuses(status):
    assert _is_paid({"type": f"payment.{status}", "data": {"status": status}}) is False


def test_is_paid_false_for_unrelated_event():
    assert _is_paid(UNRELATED_PAYLOAD) is False


def test_is_paid_false_for_unknown_status():
    """An unrecognised status must not be treated as paid."""
    assert _is_paid({"data": {"status": "pending"}}) is False