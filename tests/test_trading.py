"""Trade mode and ibguard: limits, sizes and price rules, sending through a fake OrderPort, order states, recovery,
cancelling, the sweeps, the sell protocol and the threshold preview and write (SPEC.md §15, §16, AC-26, AC-27).

IB is a fake OrderPort and AccountPort, ibguard an aiohttp test server on 127.0.0.1; nothing reaches IB.
"""

import asyncio
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from btlab import ib_orders
from btlab.config import Limits, ScheduleConfig, ServiceConfig, load_config
from btlab.ibguard_client import GuardState, GuardedPosition, connected, preview, thresholds_error
from btlab.jobs import SWEEP_KEY, Jobs, KeepAwake, LiveError
from btlab.live import apply_limits, rule_price
from btlab.records import Contract, Line, to_day
from btlab.store import MinuteStore, Store
from btlab.trading import OrderReport, Trader, TradeError
from btlab.web import Services, make_app
from builders import TAXES, synthetic_store
from fakes import FakeAccount, FakeIbguard, FakeOrderPort, InlinePool
from test_config import VALID, edited, render
from test_jobs import KEYS, Notes

REPO = Path(__file__).resolve().parents[1]
TOKEN = VALID["web"]["token"]
GUARD_TOKEN = VALID["ibguard"]["token"]
SESSION = to_day(date(2021, 1, 1))
NOW = datetime(2020, 12, 31, 23, 0, tzinfo=timezone.utc)
LIMITS = Limits(2, 2, 50.0, 20_000.0, 1_000_000.0)
AAA = Contract(101, "SMART", "USD", 1.0, 1.0, "26", NOW)
BBB = Contract(102, "SMART", "USD", 1.0, 1.0, "26", NOW)
CENTS = {26: [(0.0, 0.01)]}


def proposed(symbol: str, action: str, quantity: float, ref: float, kind: str = "MOO", limit: float | None = None) -> Line:
    return Line(symbol, action, quantity, kind, limit, ref, "PROPOSED", con_id={"AAA": 101, "BBB": 102}.get(symbol))


# ----- limits, sizes and price rules (§15.2, §15.3) -----


def test_limits_block_values_shares_of_equity_and_keep_the_largest_lines():
    lines = [proposed("AAA", "BUY", 100, 100.0), proposed("BBB", "BUY", 300, 100.0), proposed("CCC", "SELL", 50, 100.0),
             proposed("DDD", "BUY", 120, 100.0), proposed("EEE", "BUY", 210, 100.0)]
    found = apply_limits(lines, 40_000.0, LIMITS, "US")
    # BBB's 30,000 passes the value limit of 20,000? No; EEE's 21,000 neither; the rest fit 50% of 40,000.
    assert [(x.symbol, x.status) for x in found] == [
        ("AAA", "PROPOSED"), ("BBB", "BLOCKED_LIMIT"), ("CCC", "BLOCKED_LIMIT"), ("DDD", "PROPOSED"), ("EEE", "BLOCKED_LIMIT")]
    assert "above the limit" in found[1].reason and "more than 2 orders" in found[2].reason
    assert "of the equity" in apply_limits([proposed("AAA", "BUY", 150, 100.0)], 20_000.0, LIMITS, "US")[0].reason


def test_limit_prices_round_on_the_market_rule_buys_down_sells_up():
    rule = [(0.0, 0.01), (10.0, 0.05), (50.0, 0.1)]
    assert [rule_price(101.237, rule, "BUY"), rule_price(101.231, rule, "SELL"), rule_price(9.991, rule, "SELL"),
            rule_price(12.34, rule, "BUY"), rule_price(12.34, rule, "SELL")] == [101.2, 101.3, 10.0, 12.3, 12.35]


@pytest.fixture
def market(tmp_path):
    store = synthetic_store(tmp_path)
    for symbol, contract in (("AAA", AAA), ("BBB", BBB)):
        store.save_contract("US", symbol, contract)
    yield store
    store.close()


def portfolio_of(store: Store) -> int:
    config = {"name": "trend", "symbols": ["AAA", "BBB"], "initial_capital": 100_000.0}
    return store.create_portfolio("US", "trade", None, config, 100_000.0, NOW, SESSION)


def trader_of(store: Store, orders: FakeOrderPort, account: FakeAccount, notes: Notes | None = None, guard=None,
              limits: Limits = LIMITS) -> Trader:
    return Trader(store, orders, account, limits, ibguard=guard, notifier=notes or Notes(), clock=lambda: NOW.timestamp())


