"""Tier definitions, quota enforcement and usage accounting for RAVEN SaaS.

Three tiers, enforced per Telegram user per UTC day:

    free        5 scans/day, text only
    pro        50 scans/day, adds image + document analysis
    enterprise  unlimited, adds video analysis

Safety rules baked into this module
-----------------------------------
* The bot owner (`ADMIN_USER_ID`) always bypasses every quota, so a paying
  customer can never lock the administrator out of their own bot.
* Nothing here can grant access to trading. `/buy`, `/sell`, `/trade_mode`,
  `/emergency_stop` and `/resume` stay admin-only in telegram_bot.py and are
  deliberately *not* routed through this module. A tier is a product
  entitlement, never an authorisation to move money.
* Every check fails *open*: if the database is unavailable the action is
  allowed, so a DB outage degrades the product rather than bricking the bot.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict, Optional, Tuple

import database

logger = logging.getLogger("raven.saas")

# Capability names used by the @requires decorator.
A_TEXT = "text"
A_SCAN = "scan"
A_IMAGE = "image"
A_DOCUMENT = "document"
A_VIDEO = "video"
A_TRANSLATE = "translate"
A_VOICE = "voice"

UNLIMITED = -1

TIERS: Dict[str, Dict[str, Any]] = {
    database.TIER_FREE: {
        "label": "Free",
        "daily_scans": 5,
        "daily_media": 0,
        "capabilities": {A_TEXT, A_TRANSLATE},
        "blurb": "5 scans/day - chat & translation",
    },
    database.TIER_PRO: {
        "label": "Pro",
        "daily_scans": 50,
        "daily_media": 50,
        "capabilities": {A_TEXT, A_TRANSLATE, A_SCAN, A_IMAGE, A_DOCUMENT, A_VOICE},
        "blurb": "50 scans/day - adds image, document & voice replies",
    },
    database.TIER_ENTERPRISE: {
        "label": "Enterprise",
        "daily_scans": UNLIMITED,
        "daily_media": UNLIMITED,
        "capabilities": {A_TEXT, A_TRANSLATE, A_SCAN, A_IMAGE, A_DOCUMENT,
                         A_VIDEO, A_VOICE},
        "blurb": "Unlimited - adds video analysis & API access",
    },
}

# Which daily counter each metered action consumes.
_ACTION_COUNTER = {
    A_SCAN: "daily_scans",
    A_IMAGE: "daily_media",
    A_DOCUMENT: "daily_media",
    A_VIDEO: "daily_media",
    # Translation runs the local model, so it costs the same as a scan.
    A_TRANSLATE: "daily_scans",
    # Voice is metered as media: it calls out to an external speech service.
    A_VOICE: "daily_media",
}


def tier_config(tier: str) -> Dict[str, Any]:
    return TIERS.get((tier or database.TIER_FREE).lower(), TIERS[database.TIER_FREE])


def capabilities(tier: str) -> set:
    return set(tier_config(tier)["capabilities"])


def limit_for(tier: str, action: str) -> int:
    cfg = tier_config(tier)
    if action in (A_IMAGE, A_DOCUMENT, A_VIDEO, A_VOICE):
        return cfg["daily_media"]
    return cfg["daily_scans"]


async def record_usage(telegram_id: str, action: str) -> None:
    """Increment the daily counter an action maps to. Never raises."""
    counter = _ACTION_COUNTER.get(action)
    if not counter:
        return
    try:
        await database.abump(str(telegram_id), counter)
    except Exception:
        logger.warning("usage accounting failed for %s/%s", telegram_id, counter, exc_info=True)


async def evaluate(telegram_id: str, action: str,
                   is_admin: bool = False) -> Tuple[bool, str]:
    """Decide whether `telegram_id` may perform `action` now.

    Returns (allowed, reason). `reason` is empty when allowed.
    """
    if is_admin:
        return True, ""

    try:
        user = await database.aget_user(str(telegram_id))
        tier = user.get("tier", database.TIER_FREE)
    except Exception:
        logger.warning("tier lookup failed; allowing %s for %s", action,
                       telegram_id, exc_info=True)
        return True, ""

    cfg = tier_config(tier)

    if action not in cfg["capabilities"]:
        needed = (database.TIER_PRO if action in (A_IMAGE, A_DOCUMENT)
                  else database.TIER_ENTERPRISE)
        return False, (
            f"\U0001F512 `{action}` analysis needs the {tier_config(needed)['label']} "
            f"tier. You are on {cfg['label']}.\nUse /upgrade to see plans."
        )

    cap = limit_for(tier, action)
    if cap != UNLIMITED:
        counter = _ACTION_COUNTER.get(action, "daily_scans")
        used = (await database.ausage(str(telegram_id))).get(counter, 0)
        if used >= cap:
            return False, (
                f"\u26D3 Daily {action} limit reached ({used}/{cap} on {cfg['label']}).\n"
                f"Resets at 00:00 UTC, or upgrade with /upgrade."
            )
    return True, ""


def requires(action: str):
    """Gate a command handler on tier capability + daily quota.

    Usage:  @requires(A_SCAN)
            async def scan_command(update, context): ...

    The wrapped handler must be a coroutine function taking (update, context).
    `is_admin` is resolved lazily at call time so tests can patch it.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            user = getattr(update, "effective_user", None)
            if user is None:
                return await func(update, context, *args, **kwargs)

            # Imported lazily: telegram_bot imports this module, so a
            # top-level import would be circular.
            from telegram_bot import is_admin, send_long

            uid = user.id
            allowed, reason = await evaluate(uid, action,
                                            is_admin=bool(is_admin(uid)))
            if not allowed:
                await send_long(update, reason)
                return None

            await record_usage(uid, action)
            return await func(update, context, *args, **kwargs)

        wrapper.raven_tier_action = action
        return wrapper

    return decorator


