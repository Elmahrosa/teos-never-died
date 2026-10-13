import pytest
from errors import FailureReason, Failure, classify, is_retryable

# Helper exception classes to simulate real exceptions
class TimeoutError(Exception):
    pass

class ConnectError(Exception):
    pass

class ConnectionError(Exception):
    pass

class NameResolutionError(Exception):
    pass

class RateLimitError(Exception):
    pass

class TooManyRequestsError(Exception):
    pass

class ForbiddenError(Exception):
    pass

class PermissionDeniedError(Exception):
    pass

class UnauthorizedError(Exception):
    pass

class NotFoundError(Exception):
    pass

class RejectedError(Exception):
    pass

class OperationalError(Exception):
    pass

class InterfaceError(Exception):
    pass

class DatabaseError(Exception):
    pass

# Exception with retry_after attribute
class RetryAfterException(Exception):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after

# Exception with response and status
class HTTPException(Exception):
    def __init__(self, message, status=None, headers=None):
        super().__init__(message)
        self.status = status
        self.response = type('Response', (), {'status': status, 'headers': headers}) if headers or status else None

# Exception with Retry-After header
class HTTPRetryAfterException(Exception):
    def __init__(self, message, retry_after_header):
        super().__init__(message)
        self.response = type('Response', (), {'headers': {'Retry-After': str(retry_after_header)}})()

# Exception with status attribute
class StatusException(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status

# Unknown exception
class UnknownError(Exception):
    pass

@pytest.mark.parametrize("exc, expected_reason", [
    (TimeoutError(), FailureReason.TIMEOUT),
    (ConnectError(), FailureReason.UNREACHABLE),
    (ConnectionError(), FailureReason.UNREACHABLE),
    (NameResolutionError(), FailureReason.UNREACHABLE),
    (RateLimitError(), FailureReason.RATE_LIMITED),
    (TooManyRequestsError(), FailureReason.RATE_LIMITED),
    (ForbiddenError(), FailureReason.FORBIDDEN),
    (PermissionDeniedError(), FailureReason.FORBIDDEN),
    (UnauthorizedError(), FailureReason.FORBIDDEN),
    (NotFoundError(), FailureReason.NOT_FOUND),
    (RejectedError(), FailureReason.REJECTED),
    (OperationalError(), FailureReason.UNREACHABLE),
    (InterfaceError(), FailureReason.UNREACHABLE),
    (DatabaseError(), FailureReason.UNREACHABLE),
    (HTTPException("Not Found", status=404), FailureReason.NOT_FOUND),
    (HTTPException("Forbidden", status=403), FailureReason.FORBIDDEN),
    (HTTPException("Unauthorized", status=401), FailureReason.FORBIDDEN),
    (HTTPException("Rate Limited", status=429), FailureReason.RATE_LIMITED),
    (HTTPException("Server Error", status=500), FailureReason.SERVER_ERROR),
    (StatusException("Not Found", status=404), FailureReason.NOT_FOUND),
    (StatusException("Forbidden", status=403), FailureReason.FORBIDDEN),
    (StatusException("Unauthorized", status=401), FailureReason.FORBIDDEN),
    (StatusException("Rate Limited", status=429), FailureReason.RATE_LIMITED),
    (StatusException("Server Error", status=500), FailureReason.SERVER_ERROR),
    (RetryAfterException("Rate limited with retry", 30.0), FailureReason.RATE_LIMITED),
    (HTTPRetryAfterException("Rate limited with header", 10.0), FailureReason.RATE_LIMITED),
])
def test_classify_success(exc, expected_reason):
    result = classify(exc)
    assert result is not None, f"Expected classification for {exc}"
    assert result.reason == expected_reason, f"Expected {expected_reason}, got {result.reason}"
    # Check that the original exception is chained via __cause__
    assert result.__cause__ is exc, f"Cause not set correctly for {exc}"

def test_classify_unknown():
    exc = UnknownError()
    result = classify(exc)
    assert result is None, f"Expected None for unknown exception, got {result}"

def test_is_retryable():
    assert is_retryable(FailureReason.UNREACHABLE) == True
    assert is_retryable(FailureReason.TIMEOUT) == True
    assert is_retryable(FailureReason.RATE_LIMITED) == True
    assert is_retryable(FailureReason.QUOTA_EXCEEDED) == True
    assert is_retryable(FailureReason.SERVER_ERROR) == True
    assert is_retryable(FailureReason.FORBIDDEN) == False
    assert is_retryable(FailureReason.NOT_FOUND) == False
    assert is_retryable(FailureReason.REJECTED) == False

def test_failure_cause_chaining():
    cause = ValueError("original error")
    try:
        raise Failure(FailureReason.SERVER_ERROR, "Something went wrong", cause=cause)
    except Failure as e:
        assert e.__cause__ is cause, f"Expected __cause__ to be {cause}, got {e.__cause__}"

# Additional test for the constructor parameters
def test_failure_init():
    cause = RuntimeError("cause")
    failure = Failure(FailureReason.QUOTA_EXCEEDED, "Quota exceeded", retry_after=5.5, cause=cause)
    assert failure.reason == FailureReason.QUOTA_EXCEEDED
    assert failure.message == "Quota exceeded"
    assert failure.retry_after == 5.5
    assert failure.__cause__ is cause

# Test that classify returns None for exceptions that don't match any rule
def test_classify_returns_none():
    class CustomError(Exception):
        pass
    exc = CustomError()
    assert classify(exc) is None