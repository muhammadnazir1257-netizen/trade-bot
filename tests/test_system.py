"""Regression tests for the trading system's decision logic.

Every test here encodes behavior that was validated the hard way — by a live
incident or by the exit-replay harness. If one of these fails after a change,
the change re-introduces a known-bad behavior. Pure logic only: no network,
no Alpaca calls, no file writes outside tmp.

Run:  .venv/Scripts/python.exe -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import config                      # noqa: E402
import kill_switch                 # noqa: E402
import signal_engine               # noqa: E402
import intraday_monitor as im      # noqa: E402
import trade                       # noqa: E402
import analytics                   # noqa: E402
import exit_replay                 # noqa: E402


# --- kill_switch: the June 4 phantom-halt incident ---------------------------


class TestKillSwitch:
    """check_daily_loss writes heartbeat.json when it trips — point it at a
    temp file so tests can NEVER halt the production bot. (The watchdog
    caught exactly that happening on this suite's first run.)"""

    def setup_method(self, method):
        import tempfile
        self._real_path = config.HEARTBEAT_PATH
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        self._tmp.write("{}")
        self._tmp.close()
        # kill_switch joins _ROOT with this, so give it an absolute escape
        config.HEARTBEAT_PATH = os.path.relpath(self._tmp.name, _ROOT)

    def teardown_method(self, method):
        config.HEARTBEAT_PATH = self._real_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_zero_equity_hiccup_does_not_halt(self):
        """2026-06-04: API returned equity=0 for one tick; bot halted 4 days."""
        r = kill_switch.check_daily_loss(99941.74, 0.0)
        assert r["halted"] is False

    def test_negative_equity_does_not_halt(self):
        assert kill_switch.check_daily_loss(100000, -5)["halted"] is False

    def test_implausible_drop_does_not_halt(self):
        """>50% intraday without leverage = data corruption, not a loss."""
        assert kill_switch.check_daily_loss(100000, 40000)["halted"] is False

    def test_real_breach_still_halts(self):
        r = kill_switch.check_daily_loss(100000, 96500)
        assert r["halted"] is True and abs(r["loss_pct"] - 0.035) < 1e-9

    def test_within_tolerance_does_not_halt(self):
        assert kill_switch.check_daily_loss(100000, 98000)["halted"] is False

    def test_gain_does_not_halt(self):
        assert kill_switch.check_daily_loss(100000, 100500)["halted"] is False


# --- monitor_position: replay-validated tight-trail exit ladder --------------


def _long_pos(price: float, entry: float = 100.0, qty: float = 10):
    return {"qty": qty, "avg_entry_price": entry, "current_price": price}


class TestExitLadder:
    def test_trail_arms_immediately_with_default_config(self):
        """TRAIL_ACTIVATION_R=0.0: peak inits at entry, so the trail must arm
        on the first tick (the replay-validated scalper behavior)."""
        meta = {"peak_price": 100.0,
                "entry_time": datetime.now(timezone.utc).isoformat()}
        d = im.monitor_position("T", _long_pos(100.1), {"c": 100.1}, 0.4, meta)
        assert d == "HOLD"
        assert meta.get("trailing_stop") is not None

    def test_tight_trail_scratches_quickly(self):
        """A small pullback exits at a scratch, not a full -0.8% stop."""
        meta = {"peak_price": 100.0,
                "entry_time": datetime.now(timezone.utc).isoformat()}
        im.monitor_position("T", _long_pos(100.1), {"c": 100.1}, 0.4, meta)
        d = im.monitor_position("T", _long_pos(99.88), {"c": 99.88}, 0.4, meta)
        assert d == "CLOSE_STOP"

    def test_take_profit_fires(self):
        meta = {"stop_loss": 99.0, "take_profit": 103.0,
                "entry_time": datetime.now(timezone.utc).isoformat()}
        d = im.monitor_position("T", _long_pos(103.1), {"c": 103.1}, 0.4, meta)
        assert d == "CLOSE_PROFIT"

    def test_hard_stop_fires(self):
        meta = {"stop_loss": 99.0,
                "entry_time": datetime.now(timezone.utc).isoformat()}
        d = im.monitor_position("T", _long_pos(98.9), {"c": 98.9}, 0.0, meta)
        assert d == "CLOSE_STOP"

    def test_time_stop_fires_on_stagnant_position(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
        meta = {"stop_loss": 99.0, "entry_time": old, "peak_price": 100.0}
        d = im.monitor_position("T", _long_pos(100.05), {"c": 100.05}, 0.0, meta)
        assert d == "CLOSE_TIME"

    def test_short_side_mirrors(self):
        meta = {"stop_loss": 101.0, "take_profit": 97.0, "trough_price": 100.0,
                "entry_time": datetime.now(timezone.utc).isoformat()}
        pos = {"qty": -10, "avg_entry_price": 100.0, "current_price": 96.9}
        d = im.monitor_position("T", pos, {"c": 96.9}, 0.4, meta)
        assert d == "CLOSE_PROFIT"

    def test_dust_qty_holds(self):
        meta: dict = {}
        pos = {"qty": 0, "avg_entry_price": 100.0, "current_price": 100.0}
        assert im.monitor_position("T", pos, {"c": 100.0}, 0.4, meta) == "HOLD"


# --- exit_replay.simulate: deterministic ladders ------------------------------


def _bar(h, l, c, t="2026-07-02T15:00:00Z"):
    return {"h": h, "l": l, "c": c, "t": t}


class TestReplaySimulate:
    def test_hard_stop_hit(self):
        bars = [_bar(100.2, 99.0, 99.1)]
        r = exit_replay.simulate(100.0, "long", bars, atr=10.0, model="old")
        assert r["exit"] == "STOP"

    def test_profit_target_hit(self):
        bars = [_bar(103.0, 99.95, 102.9)]
        r = exit_replay.simulate(
            100.0, "long", bars, atr=100.0,
            model={"trail_mult": 50.0, "activation_r": 99.0, "breakeven_r": None})
        assert r["exit"] == "PROFIT"

    def test_stop_beats_target_intrabar(self):
        """Conservative assumption: if a bar spans both, the stop is first."""
        bars = [_bar(103.0, 99.0, 101.0)]
        r = exit_replay.simulate(100.0, "long", bars, atr=10.0, model="old")
        assert r["exit"] == "STOP"

    def test_eod_exit_when_nothing_hits(self):
        bars = [_bar(100.6, 99.9, 100.5)] * 5
        r = exit_replay.simulate(
            100.0, "long", bars, atr=100.0,
            model={"trail_mult": 50.0, "activation_r": 99.0, "breakeven_r": None})
        assert r["exit"] == "EOD" and abs(r["pnl"] - 0.005) < 1e-9


# --- signal_engine: self-learning multipliers ---------------------------------


def _tracker(side: str, wins: int, losses: int) -> dict:
    trades = []
    for i in range(wins):
        trades.append({"symbol": f"W{i}", "ts": f"2026-06-01T{i:02d}:00:00",
                       "signal": side, "pnl_pct": 0.005})
    for i in range(losses):
        trades.append({"symbol": f"L{i}", "ts": f"2026-06-02T{i:02d}:00:00",
                       "signal": side, "pnl_pct": -0.005})
    return {"strat": {"trades": trades}}


class TestSideMultiplier:
    def test_neutral_below_min_sample(self):
        assert signal_engine._side_multiplier(_tracker("BUY", 5, 5), "BUY") == 1.0

    def test_weak_side_clamped_at_floor(self):
        acc = _tracker("BUY", 6, 24)   # 20% win rate over 30
        assert signal_engine._side_multiplier(acc, "BUY") == 0.80

    def test_strong_side_clamped_at_ceiling(self):
        acc = _tracker("SELL", 27, 3)  # 90% win rate over 30
        assert signal_engine._side_multiplier(acc, "SELL") == 1.15

    def test_duplicate_rows_deduped(self):
        """One close credits every voting strategy — copies must count once."""
        acc = _tracker("BUY", 10, 10)
        acc["strat2"] = {"trades": list(acc["strat"]["trades"])}
        # 20 unique trades duplicated across two strategies -> still 20 -> active
        m = signal_engine._side_multiplier(acc, "BUY")
        assert m == 1.0  # 50% win rate -> neutral, but NOT skipped for n<20

    def test_empty_tracker_neutral(self):
        assert signal_engine._side_multiplier({}, "BUY") == 1.0


class TestAccuracyMultiplier:
    def test_benched_strategy_gets_probation_weight(self):
        """Bench = probation (small exploration weight), NOT 0.0: zero weight
        froze records forever — by 7/6 it had silenced 5 of 7 strategies."""
        rec = {"wins": 3, "losses": 7,
               "trades": [{"pnl_pct": 0.001}] * 3 + [{"pnl_pct": -0.002}] * 7}
        m = signal_engine._accuracy_multiplier(rec)
        assert m == config.PROBATION_WEIGHT
        assert 0.0 < m < 0.5, "probation must be small but nonzero"

    def test_neutral_below_min_trades(self):
        rec = {"wins": 1, "losses": 1,
               "trades": [{"pnl_pct": 0.001}, {"pnl_pct": -0.001}]}
        assert signal_engine._accuracy_multiplier(rec) == 1.0


# --- midday window: replay-validated no-entry gate ----------------------------


class TestMiddayWindow:
    def _at(self, h, m):
        class _Fake:
            def time(self):
                return time(h, m)
        real = im._now_et
        im._now_et = lambda: _Fake()
        try:
            return im._in_midday_window()
        finally:
            im._now_et = real

    def test_boundaries(self):
        assert self._at(10, 59) is False
        assert self._at(11, 0) is True
        assert self._at(12, 30) is True
        assert self._at(13, 59) is True
        assert self._at(14, 0) is False


# --- trade.validate_order: the hard trading rules -----------------------------


_WATCHLIST = {"watchlist": [
    {"symbol": "SPY", "max_allocation_pct": 5},
    {"symbol": "MARA", "max_allocation_pct": 5},
]}


class TestValidateOrder:
    """validate_order consults the live exchange clock (Rule 5) — stub it so
    the rule checks themselves are testable at any hour."""

    def setup_method(self):
        self._real_status = trade.get_market_status
        self._real_account = trade._get_account
        trade.get_market_status = lambda: {"is_open": True,
                                           "next_open": "", "next_close": ""}
        trade._get_account = lambda: {"cash": 100000.0}

    def teardown_method(self):
        trade.get_market_status = self._real_status
        trade._get_account = self._real_account

    def test_position_cap_rejected(self):
        ok, why = trade.validate_order("SPY", 100, "buy", 100.0, 100000, [], _WATCHLIST)
        assert ok is False and "cap" in why.lower()

    def test_reasonable_buy_accepted(self):
        ok, why = trade.validate_order("SPY", 30, "buy", 100.0, 100000, [], _WATCHLIST)
        assert ok is True, why

    def test_zero_qty_rejected(self):
        ok, _ = trade.validate_order("SPY", 0, "buy", 100.0, 100000, [], _WATCHLIST)
        assert ok is False

    def test_market_closed_rejected(self):
        trade.get_market_status = lambda: {"is_open": False,
                                           "next_open": "", "next_close": ""}
        ok, why = trade.validate_order("SPY", 30, "buy", 100.0, 100000, [], _WATCHLIST)
        assert ok is False and "closed" in why.lower()

    def test_cash_reserve_enforced(self):
        # 20% of 100k equity = 20k reserve; with 22k cash, a 3.2k buy
        # would leave 18.8k — below the floor, must reject.
        trade._get_account = lambda: {"cash": 22000.0}
        ok, why = trade.validate_order("SPY", 32, "buy", 100.0, 100000,
                                       [], _WATCHLIST)
        assert ok is False and "reserve" in why.lower()


# --- analytics: stat math ------------------------------------------------------


class TestAnalytics:
    def test_bucket_stats_known_values(self):
        trades = [{"pnl_pct": 0.01}, {"pnl_pct": 0.01}, {"pnl_pct": -0.01},
                  {"pnl_pct": -0.005}]
        s = analytics._bucket_stats(trades)
        assert s["n"] == 4 and s["wins"] == 2
        assert abs(s["win_rate"] - 0.5) < 1e-9
        assert abs(s["profit_factor"] - (0.02 / 0.015)) < 1e-9

    def test_return_metrics_drawdown(self):
        curve = [{"date": f"d{i}", "equity": e}
                 for i, e in enumerate([100, 110, 99, 104, 108, 103])]
        m = analytics.return_metrics(curve)
        assert abs(m["max_drawdown_pct"] - (-10.0)) < 1e-6
        assert abs(m["total_return_pct"] - 3.0) < 1e-6

    def test_return_metrics_empty(self):
        assert analytics.return_metrics([])["sharpe"] is None


# --- Phase 1-3 additions (2026-07-03) -----------------------------------------


import external_data  # noqa: E402
import signal_engine as _se  # noqa: E402


class TestSizingCapClamp:
    """Sizing must clamp to the SAME cap validate_order enforces — sizing
    above it silently discards valid entries via rejection (AAPL 6/3 live)."""

    _WL = {"max_single_position_pct": 5,
           "watchlist": [{"symbol": "AAPL", "max_allocation_pct": 8},
                          {"symbol": "MARA", "max_allocation_pct": 4}]}
    _ACCT = {"equity": 100000.0, "cash": 100000.0}

    def test_global_cap_binds(self):
        sig = {"confidence": 0.9, "entry_price": 100.0}
        r = _se.calculate_position_size(sig, self._ACCT, "TRENDING_UP", 0.5, "AAPL", self._WL)
        assert r["pct_of_equity"] <= 0.05 + 1e-9

    def test_symbol_cap_binds_when_tighter(self):
        sig = {"confidence": 0.9, "entry_price": 100.0}
        r = _se.calculate_position_size(sig, self._ACCT, "TRENDING_UP", 0.1, "MARA", self._WL)
        assert r["pct_of_equity"] <= 0.04 + 1e-9

    def test_sizing_uses_live_equity(self):
        """Compounding: same signal sizes larger on a larger account."""
        sig = {"confidence": 0.5, "entry_price": 100.0}
        small = _se.calculate_position_size(sig, {"equity": 50000.0, "cash": 50000.0},
                                            "RANGING", 0.1, "AAPL", self._WL)
        large = _se.calculate_position_size(sig, {"equity": 200000.0, "cash": 200000.0},
                                            "RANGING", 0.1, "AAPL", self._WL)
        assert large["notional"] > small["notional"] * 3.5


class TestMaxOpenPositions:
    def setup_method(self):
        self._real_status = trade.get_market_status
        self._real_account = trade._get_account
        trade.get_market_status = lambda: {"is_open": True, "next_open": "", "next_close": ""}
        trade._get_account = lambda: {"cash": 100000.0}
        self._wl = {"max_single_position_pct": 5,
                    "watchlist": [{"symbol": "SPY", "max_allocation_pct": 5}]}

    def teardown_method(self):
        trade.get_market_status = self._real_status
        trade._get_account = self._real_account

    def test_new_symbol_rejected_at_cap(self):
        positions = [{"symbol": f"P{i}", "qty": 10, "market_value": 1000.0} for i in range(8)]
        ok, why = trade.validate_order("SPY", 10, "buy", 100.0, 100000, positions, self._wl)
        assert not ok and "Max-open-positions" in why

    def test_add_to_held_symbol_allowed_at_cap(self):
        positions = ([{"symbol": f"P{i}", "qty": 10, "market_value": 1000.0} for i in range(7)]
                     + [{"symbol": "SPY", "qty": 5, "market_value": 500.0}])
        ok, _ = trade.validate_order("SPY", 10, "buy", 100.0, 100000, positions, self._wl)
        assert ok

    def test_dust_not_counted(self):
        positions = ([{"symbol": f"P{i}", "qty": 10, "market_value": 1000.0} for i in range(7)]
                     + [{"symbol": "BTCUSD", "qty": 1e-9, "market_value": 0.0}])
        ok, _ = trade.validate_order("SPY", 10, "buy", 100.0, 100000, positions, self._wl)
        assert ok


class TestExternalDataSignals:
    def test_stocktwits_symbol_mapping(self):
        assert external_data._stocktwits_symbol("BTC/USD") == "BTC.X"
        assert external_data._stocktwits_symbol("aapl") == "AAPL"

    def test_macro_event_window(self):
        from datetime import datetime, timezone, timedelta
        saved = external_data.MACRO_EVENTS
        try:
            soon = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")
            far = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
            external_data.MACRO_EVENTS = [{"date": far, "event": "CPI release"}]
            assert external_data.upcoming_macro_event(24) is None
            external_data.MACRO_EVENTS = [{"date": soon, "event": "FOMC rate decision"}]
            ev = external_data.upcoming_macro_event(48)
            # Event at 14:00 UTC on that date — may be inside or outside the
            # 48h window depending on time of day; widen to make deterministic
            ev = external_data.upcoming_macro_event(72)
            assert ev is not None and ev["event"] == "FOMC rate decision"
        finally:
            external_data.MACRO_EVENTS = saved

    def test_combined_sentiment_fails_open(self):
        saved_av = external_data.news_sentiment
        saved_st = external_data.stocktwits_sentiment
        saved_news = external_data.alpaca_news_count
        try:
            external_data.news_sentiment = lambda s: 0.0
            external_data.stocktwits_sentiment = lambda s: {"value": 0.0, "confidence": 0.0}
            external_data.alpaca_news_count = lambda s: 0
            out = external_data.combined_sentiment("TEST")
            assert out["value"] == 0.0 and out["confidence"] == 0.0
            assert "asof" in out
        finally:
            external_data.news_sentiment = saved_av
            external_data.stocktwits_sentiment = saved_st
            external_data.alpaca_news_count = saved_news

    def test_combined_sentiment_blends(self):
        saved_av = external_data.news_sentiment
        saved_st = external_data.stocktwits_sentiment
        saved_news = external_data.alpaca_news_count
        try:
            external_data.news_sentiment = lambda s: 0.5
            external_data.stocktwits_sentiment = lambda s: {"value": -0.5, "confidence": 0.6}
            external_data.alpaca_news_count = lambda s: 10
            out = external_data.combined_sentiment("TEST")
            assert -1.0 <= out["value"] <= 1.0
            assert 0.0 < out["confidence"] <= 1.0
        finally:
            external_data.news_sentiment = saved_av
            external_data.stocktwits_sentiment = saved_st
            external_data.alpaca_news_count = saved_news


class TestRiskAdjustedWeighting:
    def _rec(self, pnls):
        return {"wins": sum(1 for p in pnls if p > 0),
                "losses": sum(1 for p in pnls if p <= 0),
                "trades": [{"pnl_pct": p} for p in pnls]}

    def test_low_variance_beats_high_variance_at_same_win_rate(self):
        steady = self._rec([0.004, 0.004, 0.004, -0.003, 0.004,
                            -0.003, 0.004, 0.004, -0.003, 0.004])
        choppy = self._rec([0.02, -0.015, 0.018, -0.012, 0.02,
                            -0.016, 0.019, 0.02, -0.014, 0.001])
        assert _se._accuracy_multiplier(steady) > _se._accuracy_multiplier(choppy)

    def test_multiplier_capped_at_edge_weight_max(self):
        perfect = self._rec([0.005] * 12)
        assert _se._accuracy_multiplier(perfect) <= config.EDGE_WEIGHT_MAX


# --- Self-modification safety (2026-07-03) -------------------------------------
# The adaptive-override channel is the ONLY way the program changes its own
# behavior. These tests are the contract: whitelisted keys clamp to bounds,
# risk-floor keys are structurally impossible to override.


class TestAdaptiveOverrides:
    def _apply(self, overrides: dict) -> dict:
        import json, tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"overrides": overrides, "evidence": "unit test"}, tmp)
        tmp.close()
        saved_path = config.ADAPTIVE_PARAMS_PATH
        saved_vals = {k: getattr(config, k) for k in config.ADAPTIVE_WHITELIST}
        saved_risk = {k: getattr(config, k) for k in
                      ("CASH_RESERVE_PCT", "MAX_DAILY_LOSS_PCT", "STOP_LOSS_PCT",
                       "MAX_OPEN_POSITIONS", "MAX_GROSS_EXPOSURE_PCT")}
        try:
            config.ADAPTIVE_PARAMS_PATH = os.path.relpath(
                tmp.name, os.path.dirname(os.path.abspath(config.__file__)))
            applied = config._apply_adaptive_overrides()
            current_risk = {k: getattr(config, k) for k in saved_risk}
            return {"applied": applied, "risk_before": saved_risk,
                    "risk_after": current_risk}
        finally:
            config.ADAPTIVE_PARAMS_PATH = saved_path
            for k, v in saved_vals.items():
                setattr(config, k, v)
            for k, v in saved_risk.items():
                setattr(config, k, v)
            os.unlink(tmp.name)

    def test_whitelisted_key_applies_within_bounds(self):
        r = self._apply({"TRAILING_STOP_ATR_MULT": 0.75})
        assert r["applied"].get("TRAILING_STOP_ATR_MULT") == 0.75

    def test_out_of_bounds_value_is_clamped(self):
        r = self._apply({"TRAILING_STOP_ATR_MULT": 99.0})
        assert r["applied"].get("TRAILING_STOP_ATR_MULT") == 2.0  # whitelist max

    def test_risk_floor_keys_are_refused(self):
        """THE critical test: the self-modification channel must be
        structurally unable to touch the risk floor."""
        r = self._apply({
            "CASH_RESERVE_PCT": 0.0,        # try to remove the cash reserve
            "MAX_DAILY_LOSS_PCT": 1.0,      # try to disable the kill switch
            "STOP_LOSS_PCT": 0.5,           # try to widen stops 60x
            "MAX_OPEN_POSITIONS": 999,      # try to remove the position cap
            "MAX_GROSS_EXPOSURE_PCT": 50.0, # try to allow 50x leverage
        })
        assert r["applied"] == {}, "no risk key may ever apply"
        assert r["risk_after"] == r["risk_before"], "risk floor must be untouched"

    def test_unknown_keys_ignored(self):
        r = self._apply({"TOTALLY_MADE_UP_KEY": 123})
        assert r["applied"] == {}

    def test_int_keys_stay_int(self):
        r = self._apply({"TIME_STOP_MINUTES": 120.7})
        assert r["applied"].get("TIME_STOP_MINUTES") == 121
        assert isinstance(r["applied"]["TIME_STOP_MINUTES"], int)


