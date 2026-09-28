from collections import defaultdict, deque
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# =========================
# Trade Model
# =========================
@dataclass
class Trade:
    symbol: str
    side: str          # B / S
    qty: int
    price: float
    time: datetime
    product: str       # MIS / NRML
    segment: str       # nse_cm / nse_fo / bse_fo
    order_id: str = ""
    order_source: str = "NA"


# =========================
# Charges Calculator (India - Approx)
# =========================
class ChargesCalculator:

    @staticmethod
    def calculate(turnover: float, segment: str, brokerage: float = 0.0) -> float:
        """
        Approx Indian charges.
        Brokerage is passed explicitly.
        """
        exch = turnover * 0.0000325
        sebi = turnover * 0.000001
        gst = 0.18 * (exch + sebi + brokerage)
        stamp = turnover * 0.00015

        # STT rules
        if segment.endswith("_cm"):
            stt = turnover * 0.001        # Equity delivery sell
        else:
            stt = turnover * 0.0007       # F&O sell

        return round(stt + exch + sebi + gst + stamp + brokerage, 2)


# =========================
# FIFO P&L Engine
# =========================
class PnLEngine:

    def __init__(self):
        self.positions = defaultdict(deque)
        self.realized_trades = []

    def add_trade(self, trade: Trade):
        if trade.side == "B":
            self.positions[trade.symbol].append(trade)
        elif trade.side == "S":
            self._match_sell(trade)

    def _match_sell(self, sell: Trade):
        sell_qty = sell.qty

        while sell_qty > 0 and self.positions[sell.symbol]:
            buy = self.positions[sell.symbol][0]
            matched_qty = min(buy.qty, sell_qty)

            gross_pnl = (sell.price - buy.price) * matched_qty
            turnover = (sell.price + buy.price) * matched_qty
            charges = ChargesCalculator.calculate(turnover, sell.segment)
            net_pnl = round(gross_pnl - charges, 2)

            self.realized_trades.append({
                "symbol": sell.symbol,
                "qty": matched_qty,
                "buy_price": buy.price,
                "sell_price": sell.price,
                "gross_pnl": round(gross_pnl, 2),
                "charges": charges,
                "net_pnl": net_pnl,
                "buy_time": buy.time,
                "sell_time": sell.time,
                "trade_date": sell.time.date()
            })

            buy.qty -= matched_qty
            sell_qty -= matched_qty

            if buy.qty == 0:
                self.positions[sell.symbol].popleft()

    def daily_summary(self) -> Dict:
        summary = defaultdict(lambda: {
            "trades": 0,
            "gross_pnl": 0,
            "charges": 0,
            "net_pnl": 0
        })

        for t in self.realized_trades:
            d = t["trade_date"]
            summary[d]["trades"] += 1
            summary[d]["gross_pnl"] += t["gross_pnl"]
            summary[d]["charges"] += t["charges"]
            summary[d]["net_pnl"] += t["net_pnl"]

        return summary


# =========================
# API Adapter
# =========================
_FILLED_STATUSES = frozenset(
    {"complete", "completed", "filled", "traded", "executed"}
)

_TIMESTAMP_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
)


def _order_row_status(o: dict) -> str:
    for key in ("ordSt", "ordStatus", "stat", "status"):
        raw = o.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).lower().strip()
    return ""


def _is_filled_order_row(o: dict) -> bool:
    status = _order_row_status(o)
    if status in _FILLED_STATUSES:
        return True
    # Some rows only expose remaining qty on the order book.
    try:
        unfilled = float(o.get("unFldSz") or o.get("flQtyRem") or -1)
        filled = float(o.get("fldQty") or o.get("flQty") or o.get("qty") or 0)
        if unfilled == 0 and filled > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _parse_trade_timestamp(o: dict) -> datetime:
    ts_raw = str(
        o.get("exCfmTm", "")
        or o.get("ordTm", "")
        or o.get("ordEntTm", "")
        or o.get("hsUpTm", "")
        or o.get("exTm", "")
        or o.get("updRecvTm", "")
        or ""
    ).strip()
    if not ts_raw:
        fl_dt = str(o.get("flDt", "")).strip()
        fl_tm = str(o.get("flTm", "")).strip()
        if fl_dt and fl_tm:
            ts_raw = f"{fl_dt} {fl_tm}"
    if ts_raw:
        ts = " ".join(ts_raw.split())
        for fmt in _TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
    return datetime.now()