def test_trade_lines_from_ibs_positions_with_contracts_sizes_rules_and_limits(market):
    store = market
    portfolio = store.portfolio(portfolio_of(store))
    store.save_contract("US", "BBB", Contract(102, "SMART", "USD", 500.0, 1.0, "26", NOW))
    account = FakeAccount({101: 10.0}, increments=CENTS)
    trader = trader_of(store, FakeOrderPort(), account)
    weights = {"AAA": 0.15, "BBB": 0.1, "CCC": 0.1}
    lines, current = asyncio.run(trader.lines(portfolio, weights, {"AAA": 50.0, "BBB": 40.0, "CCC": 20.0}, SESSION, "LIMIT", 1.0))
    assert current == {"AAA": 10.0} and account.connects == 1
    # Equity 100,000 + 10 × 50: AAA to 301 shares, BBB 251 below its minimum of 500, CCC without a contract.
    assert [(x.symbol, x.action, x.quantity, x.status, x.con_id) for x in lines] == [
        ("AAA", "BUY", 291.0, "PROPOSED", 101), ("BBB", "BUY", 251.0, "SKIPPED_MIN_SIZE", 102),
        ("CCC", "BUY", 502.0, "SKIPPED_NO_CONTRACT", None)]
    assert lines[0].limit_price == 50.5
    unruled = trader_of(store, FakeOrderPort(), FakeAccount({101: 10.0}))
    lines, _ = asyncio.run(unruled.lines(portfolio, {"AAA": 0.15}, {"AAA": 50.0}, SESSION, "LIMIT", 1.0))
    assert lines[0].status == "SKIPPED_NO_PRICE_RULE"


# ----- sending (§15.4, §15.5, §15.9) -----


def batch_of(store: Store, portfolio_id: int, lines: list[Line], seen: dict | None = None) -> int:
    batch_id = store.save_batch(portfolio_id, SESSION, NOW, seen or {}, lines)
    batch = store.batch(batch_id)
    store.confirm_batch(batch_id, {x.id for x in batch.lines}, "CONFIRMED", NOW)
    return batch_id


def sent(store: Store, trader: Trader, batch_id: int, portfolio_id: int) -> tuple[str, str]:
    return asyncio.run(trader.send(store.batch(batch_id), store.portfolio(portfolio_id)))


def working(ticket, order_id: int) -> OrderReport:
    return OrderReport(order_id, ticket.order_ref, ticket.con_id, "WORKING", 0.0, 0.0, 0.0)


def test_orders_carry_the_fields_of_section_15_4_and_reducing_lines_go_first(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("BBB", "BUY", 10, 40.0, "LIMIT", 40.4),
                                              proposed("AAA", "SELL", 5, 50.0)], {"AAA": 20.0})
    orders = FakeOrderPort(working)
    outcome = sent(store, trader_of(store, orders, FakeAccount({101: 20.0})), batch_id, portfolio_id)
    assert outcome == ("DONE", f"batch {batch_id}: sent 2, skipped 0")
    sell, buy = orders.placed
    assert (sell.symbol, sell.action, sell.order_type, sell.tif, sell.limit_price) == ("AAA", "SELL", "MKT", "OPG", None)
    assert (buy.symbol, buy.order_type, buy.tif, buy.limit_price) == ("BBB", "LMT", "DAY", 40.4)
    assert {(t.outside_rth, t.account, t.exchange, t.currency) for t in orders.placed} == {(False, "DU1234567", "SMART", "USD")}
    assert re.fullmatch(rf"btlab:{batch_id}:2:\d{{13}}", sell.order_ref) and sell.order_ref != buy.order_ref
    lines = store.batch(batch_id).lines
    assert [(x.status, x.ib_order_id, x.order_ref) for x in lines] == [
        ("WORKING", 2, buy.order_ref), ("WORKING", 1, sell.order_ref)]
    assert store.batch(batch_id).status == "SENT"


def test_the_send_function_refuses_and_the_limits_apply_again(market):
    store = market
    portfolio_id = portfolio_of(store)
    other = batch_of(store, portfolio_id, [proposed("BBB", "BUY", 1, 40.0)])
    store.update_order(store.batch(other).lines[0].id, NOW, status="WORKING")
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "SELL", 30, 50.0), proposed("BBB", "BUY", 2, 40.0),
                                              proposed("AAA", "BUY", 0, 50.0), proposed("AAA", "BUY", 600, 50.0)],
                        {"AAA": 20.0})
    orders = FakeOrderPort(working)
    roomy = Limits(5, 5, 50.0, 20_000.0, 1_000_000.0)
    sent(store, trader_of(store, orders, FakeAccount({101: 20.0}), limits=roomy), batch_id, portfolio_id)
    assert [(x.status, x.reason) for x in store.batch(batch_id).lines] == [
        ("REFUSED", "it would trade more than the position of 20"),
        ("REFUSED", "a btlab order already works on the contract"),
        ("REFUSED", "the quantity is not above 0"),
        ("BLOCKED_LIMIT", "value 30,000 above the limit 20,000")]
    assert orders.placed == []