class TestRollingLearningWindow:
    def _rec_from_pnls(self, pnls):
        return {"wins": sum(1 for p in pnls if p > 0),
                "losses": sum(1 for p in pnls if p <= 0),
                "trades": [{"pnl_pct": p} for p in pnls]}

    def test_recovered_strategy_comes_off_the_bench(self):
        """Old regime: 10 losses. Recent regime: 10 solid wins. Lifetime
        counters say 50% — but the WINDOW must judge the recent form."""
        saved = config.ROLLING_LEARNING_WINDOW
        try:
            config.ROLLING_LEARNING_WINDOW = 10
            pnls = [-0.005] * 10 + [0.004] * 10   # window sees only the wins
            rec = self._rec_from_pnls(pnls)
            assert signal_engine._accuracy_multiplier(rec) > 1.0
        finally:
            config.ROLLING_LEARNING_WINDOW = saved

    def test_decayed_strategy_gets_benched_despite_good_lifetime(self):
        saved = config.ROLLING_LEARNING_WINDOW
        try:
            config.ROLLING_LEARNING_WINDOW = 10
            pnls = [0.004] * 10 + [-0.005] * 10   # window sees only the losses
            rec = self._rec_from_pnls(pnls)
            assert signal_engine._accuracy_multiplier(rec) == config.PROBATION_WEIGHT
        finally:
            config.ROLLING_LEARNING_WINDOW = saved


