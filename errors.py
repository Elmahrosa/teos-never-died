"""
RAVEN Actionable Error Taxonomy
===============================

This module defines a structured approach to error handling that provides
users with specific, actionable guidance instead of generic error messages.

Error Categories:
1. Configuration Errors - Missing or invalid configuration
2. External Service Errors - Issues with third-party services (Ollama, APIs, etc.)
3. Validation Errors - Input validation failures
4. Resource Errors - File, memory, or system resource issues
5. Permission Errors - Access denied or insufficient privileges
6. Network Errors - Connectivity or communication issues
7. Data Errors - Data corruption, format issues, or missing data
8. Rate Limit Errors - API rate limiting or throttling

Each error type provides:
- A clear error code for programmatic handling
- A user-friendly message explaining what happened
- Specific actionable steps to resolve the issue
- Optional technical details for debugging
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import logging

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Categories of errors in the RAVEN system."""
    CONFIGURATION = "configuration"
    EXTERNAL_SERVICE = "external_service"
    VALIDATION = "validation"
    RESOURCE = "resource"
    PERMISSION = "permission"
    NETWORK = "network"
    DATA = "data"
    RATE_LIMIT = "rate_limit"


@dataclass
class ActionableError:
    """Represents an actionable error with guidance for resolution."""
    category: ErrorCategory
    code: str
    message: str
    user_guidance: List[str]
    technical_details: Optional[Dict[str, Any]] = None
    recoverable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "user_guidance": self.user_guidance,
            "technical_details": self.technical_details,
            "recoverable": self.recoverable
        }

    def __str__(self) -> str:
        """Human-readable string representation."""
        guidance_str = "\n".join([f"  • {guidance}" for guidance in self.user_guidance])
        return f"{self.message}\n\nTo resolve:\n{guidance_str}"