def _parse_transaction_side(o: dict) -> str:
    raw = str(
        o.get("trnsTp", "")
        or o.get("side", "")
        or o.get("transaction_type", "")
        or o.get("txnType", "")
    ).upper()
    if "BUY" in raw or raw == "B":
        return "B"
    if "SELL" in raw or raw == "S":
        return "S"
    if raw:
        return raw[0]
    return "B"


def _parse_avg_price(o: dict) -> float:
    for key in (
        "avgPrc",
        "buyAvgPrc",
        "sellAvgPrc",
        "executionPrice",
        "prc",
        "lastRate",
    ):
        val = o.get(key)
        if val is None or val == "":
            continue
        try:
            price = float(val)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    try:
        amt = float(o.get("buyAmt") or o.get("sellAmt") or 0)
        f_qty = float(
            o.get("fldQty")
            or o.get("flQty")
            or o.get("flBuyQty")
            or o.get("flSellQty")
            or o.get("executionQty")
            or o.get("filledQty")
            or o.get("qty")
            or 0
        )
        if f_qty > 0 and amt > 0:
            return amt / f_qty
    except (TypeError, ValueError):
        pass
    return 0.0


def _parse_trade_qty(o: dict, *, fills: bool = False) -> int:
    if fills:
        q_val = (
            o.get("fldQty")
            or o.get("executionQty")
            or o.get("filledQty")
            or o.get("flQty")
            or o.get("qty")
            or 0
        )
    else:
        q_val = (
            o.get("fldQty")
            or o.get("flQty")
            or o.get("executionQty")
            or o.get("filledQty")
            or o.get("qty")
            or o.get("flBuyQty")
            or o.get("flSellQty")
            or 0
        )
    try:
        return int(float(str(q_val)))
    except (TypeError, ValueError):
        return 0


def _row_to_trade(o: dict, *, fills: bool = False) -> Optional[Trade]:
    avg_price = _parse_avg_price(o)
    if avg_price <= 0:
        return None
    qty = _parse_trade_qty(o, fills=fills)
    if qty <= 0:
        return None
    symbol = str(o.get("trdSym") or o.get("trading_symbol") or o.get("sym") or "").strip().upper()
    if not symbol:
        return None
    return Trade(
        symbol=symbol,
        side=_parse_transaction_side(o),
        qty=qty,
        price=avg_price,
        time=_parse_trade_timestamp(o),
        product=str(o.get("prod", "MIS")),
        segment=str(o.get("exSeg", "") or o.get("exch", "")),
        order_id=str(o.get("nOrdNo", "")),
        order_source=str(o.get("ordSrc", "NA")),
    )


def parse_api_orders(api_data: List[dict]) -> List[Trade]:
    trades = []
    for o in api_data:
        if not _is_filled_order_row(o):
            continue
        trade = _row_to_trade(o, fills=False)
        if trade:
            trades.append(trade)
    return trades


def parse_api_fills(api_data: List[dict]) -> List[Trade]:
    """Parse Kotak trade_report rows (individual fills)."""
    trades = []
    for o in api_data:
        trade = _row_to_trade(o, fills=True)
        if trade:
            trades.append(trade)
    return trades


def _completed_trade_count(trades: List[Trade]) -> int:
    engine = PositionPnLEngine()
    for trade in sorted(trades, key=lambda x: x.time):
        engine.add_trade(trade)
    return len(engine.completed_trades)