def test_changed_positions_make_the_batch_stale_and_the_cutoff_ends_it(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch = store.batch(batch_of(store, portfolio_id, [proposed("AAA", "SELL", 5, 50.0)], {"AAA": 20.0}))
    trader = trader_of(store, FakeOrderPort(), FakeAccount({101: 15.0}))
    with pytest.raises(TradeError) as stale:
        trader.precheck(batch, "US")
    assert stale.value.code == "BATCH_STALE"
    offline = FakeAccount({101: 20.0})
    offline.connected = False
    with pytest.raises(TradeError) as unplugged:
        trader_of(store, FakeOrderPort(), offline).precheck(batch, "US")
    assert unplugged.value.code == "NOT_CONNECTED"
    late = Trader(store, FakeOrderPort(), FakeAccount({101: 20.0}), LIMITS, ibguard=None, notifier=Notes(),
                  clock=lambda: datetime(2021, 1, 1, 15, 0, tzinfo=timezone.utc).timestamp())
    with pytest.raises(TradeError) as expired:
        late.precheck(batch, "US")
    assert expired.value.code == "BATCH_EXPIRED"


def test_line_states_fills_and_the_batch_done_email(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "BUY", 10, 50.0), proposed("BBB", "BUY", 20, 40.0)])
    orders, notes = FakeOrderPort(working), Notes()
    sent(store, trader_of(store, orders, FakeAccount(), notes), batch_id, portfolio_id)
    first, second = orders.placed

    async def reports():
        orders.report(OrderReport(1, first.order_ref, 101, "FILLED", 10.0, 50.1, 1.0))
        orders.report(OrderReport(2, second.order_ref, 102, "CANCELLED", 5.0, 40.2, 1.0))
        orders.report(OrderReport(1, first.order_ref, 101, "FILLED", 10.0, 50.1, 1.25))  # the commission comes later
        await asyncio.sleep(0)

    asyncio.run(reports())
    done = store.batch(batch_id)
    assert [(x.status, x.filled_qty, x.avg_price, x.commission) for x in done.lines] == [
        ("FILLED", 10.0, 50.1, 1.25), ("PARTIAL", 5.0, 40.2, 1.0)]
    assert done.status == "DONE"
    assert notes.sent == [("BATCH_DONE", {"batch": str(batch_id), "session": "2021-01-01",
                                          "fills": "BUY 10 AAA @ 50.1; BUY 5 BBB @ 40.2"})]


def test_a_read_only_api_stops_the_batch(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "BUY", 10, 50.0), proposed("BBB", "BUY", 20, 40.0)])

    def refused(ticket, order_id):
        return OrderReport(order_id, ticket.order_ref, ticket.con_id, "REJECTED", 0.0, 0.0, 0.0, 321)

    orders, notes = FakeOrderPort(refused), Notes()

    async def go():
        result = await trader_of(store, orders, FakeAccount(), notes).send(store.batch(batch_id), store.portfolio(portfolio_id))
        await asyncio.sleep(0)
        return result

    asyncio.run(go())
    assert len(orders.placed) == 1
    assert [x.status for x in store.batch(batch_id).lines] == ["REJECTED", "NOT_SENT"]
    assert "READ_ONLY_API" in [e.kind for e in store.latest_events(5)]
    assert notes.sent[0] == ("READ_ONLY_API", {"batch": str(batch_id)})


def test_a_failing_report_stops_the_batch_and_is_recorded(market, monkeypatch):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "BUY", 10, 50.0), proposed("BBB", "BUY", 20, 40.0)])
    orders, notes = FakeOrderPort(working), Notes()
    trader = trader_of(store, orders, FakeAccount(), notes)
    update = store.update_order

    def failing(order_id, updated_at, **changes):
        if changes.get("status") == "WORKING":
            raise RuntimeError("the disk is full")
        return update(order_id, updated_at, **changes)

    monkeypatch.setattr(store, "update_order", failing)

    async def go():
        result = await trader.send(store.batch(batch_id), store.portfolio(portfolio_id))
        await asyncio.sleep(0)
        return result

    assert asyncio.run(go()) == ("DONE", f"batch {batch_id}: sent 1, skipped 0")
    assert [x.status for x in store.batch(batch_id).lines] == ["SENT", "NOT_SENT"]
    (event,) = [e for e in store.latest_events(5) if e.kind == "INTERNAL_ERROR"]
    assert event.message == f"order report {orders.placed[0].order_ref}: RuntimeError: the disk is full"
    assert notes.sent == [("INTERNAL_ERROR", {"error": "RuntimeError: the disk is full"})]


