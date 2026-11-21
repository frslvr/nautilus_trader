# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
The snapshot PnL cache of one instrument must survive updates of another instrument.
"""

import pickle

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events.account import AccountState
from nautilus_trader.model.identifiers import PositionId
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs


AUDUSD_SIM = TestInstrumentProvider.default_fx_ccy("AUD/USD")
GBPUSD_SIM = TestInstrumentProvider.default_fx_ccy("GBP/USD")


def _snapshot_closed_cycle(cache, instrument, position_id, entry, exit_):
    order = TestExecStubs.market_order(
        instrument=instrument,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_int(100_000),
    )
    fill = TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        position_id=position_id,
        last_px=Price.from_str(entry),
    )
    position = Position(instrument=instrument, fill=fill)
    close = TestExecStubs.market_order(
        instrument=instrument,
        order_side=OrderSide.SELL,
        quantity=Quantity.from_int(100_000),
    )
    position.apply(
        TestEventStubs.order_filled(
            order=close,
            instrument=instrument,
            position_id=position_id,
            last_px=Price.from_str(exit_),
        ),
    )
    cache.snapshot_position(position)


def _count_loads(monkeypatch):
    calls = []
    real_loads = pickle.loads

    def counting_loads(data, *args, **kwargs):
        calls.append(1)
        return real_loads(data, *args, **kwargs)

    monkeypatch.setattr(pickle, "loads", counting_loads)
    return calls


def test_other_instrument_update_keeps_snapshot_counts(
    portfolio,
    cache,
    exec_engine,
    clock,
    account_id,
    monkeypatch,
):
    # Arrange
    exec_engine.start()
    account_state = AccountState(
        account_id=account_id,
        account_type=AccountType.CASH,
        base_currency=USD,
        reported=True,
        balances=[AccountBalance(Money(1_000_000, USD), Money(0, USD), Money(1_000_000, USD))],
        margins=[],
        info={},
        event_id=UUID4(),
        ts_event=clock.timestamp_ns(),
        ts_init=clock.timestamp_ns(),
    )
    cache.add_account(TestExecStubs.cash_account())
    portfolio.update_account(account_state)
    cache.add_instrument(GBPUSD_SIM)

    gbp_id = PositionId("GBP-1")
    for _ in range(5):
        _snapshot_closed_cycle(cache, GBPUSD_SIM, gbp_id, "1.20000", "1.20020")
    _snapshot_closed_cycle(cache, AUDUSD_SIM, PositionId("AUD-1"), "0.70000", "0.70010")

    gbp_before = portfolio.realized_pnl(GBPUSD_SIM.id)
    portfolio.realized_pnl(AUDUSD_SIM.id)

    # Act: a new AUD cycle, then a new GBP cycle
    _snapshot_closed_cycle(cache, AUDUSD_SIM, PositionId("AUD-1"), "0.70010", "0.70020")
    portfolio.realized_pnl(AUDUSD_SIM.id)
    _snapshot_closed_cycle(cache, GBPUSD_SIM, gbp_id, "1.20000", "1.20020")
    calls = _count_loads(monkeypatch)
    gbp_after = portfolio.realized_pnl(GBPUSD_SIM.id)

    # Assert: only the one new GBP snapshot is read, not its five old ones again
    assert len(calls) == 1, len(calls)
    assert gbp_after.as_decimal() > gbp_before.as_decimal()
