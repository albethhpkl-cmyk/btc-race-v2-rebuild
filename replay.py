"""Local replay driver for the BTC race bot (fresh code, written this session).

Drives main.py as a subprocess over the kit's frozen dev window through
the agent-fun/bars-replay/v1 line protocol, with the fill model ported
from the kit's own harness source (sim/bars/engine.ts):

  buy:  spend = notional * (10000 - 4) / 10000; fee = notional - spend
        price = open * (10000 + 2) * M / 10000   (M = 1 + spend / depth)
        qty   = spend * 10^(qty_scale+price_scale) / (price * 10^quote_scale)
  sell: price = max(1, open * (10000 - 2) / (10000 * M))
        gross = qty * price * 10^quote_scale / 10^(qty_scale+price_scale)
        proceeds = gross * (10000 - 4) / 10000; fee = gross - proceeds

Orders decided on bar k fill at bar k+1's open against that bar's
o_depth; max_slippage is checked against the close of bar k; everything
held at the end is flattened at the last close against its c_depth.

Usage: python3 replay.py [--runs N] [--bot main.py]
Prints net PnL, trades, win rate, max drawdown, fees, and whether the
runs' action logs are byte-identical. No Docker, no network.
"""

import argparse
import gzip
import json
import subprocess
import sys

BPS = 10000
FEE_BPS = 4
SLIP_BPS = 2
QUOTE_SCALE = 6
PRICE_SCALE = 5
QTY_SCALE = 8
START_CASH = 1_000_000_000  # micro-USDC
MIN_NOTIONAL = 1_000_000    # 1 USDC
MAX_ORDERS_PER_BAR = 16
MAX_WEIGHT_BPS = 10000
SYMBOL = "CBBTC"

KIT = "/home/hatch/workspace/makefun/btc-race-24h-20261123-0511-kit/btc-race-24h-20261123-0511-kit"
BARS_PATH = KIT + "/dataset/bars/CBBTC.ndjson.gz"


def bars_value(qty, price):
    return qty * price * 10 ** QUOTE_SCALE // 10 ** (QTY_SCALE + PRICE_SCALE)


def buy_fill(notional, open_price, depth):
    """Exact integer buy fill on a fresh single-hop pool of depth D."""
    spend = notional * (BPS - FEE_BPS) // BPS
    # M = 1 + spend / D  ->  price = open * (BPS+slip) * M / BPS
    price = open_price * (BPS + SLIP_BPS) * (depth + spend) // (BPS * depth)
    qty = spend * 10 ** (QTY_SCALE + PRICE_SCALE) // (price * 10 ** QUOTE_SCALE)
    return price, spend, notional - spend, qty


def sell_fill(qty, open_price, depth):
    """Exact integer sell fill on a fresh single-hop pool of depth D."""
    value = bars_value(qty, open_price)
    # M = 1 + value / D ; price = open * (BPS-slip) / (BPS * M), floored, min 1
    price = open_price * (BPS - SLIP_BPS) * depth // (BPS * (depth + value))
    if price < 1:
        price = 1
    gross = bars_value(qty, price)
    proceeds = gross * (BPS - FEE_BPS) // BPS
    return price, gross, gross - proceeds, proceeds


def load_bars():
    bars = []
    with gzip.open(BARS_PATH, "rt") as f:
        for line in f:
            d = json.loads(line)
            bars.append({
                "t": int(d["t"]),
                "o": int(d["o"]), "h": int(d["h"]),
                "l": int(d["l"]), "c": int(d["c"]),
                "o_depth": [int(x) for x in d["o_depth"]],
                "c_depth": [int(x) for x in d["c_depth"]],
            })
    return bars


def wire_bar(bar):
    return {
        "symbol": SYMBOL, "t": bar["t"],
        "o": str(bar["o"]), "h": str(bar["h"]), "l": str(bar["l"]),
        "c": str(bar["c"]), "v": None,
        "o_depth": [str(x) for x in bar["o_depth"]],
        "c_depth": [str(x) for x in bar["c_depth"]],
    }