# ----- recovery, other orders, cancelling (§15.7, §15.8) -----


def test_recovery_matches_the_exact_order_ref_and_leaves_other_orders_alone(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "BUY", 10, 50.0)])
    line = store.batch(batch_id).lines[0]
    ref = f"btlab:{batch_id}:1:1609455600000"
    store.update_order(line.id, NOW, status="SENT", order_ref=ref)
    orders = FakeOrderPort()
    orders.open = [OrderReport(7, ref + "0", 101, "WORKING", 0.0, 0.0, 0.0),  # not exactly the line's
                   OrderReport(8, "owner's own order", 101, "WORKING", 0.0, 0.0, 0.0)]
    orders.completed = [OrderReport(5, ref, 101, "FILLED", 10.0, 49.9, 1.0)]
    asyncio.run(trader_of(store, orders, FakeAccount()).recover())
    assert (store.order(line.id).status, store.order(line.id).ib_order_id, store.order(line.id).avg_price) == ("FILLED", 5, 49.9)
    assert [(e.kind, e.message) for e in store.latest_events(5) if e.kind == "UNKNOWN_BTLAB_ORDER"] == [
        ("UNKNOWN_BTLAB_ORDER", f"{ref}0 matches no line; left alone")]


def test_cancelling_touches_only_btlabs_own_working_orders(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "BUY", 10, 50.0), proposed("BBB", "BUY", 20, 40.0)])
    orders = FakeOrderPort(working)
    trader = trader_of(store, orders, FakeAccount())
    sent(store, trader, batch_id, portfolio_id)

    async def go():
        orders.report(OrderReport(2, orders.placed[1].order_ref, 102, "FILLED", 20.0, 40.0, 1.0))
        await trader.cancel(store.batch(batch_id))

    asyncio.run(go())
    assert orders.cancelled == [1]


def test_the_order_adapter_maps_tickets_and_cancels_only_its_own_btlab_orders():
    """ib_orders on a stand-in for ib_async's IB: the orders it builds, and what it may cancel (§15.4, §15.8)."""
    placed, cancelled = [], []

    def trade(order_id, ref, client_id=31, status="Submitted", log=()):
        return SimpleNamespace(order=SimpleNamespace(orderId=order_id, orderRef=ref, clientId=client_id), log=list(log),
                               orderStatus=SimpleNamespace(status=status, filled=0.0, avgFillPrice=0.0),
                               contract=SimpleNamespace(conId=101), fills=[])

    trades = [trade(1, "btlab:3:1:1"), trade(2, "owner"), trade(3, "btlab:3:2:1", client_id=0)]
    ib = SimpleNamespace(client=SimpleNamespace(clientId=31), openTrades=lambda: trades, cancelOrder=cancelled.append,
                         placeOrder=lambda contract, order: placed.append((contract, order)) or SimpleNamespace(order=SimpleNamespace(orderId=9)))
    adapter = ib_orders.IbOrders(ib)
    common = {"con_id": 101, "symbol": "BRK.B", "exchange": "SMART", "currency": "USD", "action": "BUY", "quantity": 5.0,
              "outside_rth": False, "account": "DU1", "order_ref": "btlab:3:1:1"}
    from btlab.trading import OrderTicket
    assert adapter.place(OrderTicket(**common, order_type="MKT", limit_price=None, tif="OPG")) == 9
    adapter.place(OrderTicket(**common, order_type="LMT", limit_price=101.25, tif="DAY"))
    (contract, moo), (_, limit) = placed
    assert (contract.symbol, contract.conId, moo.orderType, moo.tif, moo.outsideRth, moo.account, moo.orderRef) == (
        "BRK B", 101, "MKT", "OPG", False, "DU1", "btlab:3:1:1")
    assert (limit.orderType, limit.lmtPrice, limit.tif) == ("LMT", 101.25, "DAY")
    assert [adapter.cancel(order_id) for order_id in (1, 2, 3)] == [True, False, False]
    assert [order.orderId for order in cancelled] == [1]
    rejected = trade(4, "btlab:3:3:1", status="ValidationError", log=[SimpleNamespace(errorCode=321)])
    cancelled_by_owner = trade(5, "btlab:3:4:1", status="Cancelled", log=[SimpleNamespace(errorCode=202)])
    assert [ib_orders.report(t).state for t in (trades[0], rejected, cancelled_by_owner)] == ["WORKING", "REJECTED", "CANCELLED"]
    assert ib_orders.report(rejected).error_code == 321


