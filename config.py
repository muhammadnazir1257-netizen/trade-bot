"""Central configuration for the multi-strategy trading system.

Every tunable parameter lives here. Strategy modules and the signal engine
import from this file. Edit deliberately — these values directly shape
position sizing and risk.
"""

from __future__ import annotations

# --- Signal aggregation ----------------------------------------------------
# Lower consensus = more trades; higher pattern boost = patterns weigh more.
# Max-aggression baseline (paper-only): wider participation, faster execution.
CONSENSUS_THRESHOLD = 0.55           # was 0.60 — fire on weaker but still real majorities
PATTERN_BOOST_MAX = 0.15             # Cap on how much chart patterns can shift composite confidence

# --- Position sizing (Kelly-ish, max-aggression) ---------------------------
# Stops, kill switch, and cash reserve are UNCHANGED — only the offense moves.
BASE_POSITION_PCT = 0.04             # was 0.02 — 4% baseline per trade
MAX_POSITION_PCT = 0.08              # was 0.04 — 8% for high-confidence in trending regime
MIN_POSITION_PCT = 0.015             # was 0.01 — slightly larger floor in HIGH_VOLATILITY
CASH_RESERVE_PCT = 0.20              # SACRED — always keep 20% cash

# --- Risk management ---------------------------------------------------------
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ IMMUTABLE RISK FLOOR — no strategy, tuning pass, or "improvement" may     ║
# ║ loosen these. Enforced OUTSIDE strategy code (trade.validate_order +      ║
# ║ kill_switch), so a bad strategy cannot bypass them:                       ║
# ║   • Position cap: min(5% global, per-symbol max_allocation_pct)           ║
# ║   • Cash reserve: ≥ 20% of equity at all times                            ║
# ║   • Max open positions: MAX_OPEN_POSITIONS distinct symbols               ║
# ║   • Daily-loss kill switch: MAX_DAILY_LOSS_PCT halts all trading          ║
# ║   • Limit orders only; market orders are rejected                         ║
# ║   • Gross exposure ≤ MAX_GROSS_EXPOSURE_PCT of equity                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝
MAX_OPEN_POSITIONS = 8               # distinct symbols with open positions (dust excluded)

# Exit params validated by replay (scripts/exit_replay.py, 2026-07-02):
# 57 real entries were re-simulated on historical 5-min bars under 9 exit
# ladders. The tight always-on 0.5×ATR trail beat every "give it room"
# variant (+1.43% cum / PF 1.29 vs -0.5%..-5.8% / PF<1.0), and was the only
# ladder positive in the trending half of the sample. These entries have
# tiny favorable excursions — the scalper trail monetizes them; looser
# exits let them decay into full stops. Do NOT loosen these without
# re-running the replay grid on fresh data.
STOP_LOSS_PCT = 0.008                # 0.8% hard stop (strategy can override)
TRAILING_STOP_ATR_MULT = 0.5         # tight scalper trail — replay-validated
TRAIL_ACTIVATION_R = 0.0             # trail arms immediately
BREAKEVEN_AT_R = 0.5                 # stop to entry at +0.5R (no-op vs tight trail, cheap insurance)
CLOSE_RETRY_MINUTES = 15             # unfilled close order older than this → cancel & re-place
TIME_STOP_MINUTES = 90               # Exit if not moving in our direction after 90 min
TIME_STOP_MIN_MOVE_PCT = 0.005       # "Not moving" = less than 0.5% favorable

# --- Entry time-of-day filter (replay-validated 2026-07-02) ----------------
# MFE analysis of all 57 real entries: open-window entries averaged
# +0.317%/trade (median MFE 1.70%); midday entries -0.078%/trade (median MFE
# 0.44%), negative in BOTH the trending and ranging halves of the sample.
# Composite confidence had no predictive power midday (high-conf midday
# entries lost MORE). Skipping midday lifted replay cum P&L +1.43% → +3.69%.
# Equities only; position management still runs during the window.
MIDDAY_NO_ENTRY = True
MIDDAY_START_ET = "11:00"
MIDDAY_END_ET   = "14:00"