def run_once(bot_path):
    """One full replay. Returns (metrics dict, action_log_bytes, trade_log)."""
    bars = load_bars()
    n = len(bars)
    hello = {
        "type": "hello", "protocol": "agent-fun/bars-replay/v1", "seed": "1",
        "symbols": [{"symbol": SYMBOL,
                     "mint": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
                     "price_scale": PRICE_SCALE, "qty_scale": QTY_SCALE}],
        "capital": {"quote": "USDC", "quote_scale": QUOTE_SCALE,
                    "quote_units": str(START_CASH)},
        "bar_interval": "1m", "first_close_ms": bars[0]["t"],
        "caps": {"max_open_positions": 1, "max_orders_per_bar": MAX_ORDERS_PER_BAR,
                 "max_weight_bps": MAX_WEIGHT_BPS},
        "fees": {"fee_bps": FEE_BPS, "slippage_bps": SLIP_BPS,
                 "min_notional_quote_units": str(MIN_NOTIONAL)},
        "latency_bars": 1,
    }
    proc = subprocess.Popen(
        ["python3", "-P", "-s", bot_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)
    action_log = []
    try:
        proc.stdin.write(json.dumps(hello, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        cash = START_CASH
        qty = 0
        cost = 0
        fees = 0
        fills_count = 0
        equity_points = [START_CASH]
        trade_log = []
        open_trade = None  # (buy_bar, notional, qty, buy_price)
        pending = []  # orders decided on the previous batch: dicts
        used_cids = set()
        for k in range(n):
            bar = bars[k]
            # Fill the previous batch's orders at this bar's open.
            fills = []
            depth = bar["o_depth"][0]
            for od in pending:
                o = od["order"]
                close_dec = od["close"]
                cid = o["client_id"]
                status, reason = "filled", None
                price = qty_f = fee = None
                if od["seq"] == n - 1:
                    status, reason = "rejected", "after_end"
                elif o["side"] == "buy":
                    notional = int(o["notional_quote_units"])
                    if notional <= 0:
                        status, reason = "rejected", "zero_amount"
                    elif qty > 0:
                        status, reason = "rejected", "max_open_positions"
                    elif notional > cash:
                        status, reason = "rejected", "insufficient_cash"
                    elif notional < MIN_NOTIONAL:
                        status, reason = "rejected", "below_min_notional"
                    else:
                        price, spend, fee, qty_f = buy_fill(notional, bar["o"], depth)
                        if price * BPS > close_dec * (BPS + int(o["max_slippage_bps"])):
                            status, reason = "rejected", "max_slippage"
                        else:
                            cash -= notional
                            qty = qty_f
                            cost = notional
                            fees += fee
                            fills_count += 1
                            open_trade = (od["seq"], notional, qty_f, price)
                else:  # sell
                    q = int(o["qty"])
                    if q <= 0:
                        status, reason = "rejected", "zero_amount"
                    elif q > qty:
                        status, reason = "rejected", "insufficient_position"
                    else:
                        price, gross, fee, proceeds = sell_fill(q, bar["o"], depth)
                        if price * BPS < close_dec * (BPS - int(o["max_slippage_bps"])):
                            status, reason = "rejected", "max_slippage"
                        else:
                            cash += proceeds
                            fees += fee
                            fills_count += 1
                            if open_trade is not None:
                                trade_log.append({
                                    "buy_bar": open_trade[0],
                                    "buy_notional": open_trade[1],
                                    "buy_qty": open_trade[2],
                                    "buy_price": open_trade[3],
                                    "sell_bar": od["seq"],
                                    "sell_price": price,
                                    "pnl_usdc": (proceeds - open_trade[1]) / 1e6,
                                })
                                open_trade = None
                            qty = 0
                            cost = 0
                fills.append({
                    "client_id": cid, "symbol": SYMBOL, "side": o["side"],
                    "status": status, "reason": reason,
                    "decided_bar": od["seq"], "fill_bar": k,
                    "price": str(price if price is not None else 0),
                    "qty": str(qty_f if qty_f is not None else (int(o["qty"]) if o["side"] == "sell" else 0)),
                    "fee": str(fee if fee is not None else 0),
                })
            pending = []
            # Account + equity point at this bar's close.
            pos_value = bars_value(qty, bar["c"]) if qty > 0 else 0
            equity = cash + pos_value
            equity_points.append(equity)
            positions = ([{"symbol": SYMBOL, "qty": str(qty),
                           "cost_quote_units": str(cost)}] if qty > 0 else [])
            batch = {
                "type": "batch", "seq": k, "t": bar["t"],
                "bars": [wire_bar(bar)], "fills": fills,
                "account": {"cash_quote_units": str(cash),
                            "positions": positions,
                            "equity_quote_units": str(equity)},
            }
            proc.stdin.write(json.dumps(batch, separators=(",", ":")) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("bot exited early at batch %d: %s"
                                   % (k, proc.stderr.read()[-2000:]))
            action_log.append(line)
            reply = json.loads(line)
            assert reply.get("type") == "reply" and reply.get("seq") == k, \
                "bad reply at batch %d: %s" % (k, line[:200])
            orders = reply.get("orders") or []
            if len(orders) > MAX_ORDERS_PER_BAR:
                raise RuntimeError("reply over max_orders_per_bar at batch %d" % k)
            for o in orders:
                if o.get("client_id") in used_cids:
                    raise RuntimeError("duplicate client_id at batch %d" % k)
                used_cids.add(o.get("client_id"))
                pending.append({"order": o, "seq": k, "close": bar["c"]})
        # Orders decided on the last batch are rejected (after_end); no fills.
        # Flatten any open position at the last close against c_depth.
        flatten_fee = 0
        if qty > 0:
            price, gross, fee, proceeds = sell_fill(qty, bars[-1]["c"], bars[-1]["c_depth"][0])
            cash += proceeds
            fees += fee
            flatten_fee = fee
            qty = 0
        done = {"type": "done", "seq": n, "reason": "complete",
                "account": {"cash_quote_units": str(cash), "positions": [],
                            "equity_quote_units": str(cash)}}
        proc.stdin.write(json.dumps(done, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        rc = proc.wait(timeout=60)
        if rc != 0:
            raise RuntimeError("bot exited rc=%d: %s" % (rc, proc.stderr.read()[-2000:]))
        equity_points.append(cash)
        # Metrics.
        pnl_usdc = (cash - START_CASH) / 1e6
        peak = equity_points[0]
        max_dd = 0.0
        for e in equity_points:
            if e > peak:
                peak = e
            dd = (peak - e) / peak
            if dd > max_dd:
                max_dd = dd
        rt_wins = sum(1 for t in trade_log if t["pnl_usdc"] > 0)
        # Per-hour-block PnL: equity change per 60-bar block (approx via trade log).
        blocks = [0.0] * 24
        for t in trade_log:
            blocks[min(23, t["sell_bar"] // 60)] += t["pnl_usdc"]
        if open_trade is not None:
            # A buy never sold (should not happen: flatten sells it) — ignore.
            pass
        return {
            "net_pnl_usdc": pnl_usdc,
            "return": pnl_usdc / 1000.0,
            "trades": fills_count,
            "round_trips": len(trade_log),
            "win_rate_rt": (rt_wins / len(trade_log)) if trade_log else 0.0,
            "max_drawdown": max_dd,
            "fees_usdc": fees / 1e6,
            "flatten_fee_usdc": flatten_fee / 1e6,
            "final_cash_usdc": cash / 1e6,
            "blocks": blocks,
            "trade_log": trade_log,
        }, "".join(action_log).encode("utf-8")
    finally:
        if proc.poll() is None:
            proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--bot", default="main.py")
    args = ap.parse_args()
    logs = []
    metrics = None
    for i in range(args.runs):
        metrics, log = run_once(args.bot)
        logs.append(log)
        print("run %d: net PnL %+.2f USDC | return %+.4f | trades %d | "
              "round trips %d | win rate %.2f | max DD %.4f | fees $%.2f"
              % (i + 1, metrics["net_pnl_usdc"], metrics["return"],
                 metrics["trades"], metrics["round_trips"],
                 metrics["win_rate_rt"], metrics["max_drawdown"],
                 metrics["fees_usdc"]))
    identical = all(l == logs[0] for l in logs)
    print("action logs byte-identical across %d runs: %s (%d bytes each)"
          % (args.runs, identical, len(logs[0])))
    print("trade log:")
    for t in metrics["trade_log"]:
        print("  BUY bar %d @ %s (notional $%.2f) -> SELL bar %d @ %s : %+.2f USDC"
              % (t["buy_bar"], t["buy_price"], t["buy_notional"] / 1e6,
                 t["sell_bar"], t["sell_price"], t["pnl_usdc"]))
    print("per-hour blocks:", " ".join("%+.1f" % b for b in metrics["blocks"]))
    print("final cash: $%.2f | flatten fee: $%.2f"
          % (metrics["final_cash_usdc"], metrics["flatten_fee_usdc"]))


if __name__ == "__main__":
    main()
