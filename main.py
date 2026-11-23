"""BTC Race bot — deep-dip swing + momentum-burst, rebuilt fresh.

Paper-money bot for the make.fun BTC Race (agent-fun/bars-replay/v1,
long-only CBBTC, $1,000 USDC start). Python 3.13, standard library only.
Speaks the line protocol directly on stdin/stdout (newline-delimited JSON):
hello once, one batch per minute-bar, one reply per batch, then done.

Strategy (two engines, one open position, 5-bar cooldown after any exit):
- Engine D (deep-dip swing, primary): buy 100% of equity when the close
  is >= 130 bps below the trailing 120-bar high. Exits (log returns vs
  the entry decision's close): trailing stop 65 bps below the peak,
  take-profit +200 bps, or stop-loss -300 bps.
- Engine B (momentum burst, secondary): buy 100% when the 5-min log
  return is >= 80 bps or the 10-min log return is >= 80 bps AND the
  close is above SMA120. Exits: trailing stop 100 bps below the peak,
  or take-profit +100 bps.
- Insurance: if no trade has ever filled by bar 1380, buy 5% of equity
  (keeps the >=1-trade guardrail). Its exits use the dip engine's
  wide exits.
- Warmup: no trading before 120 bars of history.

Deterministic: no wall clock, no randomness, no environment reads.
All returns are log returns (ln) in basis points.
"""

import json
import math
import sys

SYMBOL = "CBBTC"

# --- strategy parameters (basis points, log returns) ---
WARMUP_BARS = 120
DIP_LOOKBACK = 120          # trailing high window, bars before the current one
DIP_DROP_BPS = 130          # buy when close is this far below the trailing high
DIP_TRAIL_BPS = 65          # sell when close trails the peak by this much
DIP_TP_BPS = 200            # sell when up this much from entry close
DIP_SL_BPS = 300            # sell when down this much from entry close
BURST_R5_BPS = 80           # 5-min log return trigger
BURST_R10_BPS = 80          # 10-min log return trigger
SMA_LEN = 120
BURST_TRAIL_BPS = 100
BURST_TP_BPS = 100
COOLDOWN_BARS = 5
INSURANCE_BAR = 1380
INSURANCE_BPS = 500         # 5% of equity
MAX_SLIPPAGE_BPS = 100


def log_bps(new, old):
    """Log return from old to new, in basis points."""
    return math.log(new / old) * 10000.0


class Bot:
    def __init__(self, hello):
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.closes = []            # closes, oldest first, ints
        self.entry = None           # confirmed entry: dict(engine, entry_close, peak)
        self.pending = None         # buy sent, awaiting fill: same shape + client_id, seq
        self.cooldown_until = -1    # no buys while seq < this
        self.ever_traded = False    # a buy has filled at least once
        self.next_cid = 1

    def _cid(self):
        cid = "o%d" % self.next_cid
        self.next_cid += 1
        return cid

    def _sma(self, n):
        return sum(self.closes[-n:]) / float(n)

    def on_batch(self, batch):
        seq = batch["seq"]
        bars = batch.get("bars") or []
        bar = None
        for b in bars:
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        account = batch.get("account") or {}
        self.cash = int(account.get("cash_quote_units", self.cash))
        self.equity = int(account.get("equity_quote_units", self.equity))
        positions = account.get("positions") or []
        held_qty = 0
        for p in positions:
            if p.get("symbol") == SYMBOL:
                held_qty = int(p.get("qty", 0))
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)

        # Resolve a pending buy from the fills of this batch.
        if self.pending is not None:
            for f in batch.get("fills") or []:
                if f.get("client_id") == self.pending["client_id"]:
                    if f.get("status") == "filled":
                        self.entry = {
                            "engine": self.pending["engine"],
                            "entry_close": self.pending["entry_close"],
                            "peak": max(self.pending["entry_close"], close),
                        }
                        self.ever_traded = True
                    self.pending = None
                    break

        # Safety net: a position exists but we hold no entry record
        # (e.g. a sell was rejected after we cleared the record).
        # Re-derive with the wide dip exits from the current close.
        if held_qty > 0 and self.entry is None and self.pending is None:
            self.entry = {"engine": "dip", "entry_close": close, "peak": close}

        # Holding: check exits against the latest close.
        if held_qty > 0 and self.entry is not None:
            eng = self.entry["engine"]
            entry_close = self.entry["entry_close"]
            if close > self.entry["peak"]:
                self.entry["peak"] = close
            peak = self.entry["peak"]
            if eng == "dip":
                trail, tp, sl = DIP_TRAIL_BPS, DIP_TP_BPS, DIP_SL_BPS
            else:
                trail, tp, sl = BURST_TRAIL_BPS, BURST_TP_BPS, None
            fire = False
            if log_bps(close, peak) <= -trail:
                fire = True
            elif log_bps(close, entry_close) >= tp:
                fire = True
            elif sl is not None and log_bps(close, entry_close) <= -sl:
                fire = True
            if fire:
                self.entry = None
                self.cooldown_until = seq + COOLDOWN_BARS
                return [{
                    "side": "sell",
                    "client_id": self._cid(),
                    "symbol": SYMBOL,
                    "qty": str(held_qty),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # Flat: look for an entry. Last batch takes no orders.
        if seq >= 1439:
            return []
        if len(self.closes) < WARMUP_BARS + 1:
            return []
        if seq < self.cooldown_until:
            return []
        if self.pending is not None:
            return []

        # Insurance: no filled trade by bar 1380 -> buy 5%.
        if seq == INSURANCE_BAR and not self.ever_traded:
            notional = self.cash * INSURANCE_BPS // 10000
            if notional >= 1000000:
                cid = self._cid()
                self.pending = {
                    "engine": "dip",
                    "entry_close": close,
                    "peak": close,
                    "client_id": cid,
                    "seq": seq,
                }
                return [{
                    "side": "buy",
                    "client_id": cid,
                    "symbol": SYMBOL,
                    "notional_quote_units": str(notional),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # Engine D: deep dip below the trailing 120-bar high.
        if len(self.closes) > DIP_LOOKBACK:
            high = max(self.closes[-DIP_LOOKBACK - 1:-1])
            if log_bps(high, close) >= DIP_DROP_BPS:
                cid = self._cid()
                self.pending = {
                    "engine": "dip",
                    "entry_close": close,
                    "peak": close,
                    "client_id": cid,
                    "seq": seq,
                }
                return [{
                    "side": "buy",
                    "client_id": cid,
                    "symbol": SYMBOL,
                    "notional_quote_units": str(self.cash),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]

        # Engine B: momentum burst above SMA120.
        if len(self.closes) >= SMA_LEN:
            r5 = log_bps(close, self.closes[-6])
            r10 = log_bps(close, self.closes[-11])
            if (r5 >= BURST_R5_BPS or r10 >= BURST_R10_BPS) and close > self._sma(SMA_LEN):
                cid = self._cid()
                self.pending = {
                    "engine": "burst",
                    "entry_close": close,
                    "peak": close,
                    "client_id": cid,
                    "seq": seq,
                }
                return [{
                    "side": "buy",
                    "client_id": cid,
                    "symbol": SYMBOL,
                    "notional_quote_units": str(self.cash),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
        return []


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line.decode("utf-8"))
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            orders = bot.on_batch(msg)
            out.write(json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":"), ensure_ascii=False) + "\n")
            out.flush()
        elif kind == "done":
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