# --- Long / short ----------------------------------------------------------
SHORTING_ENABLED = True              # Allow SELL composites to open short positions
MAX_GROSS_EXPOSURE_PCT = 1.5         # Cap on gross |long|+|short| exposure (150% of equity)

# --- Data quality ----------------------------------------------------------
MAX_BAR_STALENESS_MINUTES = 20       # Skip new entries if latest bar older than this (during market hours)

# --- Backtest realism ------------------------------------------------------
COMMISSION_PER_SHARE = 0.0           # Alpaca equities are commission-free
SLIPPAGE_BPS = 5.0                   # Modeled slippage per side, in basis points (0.05%)

# --- Correlation-aware exposure -------------------------------------------
CORRELATION_LOOKBACK_DAYS = 60       # Daily-return window for the correlation matrix
CORRELATION_THRESHOLD = 0.70         # |corr| >= this groups symbols into one cluster
MAX_CLUSTER_EXPOSURE_PCT = 0.10      # Max combined exposure (% equity) to one correlated cluster

# --- Self-learning (adaptive strategy selection) ---------------------------
# The accuracy tracker records WIN/LOSS per strategy on every close. These
# knobs turn that history into automatic promotion/benching of strategies.
SELF_LEARNING_ENABLED = True
MIN_TRADES_TO_JUDGE = 8              # was 10 — judge sooner so winners get amplified faster
AUTO_BENCH_WINRATE = 0.45            # was 0.40 — kill underperforming strategies more aggressively
AUTO_BENCH_PROFIT_FACTOR = 1.0       # was 0.9 — require PF >= 1.0 to keep voting
EDGE_WEIGHT_MAX = 3.0                # was 2.0 — concentrate harder on proven winners
APPLY_OPTIMIZED_PARAMS = True        # Load models/optimized_params.json into strategies at runtime
MAX_DAILY_TRADES_AGGRESSIVE = 20     # informational — pair with MAX_DAILY_TRADES below if cranking

# --- Multi-universe / crypto ----------------------------------------------
CRYPTO_ENABLED = True
CRYPTO_DATA_URL = "https://data.alpaca.markets"   # v1beta3/crypto/us/bars
CRYPTO_TIME_IN_FORCE = "gtc"        # crypto does not support "day"
ALLOW_FRACTIONAL = True             # fractional qty for crypto (and notional equities)

# --- Out-of-sample validation ---------------------------------------------
OOS_SPLIT_FRACTION = 0.70           # First 70% of bars = in-sample, last 30% = out-of-sample
EDGE_MIN_WINRATE = 0.55             # "Has edge" gate for the discovery report
EDGE_MIN_PROFIT_FACTOR = 1.30

# --- External data providers (Finnhub + Alpha Vantage) --------------------
# Defensive: skip NEW entries on any equity with earnings inside this window
# (binary events are the single biggest avoidable loss source).
EARNINGS_BLACKOUT_HOURS = 48
# Sentiment-aware confidence boost: cap how much news sentiment can shift the
# composite. Combined with chart-pattern boost.
SENTIMENT_BOOST_MAX = 0.10
SENTIMENT_MIN_ARTICLES = 3          # Need >= N relevant articles before trusting the score
# Scheduled macro events (FOMC/CPI — dates published in advance, see
# external_data.MACRO_EVENTS): new entries within this window are SIZE-HALVED,
# not blocked. Symbol earnings remain a hard 48h block.
MACRO_EVENT_REDUCE_HOURS = 24
MACRO_EVENT_SIZE_FACTOR = 0.5
# Per-provider cache TTLs (file-cached under models/external_cache/)
EARNINGS_CACHE_TTL_HOURS = 24
SENTIMENT_CACHE_TTL_HOURS = 6
EXTERNAL_CACHE_DIR = "models/external_cache"