def test_only_ib_orders_places_or_cancels_and_nothing_cancels_globally():
    calls = re.compile(r"\.(placeOrder|cancelOrder|reqGlobalCancel)\s*\(")
    found = {(path.relative_to(REPO).as_posix(), call) for folder in ("btlab", "strategies", "scripts")
             for path in (REPO / folder).rglob("*.py") for call in calls.findall(path.read_text(encoding="utf-8"))}
    assert found == {("btlab/ib_orders.py", "placeOrder"), ("btlab/ib_orders.py", "cancelOrder")}


# ----- ibguard (§16) -----


def guarded(con_id: int, quantity: float, ref: float | None, price: float | None, status: str = "ACTIVE",
            sec_type: str = "STK", pending: bool = False) -> dict:
    return {"source": "REAL", "con_id": con_id, "symbol": f"S{con_id}", "sec_type": sec_type, "currency": "USD",
            "quantity": quantity, "ref": ref, "price": price, "status": status, "pending_exit": pending}


def test_the_preview_lists_the_stk_positions_that_would_exit_at_once():
    positions = (GuardedPosition(1, "L_TP", "STK", 10, 100.0, 116.0, "ACTIVE", False),
                 GuardedPosition(2, "L_SL", "STK", 10, 100.0, 94.0, "ACTIVE", False),
                 GuardedPosition(3, "L_KEEP", "STK", 10, 100.0, 110.0, "ACTIVE", False),
                 GuardedPosition(4, "S_TP", "STK", -10, 100.0, 84.0, "ACTIVE", False),
                 GuardedPosition(5, "S_SL", "STK", -10, 100.0, 106.0, "ACTIVE", False),
                 GuardedPosition(6, "UNKNOWN", "STK", 10, 100.0, None, "ACTIVE", False),
                 GuardedPosition(7, "OPTION", "OPT", 1, 1.0, 9.0, "ACTIVE", False))
    state = GuardState("DU1", "paper", True, {"STK": {"tp_pct": 10.0, "sl_pct": 5.0}}, positions)
    found = preview(state, 15.0, 5.0)
    assert (found["account"], found["account_kind"], found["paused"], found["current"], found["new"]) == (
        "DU1", "paper", True, {"tp_pct": 10.0, "sl_pct": 5.0}, {"tp_pct": 15.0, "sl_pct": 5.0})
    assert [(x["symbol"], x["side"], x["reason"]) for x in found["affected"]] == [
        ("L_TP", "LONG", "TAKE_PROFIT"), ("L_SL", "LONG", "STOP_LOSS"), ("S_TP", "SHORT", "TAKE_PROFIT"),
        ("S_SL", "SHORT", "STOP_LOSS"), ("UNKNOWN", "LONG", "UNKNOWN")]
    assert preview(state, 15.0, 5.0)["token"] == found["token"]
    assert preview(state, 20.0, 5.0)["token"] != found["token"]
    assert [thresholds_error(15, 5), thresholds_error(0, 5), thresholds_error(1001, 5), thresholds_error(15, 100)] == [
        None, "tp_pct must be above 0 and at most 1000", "tp_pct must be above 0 and at most 1000",
        "sl_pct must be above 0 and below 100"]


def guard_for(server: TestServer):
    config = ServiceConfig(str(server.make_url("/")), GUARD_TOKEN)

    def guard():
        return connected(config, aiohttp.ClientSession, _no_sleep, lambda: NOW.timestamp())

    return guard


async def _no_sleep(seconds: float) -> None:
    return None


def protected_send(store: Store, fake: FakeIbguard, positions: dict[int, float], lines: list[Line], seen: dict,
                   then=None, stop: bool = False):
    """A batch sent with ibguard's protocol around its reducing lines; then(orders, trader) follows within the server."""
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, lines, seen)
    orders, notes = FakeOrderPort(working), Notes()

    async def go():
        server = TestServer(fake.app())
        await server.start_server()
        guard = guard_for(server)
        if stop:
            await server.close()
        trader = trader_of(store, orders, FakeAccount(positions), notes, guard)
        await trader.send(store.batch(batch_id), store.portfolio(portfolio_id))
        if then is not None:
            await then(orders, trader)
        await server.close()

    asyncio.run(go())
    return store.batch(batch_id), orders, notes


def test_a_protected_sell_is_excluded_verified_sent_then_restored(market):
    fake = FakeIbguard(GUARD_TOKEN, [guarded(101, 20.0, 50.0, 52.0)])

    async def partly_filled(orders, trader):
        orders.report(OrderReport(1, orders.placed[0].order_ref, 101, "CANCELLED", 5.0, 52.0, 1.0))
        while not market.exclusions("RESTORED"):
            await asyncio.sleep(0.01)

    batch, orders, notes = protected_send(market, fake, {101: 20.0}, [proposed("AAA", "SELL", 10, 50.0)], {"AAA": 20.0},
                                          partly_filled)
    calls = [(m, path, body) for m, path, body in fake.requests if path != "/api/login"]
    assert calls == [("GET", "/api/state", None), ("POST", "/api/positions/REAL/101/exclude", {"excluded": True}),
                     ("GET", "/api/state", None), ("POST", "/api/positions/REAL/101/exclude", {"excluded": False})]
    assert batch.lines[0].status == "PARTIAL" and [x.state for x in market.exclusions("RESTORED")] == ["RESTORED"]
    assert ("PROTECTION_RESTORED", {"symbol": "AAA", "filled": "5"}) in notes.sent
    assert {"EXCLUDED", "PROTECTION_RESTORED"} <= {e.kind for e in market.latest_events(10)}