# --- Firm-analyst fixes (2026-07-07) --------------------------------------------


class TestRiskBasedSizing:
    _WL = {"max_single_position_pct": 5,
           "watchlist": [{"symbol": "SPY", "max_allocation_pct": 15}]}
    _ACCT = {"equity": 100000.0, "cash": 100000.0}

    def test_wide_stop_means_small_position(self):
        """Risk sizing binds when the stop is wide: with a 0.25% risk budget,
        an 8%-away stop caps notional at ~3.1% of equity (below the 4% base);
        a 2% stop leaves the notional cap binding instead. Either way the
        loss at the stop can never exceed the risk budget + cap geometry."""
        sig_wide = {"confidence": 0.5, "entry_price": 100.0, "stop_loss": 92.0}   # 8% away
        sig_tight = {"confidence": 0.5, "entry_price": 100.0, "stop_loss": 98.0}  # 2% away
        wide = signal_engine.calculate_position_size(
            sig_wide, self._ACCT, "RANGING", 0.5, "SPY", self._WL)
        tight = signal_engine.calculate_position_size(
            sig_tight, self._ACCT, "RANGING", 0.5, "SPY", self._WL)
        assert wide["notional"] < tight["notional"], \
            "wider stop must produce the smaller position"
        # Loss if the wide trade stops out ~ 0.25% of equity, not 0.32% (4% x 8%)
        risk_at_stop = wide["notional"] * 0.08
        assert risk_at_stop <= 0.0026 * 100000 + 1e-6

    def test_risk_sizing_never_exceeds_notional_caps(self):
        sig = {"confidence": 0.9, "entry_price": 100.0, "stop_loss": 99.9}  # paper-thin... 0.1%
        r = signal_engine.calculate_position_size(
            sig, self._ACCT, "TRENDING_UP", 0.5, "SPY", self._WL)
        assert r["pct_of_equity"] <= 0.05 + 1e-9, "caps must bound risk sizing"

    def test_no_stop_falls_back_to_notional(self):
        sig = {"confidence": 0.5, "entry_price": 100.0}
        r = signal_engine.calculate_position_size(
            sig, self._ACCT, "RANGING", 0.5, "SPY", self._WL)
        assert r["notional"] > 0 and "notional" in r["reason"]