# --- Intraday loop ---------------------------------------------------------
POLL_INTERVAL_SECONDS = 60           # Long-running loop sleep between iterations
ORB_MINUTES = 30                     # Opening Range duration (9:30–10:00 ET)
CLOSE_POSITIONS_BEFORE = "15:30"     # ET — force-close swing-disabled positions after this
MAX_DAILY_TRADES = 20                # was 10 — max-aggression cap (paper-only baseline)
MAX_DAILY_LOSS_PCT = 0.03            # Kill switch threshold: halt if day's loss > 3%
OVERNIGHT_ALLOWED = False            # Close every position before market close
GAP_AGAINST_THRESHOLD_PCT = 0.02     # If position gaps >2% against us at open, flatten before loop

# --- Strategy default weights (regime + accuracy further modulate these) ---
STRATEGY_WEIGHTS = {
    "vwap_reversion":    1.0,
    "momentum_breakout": 1.0,
    "ema_crossover":     1.0,
    "rsi_divergence":    0.8,
    "volume_profile":    0.9,
    "squeeze_momentum":  0.8,
    "support_resistance": 1.0,
}

# --- Regime-specific multipliers (applied on top of STRATEGY_WEIGHTS) ------
REGIME_WEIGHT_MULTIPLIERS = {
    "TRENDING_UP": {
        "momentum_breakout": 1.5,
        "ema_crossover":     1.4,
        "vwap_reversion":    0.6,   # mean reversion struggles in strong trends
        "support_resistance": 1.0,
    },
    "TRENDING_DOWN": {
        # Cash preservation mode — only SELL signals are honored in aggregator
        "momentum_breakout": 1.2,
        "ema_crossover":     1.2,
        "vwap_reversion":    0.5,
    },
    "RANGING": {
        "vwap_reversion":     1.5,
        "support_resistance": 1.4,
        "volume_profile":     1.3,
        "momentum_breakout":  0.5,
        "ema_crossover":      0.6,
    },
    "HIGH_VOLATILITY": {
        # Only mean-reversion survives high VIX; everything else gets damped
        "vwap_reversion":     1.0,
        "momentum_breakout":  0.2,
        "ema_crossover":      0.2,
        "rsi_divergence":     0.3,
        "volume_profile":     0.5,
        "squeeze_momentum":   0.3,
        "support_resistance": 0.5,
    },
}

# --- Indicator defaults (any strategy may override locally) ----------------
EMA_FAST  = 8
EMA_MID   = 21
EMA_SLOW  = 55
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
BB_PERIOD  = 20
BB_STDEV   = 2.0
KC_PERIOD  = 20
KC_ATR_MULT = 1.5
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25
ADX_RANGE_THRESHOLD = 20

# --- Volume profile --------------------------------------------------------
VP_VALUE_AREA_PCT = 0.70             # 70% of volume defines VAH/VAL
VP_BIN_COUNT      = 50               # discretization for price histogram

# --- File locations (relative to project root) -----------------------------
ACCURACY_TRACKER_PATH    = "models/accuracy_tracker.json"
BACKTEST_RESULTS_PATH    = "models/backtest_results.json"
OPTIMIZED_PARAMS_PATH    = "models/optimized_params.json"
OPENING_RANGES_PATH      = "models/opening_ranges.json"
INTRADAY_LOG_DIR         = "intraday_log"
HEARTBEAT_PATH           = "heartbeat.json"
WATCHLIST_PATH           = "watchlist.json"
JOURNAL_DIR              = "journal"

# --- Watchdog (dead-man's switch) -------------------------------------------
# The June 2026 phantom halt went unnoticed for 4 days. The watchdog emails
# when the heartbeat goes stale or the kill switch engages.
WATCHDOG_STALE_MINUTES = 30          # heartbeat older than this → alert
WATCHDOG_ALERT_COOLDOWN_HOURS = 6    # max one email per condition per window

# --- Accuracy tracker tuning ----------------------------------------------
ACCURACY_BASELINE = 0.5              # Strategies with no history get 0.5 (neutral)
ACCURACY_DECAY = 0.95                # Exponential decay on past wins/losses per day (recency weighting)
ACCURACY_MIN_TRADES = 5              # Require at least N closed trades before tracker influences weight