def test_a_sell_that_closes_the_position_closes_the_exclusion(market):
    fake = FakeIbguard(GUARD_TOKEN, [guarded(101, 20.0, 50.0, 52.0)])

    async def flat(orders, trader):
        trader.account.held[101] = 0.0
        orders.report(OrderReport(1, orders.placed[0].order_ref, 101, "FILLED", 20.0, 52.0, 1.0))
        while not market.exclusions("CLOSED"):
            await asyncio.sleep(0.01)

    protected_send(market, fake, {101: 20.0}, [proposed("AAA", "SELL", 20, 50.0)], {"AAA": 20.0}, flat)
    assert ("POST", "/api/positions/REAL/101/exclude", {"excluded": False}) not in fake.requests
    assert market.exclusions("ACTIVE") == []


@pytest.mark.parametrize(("position", "sticky", "status"), [
    (guarded(101, 20.0, 50.0, 52.0, "EXITING"), False, "SKIPPED_IBGUARD_EXITING"),
    (guarded(101, 20.0, 50.0, 52.0, pending=True), False, "SKIPPED_IBGUARD_EXITING"),
    (guarded(101, 20.0, 50.0, 52.0), True, "SKIPPED_IBGUARD_EXCLUDE_FAILED"),
    (guarded(101, 20.0, 50.0, 52.0, "EXCLUDED"), False, "WORKING"),
])
def test_the_sell_protocol_skips_or_keeps_the_owners_exclusion(market, position, sticky, status):
    fake = FakeIbguard(GUARD_TOKEN, [position])
    if sticky:
        fake.sticky.add(101)
    batch, orders, _ = protected_send(market, fake, {101: 20.0}, [proposed("AAA", "SELL", 10, 50.0),
                                                                  proposed("BBB", "BUY", 10, 40.0)], {"AAA": 20.0})
    assert [x.status for x in batch.lines] == [status, "WORKING"]
    owners = position["status"] == "EXCLUDED"
    # The owner's exclusion is never changed; nothing else is excluded when ibguard is exiting.
    excluded = [body for _, path, body in fake.requests if path.endswith("/exclude")]
    assert excluded == ([] if owners or status == "SKIPPED_IBGUARD_EXITING" else [{"excluded": True}])
    assert market.exclusions("ACTIVE") == []


def test_an_unreachable_ibguard_skips_sells_but_not_buys(market):
    batch, orders, _ = protected_send(market, FakeIbguard(GUARD_TOKEN), {101: 20.0},
                                      [proposed("AAA", "SELL", 10, 50.0), proposed("BBB", "BUY", 10, 40.0)],
                                      {"AAA": 20.0}, stop=True)
    assert [x.status for x in batch.lines] == ["SKIPPED_IBGUARD_UNREACHABLE", "WORKING"]
    assert [t.symbol for t in orders.placed] == ["BBB"]


def test_a_failing_include_is_retried_with_one_event_and_one_email(market):
    fake = FakeIbguard(GUARD_TOKEN, [guarded(101, 20.0, 50.0, 52.0)])

    async def failing_then_fixed(orders, trader):
        fake.failing.add("/api/positions/REAL/101/exclude")
        orders.report(OrderReport(1, orders.placed[0].order_ref, 101, "CANCELLED", 0.0, 0.0, 0.0))
        await asyncio.sleep(0.2)
        assert trader.unresolved()
        await trader.sweep("US")
        fake.failing.clear()
        await trader.sweep(None)

    batch, _, notes = protected_send(market, fake, {101: 20.0}, [proposed("AAA", "SELL", 10, 50.0)], {"AAA": 20.0},
                                     failing_then_fixed)
    assert [x.state for x in market.exclusions("RESTORED")] == ["RESTORED"]
    assert [kind for kind, _ in notes.sent].count("IBGUARD_RESTORE_FAILED") == 1
    assert [e.kind for e in market.latest_events(20)].count("IBGUARD_RESTORE_FAILED") == 1


