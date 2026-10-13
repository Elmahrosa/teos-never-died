"""Tests for registry.py."""
import pytest
import registry
from registry import Spec, register, get, build_all


def reset_registry():
    """Reset the registry to empty for a clean test."""
    registry._REGISTRY.clear()
    registry.REGISTRY = tuple(registry._REGISTRY)


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test runs with a fresh registry."""
    reset_registry()
    yield
    reset_registry()


def test_get_returns_registered_spec():
    """A. get returns the registered spec."""
    spec = Spec(kind="x", name="X", enabled=lambda cfg: True, build=lambda cfg: "x-built")
    register(spec)
    assert get("x") is spec


def test_get_raises_key_error_for_unknown_kind():
    """B. get raises KeyError for unknown kind."""
    with pytest.raises(KeyError) as excinfo:
        get("unknown")
    assert "unknown" in str(excinfo.value)


def test_duplicate_kind_rejected():
    """C. duplicate kind rejected."""
    spec1 = Spec(kind="x", name="X1", enabled=lambda cfg: True, build=lambda cfg: "x1")
    spec2 = Spec(kind="x", name="X2", enabled=lambda cfg: True, build=lambda cfg: "x2")
    register(spec1)
    with pytest.raises(ValueError) as excinfo:
        register(spec2)
    assert "duplicate kind" in str(excinfo.value)
    assert "x" in str(excinfo.value)


def test_build_all_skips_disabled_specs():
    """D. build_all skips disabled specs silently."""
    # Spec A: disabled, build that would raise if called.
    spec_a = Spec(
        kind="a",
        name="A",
        enabled=lambda cfg: False,
        build=lambda cfg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    # Spec B: enabled, returns "b".
    spec_b = Spec(
        kind="b",
        name="B",
        enabled=lambda cfg: True,
        build=lambda cfg: "b",
    )
    register(spec_a)
    register(spec_b)
    result = build_all(object())
    assert result == {"B": "b"}


def test_build_all_raises_on_enabled_spec_build_failure():
    """E. build_all raises when an enabled spec's build fails."""
    spec_a = Spec(
        kind="a",
        name="A",
        enabled=lambda cfg: True,
        build=lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    register(spec_a)
    with pytest.raises(RuntimeError) as excinfo:
        build_all(object())
    # Check that the exception message contains the kind and the original error.
    assert "Failed to build 'a'" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_build_all_respects_registration_order():
    """F. build_all respects registration order."""
    spec_b = Spec(
        kind="b",
        name="B",
        enabled=lambda cfg: True,
        build=lambda cfg: "b",
    )
    spec_a = Spec(
        kind="a",
        name="A",
        enabled=lambda cfg: True,
        build=lambda cfg: "a",
    )
    # Register B first, then A.
    register(spec_b)
    register(spec_a)
    result = build_all(object())
    # In Python 3.7+, dict preserves insertion order.
    assert list(result.keys()) == ["B", "A"]
    assert result["B"] == "b"
    assert result["A"] == "a"


def test_register_adds_to_registry():
    """G. register with valid spec adds to REGISTRY."""
    assert len(registry.REGISTRY) == 0
    spec = Spec(kind="x", name="X", enabled=lambda cfg: True, build=lambda cfg: "x")
    register(spec)
    assert len(registry.REGISTRY) == 1
    assert registry.REGISTRY[0] is spec