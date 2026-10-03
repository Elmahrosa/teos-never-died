"""
RAVEN Analytics
===============
P&L reporting, trading statistics and system health summaries.
"""

import os
import json
import datetime
import logging

logger = logging.getLogger("raven.analytics")


def _load_trades():
    try:
        with open("raven_trades.json", "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return []


def _load_portfolio():
    try:
        with open("raven_portfolio.json", "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {"cash": float(os.getenv("RAVEN_PAPER_CASH", "10000")), "positions": {}}


def trading_report(period: str = "daily", engine=None) -> str:
    """Return a formatted P&L report (daily / weekly / monthly)."""
    days = {"daily": 1, "weekly": 7, "monthly": 30}.get(period, 1)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    trades = [t for t in _load_trades() if t.get("date", "") >= cutoff]

    if not trades:
        return f"📊 No trades in the last {days} day(s)."

    buys = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell"]
    realized = sum(t.get("pnl", 0) for t in sells)
    volume = sum(t.get("amount_usdt", 0) for t in trades)
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0.0

    line = "=" * 34
    report = (
        f"{line}\n"
        f" RAVEN P&L REPORT ({period.upper()})\n"
        f"{line}\n"
        f" Trades        {len(trades)} ({len(buys)} buys / {len(sells)} sells)\n"
        f" Volume        ${volume:,.2f}\n"
        f" Realized P&L  ${realized:,.2f}\n"
        f" Win rate      {win_rate:.1f}%\n"
        f" Period        last {days} day(s)\n"
        f"{line}"
    )
    return f"```\n{report}\n```"


def system_stats(uptime_seconds: float, router=None, engine=None) -> str:
    """Return a compact system status block."""
    uptime = str(datetime.timedelta(seconds=int(uptime_seconds)))
    providers = ""
    if router is not None:
        for name, ready in router.providers_status().items():
            providers += f"{'🟢' if ready else '🔴'} {name}  "
    mode = engine.mode.upper() if engine else "N/A"
    portfolio = _load_portfolio()
    return (
        f"```\n┌─ RAVEN SYSTEM ────────────────\n"
        f"│ Uptime      {uptime}\n"
        f"│ AI engine   {providers or 'none'}\n"
        f"│ Trading     {mode}\n"
        f"│ Cash        ${float(portfolio.get('cash', 0)):,.2f}\n"
        f"│ Positions   {len(portfolio.get('positions', {}))}\n"
        f"└───────────────────────────────\n```"
    )