class ErrorTaxonomy:
    """Central registry of actionable errors in the RAVEN system."""

    # Configuration Errors
    MISSING_DASHBOARD_TOKEN = ActionableError(
        category=ErrorCategory.CONFIGURATION,
        code="RAVEN-001",
        message="Dashboard token not configured",
        user_guidance=[
            "Set the DASHBOARD_TOKEN environment variable in your .env file",
            "Generate a secure token using: python -c \"import secrets; print(secrets.token_urlsafe(24))\"",
            "Restart the dashboard after setting the token"
        ],
        technical_details={"env_var": "DASHBOARD_TOKEN"}
    )

    MISSING_Ollama_MODEL = ActionableError(
        category=ErrorCategory.CONFIGURATION,
        code="RAVEN-002",
        message="Required Ollama model not installed",
        user_guidance=[
            "Pull the required model using: ollama pull <model-name>",
            "Check available models with: ollama list",
            "Verify the model name in your .env file matches what's installed"
        ],
        technical_details={"suggested_command": "ollama list"}
    )

    # External Service Errors
    OLLAMA_UNAVAILABLE = ActionableError(
        category=ErrorCategory.EXTERNAL_SERVICE,
        code="RAVEN-010",
        message="Ollama service is not responding",
        user_guidance=[
            "Ensure Ollama is running: ollama serve",
            "Check if Ollama is installed correctly",
            "Verify the OLLAMA_HOST environment variable points to the correct address",
            "Try restarting the Ollama service"
        ],
        technical_details={"service": "ollama", "endpoint": "/api/tags"}
    )

    HIBP_API_KEY_INVALID = ActionableError(
        category=ErrorCategory.EXTERNAL_SERVICE,
        code="RAVEN-011",
        message="Have I Been Pwned API key is invalid or missing",
        user_guidance=[
            "Get a free API key from https://haveibeenpwned.com/API/Key",
            "Set the HIBP_API_KEY environment variable in your .env file",
            "Verify the key is correctly formatted and not expired"
        ],
        technical_details={"service": "hibp", "api": "haveibeenpwned.com"}
    )

    # Validation Errors
    INVALID_TELEGRAM_ID = ActionableError(
        category=ErrorCategory.VALIDATION,
        code="RAVEN-020",
        message="Invalid Telegram ID format",
        user_guidance=[
            "Telegram ID must be a positive integer",
            "Check that you're entering the correct user ID",
            "Telegram IDs can be found by messaging @userinfobot on Telegram"
        ],
        technical_details={"expected_format": "positive_integer"}
    )

    INVALID_WEBHOOK_SIGNATURE = ActionableError(
        category=ErrorCategory.VALIDATION,
        code="RAVEN-021",
        message="Webhook signature verification failed",
        user_guidance=[
            "Verify the webhook secret in your .env file matches the provider's secret",
            "Check that the webhook payload hasn't been tampered with",
            "Ensure your system clock is synchronized (signature includes timestamp)"
        ],
        technical_details={"verification_type": "hmac_sha256"}
    )

    # Resource Errors
    FILE_HANDLE_LEAK = ActionableError(
        category=ErrorCategory.RESOURCE,
        code="RAVEN-030",
        message="File handle leak detected - file not properly closed",
        user_guidance=[
            "Use context managers (with open(...) as f:) when working with files",
            "Ensure all file operations are properly closed",
            "Consider using try/finally blocks for legacy code"
        ],
        technical_details={"pattern": "open() without corresponding close()"}
    )

    INSUFFICIENT_DISK_SPACE = ActionableError(
        category=ErrorCategory.RESOURCE,
        code="RAVEN-031",
        message="Insufficient disk space for operation",
        user_guidance=[
            "Free up disk space by removing unnecessary files",
            "Check disk usage with: df -h (Linux/Mac) or Get-PSDrive C: (Windows)",
            "Consider moving the database to a larger volume"
        ],
        technical_details={"check_command": "df -h"}
    )

    # Permission Errors
    INSUFFICIENT_PERMISSIONS = ActionableError(
        category=ErrorCategory.PERMISSION,
        code="RAVEN-040",
        message="Insufficient permissions to perform operation",
        user_guidance=[
            "Check file and directory permissions",
            "Run the application with appropriate user privileges",
            "Ensure the user has read/write access to required directories"
        ],
        technical_details={"required_access": "read_write"}
    )

    # Network Errors
    NETWORK_TIMEOUT = ActionableError(
        category=ErrorCategory.NETWORK,
        code="RAVEN-050",
        message="Network operation timed out",
        user_guidance=[
            "Check your internet connectivity",
            "Verify the target service is reachable",
            "Consider increasing timeout values if the operation legitimately takes longer",
            "Try the operation again - temporary network issues often resolve quickly"
        ],
        technical_details={"timeout_type": "operation_timeout"}
    )

    DNS_RESOLUTION_FAILED = ActionableError(
        category=ErrorCategory.NETWORK,
        code="RAVEN-051",
        message="Failed to resolve hostname",
        user_guidance=[
            "Check your DNS configuration",
            "Verify the hostname is spelled correctly",
            "Try using an IP address instead of hostname if appropriate",
            "Flush DNS cache: ipconfig /flushdns (Windows) or sudo systemd-resolve --flush-caches (Linux)"
        ],
        technical_details={"resolution_type": "dns_lookup"}
    )

    # Data Errors
    CORRUPTED_DATABASE = ActionableError(
        category=ErrorCategory.DATA,
        code="RAVEN-060",
        message="Database appears to be corrupted",
        user_guidance=[
            "Backup your current database file (raven_saas.db)",
            "Try restarting the application - SQLite may auto-recover on restart",
            "If corruption persists, you may need to delete the database and let it recreate",
            "Note: Deleting the database will reset all user tiers and usage data"
        ],
        technical_details={"database_file": "raven_saas.db", "auto_recovery": True}
    )

    INVALID_JSON_FORMAT = ActionableError(
        category=ErrorCategory.DATA,
        code="RAVEN-061",
        message="Invalid JSON format received",
        user_guidance=[
            "Verify the data source is producing valid JSON",
            "Check for encoding issues or truncated data",
            "Ensure special characters are properly escaped",
            "Use a JSON validator to check the format"
        ],
        technical_details={"format": "json", "validation_tool": "jsonlint"}
    )

    # Rate Limit Errors
    API_RATE_LIMIT_EXCEEDED = ActionableError(
        category=ErrorCategory.RATE_LIMIT,
        code="RAVEN-070",
        message="API rate limit exceeded",
        user_guidance=[
            "Wait before making additional requests",
            "Consider implementing exponential backoff for retry attempts",
            "Check if you can upgrade your API plan for higher rate limits",
            "Review your usage patterns to optimize API call frequency"
        ],
        technical_details={"retry_after_header": "Retry-After"}
    )

    @classmethod
    def get_error_by_code(cls, code: str) -> Optional[ActionableError]:
        """Get an error by its code."""
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if isinstance(attr, ActionableError) and attr.code == code:
                return attr
        return None

    @classmethod
    def get_errors_by_category(cls, category: ErrorCategory) -> List[ActionableError]:
        """Get all errors in a specific category."""
        errors = []
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if isinstance(attr, ActionableError) and attr.category == category:
                errors.append(attr)
        return errors


