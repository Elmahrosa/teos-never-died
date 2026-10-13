"""Release verification module for tracking component versions before and after updates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    """Verification outcome."""
    MATCH = "match"
    NEWER = "newer"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Target:
    """Target component identity to verify against."""
    component: str
    identity: str


@dataclass(frozen=True)
class Result:
    """Verification result for a component."""
    component: str
    outcome: Outcome
    expected: str
    actual: str


def capture(answer: dict) -> list[Target]:
    """
    Capture component identities before an update.

    Prevents: "we lost track of what was there before the update"

    Args:
        answer: Dict with optional "components" key containing component->identity mapping.
                If "components" key is absent, the entire dict is treated as the mapping.

    Returns:
        List of Target objects for each component-identity pair.
        Empty identity strings are preserved (will result in UNVERIFIABLE during verification).
    """
    # Handle both {"components": {...}} and flat {component: identity} formats
    components = answer.get("components", answer)

    targets = []
    for component, identity in components.items():
        # Ensure identity is string (handles cases where it might be None or other types)
        identity_str = str(identity) if identity is not None else ""
        targets.append(Target(component=component, identity=identity_str))

    return targets


def verify(targets: list[Target], fresh: dict | None) -> list[Result]:
    """
    Verify component identities after an update.

    Prevents: "we assumed the update worked because no error was raised"

    Args:
        targets: List of Target objects from capture()
        fresh: Dict containing post-update component identities and optional pending flags.
               Expected format: {"components": {...}, "pending": {...}} or just {"components": {...}}
               If None, fresh check could not be obtained.

    Returns:
        List of Result objects indicating verification outcome for each target.
    """
    logger = logging.getLogger("raven.release")
    results = []

    for target in targets:
        # Case 1: Empty identity -> UNVERIFIABLE
        if target.identity == "":
            result = Result(
                component=target.component,
                outcome=Outcome.UNVERIFIABLE,
                expected=target.identity,
                actual=""
            )
            results.append(result)
            logger.info(
                f"Component {target.component}: UNVERIFIABLE (empty identity)"
            )
            continue

        # Case 2: Fresh check unavailable (None or not a dict) -> MISMATCH
        if fresh is None or not isinstance(fresh, dict):
            result = Result(
                component=target.component,
                outcome=Outcome.MISMATCH,
                expected=target.identity,
                actual=""
            )
            results.append(result)
            logger.info(
                f"Component {target.component}: MISMATCH (fresh check unavailable)"
            )
            continue

        # Extract components and pending info from fresh dict
        fresh_components = fresh.get("components", fresh)
        pending_info = fresh.get("pending", {})

        # Get actual identity from fresh check
        # Handle case where fresh_components is not a dict (e.g., None, string, etc.)
        if isinstance(fresh_components, dict):
            actual_identity = fresh_components.get(target.component)
        else:
            actual_identity = None

        # Case 3: Component not found in fresh check -> UNVERIFIABLE
        if actual_identity is None:
            result = Result(
                component=target.component,
                outcome=Outcome.UNVERIFIABLE,
                expected=target.identity,
                actual=""
            )
            results.append(result)
            logger.info(
                f"Component {target.component}: UNVERIFIABLE (not in fresh check)"
            )
            continue

        # Convert actual to string for comparison
        actual_str = str(actual_identity)

        # Case 4: Identities match -> MATCH
        if actual_str == target.identity:
            result = Result(
                component=target.component,
                outcome=Outcome.MATCH,
                expected=target.identity,
                actual=actual_str
            )
            results.append(result)
            logger.info(
                f"Component {target.component}: MATCH ({actual_str})"
            )
            continue

        # Case 5: Identities differ
        # Check if pending flag is explicitly False -> NEWER
        is_explicitly_not_pending = pending_info.get(target.component) is False

        if is_explicitly_not_pending:
            result = Result(
                component=target.component,
                outcome=Outcome.NEWER,
                expected=target.identity,
                actual=actual_str
            )
            results.append(result)
            logger.info(
                f"Component {target.component}: NEWER (expected {target.identity}, got {actual_str})"
            )
        else:
            # Pending is True, missing, or any other value -> MISMATCH
            result = Result(
                component=target.component,
                outcome=Outcome.MISMATCH,
                expected=target.identity,
                actual=actual_str
            )
            results.append(result)
            logger.info(
                f"Component {target.component}: MISMATCH (expected {target.identity}, got {actual_str})"
            )

    # Summary line with counts per Outcome
    counts = {outcome: 0 for outcome in Outcome}
    for result in results:
        counts[result.outcome] += 1

    summary_parts = [f"{outcome.value}:{count}" for outcome, count in counts.items()]
    logger.info(f"Verification summary: {' '.join(summary_parts)}")

    return results