def broker_trades_for_pnl(client) -> Tuple[List[Trade], Dict]:
    """
    Load today's broker executions for PnL. Prefer order_report; if that yields
    no closed trades, fall back to trade_report fills.
    """
    meta: Dict = {
        "order_report_rows": 0,
        "trade_report_rows": 0,
        "parsed_order_trades": 0,
        "parsed_fill_trades": 0,
        "completed_from_orders": 0,
        "completed_from_fills": 0,
        "source": "none",
    }

    report = client.order_report()
    if isinstance(report, dict) and (report.get("Error") or report.get("Error Message")):
        meta["order_report_error"] = str(report.get("Error") or report.get("Error Message"))

    order_rows = report.get("data", []) if isinstance(report, dict) else (report or [])
    if not isinstance(order_rows, list):
        order_rows = []
    meta["order_report_rows"] = len(order_rows)

    order_trades = parse_api_orders(order_rows)
    meta["parsed_order_trades"] = len(order_trades)
    meta["completed_from_orders"] = _completed_trade_count(order_trades)

    fill_rows: List[dict] = []
    trade_report = client.trade_report()
    if isinstance(trade_report, dict) and not (
        trade_report.get("Error") or trade_report.get("Error Message")
    ):
        raw = trade_report.get("data", [])
        if isinstance(raw, list):
            fill_rows = raw
    elif isinstance(trade_report, dict) and (
        trade_report.get("Error") or trade_report.get("Error Message")
    ):
        meta["trade_report_error"] = str(
            trade_report.get("Error") or trade_report.get("Error Message")
        )
    meta["trade_report_rows"] = len(fill_rows)

    fill_trades = parse_api_fills(fill_rows)
    meta["parsed_fill_trades"] = len(fill_trades)
    meta["completed_from_fills"] = _completed_trade_count(fill_trades)

    if meta["completed_from_orders"] > 0:
        meta["source"] = "order_report"
        return order_trades, meta
    if meta["completed_from_fills"] > 0:
        meta["source"] = "trade_report"
        return fill_trades, meta
    if order_trades:
        meta["source"] = "order_report"
        return order_trades, meta
    if fill_trades:
        meta["source"] = "trade_report"
        return fill_trades, meta
    return [], meta