async def usage_summary(telegram_id: str, is_admin: bool = False) -> Dict[str, Any]:
    """Everything /my_usage needs in a single call.

    `is_admin` affects display only: quotas are bypassed for the owner at check
    time, so without it the panel would claim "Free tier, 0 media left" to
    someone who in fact has unrestricted access.
    """
    user = await database.aget_user(str(telegram_id))
    tier = user.get("tier", database.TIER_FREE)
    cfg = tier_config(tier)
    used = await database.ausage(str(telegram_id))

    def remaining(cap: int, key: str) -> Optional[int]:
        if cap == UNLIMITED:
            return None
        return max(0, cap - used.get(key, 0))

    return {
        "telegram_id": str(telegram_id),
        "username": user.get("username", ""),
        "tier": tier,
        "tier_label": cfg["label"],
        "blurb": cfg["blurb"],
        "capabilities": sorted(cfg["capabilities"]),
        "used": used,
        "scans_limit": cfg["daily_scans"],
        "scans_remaining": remaining(cfg["daily_scans"], "daily_scans"),
        "media_limit": cfg["daily_media"],
        "media_remaining": remaining(cfg["daily_media"], "daily_media"),
        "storage": "sqlite" if database.db_available() else "memory",
        "is_admin": bool(is_admin),
    }


def render_usage(summary: Dict[str, Any]) -> str:
    """Render a /my_usage summary as Telegram-friendly plain text."""

    def line(label: str, used: int, limit: int, left: Optional[int]) -> str:
        if limit == UNLIMITED:
            return f"  {label:<8} {used} used / unlimited"
        return f"  {label:<8} {used}/{limit} used - {left} left"

    icons = {
        database.TIER_FREE: "\U0001F7E2",
        database.TIER_PRO: "\U0001F535",
        database.TIER_ENTERPRISE: "\U0001F4E6",
    }
    who = summary.get("username") or summary["telegram_id"]

    # The owner is not on a tier at all. Show what is actually true instead of a
    # free-tier panel full of zeroes, which reads as "broken" or "locked".
    if summary.get("is_admin"):
        used = summary["used"]
        return "\n".join([
            "\U0001F451 MY USAGE - OWNER (ADMIN)",
            "-" * 34,
            f"  User     @{who}",
            "  Access   all features, no daily quota",
            f"  Storage  {summary['storage']}",
            f"  Stored tier: {summary['tier_label']} (not applied to you)",
            "",
            "TODAY - counted for reference, never enforced",
            line("Scans", used.get("daily_scans", 0), UNLIMITED, None),
            line("Media", used.get("daily_media", 0), UNLIMITED, None),
            "",
            "FEATURES - all unlocked",
            "  [yes] Text chat        [yes] Security scans",
            "  [yes] Image analysis   [yes] Document analysis",
            "  [yes] Video analysis   [yes] Translation",
            "  [yes] Voice replies",
        ])

    icon = icons.get(summary["tier"], "\u26AA")
    lines = [
        f"{icon} MY USAGE - {summary['tier_label'].upper()} TIER",
        "-" * 34,
        f"  User     @{who}",
        f"  Plan     {summary['blurb']}",
        f"  Storage  {summary['storage']}",
        "",
        "TODAY (resets 00:00 UTC)",
        line("Scans", summary["used"].get("daily_scans", 0),
             summary["scans_limit"], summary["scans_remaining"]),
        line("Media", summary["used"].get("daily_media", 0),
             summary["media_limit"], summary["media_remaining"]),
        "",
        "FEATURES",
    ]
    labels = {
        A_TEXT: "Text chat",
        A_SCAN: "Security scans",
        A_IMAGE: "Image analysis",
        A_DOCUMENT: "Document analysis",
        A_VIDEO: "Video analysis",
        A_TRANSLATE: "Translation",
        A_VOICE: "Voice replies",
    }
    for cap in (A_TEXT, A_TRANSLATE, A_SCAN, A_IMAGE, A_DOCUMENT, A_VIDEO, A_VOICE):
        mark = "yes" if cap in summary["capabilities"] else "no "
        lines.append(f"  [{mark}] {labels[cap]}")
    if summary["tier"] != database.TIER_ENTERPRISE:
        lines += ["", "Use /upgrade for more scans and media analysis."]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import asyncio

    async def _demo():
        uid = "999001"
        await database.aget_user(uid, "demo")
        await database.aset_tier(uid, database.TIER_FREE)
        print("free -> image :", await evaluate(uid, A_IMAGE))
        await database.aset_tier(uid, database.TIER_PRO)
        print("pro  -> image :", await evaluate(uid, A_IMAGE))
        print("pro  -> video :", (await evaluate(uid, A_VIDEO))[0])
        for _ in range(3):
            await record_usage(uid, A_SCAN)
        print(render_usage(await usage_summary(uid)))
        print("admin bypasses video:", await evaluate(uid, A_VIDEO, is_admin=True))

    asyncio.run(_demo())