def log_actionable_error(error: ActionableError, exception: Optional[Exception] = None,
                        context: Optional[str] = None) -> None:
    """
    Log an actionable error with optional exception and context.

    Args:
        error: The ActionableError to log
        exception: Optional exception that caused this error
        context: Optional context information (e.g., function name, operation)
    """
    log_parts = [
        f"ERROR [{error.code}]: {error.message}"
    ]

    if context:
        log_parts.append(f"Context: {context}")

    if exception:
        log_parts.append(f"Exception: {type(exception).__name__}: {str(exception)}")
        logger.error(" ".join(log_parts), exc_info=True)
    else:
        logger.error(" ".join(log_parts))


def format_user_error(error: ActionableError, include_technical: bool = False) -> str:
    """
    Format an actionable error for display to users.

    Args:
        error: The ActionableError to format
        include_technical: Whether to include technical details

    Returns:
        Formatted string suitable for user display
    """
    user_message = str(error)

    if include_technical and error.technical_details:
        tech_str = ", ".join([f"{k}={v}" for k, v in error.technical_details.items()])
        user_message += f"\n\nTechnical details: {tech_str}"

    return user_message


# Convenience functions for common error scenarios
def raise_configuration_error(message: str, guidance: List[str],
                            technical_details: Optional[Dict[str, Any]] = None) -> ActionableError:
    """Create a configuration error."""
    return ActionableError(
        category=ErrorCategory.CONFIGURATION,
        code="RAVEN-900",  # Dynamic errors use 900 series
        message=message,
        user_guidance=guidance,
        technical_details=technical_details
    )


def raise_external_service_error(service: str, message: str, guidance: List[str],
                               technical_details: Optional[Dict[str, Any]] = None) -> ActionableError:
    """Create an external service error."""
    return ActionableError(
        category=ErrorCategory.EXTERNAL_SERVICE,
        code="RAVEN-901",
        message=f"{service}: {message}",
        user_guidance=guidance,
        technical_details=technical_details
    )


def raise_validation_error(field: str, message: str, guidance: List[str],
                         technical_details: Optional[Dict[str, Any]] = None) -> ActionableError:
    """Create a validation error."""
    return ActionableError(
        category=ErrorCategory.VALIDATION,
        code="RAVEN-902",
        message=f"Invalid {field}: {message}",
        user_guidance=guidance,
        technical_details=technical_details
    )


class FailureReason(str, Enum):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    SERVER_ERROR = "server_error"


class Failure(Exception):
    def __init__(self, reason: FailureReason, message: str, *,
                 retry_after: float = 0.0, cause: BaseException | None = None):
        self.reason = reason
        self.message = message
        self.retry_after = retry_after
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


def is_retryable(reason: FailureReason) -> bool:
    RETRYABLE = {
        FailureReason.UNREACHABLE,
        FailureReason.TIMEOUT,
        FailureReason.RATE_LIMITED,
        FailureReason.QUOTA_EXCEEDED,
        FailureReason.SERVER_ERROR,
    }
    return reason in RETRYABLE


