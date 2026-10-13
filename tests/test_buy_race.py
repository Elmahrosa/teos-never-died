import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

import telegram_bot as tb
from telegram_bot import TRADING


@pytest.fixture(autouse=True)
def reset_trading_state():
    TRADING.cash = 100.0
    TRADING.positions = {}
    TRADING.trades = []
    TRADING.mode = "paper"
    yield


def test_buy_race_protection():
    """5 concurrent buys against 100 USDT: exactly one wins."""
    symbol = "BTC/USDT"
    amount_usdt = "100.0"

    sent = []

    async def fake_send_long(update, text, parse_mode=None):
        sent.append(text)
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        return msg

    def make_update_context(uid):
        update = AsyncMock()
        update.effective_user.id = uid
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = AsyncMock()
        context.args = [symbol, amount_usdt]
        return update, context

    async def run_all():
        tasks = []
        for i in range(5):
            update, context = make_update_context(1000 + i)
            tasks.append(tb.buy_command(update, context))
        return await asyncio.gather(*tasks, return_exceptions=True)

    with patch.dict(os.environ, {"ADMIN_USER_ID": "1000"}), \
         patch.object(TRADING, "get_price", return_value=1.0), \
         patch("telegram_bot.send_long", side_effect=fake_send_long):
        asyncio.run(run_all())

    successes = [m for m in sent if m.startswith("✅")]
    assert len(successes) == 1, f"expected 1 success, got {len(successes)}"
    assert TRADING.cash == 0.0, f"expected cash 0.0, got {TRADING.cash}"
    assert TRADING.cash >= 0