"""Orders through ib_async (SPEC.md §15): trade mode's OrderPort on the link's connection.

The only module that places or cancels orders, and it never calls reqGlobalCancel; test mode never constructs it
(§15.1). Every ib_async name used here was checked against the installed 2.1.0.
"""

from collections.abc import Callable

from ib_async import IB, LimitOrder, MarketOrder, Stock, Trade

from btlab.trading import CANCELLED_CODE, PREFIX, OrderReport, OrderTicket

# ib_async's states of an order that ended without a fill or with a part of it.
_ENDED = ("Cancelled", "ApiCancelled", "Inactive")


class IbOrders:
    def __init__(self, ib: IB) -> None:
        self._ib = ib

    def place(self, ticket: OrderTicket) -> int:
        """The order of §15.4: MKT with OPG (MOO) or LMT DAY, outside the regular hours never, on the account, with
        btlab's orderRef. Returns IB's order id."""
        contract = Stock(ticket.symbol.replace(".", " "), ticket.exchange, ticket.currency, conId=ticket.con_id)
        fields = {"tif": ticket.tif, "outsideRth": ticket.outside_rth, "account": ticket.account, "orderRef": ticket.order_ref}
        if ticket.order_type == "MKT":
            order = MarketOrder(ticket.action, ticket.quantity, **fields)
        else:
            order = LimitOrder(ticket.action, ticket.quantity, ticket.limit_price, **fields)
        return self._ib.placeOrder(contract, order).order.orderId

    def cancel(self, order_id: int) -> bool:
        """Cancels one of this client's working btlab orders; any other order is never touched (§15.8)."""
        trade = next((trade for trade in self._own() if trade.order.orderId == order_id), None)
        if trade is None:
            return False
        self._ib.cancelOrder(trade.order)
        return True

    def open_orders(self) -> list[OrderReport]:
        return [report(trade) for trade in self._ib.openTrades() if trade.order.clientId == self._ib.client.clientId]

    async def completed_orders(self) -> list[OrderReport]:
        return [report(trade) for trade in await self._ib.reqCompletedOrdersAsync(apiOnly=True)]

    def on_report(self, handler: Callable[[OrderReport], None]) -> None:
        """Every change of an order's state, and every commission once IB reports it."""
        self._ib.orderStatusEvent += lambda trade: handler(report(trade))
        self._ib.commissionReportEvent += lambda trade, fill, commission: handler(report(trade))

    def _own(self) -> list[Trade]:
        return [trade for trade in self._ib.openTrades()
                if trade.order.clientId == self._ib.client.clientId and trade.order.orderRef.startswith(PREFIX)]


def report(trade: Trade) -> OrderReport:
    """An ib_async Trade as trading.py sees it. ib_async 2.1.0 keeps error 321 (read-only API) as a ValidationError;
    a cancellation's own message (202) is not a rejection."""
    status = trade.orderStatus.status
    errors = [entry.errorCode for entry in trade.log if entry.errorCode and entry.errorCode != CANCELLED_CODE]
    if status == "Filled":
        state = "FILLED"
    elif status == "ValidationError" or (status in _ENDED and errors):
        state = "REJECTED"
    elif status in _ENDED:
        state = "CANCELLED"
    else:
        state = "WORKING"
    commission = sum(fill.commissionReport.commission for fill in trade.fills)
    return OrderReport(trade.order.orderId, trade.order.orderRef, trade.contract.conId, state, trade.orderStatus.filled,
                       trade.orderStatus.avgFillPrice, commission, errors[-1] if errors else None)
