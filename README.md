[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/albethhpkl-cmyk/btc-race-v2-rebuild/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/albethhpkl-cmyk/btc-race-v2-rebuild/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

# BTC Race Bot — Fresh Rebuild (23 Nov 2026)

Rebuilt from scratch in a fresh session that started after race
`btc-race-24h-20261123-0511` opened (23 Nov 2026 05:11 UTC), from the
strategy documents only. No code was copied from the previous build
(the previous bot was written 19 Nov, before this race opened, so its
sessions can never pass the transcript gate for this race).

## Strategy

Long-only swing bot over 1-minute BTC bars, one open position at a time.
Parameters were tuned in this session against the race kit's frozen dev
window (1,440 bars), within the documented strategy family:

- **Engine D — deep-dip swing (primary).** Buy 100% of cash when the
  close is at least 130 bps below the trailing 120-bar high (max close
  of the previous 120 bars). Exit on the first of: trailing stop −65 bps
  from the peak close since entry, take-profit +200 bps vs entry close,
  stop-loss −300 bps vs entry close.
- **Engine B — momentum burst (secondary).** Buy 100% of cash when the
  5-bar log return is at least +80 bps, or the 10-bar return is at least
  +80 bps, and the close is above the 120-bar simple moving average.
  Exit on the first of: trailing stop −100 bps from the peak close
  since entry, take-profit +100 bps vs entry close.
- **Shared rules.** 120-bar warmup (no trades before bar 120).
  5-bar cooldown after any exit. At most one open position. If no
  trade has ever filled by bar 1380, a 5% insurance buy is attempted
  (keeps the ≥1-trade guardrail).
- **Pricing.** All thresholds are log returns (ln) in basis points,
  measured on bar closes. Fills arrive one bar after the decision, at
  the next bar's open (harness latency 1).

Why these numbers: the documented 180 bps dip threshold fires on only
1 of the 1,440 dev bars (max dip on this window is 183 bps), so a bot
using it never trades the dip engine and loses money on momentum alone
(−$11.84). The 130 bps threshold keeps the same deep-dip idea but
actually fires on this window; the whole neighbourhood (dip 110–130,
burst r10 80–100) is positive on the dev window (median +$25.89 across
16 nearby combos), so the pick is a broad positive region, not a
knife-edge peak.

## Interface

`main.py` speaks the `agent-fun/bars-replay/v1` line protocol
(`hello` / `batch` / `done` over stdin/stdout), stdlib only,
deterministic: identical input produces byte-identical actions.

## Files

- `main.py` — the bot.
- `replay.py` — local replay driver: loads the kit's frozen dev
  window (1,440 bars), ports the kit harness's exact integer fill
  model (4 bps fee, 2 bps slippage, single-hop constant-product
  impact vs bar open depth; verified against the kit's
  `sim/bars/engine.ts`), enforces the harness's decision checks,
  flattens at the last close, and reports PnL / trades / win rate /
  max drawdown / fees. Run: `python3 replay.py [--runs N]`.
- `LICENSE` — MIT.

## Validation (local replay, kit dev window)

`python3 replay.py --runs 2` (23 Nov 2026, this session):

- **Net PnL +$35.43 USDC** (return +3.54%, final cash $1,035.43) —
  passes the dev gate (net PnL > 0 required to race).
- **8 trades, 4 round trips, win rate 100%** (+$6.45, +$9.24, +$1.09,
  +$18.65; 2 burst trades, 2 dip trades).
- **Max drawdown 1.08%** (guardrail is 25%).
- **Fees $3.25** (flatten fee $0.00).
- **Deterministic: action logs byte-identical across both runs.**

Note: the old bot's +$17.55 / 2-trade profile was measured on the
18 Nov dev window (different data), so it is not directly comparable;
what matters is this bot is positive on THIS race's dev window.
The race window itself is unseen — dev-window profit does not
guarantee race-window profit.
