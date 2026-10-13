import pytest
from supervisor_guard import Detector, Budget


def test_detector_fires_at_same_tool_limit():
    """A. Detector fires at same_tool_limit=3"""
    det = Detector(same_tool_limit=3, total_tool_limit=100)

    # First two calls should return False
    assert det.check("a") == (False, "")
    assert det.check("a") == (False, "")

    # Third call hits the limit
    result = det.check("a")
    assert result[0] is True
    assert "3" in result[1]

    # Once fired, subsequent calls stay fired until reset
    assert det.check("a")[0] is True


def test_detector_fires_at_total_tool_limit():
    """B. Detector fires at total_tool_limit=4 with distinct tools"""
    det = Detector(same_tool_limit=100, total_tool_limit=4)

    assert det.check("a") == (False, "")
    assert det.check("b") == (False, "")
    assert det.check("c") == (False, "")

    result = det.check("d")
    assert result[0] is True
    assert "4" in result[1]


def test_detector_reset_clears_counters():
    """C. Detector.reset() clears both counters (post-reset check is False)"""
    det = Detector(same_tool_limit=2, total_tool_limit=2)

    # Trigger the detector: two checks on the same tool hit the limit
    det.check("a")
    assert det.check("a")[0] is True

    # Reset and check once — the counter starts fresh, so this is False
    det.reset()
    assert det.check("a") == (False, "")


def test_detector_disabled_when_both_limits_zero():
    """D. Detector disabled when both limits 0: 100 checks -> always False"""
    det = Detector(same_tool_limit=0, total_tool_limit=0)

    for _ in range(100):
        assert det.check("any_tool") == (False, "")


def test_basic_budget_operations():
    """E. Budget(general_calls=5, restricted_calls=3)"""
    budget = Budget(general_calls=5, restricted_calls=3)

    assert budget.remaining("general") == 5
    assert budget.remaining("restricted") == 3

    budget.consume("general")
    assert budget.remaining("general") == 4

    budget.consume("restricted")
    assert budget.remaining("restricted") == 2


def test_budget_approaching():
    """F. Budget(general_calls=10): approaching behavior"""
    budget = Budget(general_calls=10)

    # Initially remaining=10, margin=3 -> 10 > 3 -> False
    assert budget.approaching("general", margin=3) is False

    # Consume until remaining == 3
    for _ in range(7):
        budget.consume("general")
    assert budget.approaching("general", margin=3) is True

    # Consume until remaining == 0
    budget.consume("general")
    budget.consume("general")
    budget.consume("general")
    assert budget.approaching("general", margin=3) is True

    # Consume one more (remaining == -1) -> False
    budget.consume("general")
    assert budget.approaching("general", margin=3) is False


def test_budget_unknown_kind_raises_value_error():
    """G. Budget unknown kind raises ValueError"""
    budget = Budget()

    with pytest.raises(ValueError):
        budget.remaining("x")

    with pytest.raises(ValueError):
        budget.consume("x")


def test_budget_disabled_when_limit_zero():
    """H. Budget disabled (general_calls=0): no-op, approaching False"""
    budget = Budget(general_calls=0, restricted_calls=5)

    assert budget.remaining("general") == 0
    budget.consume("general")
    assert budget.remaining("general") == 0
    assert budget.approaching("general", margin=3) is False

    # Restricted still works normally
    assert budget.remaining("restricted") == 5
    budget.consume("restricted")
    assert budget.remaining("restricted") == 4