"""Central configuration for the multi-strategy trading system.

Every tunable parameter lives here. Strategy modules and the signal engine
import from this file. Edit deliberately — these values directly shape
position sizing and risk.
"""

from __future__ import annotations

# --- Signal aggregation ----------------------------------------------------
CONSENSUS_THRESHOLD = 0.60           # Min weighted vote (BUY or SELL share) to emit a signal
PATTERN_BOOST_MAX = 0.15             # Cap on how much chart patterns can shift composite confidence

# --- Position sizing (Kelly-ish) -------------------------------------------
BASE_POSITION_PCT = 0.02             # 2% of portfolio per trade (default)
MAX_POSITION_PCT = 0.04              # 4% on high-confidence trades (confidence >= 0.80 + trending regime)
MIN_POSITION_PCT = 0.01              # 1% in HIGH_VOLATILITY regime
CASH_RESERVE_PCT = 0.20              # Always keep 20% cash

# --- Risk management -------------------------------------------------------
STOP_LOSS_PCT = 0.008                # 0.8% hard stop (strategy can override)
TRAILING_STOP_ATR_MULT = 0.5         # Trail by 0.5 × ATR
TIME_STOP_MINUTES = 90               # Exit if not moving in our direction after 90 min
TIME_STOP_MIN_MOVE_PCT = 0.005       # "Not moving" = less than 0.5% favorable

# --- Intraday loop ---------------------------------------------------------
POLL_INTERVAL_SECONDS = 60           # Long-running loop sleep between iterations
ORB_MINUTES = 30                     # Opening Range duration (9:30–10:00 ET)
CLOSE_POSITIONS_BEFORE = "15:30"     # ET — force-close swing-disabled positions after this
MAX_DAILY_TRADES = 10                # Per-day cap to prevent overtrading
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

# --- Accuracy tracker tuning ----------------------------------------------
ACCURACY_BASELINE = 0.5              # Strategies with no history get 0.5 (neutral)
ACCURACY_DECAY = 0.95                # Exponential decay on past wins/losses per day (recency weighting)
ACCURACY_MIN_TRADES = 5              # Require at least N closed trades before tracker influences weight