class TestProbationKeepsLearning:
    def test_benched_voter_still_reaches_owners(self):
        """The death-spiral fix: a benched strategy voting on the winning side
        must appear in opening_strategies (weight > 0) so its record keeps
        accruing evidence."""
        benched_trades = [{"pnl_pct": 0.001}] * 3 + [{"pnl_pct": -0.002}] * 7
        import json, tempfile
        tracker = {"strat_benched": {"wins": 3, "losses": 7, "trades": benched_trades}}
        saved = signal_engine._load_accuracy_tracker
        try:
            signal_engine._load_accuracy_tracker = lambda: tracker
            signals = [
                {"strategy": "strat_benched", "signal": "SELL", "confidence": 0.9,
                 "reason": "t", "entry_price": 100.0, "stop_loss": 101.0, "take_profit": 97.0},
                {"strategy": "strat_new", "signal": "SELL", "confidence": 0.9,
                 "reason": "t", "entry_price": 100.0, "stop_loss": 101.0, "take_profit": 97.0},
            ]
            out = signal_engine.aggregate_signals(signals, "RANGING", {}, 0.0)
            assert out["signal"] == "SELL"
            assert "strat_benched" in out.get("opening_strategies", []), \
                "probationed strategy must keep earning evidence"
        finally:
            signal_engine._load_accuracy_tracker = saved