def classify(exc: BaseException) -> Failure | None:
    # Rule a: If the exception (or any attribute chain) carries a Retry-After header or a retry_after attribute, match RATE_LIMITED FIRST
    retry_after = getattr(exc, "retry_after", None)
    response = getattr(exc, "response", None)
    headers = None
    if response is not None:
        headers = getattr(response, "headers", None)
    retry_after_header = None
    if headers is not None:
        retry_after_header = headers.get("Retry-After") or headers.get("retry-after")
    # Determine if we have a retry_after signal (attribute or header)
    has_retry_after_signal = False
    retry_after_val = 0.0
    if retry_after is not None:
        try:
            v = float(retry_after)
            if v >= 0:
                has_retry_after_signal = True
                retry_after_val = v
        except (ValueError, TypeError):
            pass
    if not has_retry_after_signal and retry_after_header is not None:
        try:
            v = float(retry_after_header)
            if v >= 0:
                has_retry_after_signal = True
                retry_after_val = v
        except (ValueError, TypeError):
            pass
    if has_retry_after_signal:
        return Failure(FailureReason.RATE_LIMITED, str(exc), retry_after=retry_after_val, cause=exc)

    # Rule b: Match by type, defensively
    exc_type = type(exc)
    exc_name = exc_type.__name__
    # Check for Timeout
    if "Timeout" in exc_name:
        return Failure(FailureReason.TIMEOUT, str(exc), cause=exc)
    # Check for Connect, Connection, NameResolution
    if any(name in exc_name for name in ("Connect", "Connection", "NameResolution")):
        return Failure(FailureReason.UNREACHABLE, str(exc), cause=exc)
    # Check for RateLimit or TooManyRequests
    if "RateLimit" in exc_name or "TooManyRequests" in exc_name:
        return Failure(FailureReason.RATE_LIMITED, str(exc), cause=exc)
    # Check for Forbidden / PermissionDenied / Unauthorized
    if any(name in exc_name for name in ("Forbidden", "PermissionDenied", "Unauthorized")):
        return Failure(FailureReason.FORBIDDEN, str(exc), cause=exc)
    # Check for NotFound
    if "NotFound" in exc_name:
        return Failure(FailureReason.NOT_FOUND, str(exc), cause=exc)
    # Check for Rejected
    if "Rejected" in exc_name:
        return Failure(FailureReason.REJECTED, str(exc), cause=exc)
    # SQLAlchemy errors
    if exc_name.endswith("Error") and ("OperationalError" in exc_name or "InterfaceError" in exc_name or "DatabaseError" in exc_name):
        # Assuming sqlalchemy exceptions; we match by name only
        return Failure(FailureReason.UNREACHABLE, str(exc), cause=exc)
    # HTTP status attribute
    status = getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status", None)
    if status is not None:
        try:
            status_code = int(status)
            if status_code == 429:
                return Failure(FailureReason.RATE_LIMITED, str(exc), cause=exc)
            if status_code == 403 or status_code == 401:
                return Failure(FailureReason.FORBIDDEN, str(exc), cause=exc)
            if status_code == 404:
                return Failure(FailureReason.NOT_FOUND, str(exc), cause=exc)
            if status_code >= 500:
                return Failure(FailureReason.SERVER_ERROR, str(exc), cause=exc)
        except (ValueError, TypeError):
            pass
    # Rule c: If nothing matches, return None
    return None

    # Rule b: Match by type, defensively
    exc_type = type(exc)
    exc_name = exc_type.__name__
    # Check for Timeout
    if "Timeout" in exc_name:
        return Failure(FailureReason.TIMEOUT, str(exc), cause=exc)
    # Check for Connect, Connection, NameResolution
    if any(name in exc_name for name in ("Connect", "Connection", "NameResolution")):
        return Failure(FailureReason.UNREACHABLE, str(exc), cause=exc)
    # Check for RateLimit or TooManyRequests
    if "RateLimit" in exc_name or "TooManyRequests" in exc_name:
        return Failure(FailureReason.RATE_LIMITED, str(exc), cause=exc)
    # Check for Forbidden / PermissionDenied / Unauthorized
    if any(name in exc_name for name in ("Forbidden", "PermissionDenied", "Unauthorized")):
        return Failure(FailureReason.FORBIDDEN, str(exc), cause=exc)
    # Check for NotFound
    if "NotFound" in exc_name:
        return Failure(FailureReason.NOT_FOUND, str(exc), cause=exc)
    # Check for Rejected
    if "Rejected" in exc_name:
        return Failure(FailureReason.REJECTED, str(exc), cause=exc)
    # SQLAlchemy errors
    if exc_name.endswith("Error") and ("OperationalError" in exc_name or "InterfaceError" in exc_name or "DatabaseError" in exc_name):
        # Assuming sqlalchemy exceptions; we match by name only
        return Failure(FailureReason.UNREACHABLE, str(exc), cause=exc)
    # HTTP status attribute
    status = getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status", None)
    if status is not None:
        try:
            status_code = int(status)
            if status_code == 429:
                return Failure(FailureReason.RATE_LIMITED, str(exc), cause=exc)
            if status_code == 403 or status_code == 401:
                return Failure(FailureReason.FORBIDDEN, str(exc), cause=exc)
            if status_code == 404:
                return Failure(FailureReason.NOT_FOUND, str(exc), cause=exc)
            if status_code >= 500:
                return Failure(FailureReason.SERVER_ERROR, str(exc), cause=exc)
        except (ValueError, TypeError):
            pass
    # Rule c: If nothing matches, return None
    return None
