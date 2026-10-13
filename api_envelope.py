"""API envelope unwrapping utilities.

Decoding the wrapper into the payload type silently succeeds with every field defaulted,
so the caller sees 'no data' instead of an error. This function forces the wrapper to
be unwrapped first.
"""

import json
from typing import Any, Type, TypeVar

T = TypeVar('T')


class APIError(Exception):
    """Raised when the API envelope indicates an error or is malformed."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"API error: {status} - {message}")


def parse_envelope(raw: bytes | str, model: Type[T]) -> T:
    """Decode and validate an API envelope.

    Args:
        raw: The raw response bytes or string.
        model: A type to validate the data against (duck-typed for pydantic v1/v2 or dataclass).

    Returns:
        An instance of model populated with the data from the envelope.

    Raises:
        APIError: If the envelope is malformed, the status is not success, or data validation fails.
    """
    # 1. Decode raw to dict
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8')
        except UnicodeDecodeError as e:
            raise APIError("malformed", f"Failed to decode bytes: {e}") from e

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        raise APIError("malformed", str(e)) from e

    # 2. Check top-level is dict and has status
    if not isinstance(envelope, dict):
        raise APIError("malformed", f"Top-level is not a dict, got {type(envelope).__name__}")
    if "status" not in envelope:
        raise APIError("malformed", "Missing 'status' field")

    status = envelope["status"]
    if status != "success":
        # 3. If status != success, raise APIError with server's message or default
        message = envelope.get("message", f"server returned status={status}")
        raise APIError(status, message)

    # 4. On success, validate data against model
    data = envelope.get("data")
    if data is None:
        data = {}

    try:
        # Try pydantic v2
        if hasattr(model, "model_validate"):
            return model.model_validate(data)
        # Try pydantic v1
        elif hasattr(model, "parse_obj"):
            return model.parse_obj(data)
        # Try dataclass
        elif hasattr(model, "__dataclass_fields__"):
            if isinstance(data, dict):
                return model(**data)
            else:
                return model(data)
        else:
            # Fallback: call model with data
            return model(data)
    except (TypeError, ValueError) as e:
        # Re-raise as APIError with invalid_data status, naming the field if possible
        field_name = _extract_field_name(e)
        raise APIError("invalid_data", f"Invalid data: {field_name}") from e


def _extract_field_name(exc: Exception) -> str:
    """Attempt to extract a field name from a TypeError or ValueError."""
    exc_str = str(exc)
    # Common patterns: "missing 1 required positional argument: 'x'", "required field missing", etc.
    # We'll try to find a quoted string that looks like a field name.
    import re
    # Look for patterns like 'fieldname' or "fieldname"
    matches = re.findall(r"['\"]([^'\"]+)['\"]", exc_str)
    if matches:
        return matches[-1]  # Return the last match as a heuristic
    return "unknown field"