def test_the_sweep_expires_day_orders_cancelled_at_the_close(market):
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = batch_of(store, portfolio_id, [proposed("AAA", "BUY", 10, 50.0), proposed("BBB", "BUY", 20, 40.0)])
    orders = FakeOrderPort(working)
    trader = trader_of(store, orders, FakeAccount())
    sent(store, trader, batch_id, portfolio_id)
    orders.completed = [OrderReport(1, orders.placed[0].order_ref, 101, "CANCELLED", 0.0, 0.0, 0.0)]
    orders.open = [OrderReport(2, orders.placed[1].order_ref, 102, "WORKING", 0.0, 0.0, 0.0)]
    asyncio.run(trader.sweep("US"))
    assert [x.status for x in store.batch(batch_id).lines] == ["EXPIRED", "WORKING"]


# ----- routes (§19) -----


def serve(tmp_path, store: Store, scenario, fake: FakeIbguard | None = None):
    document = edited(None, "mode", "trade")
    document["limits"] = {"us_max_orders_per_day": 2, "tw_max_orders_per_day": 2, "max_order_pct_of_equity": 50.0,
                          "us_max_order_value": 20000.0, "tw_max_order_value": 1000000.0}
    minute = MinuteStore(tmp_path)

    async def go():
        server = TestServer(fake.app()) if fake else None
        if server:
            await server.start_server()
            document["ibguard"] = {"url": str(server.make_url("/")), "token": GUARD_TOKEN}
        (tmp_path / "config.toml").write_text(render(document), encoding="utf-8")
        (tmp_path / "strategies").mkdir(exist_ok=True)
        orders, account = FakeOrderPort(working), FakeAccount({101: 20.0})
        trader = trader_of(store, orders, account)
        jobs = Jobs(store, minute, KEYS, TAXES, lambda: {}, InlinePool, 1, clock=lambda: NOW.timestamp(),
                    keep_awake=KeepAwake(lambda flags: None), mode="trade", trader=trader)
        services = Services(load_config(tmp_path / "config.toml"), store, minute, jobs, None, lambda: NOW.timestamp(),
                            _no_sleep)
        try:
            async with TestClient(TestServer(make_app(services))) as client:
                assert (await client.post("/api/login", json={"token": TOKEN})).status == 204
                return await scenario(client, jobs, orders, account)
        finally:
            if server:
                await server.close()

    try:
        return asyncio.run(go())
    finally:
        minute.close()


async def idle(jobs: Jobs) -> None:
    while (job := jobs.lane_job("LIVE")) is not None:
        await job.task


async def answer(response) -> tuple[int, object]:
    return response.status, (await response.json() if response.content_type == "application/json" else None)


def test_confirming_in_trade_mode_sends_or_refuses(market, tmp_path):
    store = market
    portfolio_id = portfolio_of(store)
    stale = store.save_batch(portfolio_id, SESSION, NOW, {"AAA": 25.0}, [proposed("AAA", "SELL", 5, 50.0)])

    async def scenario(client, jobs, orders, account):
        found = [await answer(await client.post(f"/api/live/batches/{stale}/confirm", json={"lines": []}))]
        store.set_batch_status(stale, "REPLACED")
        fresh = store.save_batch(portfolio_id, SESSION, NOW, {"AAA": 20.0}, [proposed("AAA", "SELL", 5, 50.0)])
        line = store.batch(fresh).lines[0].id
        found.append(await answer(await client.post(f"/api/live/batches/{fresh}/confirm", json={"lines": [line]})))
        await idle(jobs)
        found.append(await answer(await client.post(f"/api/live/batches/{fresh}/cancel", json={})))
        await idle(jobs)
        found.append(await answer(await client.get("/api/state")))
        return found, fresh, orders

    (stale_answer, confirmed, cancelling, state), fresh, orders = serve(tmp_path, store, scenario)
    assert stale_answer[0] == 409 and stale_answer[1]["error"] == "BATCH_STALE"
    assert confirmed == (202, {"batch": fresh, "status": "CONFIRMED", "job": "LIVE_SEND"})
    assert [t.symbol for t in orders.placed] == ["AAA"] and store.batch(fresh).status == "SENT"
    assert cancelling == (202, {"lane": "LIVE", "kind": "LIVE_CANCEL"}) and orders.cancelled == [1]
    assert state[1]["app"]["services"]["ibguard"] == {"configured": True, "reachable": None, "paused": None,
                                                      "account_kind": None}


