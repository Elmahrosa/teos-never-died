"""Persistence layer for RAVEN multi-tenant support.

Backed by SQLAlchemy over SQLite by default, so deployment needs no external
service. Set DATABASE_URL to point at PostgreSQL instead:

    DATABASE_URL=postgresql+psycopg://user:pass@host/db

Design notes
------------
* Every public coroutine runs its blocking work in a worker thread. SQLite and
  the PostgreSQL driver are both synchronous, and calling them directly from an
  `async def` Telegram handler would freeze the event loop - which is exactly the
  bug that made the bot unresponsive during AI calls.
* If SQLAlchemy is missing the module degrades to an in-memory store instead of
  raising at import time, so the bot always starts.
* No credentials are ever returned by the API; `api_key` is write-only from the
  bot's perspective and is only used by the separate webhook service.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("raven.db")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or None
DEFAULT_SQLITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raven_saas.db")

# Tier names live here so the DB, saas_manager and webhook all agree.
TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ENTERPRISE = "enterprise"
VALID_TIERS = (TIER_FREE, TIER_PRO, TIER_ENTERPRISE)

_lock = asyncio.Lock()
_async_engine: Optional[AsyncEngine] = None
_AsyncSessionFactory: Optional[sessionmaker] = None
_memory: Dict[str, Dict[str, Dict[str, Any]]] = {"users": {}, "daily": {}}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def today_str() -> str:
    return _utcnow().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# SQLAlchemy models
# ---------------------------------------------------------------------------
try:
    from sqlalchemy import (  # type: ignore
        Boolean,
        Column,
        DateTime,
        Integer,
        String,
        Text,
        UniqueConstraint,
        select,
        create_engine,
        inspect,
        text as _sql_text,
    )
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
    from sqlalchemy.orm import declarative_base, sessionmaker  # type: ignore

    _HAVE_SQLALCHEMY = True
except ImportError:  # pragma: no cover - exercised only on un-provisioned hosts
    _HAVE_SQLALCHEMY = False
    logger.warning(
        "SQLAlchemy not installed - SaaS tiers and usage tracking will use an "
        "in-memory store and reset on restart. Install with: "
        "pip install SQLAlchemy"
    )


if _HAVE_SQLALCHEMY:
    Base = declarative_base()

    class User(Base):
        """One row per Telegram user who has interacted with the bot."""

        __tablename__ = "users"

        id = Column(Integer, primary_key=True, autoincrement=True)
        telegram_id = Column(String(64), unique=True, nullable=False, index=True)
        username = Column(String(128), default="")
        tier = Column(String(32), default=TIER_FREE, nullable=False)
        api_key = Column(String(128), default="", nullable=False)
        # Voice / translation preferences. Added after the first release, so
        # existing databases get them via _migrate_columns() below.
        voice_on = Column(Boolean, default=False, nullable=False)
        voice_lang = Column(String(16), default="en-US", nullable=False)
        voice_gender = Column(String(16), default="female", nullable=False)
        voice_name = Column(String(64), default="")
        translate_lang = Column(String(16), default="", nullable=False)
        created_at = Column(DateTime, default=_utcnow)
        last_seen = Column(DateTime, default=_utcnow)
        daily_api_calls = Column(Integer, default=0, nullable=False)
        last_reset = Column(DateTime, nullable=True)

    class Subscription(Base):
        """Billing history. `expires_at` is NULL for a lifetime/manual grant."""

        __tablename__ = "subscriptions"

        id = Column(Integer, primary_key=True, autoincrement=True)
        telegram_id = Column(String(64), nullable=False, index=True)
        plan = Column(String(32), nullable=False)
        provider = Column(String(32), default="manual")
        reference = Column(String(128), default="")
        created_at = Column(DateTime, default=_utcnow)
        expires_at = Column(DateTime, nullable=True)
        tier = Column(String(32), nullable=True)

    class DailyUsage(Base):
        """Per-user, per-day counters backing the tier limits."""

        __tablename__ = "daily_usage"
        __table_args__ = (UniqueConstraint("telegram_id", "day", name="uq_usage_user_day"),)

        id = Column(Integer, primary_key=True, autoincrement=True)
        telegram_id = Column(String(64), nullable=False, index=True)
        day = Column(String(10), nullable=False)
        daily_scans = Column(Integer, default=0)
        daily_ai_calls = Column(Integer, default=0)
        daily_media = Column(Integer, default=0)


# ---------------------------------------------------------------------------
# Engine management
# ---------------------------------------------------------------------------
def _async_url(url: str) -> str:
    """Point a sync SQLAlchemy URL at its async driver.

    create_async_engine() rejects a bare `sqlite://` or `postgresql://` URL
    with "The asyncio extension requires an async driver to be used", and
    init_db() swallows that into its in-memory fallback. `DATABASE_URL=
    sqlite:///raven.db` is the most natural thing to write, so accept it
    rather than silently persisting nothing.
    """
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + url[len("sqlite://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def init_db() -> bool:
    """Create tables if needed. Returns True when a real DB is available."""
    global _async_engine, _AsyncSessionFactory
    if not _HAVE_SQLALCHEMY:
        return False
    if _AsyncSessionFactory is not None:
        return True
    async with _lock:
        if _AsyncSessionFactory is not None:
            return True
        try:
            url = _async_url(DATABASE_URL or f"sqlite+aiosqlite:///{DEFAULT_SQLITE}")
            kwargs = {"echo": False}
            # SQLite is only pooled when it is backed by a file. For an
            # in-memory URL SQLAlchemy selects StaticPool (aiosqlite) or
            # SingletonThreadPool (pysqlite), and neither accepts pool_size or
            # max_overflow - it raises TypeError. Passing them anyway made
            # init_db() fall back to the in-memory dict store, so a
            # DATABASE_URL of sqlite+aiosqlite:///:memory: silently lost all
            # subscription and usage data on every restart.
            in_memory = (":memory:" in url or "mode=memory" in url)
            if url.startswith("sqlite") and not in_memory:
                kwargs["pool_pre_ping"] = True   # connection health check
                kwargs["pool_recycle"] = 300     # recycle every 5 minutes
                kwargs["pool_size"] = 5
                kwargs["max_overflow"] = 10
            else:
                # Non-SQLite backends (Postgres) pool fine; SQLite memory
                # pools are fixed by the dialect.
                kwargs["pool_pre_ping"] = True
            _async_engine = create_async_engine(url, **kwargs)
            async with _async_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await _run_migrate_columns(_async_engine)
            _AsyncSessionFactory = sessionmaker(
                bind=_async_engine, class_=AsyncSession, expire_on_commit=False
            )
            logger.info("SaaS database ready (%s)", url.split("://")[0])
            return True
        except Exception:
            logger.exception("Database init failed - falling back to in-memory store")
            return False


async def db_available() -> bool:
    return await init_db()


# Columns added after the first release. `create_all` never alters an existing
# table, so an already-created raven_saas.db would raise OperationalError on
# the first SELECT of a new column. Adding them explicitly keeps upgrades
# non-destructive: existing rows keep their tier and history.
_ADDED_COLUMNS = (
    ("users", "voice_on", "BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "voice_lang", "VARCHAR(16) NOT NULL DEFAULT 'en-US'"),
    ("users", "voice_gender", "VARCHAR(16) NOT NULL DEFAULT 'female'"),
    ("users", "voice_name", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("users", "translate_lang", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("users", "daily_api_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_reset", "TIMESTAMP NULL"),
    ("subscriptions", "tier", "VARCHAR(32) NULL"),
)


async def _run_migrate_columns(async_engine: AsyncEngine) -> None:
    """Add any missing columns. Never drops or rewrites data."""
    try:
        # Create a synchronous engine for inspection and migration
        url = str(async_engine.url).replace("sqlite+aiosqlite://", "sqlite://").replace("postgresql+asyncpg://", "postgresql://")
        sync_engine = create_engine(url)
        insp = inspect(sync_engine)
        tables = set(insp.get_table_names())
        with sync_engine.begin() as conn:
            for table, column, ddl in _ADDED_COLUMNS:
                if table not in tables:
                    continue
                existing = {c["name"] for c in insp.get_columns(table)}
                if column in existing:
                    continue
                conn.execute(_sql_text(
                    "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl)))
                logger.info("Migrated: added %s.%s", table, column)
    except Exception:
        # A failed migration must not stop the bot; the column read below is
        # guarded, and the next start retries.
        logger.warning("column migration skipped", exc_info=True)


# ---------------------------------------------------------------------------
# Synchronous core (always called from a worker thread via the wrappers below)
# ---------------------------------------------------------------------------
async def _get_user(telegram_id: str, username: str = "") -> Dict[str, Any]:
    if not await init_db():
        async with _lock:
            rec = _memory["users"].setdefault(
                str(telegram_id),
                {
                    "telegram_id": str(telegram_id),
                    "username": username,
                    "tier": TIER_FREE,
                    "api_key": "",
                    "voice_on": False,
                    "voice_lang": "en-US",
                    "voice_gender": "female",
                    "voice_name": "",
                    "translate_lang": "",
                    "created_at": _utcnow().isoformat(),
                    "last_seen": _utcnow().isoformat(),
                },
            )
            if username:
                rec["username"] = username
            rec["last_seen"] = _utcnow().isoformat()
            return dict(rec)

    # Use the database
    session = _AsyncSessionFactory()
    try:
        # Check if user exists
        result = await session.execute(select(User).where(User.telegram_id == str(telegram_id)))
        row = result.scalar_one_or_none()
        if row is None:
            row = User(telegram_id=str(telegram_id), username=username or "",
                       tier=TIER_FREE, api_key="", created_at=_utcnow(), last_seen=_utcnow())
            session.add(row)
        else:
            row.last_seen = _utcnow()
            if username:
                row.username = username
        await session.commit()
        return {
            "telegram_id": row.telegram_id,
            "username": row.username or "",
            "tier": row.tier or TIER_FREE,
            "has_api_key": bool(row.api_key),
            "voice_on": bool(row.voice_on),
            "voice_lang": row.voice_lang or "en-US",
            "voice_gender": row.voice_gender or "female",
            "voice_name": row.voice_name or "",
            "translate_lang": row.translate_lang or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        }
    finally:
        await session.close()


# Preference columns that users may set. Whitelisted so no caller can use this
# to write `tier` or `api_key` by passing a field name through.
_PREF_COLUMNS = ("voice_on", "voice_lang", "voice_gender", "voice_name",
                 "translate_lang")


async def _set_prefs(telegram_id: str, prefs: Dict[str, Any]) -> bool:
    """Update voice/translation preferences. Unknown keys are ignored."""
    if not await init_db():
        async with _lock:
            rec = _memory["users"].setdefault(
                str(telegram_id),
                {"telegram_id": str(telegram_id), "username": "",
                 "tier": TIER_FREE, "api_key": "", "voice_on": False,
                 "voice_lang": "en-US", "voice_gender": "female",
                 "voice_name": "", "translate_lang": "",
                 "created_at": _utcnow().isoformat(),
                 "last_seen": _utcnow().isoformat()},
            )
            rec.update({k: v for k, v in prefs.items() if k in _PREF_COLUMNS})
            return True

    clean = {k: v for k, v in prefs.items() if k in _PREF_COLUMNS}
    if not clean:
        return False
    session = _AsyncSessionFactory()
    try:
        result = await session.execute(select(User).where(User.telegram_id == str(telegram_id)))
        row = result.scalar_one_or_none()
        if row is None:
            row = User(telegram_id=str(telegram_id), username="",
                       tier=TIER_FREE, api_key="", created_at=_utcnow(),
                       last_seen=_utcnow())
            session.add(row)
        for key, value in clean.items():
            setattr(row, key, value)
        await session.commit()
        return True
    except Exception:
        logger.warning("preference write failed for %s", telegram_id,
                       exc_info=True)
        return False
    finally:
        await session.close()


async def _get_prefs(telegram_id: str) -> Dict[str, Any]:
    """Preferences only, without touching tier or the username."""
    user = await _get_user(telegram_id)
    return {k: user.get(k) for k in _PREF_COLUMNS}


async def _set_tier(telegram_id: str, tier: str, plan: Optional[str] = None,
              provider: str = "manual", reference: str = "") -> bool:
    tier = (tier or TIER_FREE).lower()
    if tier not in VALID_TIERS:
        logger.warning("Refusing unknown tier %r", tier)
        return False
    await _get_user(telegram_id)  # ensure the user row exists
    if not await init_db():
        async with _lock:
            _memory["users"][str(telegram_id)]["tier"] = tier
        return True
    session = _AsyncSessionFactory()
    try:
        result = await session.execute(select(User).where(User.telegram_id == str(telegram_id)))
        row = result.scalar_one()
        row.tier = tier
        session.add(
            Subscription(
                telegram_id=str(telegram_id),
                plan=plan or tier,
                provider=provider,
                reference=reference,
                created_at=_utcnow(),
                expires_at=None,
                tier=tier,  # Set the tier on the subscription
            )
        )
        await session.commit()
        return True
    except Exception:
        logger.warning("failed to set tier for %s", telegram_id, exc_info=True)
        return False
    finally:
        await session.close()


async def _bump(telegram_id: str, field: str, day: Optional[str] = None) -> int:
    """Increment one daily counter and return its new value."""
    day = day or today_str()
    # Map the field name to the column name in DailyUsage
    field_map = {
        "daily_scans": "daily_scans",
        "daily_ai_calls": "daily_ai_calls",
        "daily_media": "daily_media",
        "daily_api_calls": "daily_ai_calls",  # map to the existing column
    }
    if field not in field_map:
        raise ValueError(f"unknown counter {field!r}")
    column_name = field_map[field]
    if not await init_db():
        async with _lock:
            bucket = _memory["daily"].setdefault(f"{telegram_id}|{day}",
                                                 {"daily_scans": 0, "daily_ai_calls": 0, "daily_media": 0})
            bucket[column_name] += 1
            return bucket[column_name]
    session = _AsyncSessionFactory()
    try:
        result = await session.execute(
            select(DailyUsage)
            .where(DailyUsage.telegram_id == str(telegram_id), DailyUsage.day == day)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DailyUsage(telegram_id=str(telegram_id), day=day, daily_scans=0,
                             daily_ai_calls=0, daily_media=0)
            session.add(row)
        # Increment the column
        current_value = getattr(row, column_name)
        setattr(row, column_name, (current_value or 0) + 1)
        await session.commit()
        return getattr(row, column_name)
    finally:
        await session.close()


async def _usage_for(telegram_id: str, day: Optional[str] = None) -> Dict[str, int]:
    day = day or today_str()
    if not await init_db():
        async with _lock:
            b = _memory["daily"].get(f"{telegram_id}|{day}",
                                     {"daily_scans": 0, "daily_ai_calls": 0, "daily_media": 0})
            # Return daily_api_calls instead of daily_ai_calls for consistency
            return {"daily_scans": b["daily_scans"],
                    "daily_api_calls": b["daily_ai_calls"],
                    "daily_media": b["daily_media"]}
    session = _AsyncSessionFactory()
    try:
        result = await session.execute(
            select(DailyUsage)
            .where(DailyUsage.telegram_id == str(telegram_id), DailyUsage.day == day)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return {"daily_scans": 0, "daily_api_calls": 0, "daily_media": 0}
        return {"daily_scans": row.daily_scans or 0,
                "daily_api_calls": row.daily_ai_calls or 0,
                "daily_media": row.daily_media or 0}
    finally:
        await session.close()


async def _stats() -> Dict[str, Any]:
    """Platform-wide counters for the admin dashboard.

    Performance optimized to avoid loading entire user list when not necessary.
    """
    if not await init_db():
        async with _lock:
            users = list(_memory["users"].values())
            counts: Dict[str, int] = {}
            for u in users:
                counts[u.get("tier", TIER_FREE)] = counts.get(u.get("tier", TIER_FREE), 0) + 1
            return {"total_users": len(users), "by_tier": counts, "storage": "memory"}
    session = _AsyncSessionFactory()
    try:
        # Performance optimization: Use count() instead of loading all records
        from sqlalchemy import func
        user_count_result = await session.execute(select(func.count(User.id)))
        total_users = user_count_result.scalar()

        # Get tier distribution efficiently
        tier_count_result = await session.execute(
            select(User.tier, func.count(User.id))
            .group_by(User.tier)
        )
        counts: Dict[str, int] = {}
        for tier, count in tier_count_result.all():
            t = tier or TIER_FREE
            counts[t] = count

        # Get subscription count
        sub_count_result = await session.execute(select(func.count(Subscription.id)))
        total_subs = sub_count_result.scalar()

        return {
            "total_users": total_users,
            "by_tier": counts,
            "subscriptions": total_subs,
            "storage": DATABASE_URL.split("://")[0] if DATABASE_URL else "sqlite",
        }
    finally:
        await session.close()


async def _history(telegram_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Recent subscription/plan events for one user."""
    if not await init_db():
        return []
    session = _AsyncSessionFactory()
    try:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.telegram_id == str(telegram_id))
            .order_by(Subscription.id.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
        return [{"plan": r.plan, "provider": r.provider, "reference": r.reference,
                 "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in rows]
    finally:
        await session.close()


async def _plan_counts(limit: int = 1000) -> Dict[str, int]:
    """How many times each plan has been purchased. Powers the admin dashboard.

    A plan tally, not a revenue figure: there is no price table in this
    repository, so any currency amount would be invented.
    """
    if not await init_db():
        async with _lock:
            return {}
    session = _AsyncSessionFactory()
    try:
        # Performance optimization: Only fetch what we need and use efficient aggregation
        from sqlalchemy import func
        result = await session.execute(
            select(Subscription.plan, func.count(Subscription.id))
            .group_by(Subscription.plan)
            .order_by(func.count(Subscription.id).desc())
            .limit(limit)
        )
        rows = result.all()
        counts: Dict[str, int] = {}
        for plan, count in rows:
            key = plan or "unknown"
            counts[key] = count
        return counts
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Async wrappers - use these from Telegram handlers
# ---------------------------------------------------------------------------
async def aget_user(telegram_id: str, username: str = "") -> Dict[str, Any]:
    return await _get_user(telegram_id, username)


async def aget_tier(telegram_id: str) -> str:
    user = await aget_user(telegram_id)
    return user.get("tier", TIER_FREE)


async def aset_tier(telegram_id: str, tier: str, plan: Optional[str] = None,
                    provider: str = "manual", reference: str = "") -> bool:
    return await _set_tier(telegram_id, tier, plan, provider, reference)


async def abump(telegram_id: str, field: str) -> int:
    return await _bump(telegram_id, field)


async def ausage(telegram_id: str) -> Dict[str, int]:
    return await _usage_for(telegram_id)


async def astats() -> Dict[str, Any]:
    return await _stats()


async def ahistory(telegram_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    return await _history(telegram_id, limit)


async def aplan_counts(limit: int = 1000) -> Dict[str, int]:
    return await _plan_counts(limit)


async def aset_prefs(telegram_id: str, **prefs: Any) -> bool:
    """Persist voice/translation preferences off the event loop."""
    return await _set_prefs(telegram_id, prefs)


async def aget_prefs(telegram_id: str) -> Dict[str, Any]:
    return await _get_prefs(telegram_id)


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import json
    import asyncio

    async def main():
        print("SQLAlchemy available:", _HAVE_SQLALCHEMY)
        print("init_db:", await init_db())
        print("user:", json.dumps(await _get_user("123", "tester"), indent=2))
        print("scan count:", await _bump("123", "daily_scans"))
        print("usage:", await _usage_for("123"))
        print("tier set:", await _set_tier("123", TIER_PRO, plan="pro_monthly", provider="dodo"))
        print("tier now:", (await _get_user("123"))["tier"])
        print("stats:", await _stats())
        print("history:", await _history("123"))

    asyncio.run(main())