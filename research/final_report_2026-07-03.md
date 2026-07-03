# Final Validation Report — 2026-07-03

System: multi-strategy intraday ensemble, Alpaca paper trading.
All six phases of the 2026-07-03 hardening program are complete; this
report is the honest scorecard, not a sales pitch.

## Live performance (42 days, real paper fills)
- Equity: $99,833.50 (start $100,045.39)
- Total return: -0.21% | Max drawdown: -0.33%
- Sharpe (annualized, active days): -2.88
- Closed trades (deduped): 62 | Win rate: 45% | Payoff: 1.42 | PF: 1.17
- Expectancy: +0.0259%/trade
- Exits: {'CLOSE_STOP': 57, 'ENTRIES': 75, 'CLOSE_PROFIT': 10, 'CLOSE_FAILED': 1175, 'CLOSE_EOD': 8}

## Walk-forward out-of-sample backtest (honest fills, 5 bps slippage/side)
- 91 strategy-symbol pairs, 70/30 IS/OOS split, fill-at-close, degenerate-
  geometry signals rejected, stop-before-target intrabar.
- **OOS edges found: 0.** After the 2026-07-02 fantasy-fill fix, no pair
  clears win rate >= 55% AND PF >= 1.3 out-of-sample. The prior '4 edges'
  (e.g. volume_profile/AAPL PF 916) were simulator artifacts.
- IS vs OOS gap: in-sample PFs of 2-5 collapse to <= 1.0 OOS for most
  pairs — textbook overfit signature; this is why the walk-forward gate
  now blocks tuned params that fail the recent-window re-run.

## Per-strategy live scorecard (close-loop, deduped overall)
- ema_crossover: 2t, win 50%, PF 0.89, expectancy -0.0373%/t
- momentum_breakout: 8t, win 38%, PF 1.43, expectancy +0.0494%/t
- rsi_divergence: 12t, win 42%, PF 1.00, expectancy -0.0006%/t
- squeeze_momentum: 10t, win 60%, PF 0.68, expectancy -0.0383%/t
- support_resistance: 6t, win 50%, PF 1.58, expectancy +0.2507%/t
- volume_profile: 16t, win 44%, PF 1.39, expectancy +0.0350%/t
- vwap_reversion: 8t, win 38%, PF 0.63, expectancy -0.0490%/t

## Per-side (live)
- BUY: 23t, win 30%, PF 0.63 (side multiplier active: BUY sized down, SELL up)
- SELL: 39t, win 54%, PF 1.52 (side multiplier active: BUY sized down, SELL up)

## Immutable risk floor (enforced outside strategy code)
- Position cap: min(5% global, per-symbol) | Cash reserve: >= 20%
- Max open positions: 8 | Daily-loss kill switch: 3%
- Limit orders only | Gross exposure <= 150% of equity
- Earnings hard block 48h | Macro events (FOMC/CPI) size x0.5
- Midday no-entry gate 11:00-13:59 ET (replay-validated)

## Verdict
The engineering layer is validated: 45 regression tests, honest
backtesting, watchdog alerting, state reconciliation, retry-hardened
API. The EDGE layer is unproven: zero OOS edges in textbook strategies,
live expectancy +0.03%/trade on a 62-trade sample. The system is
correctly configured to find edge the only defensible way: live paper
trades under strict risk caps, with the accuracy tracker and analytics
deciding the next iteration at ~50 new closed trades.
