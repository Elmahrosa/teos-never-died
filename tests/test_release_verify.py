"""Test suite for release_verify module."""

import pytest
from release_verify import Outcome, Target, Result, capture, verify


def test_capture_well_formed():
    """Test A: capture on well-formed answer."""
    answer = {"components": {"app": "1.2.3", "db": "2024-01"}}
    expected = [Target("app", "1.2.3"), Target("db", "2024-01")]
    assert capture(answer) == expected


def test_capture_flat_answer():
    """Test B: capture on flat answer (no 'components' key)."""
    answer = {"app": "1.2.3"}
    expected = [Target("app", "1.2.3")]
    assert capture(answer) == expected


def test_verify_match():
    """Test C: verify MATCH."""
    target = Target("app", "1.2.3")
    fresh = {"components": {"app": "1.2.3"}}
    expected = [Result("app", Outcome.MATCH, "1.2.3", "1.2.3")]
    assert verify([target], fresh) == expected


def test_verify_newer_only_when_pending_false():
    """Test D: verify NEWER only when fresh says not-pending."""
    target = Target("app", "1.2.3")
    fresh = {"components": {"app": "1.2.4"}, "pending": {"app": False}}
    expected = [Result("app", Outcome.NEWER, "1.2.3", "1.2.4")]
    assert verify([target], fresh) == expected


def test_verify_mismatch_when_fresh_none():
    """Test E: verify MISMATCH when fresh is None."""
    target = Target("app", "1.2.3")
    expected = [Result("app", Outcome.MISMATCH, "1.2.3", "")]
    assert verify([target], None) == expected


def test_verify_mismatch_when_fresh_still_pending():
    """Test F: verify MISMATCH when fresh still offers the update."""
    target = Target("app", "1.2.3")
    fresh = {"components": {"app": "1.2.4"}, "pending": {"app": True}}
    expected = [Result("app", Outcome.MISMATCH, "1.2.3", "1.2.4")]
    assert verify([target], fresh) == expected


def test_verify_unverifiable_when_target_identity_empty():
    """Test G: verify UNVERIFIABLE when target.identity is ""."""
    target = Target("app", "")
    fresh = {"components": {"app": "1.2.3"}}  # fresh value doesn't matter here
    expected = [Result("app", Outcome.UNVERIFIABLE, "", "")]
    assert verify([target], fresh) == expected


def test_verify_unverifiable_when_fresh_lacks_component():
    """Test H: verify UNVERIFIABLE when fresh lacks the component."""
    target = Target("app", "1.2.3")
    fresh = {"components": {"db": "2024-01"}}
    expected = [Result("app", Outcome.UNVERIFIABLE, "1.2.3", "")]
    assert verify([target], fresh) == expected


def test_verify_never_raises():
    """Test I: verify never raises on any inputs."""
    # Test with malformed inputs
    test_cases = [
        {},  # empty dict
        [],  # list
        {"components": None},  # None value
        {"components": {}},  # empty components
        {"components": {"app": None}},  # None identity
    ]

    for fresh in test_cases:
        # Should not raise any exception
        try:
            result = verify([Target("app", "1.2.3")], fresh)
            # Result should be a list
            assert isinstance(result, list)
        except Exception as e:
            pytest.fail(f"verify() raised {type(e).__name__}: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])