class PositionPnLEngine:

    def __init__(self, initial_capital=0.0):
        self.open_trades = defaultdict(deque)        # key = symbol, value = deque of open positions
        self.completed_trades = []
        self.initial_capital = initial_capital

    def get_current_capital(self):
        net_pnl = sum(t.get("net_pnl", 0) for t in self.completed_trades)
        return self.initial_capital + net_pnl

    def get_pnl_percentage(self):
        if self.initial_capital == 0: return 0.0
        net_pnl = sum(t.get("net_pnl", 0) for t in self.completed_trades)
        return round((net_pnl / self.initial_capital) * 100, 2)

    def add_trade(self, trade: Trade):
        symbol = trade.symbol.strip().upper()

        # BUY opens or adds to a position (per symbol)
        if trade.side == "B":
            self.open_trades[symbol].append({
                "symbol": symbol,
                "buy_price": trade.price,
                "buy_qty": trade.qty,
                "open_qty": trade.qty,
                "buy_time": trade.time,
                "buy_order_id": trade.order_id,
                "order_source": trade.order_source, # Carry order_source for buy
                "gross_pnl": 0.0,
                "charges": 0.0
            })

        # SELL reduces position using FIFO
        elif trade.side == "S" and self.open_trades.get(symbol):
            sell_qty = trade.qty
            
            while sell_qty > 0 and self.open_trades[symbol]:
                pos = self.open_trades[symbol][0]
                matched_qty = min(sell_qty, pos["open_qty"])

                # Brokerage logic: Flat 10 if order source is Broker Website (TFC_W)
                # Note: We apply brokerage on the MATCHED lot. 
                # If either the Buy OR the Sell was from Web, we apply it? 
                # Usually brokerage is per order. Let's assume 10 if the Sell was from Web for now, 
                # or check both.
                
                brokerage = 0.0
                # If source is NOT our terminal (NEOTRADEAPI), apply 10 point brokerage
                # 'trade' is the Sell, 'pos' is the matching Buy
                sell_src = trade.order_source
                buy_src = pos.get("order_source", "NA")

                if sell_src != "NA" and "NEOTRADEAPI" not in sell_src:
                    brokerage += 10.0
                if buy_src != "NA" and "NEOTRADEAPI" not in buy_src:
                    brokerage += 10.0

                pnl = (trade.price - pos["buy_price"]) * matched_qty
                turnover = (trade.price + pos["buy_price"]) * matched_qty
                charges = ChargesCalculator.calculate(turnover, trade.segment, brokerage=brokerage)

                pos["gross_pnl"] += pnl
                pos["charges"] += charges
                pos["open_qty"] -= matched_qty
                sell_qty -= matched_qty

                # Position fully closed for this buy lot
                if pos["open_qty"] == 0:
                    pos["net_pnl"] = round(pos["gross_pnl"] - pos["charges"], 2)
                    pos["sell_price"] = trade.price
                    pos["sell_time"] = trade.time
                    pos["sell_order_id"] = trade.order_id
                    pos["sell_order_source"] = trade.order_source # Carry order_source for sell
                    pos["trade_date"] = trade.time.date()

                    self.completed_trades.append(pos)
                    self.open_trades[symbol].popleft()
                    #print(f"DEBUG: Trade Completed - {symbol} PnL: {pos['net_pnl']} [B:{pos['buy_order_id']} S:{pos['sell_order_id']}]")
            
            if not self.open_trades[symbol]:
                del self.open_trades[symbol]

    def hourly_summary(self, for_date=None) -> Dict[str, int]:
        """
        Return a dict of hourly trade counts for the given date.
        Keys are labeled like '09:00-10:00'. If `for_date` is None,
        uses today's date.
        """
        counts = defaultdict(int)

        if for_date is None:
            for_date = datetime.now().date()

        for t in self.completed_trades:
            st = t.get("sell_time")
            if not st:
                continue
            if st.date() != for_date:
                continue
            counts[st.hour] += 1

        # Build labeled dict for all 24 hours
        hourly = {}
        for h in range(24):
            label = f"{h:02d}:00-{(h+1)%24:02d}:00"
            hourly[label] = counts.get(h, 0)

        return hourly

    def trading_hours_summary(self, start_time="09:15", end_time="15:30", slot_minutes=60, for_date=None) -> Dict:
        """
        Compute trade counts for consecutive slots between `start_time` and `end_time`.
        Default trading window is 09:15 to 15:30. Slots are `slot_minutes` long.

        Returns a dict with:
          - 'slots': Ordered dict label -> count (labels like '09:15-10:15')
          - 'total_trades': int
          - 'total_hours': float (hours)
          - 'avg_per_hour': float
        """
        from datetime import datetime, date, time, timedelta
        from collections import OrderedDict

        if for_date is None:
            for_date = datetime.now().date()

        # parse start/end times
        sh, sm = map(int, start_time.split(":"))
        eh, em = map(int, end_time.split(":"))

        start_dt = datetime.combine(for_date, time(sh, sm))
        end_dt = datetime.combine(for_date, time(eh, em))

        # Build slots
        slots = OrderedDict()
        cur = start_dt
        delta = timedelta(minutes=slot_minutes)
        while cur < end_dt:
            nxt = min(cur + delta, end_dt)
            label = f"{cur.strftime('%H:%M')}-{nxt.strftime('%H:%M')}"
            slots[label] = 0
            cur = nxt

        total = 0
        for t in self.completed_trades:
            st = t.get("sell_time")
            if not st:
                continue
            if st.date() != for_date:
                continue

            # find slot
            for label in slots:
                parts = label.split("-")
                s_part = datetime.combine(for_date, datetime.strptime(parts[0], "%H:%M").time())
                e_part = datetime.combine(for_date, datetime.strptime(parts[1], "%H:%M").time())
                if s_part <= st < e_part:
                    slots[label] += 1
                    total += 1
                    break

        total_hours = (end_dt - start_dt).total_seconds() / 3600.0
        avg_per_hour = round((total / total_hours) if total_hours > 0 else 0.0, 2)

        return {
            "slots": slots,
            "total_trades": total,
            "total_hours": round(total_hours, 2),
            "avg_per_hour": avg_per_hour
        }
