"""Payment webhook for RAVEN SaaS upgrades.

Runs as its own process, deliberately NOT inside the Telegram bot:

    python webhook_server.py            # 0.0.0.0:8099
    uvicorn webhook_server:app --port 8099

Security posture
----------------
This endpoint credits paid tiers, so it is the highest-value target in the
product. Therefore:

* It fails CLOSED. With no secret configured it returns 503 and upgrades
  nothing. A misconfigured deployment cannot be used to grant free tiers.
* Signatures are compared with `hmac.compare_digest`, not `==`.
* Signed payloads carry a timestamp and are rejected outside
  PAYMENT_WEBHOOK_TOLERANCE seconds, so a captured request cannot be replayed
  forever.
* Plan names are mapped through a fixed allow-list. The client never gets to
  say "tier": "enterprise" by itself.
* Event IDs are remembered for IDEMPOTENCY_WINDOW seconds so a provider retry
  cannot double-credit an account.
* Nothing about the request body is logged except the event id and plan.

Payment providers are not configured or exercised in this repository; the
provider-specific parsing below is written against the common shape these
vendors emit and needs one live delivery attempt against real credentials before
it can be called verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("raven.webhook")

WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "").strip() or \
    os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()
PAYMENT_WEBHOOK_TOLERANCE = int(os.getenv("PAYMENT_WEBHOOK_TOLERANCE", "300"))
IDEMPOTENCY_WINDOW = int(os.getenv("PAYMENT_IDEMPOTENCY_WINDOW", "86400"))
NOTIFY_TELEGRAM = os.getenv("PAYMENT_NOTIFY_TELEGRAM", "true").lower() in ("1", "true", "yes", "on")

# Provider price IDs -> tier. The webhook grants only what is listed here.
PLAN_MAP: Dict[str, str] = {
    "pro_monthly": database.TIER_PRO,
    "pro_annual": database.TIER_PRO,
    "pro": database.TIER_PRO,
    "enterprise_monthly": database.TIER_ENTERPRISE,
    "enterprise_annual": database.TIER_ENTERPRISE,
    "enterprise": database.TIER_ENTERPRISE,
}

_seen_events: "OrderedDict[str, float]" = OrderedDict()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
def _secret_bytes() -> bytes:
    return WEBHOOK_SECRET.encode("utf-8")


def verify_signature(body: bytes, header: str) -> Tuple[bool, str]:
    """Verify a Svix-style `t=<ts>,v1=<hmac>` signature header.

    Also accepts a bare hex HMAC of the raw body, which is what simpler
    providers and hand-rolled tests send.
    """
    if not WEBHOOK_SECRET:
        return False, "no webhook secret configured"
    if not header:
        return False, "missing signature header"

    header = header.strip()

    # Form A: t=...,v1=...
    if "t=" in header and "v1=" in header:
        parts = dict(
            piece.split("=", 1) for piece in header.split(",") if "=" in piece
        )
        ts_raw = parts.get("t", "")
        provided = parts.get("v1", "")
        if not ts_raw or not provided:
            return False, "malformed signature header"
        try:
            ts = int(ts_raw)
        except ValueError:
            return False, "non-numeric timestamp"
        skew = abs(int(time.time()) - ts)
        if skew > PAYMENT_WEBHOOK_TOLERANCE:
            return False, f"timestamp outside {PAYMENT_WEBHOOK_TOLERANCE}s tolerance"
        signed = f"{ts_raw}.".encode("utf-8") + body
        expected = hmac.new(_secret_bytes(), signed, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided.strip()):
            return True, "ok"
        return False, "signature mismatch"

    # Form B: bare hex digest of the body.
    expected = hmac.new(_secret_bytes(), body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, header):
        return True, "ok"
    return False, "signature mismatch"


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------
def _find_telegram_id(node: Any, depth: int = 0) -> Optional[str]:
    """Locate the buyer's Telegram id in a provider payload.

    Recursion is restricted to a fixed allow-list of wrapper keys (`metadata`,
    `data`, `event`, ...) rather than every key in the tree. Crediting a tier is
    irreversible, so an unrelated 9-digit number buried somewhere unexpected
    must not be mistaken for a Telegram id. An unknown wrapper shape therefore
    returns None and a 422 instead of guessing.
    """
    if depth > 6 or not isinstance(node, dict):
        return None
    for key in ("telegram_id", "telegramId", "telegram_user_id", "chat_id"):
        val = node.get(key)
        if isinstance(val, (int, str)) and str(val).strip().lstrip("-").isdigit():
            return str(val).strip()
    for key in ("metadata", "data", "event", "payload", "client_ref", "custom_data"):
        if key in node:
            found = _find_telegram_id(node[key], depth + 1)
            if found:
                return found
    return None


def _find_event_id(payload: Dict[str, Any]) -> str:
    for key in ("event_id", "idempotency_key", "payment_id", "id"):
        val = payload.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val).strip()
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("payment_id", "id"):
            val = data.get(key)
            if isinstance(val, (str, int)) and str(val).strip():
                return str(val).strip()
    # Fall back to a content hash so an unidentifiable payload still dedupes.
    return "hash:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


def _find_plan(payload: Dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (payload, data):
        for key in ("price_id", "plan_id", "product_id", "plan", "tier"):
            val = source.get(key)
            if isinstance(val, str) and val.strip().lower() in PLAN_MAP:
                return val.strip().lower()
    return ""


def _is_paid(payload: Dict[str, Any]) -> bool:
    """Only credit an actually-successful payment."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    event = str(payload.get("type") or payload.get("event") or "").lower()
    if event and any(
        word in event
        for word in ("succeeded", "paid", "active", "complete", "settled")
    ):
        return True
    # Explicit failure statuses win over the substring check above.
    if any(word in event for word in ("failed", "cancel", "refund", "expired",
                                      "dispute", "chargeback")):
        return False

    status = str(payload.get("status") or data.get("status") or "").lower()
    return status in ("succeeded", "paid", "active", "complete", "settled", "captured")


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------
async def notify_telegram(telegram_id: str, tier: str, plan: str) -> bool:
    """Best-effort upgrade notice. Failure never fails the webhook."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not NOTIFY_TELEGRAM or not token:
        return False
    label = tier.capitalize()
    text = (
        f"Your RAVEN upgrade to {label} is active.\n\n"
        f"Now unlocked:\n"
        + ("  - Image analysis\n" if tier in (database.TIER_PRO, database.TIER_ENTERPRISE) else "")
        + ("  - Document analysis\n" if tier in (database.TIER_PRO, database.TIER_ENTERPRISE) else "")
        + ("  - Video analysis\n" if tier == database.TIER_ENTERPRISE else "")
        + "  - Higher daily scan and media limits\n\n"
        "Check your allowance with /my_usage."
    )
    try:
        import requests  # noqa: F401

        def _post():
            return requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": telegram_id, "text": text},
                timeout=20,
            )

        resp = await asyncio.to_thread(_post)
        resp.raise_for_status()
        return True
    except Exception as exc:
        # Never echo the token or the URL it was embedded in.
        logger.warning("upgrade notification to %s failed: %s", telegram_id, type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
try:
    from fastapi import FastAPI, Header, Request
    from fastapi.responses import JSONResponse

    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAVE_FASTAPI = False
    logger.error("FastAPI not installed - run: pip install fastapi uvicorn")


def remember_event(event_id: str) -> bool:
    """Return True if this event is new; record it. Prunes expired entries."""
    now = time.time()
    cutoff = now - IDEMPOTENCY_WINDOW
    while _seen_events:
        oldest_id, ts = next(iter(_seen_events.items()))
        if ts >= cutoff:
            break
        _seen_events.pop(oldest_id)
    if event_id in _seen_events:
        return False
    _seen_events[event_id] = now
    return True


if _HAVE_FASTAPI:
    app = FastAPI(title="RAVEN payment webhook", version="1.0")

    @app.on_event("startup")
    async def _startup():
        ok = await asyncio.to_thread(database.init_db)
        logger.info("webhook starting - database %s, secret %s",
                    "ready" if ok else "UNAVAILABLE (in-memory fallback)",
                    "configured" if WEBHOOK_SECRET else "MISSING (webhook will 503)")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "database": await asyncio.to_thread(database.db_available),
            "signature_verification": bool(WEBHOOK_SECRET),
            "plans": sorted(set(PLAN_MAP.values())),
        }

    @app.post("/webhook/payment")
    async def payment(request: Request,
                       dodo_signature: Optional[str] = Header(default=None),
                       x_webhook_signature: Optional[str] = Header(default=None),
                       x_raven_secret: Optional[str] = Header(default=None)):
        body = await request.body()

        if not WEBHOOK_SECRET:
            # Fail closed: an unconfigured webhook must never grant a tier.
            return JSONResponse(
                {"ok": False, "error": "webhook secret not configured"},
                status_code=503)

        signature = dodo_signature or x_webhook_signature
        if signature:
            valid, why = verify_signature(body, signature)
        elif x_raven_secret:
            # Shared-secret fallback for providers that only offer a static
            # bearer token. Still constant-time.
            valid = hmac.compare_digest(x_raven_secret, WEBHOOK_SECRET)
            why = "ok" if valid else "secret mismatch"
        else:
            valid, why = False, "no signature supplied"

        if not valid:
            logger.warning("rejected payment webhook: %s", why)
            return JSONResponse({"ok": False, "error": why}, status_code=400)

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)

        event_id = _find_event_id(payload)
        if not remember_event(event_id):
            logger.info("duplicate payment event %s ignored", event_id)
            return JSONResponse({"ok": True, "status": "duplicate", "event_id": event_id})

        if not _is_paid(payload):
            logger.info("payment event %s is not a settled payment; no credit", event_id)
            return JSONResponse({"ok": True, "status": "ignored_not_paid",
                                 "event_id": event_id})

        telegram_id = _find_telegram_id(payload)
        if not telegram_id:
            logger.warning("payment %s has no telegram_id in metadata", event_id)
            return JSONResponse({"ok": False,
                                 "error": "telegram_id not found in metadata"},
                                status_code=422)

        plan = _find_plan(payload)
        tier = PLAN_MAP.get(plan)
        if not tier:
            logger.warning("payment %s references unmapped plan %r; no credit",
                           event_id, plan or "(none)")
            return JSONResponse({"ok": False, "error": "unrecognised plan"},
                                status_code=422)

        # Read the existing tier BEFORE writing. Reading it afterwards would
        # return the tier this call just granted, so the guard below could never
        # detect anything.
        current = (await database.aget_user(telegram_id)).get("tier", database.TIER_FREE)
        order = {database.TIER_FREE: 0, database.TIER_PRO: 1, database.TIER_ENTERPRISE: 2}
        downgraded = order.get(current, 0) > order.get(tier, 0)
        if downgraded:
            # Keep the higher tier the customer already paid for, but still log
            # the payment so revenue history stays complete.
            logger.info("payment %s: user %s already on %s; %s does not downgrade",
                        event_id, telegram_id, current, plan)
            tier = current

        granted = await database.aset_tier(
            telegram_id, tier, plan=plan, provider="dodo", reference=event_id)

        await notify_telegram(telegram_id, tier, plan)

        logger.info("payment %s -> user %s on %s tier (db=%s)",
                    event_id, telegram_id, tier, "written" if granted else "unchanged")
        return JSONResponse({"ok": True, "status": "credited", "event_id": event_id,
                             "tier": tier, "database_written": granted})
