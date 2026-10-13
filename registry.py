"""Registry for table-driven backend selection.

Public API:
    @dataclass(frozen=True)
    class Spec:
        kind: str
        name: str
        enabled: Callable[[Any], bool]
        build:   Callable[[Any], Any]

    REGISTRY: tuple[Spec, ...] = ()

    def get(kind: str) -> Spec
    def build_all(cfg: Any) -> dict[str, Any]
    def register(spec: Spec) -> None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Tuple


@dataclass(frozen=True)
class Spec:
    """Specification for a backend."""
    kind: str
    name: str
    enabled: Callable[[Any], bool]
    build:   Callable[[Any], Any]


# Internal mutable list of specs.
_REGISTRY: list[Spec] = []
# Public tuple view of the registry, rebuilt on each registration.
REGISTRY: Tuple[Spec, ...] = tuple(_REGISTRY)


def register(spec: Spec) -> None:
    """Register a new backend spec.

    Args:
        spec: The spec to register.

    Raises:
        ValueError: If a spec with the same kind is already registered.
    """
    global _REGISTRY, REGISTRY
    if any(existing.kind == spec.kind for existing in _REGISTRY):
        raise ValueError(f"duplicate kind: {spec.kind!r}")
    _REGISTRY.append(spec)
    REGISTRY = tuple(_REGISTRY)


def get(kind: str) -> Spec:
    """Get the spec for the given kind.

    Args:
        kind: The backend kind to look up.

    Returns:
        The spec with the given kind.

    Raises:
        KeyError: If no spec is registered for the given kind.
    """
    for spec in REGISTRY:
        if spec.kind == kind:
            return spec
    raise KeyError(f"no spec registered for kind={kind!r}")


def build_all(cfg: Any) -> dict[str, Any]:
    """Build all enabled backends.

    Startup must not fail because an optional backend is unconfigured.
    A build failure for an ENABLED spec is fatal and names the kind.

    Args:
        cfg: Configuration object passed to enabled/build callbacks.

    Returns:
        Mapping of backend name to built instance for enabled backends.

    Raises:
        Exception: If building an enabled backend fails, with a message
                   that includes the backend kind.
    """
    result: dict[str, Any] = {}
    for spec in REGISTRY:
        if not spec.enabled(cfg):
            # Skip disabled backends (optional, unconfigured).
            continue
        try:
            built = spec.build(cfg)
        except Exception as exc:
            # Re-raise with a message that names the kind.
            raise type(exc)(f"Failed to build {spec.kind!r}: {exc}") from exc
        result[spec.name] = built
    return result