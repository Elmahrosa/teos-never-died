"""Credly badge verification for RAVEN.

Lets RAVEN state the plain facts about a Credly badge - badge name,
issuer, recipient, description - straight from what Credly itself
publishes, instead of letting the LLM guess. That is how "Claude
Partner Badge issued by Anthropic to Ayman Seif" became "Issuer:
Twitter" in production: the chat model invented a summary from the
/twitter share card.

Collecting these facts never touches a model. The verifier fetches the
public badge page, reads Credly's own meta tags, and returns the fields
verbatim. A badge that exists publicly is by definition verified by the
issuer (Credly only serves pages for badges whose issuing organization
made them shareable).

The public API is attempted first (it is documented but not available
for every issuing organization); the HTML page is the reliable fallback
and is the source of the values below.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("raven.badge")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover - bot always ships requests
    requests = None
    REQUESTS_AVAILABLE = False

API_URL = "https://api.credly.com/v1/badges/{uuid}"
PAGE_URL = "https://www.credly.com/badges/{uuid}"

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Credly's og:title looks like:
#   "Claude Partner Badge - Claude Code was issued by Anthropic to Ayman Seif."
_TITLE_RE = re.compile(
    r"^(.*?)\s+was issued by\s+(.*?)\s+to\s+(.*?)\.?$", re.IGNORECASE
)


class BadgeVerifyError(ValueError):
    """The input is not a verifiable Credly badge."""


def extract_uuid(text: str) -> Optional[str]:
    """Pull a Credly badge UUID out of a URL, a /twitter share link, or a
    bare UUID string. Returns None when nothing looks like a badge id."""
    if not text:
        return None
    match = UUID_RE.search(str(text).strip())
    return match.group(0) if match else None


def _og(html: str, property_name: str) -> Optional[str]:
    """Value of <meta property="og:xxx" content="...">, unescaped."""
    pattern = re.compile(
        r'<meta[^>]+property=["\']og:{0}["\'][^>]+content=["\'](.*?)["\']'
        .format(re.escape(property_name)),
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        # Some pages put content= before property=.
        pattern = re.compile(
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:{0}["\']'
            .format(re.escape(property_name)),
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(html)
    if not match:
        return None
    return _unescape(match.group(1))


def _unescape(text: str) -> str:
    return (text.replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))


def _parse_title(title: str) -> Dict[str, str]:
    """Split 'X was issued by Y to Z.' into its parts. Falls back to the
    whole string as the badge name when the shape is unexpected."""
    match = _TITLE_RE.match(title.strip())
    if match:
        return {
            "badge": match.group(1).strip(),
            "issuer": match.group(2).strip(),
            "recipient": match.group(3).strip(),
        }
    return {"badge": title.strip(), "issuer": "", "recipient": ""}


def _from_api(data: Any, uuid: str) -> Dict[str, Any]:
    """Best-effort mapping of the v1 API JSON (used when the issuer's
    organization exposes it publicly)."""
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        raise BadgeVerifyError("Credly returned an unexpected shape")
    template = d.get("badge_template") or {}
    issuer = template.get("issuer") or {}
    issued = {
        "uuid": uuid,
        "badge": (template.get("name") or "").strip(),
        "issuer": (issuer.get("name") or "").strip(),
        "recipient": ((d.get("issued_to") or {}).get("first_name") or "").strip(),
        "description": (template.get("description") or "").strip(),
        "issued_at": d.get("issued_at") or "",
        "expires_at": d.get("expires_at") or "",
        "state": d.get("state") or "",
        "url": PAGE_URL.format(uuid=uuid),
        "source": "api",
    }
    return issued


def _from_page(html: str, uuid: str) -> Dict[str, Any]:
    title = _og(html, "title") or ""
    parts = _parse_title(title)
    return {
        "uuid": uuid,
        "badge": parts["badge"],
        "issuer": parts["issuer"],
        "recipient": parts["recipient"],
        "description": (_og(html, "description") or "").strip(),
        "issued_at": "",
        "expires_at": "",
        "state": "issued",
        "url": _og(html, "url") or PAGE_URL.format(uuid=uuid),
        "source": "page",
    }


def verify_badge(text: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Verify a Credly badge URL (or UUID) and return its public facts.

    Raises BadgeVerifyError with an actionable message when the input has
    no badge id or the badge is not publicly verifiable.
    """
    if not REQUESTS_AVAILABLE:  # pragma: no cover
        raise BadgeVerifyError("requests is not installed - can't verify badges")

    uuid = extract_uuid(text)
    if not uuid:
        raise BadgeVerifyError(
            "No Credly badge id found. Paste the full badge link or its UUID, "
            "e.g. /verify_badge https://www.credly.com/badges/<uuid>"
        )

    errors = []
    # Preferred: the JSON API.
    try:
        resp = requests.get(API_URL.format(uuid=uuid), timeout=timeout)
        if resp.ok:
            return _from_api(resp.json(), uuid)
        errors.append(f"api {resp.status_code}")
    except Exception as exc:
        errors.append(f"api {type(exc).__name__}")

    # Fallback: the public page's own metadata.
    try:
        resp = requests.get(PAGE_URL.format(uuid=uuid), timeout=timeout,
                            headers={"User-Agent": "RAVEN-badge-verifier/1.0"})
        if resp.ok:
            return _from_page(resp.text, uuid)
        errors.append(f"page {resp.status_code}")
    except Exception as exc:
        errors.append(f"page {type(exc).__name__}")

    raise BadgeVerifyError(
        "That badge could not be verified - Credly returned "
        f"{', '.join(errors)}. It may be private, revoked, or mistyped."
    )


def format_badge(info: Dict[str, Any]) -> str:
    """Render the verified facts as the bot's reply (no markdown)."""
    lines = [
        "✅ BADGE VERIFIED",
        "-" * 32,
        f"Badge:     {info['badge']}",
        f"Issuer:    {info['issuer']}",
        f"Recipient: {info['recipient']}",
    ]
    if info.get("issued_at"):
        lines.append(f"Issued:    {info['issued_at'][:10]}")
    if info.get("expires_at"):
        lines.append(f"Expires:   {info['expires_at'][:10]}")
    if info.get("description"):
        text = info["description"]
        lines.append("")
        lines.append(text[:600] + ("..." if len(text) > 600 else ""))
    lines += ["", f"Source:    Credly public page ({info.get('source', 'page')})"]
    return "\n".join(lines)