def test_the_pc_stays_awake_while_a_btlab_order_works(market, tmp_path):
    """§23: the hold passes from the LIVE_SEND job to its working order, and ends at the tick after the fill."""
    store = market
    portfolio_id = portfolio_of(store)
    batch_id = store.save_batch(portfolio_id, SESSION, NOW, {"AAA": 20.0}, [proposed("AAA", "SELL", 5, 50.0)])
    store.save_setting(SWEEP_KEY.format(market="US"), to_day(NOW.date()))  # today's sweep is done

    async def scenario(client, jobs, orders, account):
        line = store.batch(batch_id).lines[0].id
        await client.post(f"/api/live/batches/{batch_id}/confirm", json={"lines": [line]})
        await idle(jobs)
        working = (store.batch(batch_id).lines[0].status, jobs.keep_awake.holders)
        orders.report(OrderReport(1, orders.placed[0].order_ref, 101, "FILLED", 5.0, 50.0, 1.0))
        jobs.tick(ScheduleConfig(False, time(8, 30), time(18, 0)))
        return working, (store.batch(batch_id).lines[0].status, jobs.keep_awake.holders)

    assert serve(tmp_path, store, scenario) == (("WORKING", 1), ("FILLED", 0))


def test_the_ibguard_preview_and_write(market, tmp_path):
    from btlab.backtest import load_run, run_backtest, save_result, strategy_factory, validate_config
    store = market
    config = validate_config({"name": "guarded", "market": "US", "symbols": ["AAA"], "start": "2020-06-01",
                              "strategy": {"source": "builtin", "id": "BUY_HOLD", "params": {}},
                              "exits": {"ibguard": {"tp_pct": 15, "sl_pct": 5}}})
    guarded_id = save_result(store, run_backtest(config, load_run(store, None, config), TAXES, strategy_factory(config)), NOW)
    plain = validate_config({**config.document, "name": "plain", "exits": {}})
    plain_id = save_result(store, run_backtest(plain, load_run(store, None, plain), TAXES, strategy_factory(plain)), NOW)
    fake = FakeIbguard(GUARD_TOKEN, [guarded(101, 20.0, 50.0, 58.0), guarded(102, 10.0, 40.0, 41.0)])

    async def scenario(client, jobs, orders, account):
        found = {"preview": await answer(await client.get(f"/api/ibguard/preview?backtest_id={guarded_id}")),
                 "plain": await answer(await client.get(f"/api/ibguard/preview?backtest_id={plain_id}"))}
        token = found["preview"][1]["token"]
        fake.positions[1]["price"] = 30.0  # another position would now exit
        found["changed"] = await answer(await client.post("/api/ibguard/thresholds",
                                                          json={"backtest_id": guarded_id, "preview_token": token}))
        token = (await answer(await client.get(f"/api/ibguard/preview?backtest_id={guarded_id}")))[1]["token"]
        found["write"] = await answer(await client.post("/api/ibguard/thresholds",
                                                        json={"backtest_id": guarded_id, "preview_token": token}))
        found["state"] = await answer(await client.get("/api/state"))
        return found

    found = serve(tmp_path, store, scenario, fake)
    status, shown = found["preview"]
    assert status == 200 and (shown["account"], shown["new"]) == ("DU1234567", {"tp_pct": 15.0, "sl_pct": 5.0})
    assert [(x["symbol"], x["reason"]) for x in shown["affected"]] == [("S101", "TAKE_PROFIT")]
    assert found["plain"][1]["error"] == "NO_IBGUARD_EXIT"
    assert found["changed"][1]["error"] == "PREVIEW_CHANGED"
    assert found["write"] == (204, None)
    assert [body for method, path, body in fake.requests if path == "/api/thresholds"] == [{"STK": {"tp_pct": 15.0, "sl_pct": 5.0}}]
    assert fake.thresholds["OPT"] == {"tp_pct": 50.0, "sl_pct": 30.0}
    assert store.latest_events(1)[0].kind == "THRESHOLDS_WRITTEN"
    assert found["state"][1]["app"]["services"]["ibguard"] == {"configured": True, "reachable": True, "paused": False,
                                                               "account_kind": "paper"}


def test_an_unreachable_ibguard_answers_409(market, tmp_path):
    from btlab.backtest import load_run, run_backtest, save_result, strategy_factory, validate_config
    store = market
    config = validate_config({"name": "guarded", "market": "US", "symbols": ["AAA"], "start": "2020-06-01",
                              "strategy": {"source": "builtin", "id": "BUY_HOLD", "params": {}},
                              "exits": {"ibguard": {"tp_pct": 15, "sl_pct": 5}}})
    guarded_id = save_result(store, run_backtest(config, load_run(store, None, config), TAXES, strategy_factory(config)), NOW)
    fake = FakeIbguard(GUARD_TOKEN)
    fake.failing.add("/api/state")

    async def scenario(client, jobs, orders, account):
        return await answer(await client.get(f"/api/ibguard/preview?backtest_id={guarded_id}"))

    status, body = serve(tmp_path, store, scenario, fake)
    assert (status, body["error"]) == (409, "IBGUARD_UNREACHABLE") and GUARD_TOKEN not in body["message"]
