"""
RAVEN Trading Engine
====================
Paper-trading engine by default (TRADING_MODE=paper), with optional live
trading when exchange API keys are provided AND TRADING_MODE=live is set
explicitly. Live keys must have TRADE permission ONLY (never withdrawal).

Safety features:
    - Position size cap        (MAX_POSITION_USDT)
    - Daily loss limit         (DAILY_LOSS_LIMIT_PCT)
    - Kill-switch integration  (checked by the bot before every trade)
    - Full trade log           (raven_trades.json)

Amounts are NOTIONAL in USDT:  /buy BTC 100  ==  buy $100 worth of BTC.
"""

import os
import json
import time
import logging
import datetime

logger = logging.getLogger("raven.trading")

try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    ccxt = None

PORTFOLIO_FILE = "raven_portfolio.json"
TRADES_FILE = "raven_trades.json"


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        return True
    except Exception as exc:
        logger.error("Failed to save %s: %s", path, exc)
        return False


class TradingEngine:
    def __init__(self):
        self.mode = os.getenv("TRADING_MODE", "paper").strip().lower()
        if self.mode not in ("paper", "live"):
            self.mode = "paper"
        self.max_position_usdt = float(os.getenv("MAX_POSITION_USDT", "500"))
        self.daily_loss_limit_pct = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "5"))
        self._exchange = None
        self._live = None
        self._load_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _load_state(self):
        state = _load_json(
            PORTFOLIO_FILE,
            {"cash": float(os.getenv("RAVEN_PAPER_CASH", "10000")), "positions": {}},
        )
        self.cash = float(state.get("cash", 10000))
        self.positions = state.get("positions", {})
        self.trades = _load_json(TRADES_FILE, [])

    def _save_state(self):
        _save_json(PORTFOLIO_FILE, {"cash": self.cash, "positions": self.positions})
        _save_json(TRADES_FILE, self.trades[-500:])  # keep last 500 trades

    # ------------------------------------------------------------------
    # Market data (public, no keys needed)
    # ------------------------------------------------------------------
    def _public_exchange(self):
        if not CCXT_AVAILABLE:
            raise RuntimeError("ccxt not installed - run: pip install ccxt")
        if self._exchange is None:
            self._exchange = ccxt.binance({"enableRateLimit": True})
        return self._exchange

    def get_price(self, symbol: str):
        """Return the last price for a symbol like 'BTC/USDT'."""
        symbol = self.normalize_symbol(symbol)
        ticker = self._public_exchange().fetch_ticker(symbol)
        return float(ticker["last"])

    def normalize_symbol(self, symbol: str) -> str:
        symbol = symbol.upper().replace("-", "/").replace("_", "/").strip()
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
        return symbol

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    def portfolio_snapshot(self):
        """Return {'cash', 'positions': [...], 'equity', 'unrealized_pnl'}."""
        rows = []
        unrealized = 0.0
        for symbol, pos in self.positions.items():
            try:
                price = self.get_price(symbol)
            except Exception:
                price = float(pos.get("avg_price", 0))
            qty = float(pos.get("qty", 0))
            avg = float(pos.get("avg_price", 0))
            value = qty * price
            pnl = (price - avg) * qty
            unrealized += pnl
            rows.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "avg_price": avg,
                    "price": price,
                    "value": value,
                    "pnl": pnl,
                    "pnl_pct": ((price - avg) / avg * 100) if avg else 0.0,
                }
            )
        positions_value = sum(r["value"] for r in rows)
        return {
            "mode": self.mode,
            "cash": self.cash,
            "positions": rows,
            "positions_value": positions_value,
            "equity": self.cash + positions_value,
            "unrealized_pnl": unrealized,
        }

    # ------------------------------------------------------------------
    # Risk checks
    # ------------------------------------------------------------------
    def _today_realized_pnl(self):
        today = datetime.date.today().isoformat()
        return sum(
            t.get("pnl", 0)
            for t in self.trades
            if t.get("date", "").startswith(today) and t.get("side") == "sell"
        )

    def check_risk(self, side: str, symbol: str, amount_usdt: float):
        """Return (ok, reason)."""
        if amount_usdt <= 0:
            return False, "Amount must be positive."
        if amount_usdt > self.max_position_usdt:
            return False, (
                f"Amount ${amount_usdt:,.2f} exceeds max position size "
                f"${self.max_position_usdt:,.2f} (MAX_POSITION_USDT)."
            )
        snapshot = self.portfolio_snapshot()
        if side == "buy" and amount_usdt > snapshot["cash"]:
            return False, f"Insufficient cash (${snapshot['cash']:,.2f} available)."
        if side == "sell":
            pos = self.positions.get(symbol)
            if not pos or float(pos.get("qty", 0)) <= 0:
                return False, f"No open position in {symbol}."
            pos_value = float(pos.get("qty", 0)) * self.get_price(symbol)
            if amount_usdt > pos_value:
                return False, (
                    f"Sell amount ${amount_usdt:,.2f} exceeds position value "
                    f"${pos_value:,.2f} in {symbol}."
                )

        # Daily loss circuit breaker
        day_start_equity = snapshot["equity"] - self._today_realized_pnl() - snapshot["unrealized_pnl"]
        loss = -(self._today_realized_pnl() + snapshot["unrealized_pnl"])
        if day_start_equity > 0 and loss / day_start_equity * 100 >= self.daily_loss_limit_pct:
            return False, (
                f"Daily loss limit hit ({loss:,.2f} USD >= "
                f"{self.daily_loss_limit_pct}% of day-start equity). Trading halted until tomorrow."
            )
        return True, "OK"

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self, side: str, symbol: str, amount_usdt: float, user_id: str):
        """
        Execute a market order.
        Paper mode : simulated fill at live market price.
        Live mode  : real ccxt market order (requires keys + TRADING_MODE=live).
        Returns a receipt dict.
        """
        symbol = self.normalize_symbol(symbol)
        price = self.get_price(symbol)
        qty = amount_usdt / price
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self.mode == "paper":
            if side == "buy":
                self.cash -= amount_usdt
                pos = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
                total_qty = float(pos["qty"]) + qty
                pos["avg_price"] = (
                    (float(pos["avg_price"]) * float(pos["qty"]) + price * qty) / total_qty
                    if total_qty
                    else price
                )
                pos["qty"] = total_qty
                self.positions[symbol] = pos
                pnl = 0.0
            else:  # sell
                pos = self.positions.get(symbol)
                sold_qty = min(qty, float(pos["qty"]))
                pnl = (price - float(pos["avg_price"])) * sold_qty
                self.cash += sold_qty * price
                pos["qty"] = float(pos["qty"]) - sold_qty
                if pos["qty"] <= 1e-12:
                    self.positions.pop(symbol, None)
                else:
                    self.positions[symbol] = pos
        else:
            result = self._live_order(side, symbol, amount_usdt)
            pnl = 0.0
            qty = result.get("filled_qty", qty)

        trade = {
            "ts": ts,
            "date": datetime.date.today().isoformat(),
            "mode": self.mode,
            "side": side,
            "symbol": symbol,
            "amount_usdt": amount_usdt,
            "price": price,
            "qty": qty,
            "pnl": pnl,
            "user_id": str(user_id),
        }
        self.trades.append(trade)
        self._save_state()
        logger.info("TRADE %s %s %s %.2f USDT @ %.2f", self.mode, side, symbol, amount_usdt, price)
        return trade

    def _live_exchange(self):
        if self._live is None:
            key = os.getenv("BINANCE_API_KEY", "").strip()
            secret = os.getenv("BINANCE_API_SECRET", "").strip()
            if not key or not secret or key.startswith("your_"):
                raise RuntimeError(
                    "Live trading requires BINANCE_API_KEY / BINANCE_API_SECRET in .env"
                )
            if not CCXT_AVAILABLE:
                raise RuntimeError("ccxt not installed - run: pip install ccxt")
            self._live = ccxt.binance({"apiKey": key, "secret": secret, "enableRateLimit": True})
        return self._live

    def _live_order(self, side, symbol, amount_usdt):
        exchange = self._live_exchange()
        order = exchange.create_order(symbol, "market", side, None, params={"quoteOrderQty": amount_usdt})
        return {
            "id": order.get("id"),
            "filled_qty": float(order.get("filled") or 0),
            "status": order.get("status"),
        }

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def trades_report(self, days: int = 1):
        """Return trades executed within the last `days` days."""
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        return [t for t in self.trades if t.get("date", "") >= cutoff]
