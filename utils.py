import json
import math
import datetime
import urllib.request
import urllib.parse
from pathlib import Path

from utils_get_data import (
    load_creds
)


def add_bollinger_bands(candles, period, stddev_mult):
    if period <= 0:
        raise ValueError(f"[BB] ERROR: period must be > 0, got {period}.")
    if stddev_mult <= 0:
        raise ValueError(f"[BB] ERROR: std-dev multiplier must be > 0, got {stddev_mult}.")

    closes = [c["close"] for c in candles]
    for i, candle in enumerate(candles):
        if i < period - 1:
            candle["bb_upper"] = candle["bb_middle"] = candle["bb_lower"] = None
        else:
            window = closes[i - period + 1: i + 1]
            mean   = sum(window) / period
            sd     = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
            candle["bb_middle"] = round(mean, 4)
            candle["bb_upper"]  = round(mean + stddev_mult * sd, 4)
            candle["bb_lower"]  = round(mean - stddev_mult * sd, 4)
    return candles


def empty_candle_rule():
    return {
        # ── Color — two independent on/off flags ─────────────────
        "color_green":                  None,
        "color_red":                    None,
        # ── Wick ratios ───────────────────────────────────────────
        "upper_wick_x_body_min":        None,
        "upper_wick_x_body_max":        None,
        "lower_wick_x_body_min":        None,
        "lower_wick_x_body_max":        None,
        "body_x_prev_min":              None,
        "body_x_prev_max":              None,
        # ── Bollinger Band checks ─────────────────────────────────
        "close_above_upper_band":       None,
        "close_below_lower_band":       None,
        "close_above_middle_band":      None,
        "close_below_middle_band":      None,
        "low_below_lower_band":         None,
        "high_above_upper_band":        None,
        "open_above_upper_band":        None,
        "open_below_lower_band":        None,
        "whole_candle_within_bands":    None,
        "body_within_bands":            None,
        # ── Volume ───────────────────────────────────────────────
        "volume_gt_prev_candle":        None,
        "volume_lt_prev_candle":        None,
        "volume_x_prev_min":            None,
        # ── % change ─────────────────────────────────────────────
        "candle_change_pct_min":        None,
        "candle_change_pct_max":        None,
        "change_vs_prev_close_pct_min": None,
        "change_vs_prev_close_pct_max": None,
        # ── Gap ──────────────────────────────────────────────────
        "gap_up_pct_min":               None,
        "gap_down_pct_min":             None,
    }


def make_candle_rule(**overrides):
    rule = empty_candle_rule()
    for k, v in overrides.items():
        if k not in rule:
            raise ValueError(
                f"[Rule] ERROR: Unknown candle rule key: '{k}'.\n"
                f"  → Valid keys are: {list(rule.keys())}"
            )
        rule[k] = v
    return rule


def _candle_passes(candle, rule, prev_candle):
    body       = candle["body_length"]
    open_      = candle["open"]
    close_     = candle["close"]
    high_      = candle["high"]
    low_       = candle["low"]

    bb_u = candle.get("bb_upper")
    bb_m = candle.get("bb_middle")
    bb_l = candle.get("bb_lower")

    pv         = prev_candle["volume"]       if prev_candle else None
    pb         = prev_candle["body_length"]  if prev_candle else None
    prev_close = prev_candle["close"]        if prev_candle else None

    candle_change_pct = (
        round((close_ - open_) / open_ * 100, 6) if open_ != 0 else None
    )
    change_vs_prev_close_pct = (
        round((close_ - prev_close) / prev_close * 100, 6)
        if (prev_close is not None and prev_close != 0) else None
    )
    gap_up_pct = (
        round((open_ - prev_close) / prev_close * 100, 6)
        if (prev_close is not None and prev_close != 0) else None
    )
    gap_down_pct = (
        round((prev_close - open_) / prev_close * 100, 6)
        if (prev_close is not None and prev_close != 0) else None
    )

    def bb(flag, condition_fn):
        if flag is None:
            return True
        if bb_u is None:
            return False
        return condition_fn()

    def whole_candle_within():
        if rule["whole_candle_within_bands"] is None:
            return True
        if bb_u is None:
            return False
        return candle["high"] <= bb_u and candle["low"] >= bb_l

    def body_within():
        if rule["body_within_bands"] is None:
            return True
        if bb_u is None:
            return False
        lo = min(open_, close_)
        hi = max(open_, close_)
        return lo >= bb_l and hi <= bb_u

    def pct_min(rule_val, computed_pct):
        if rule_val is None:
            return True
        if computed_pct is None:
            return False
        return computed_pct >= rule_val

    def pct_max(rule_val, computed_pct):
        if rule_val is None:
            return True
        if computed_pct is None:
            return False
        return computed_pct <= rule_val

    return all([
        # ── Color checks ─────────────────────────────────────────
        rule.get("color_green") is None or candle["candle_color"] == "green",
        rule.get("color_red")   is None or candle["candle_color"] == "red",
        # ── Wick / body ───────────────────────────────────────────
        rule["upper_wick_x_body_min"]  is None or (body > 0 and candle["upper_wick"] >= rule["upper_wick_x_body_min"] * body),
        rule["upper_wick_x_body_max"]  is None or (body > 0 and candle["upper_wick"] <= rule["upper_wick_x_body_max"] * body),
        rule["lower_wick_x_body_min"]  is None or (body > 0 and candle["lower_wick"] >= rule["lower_wick_x_body_min"] * body),
        rule["lower_wick_x_body_max"]  is None or (body > 0 and candle["lower_wick"] <= rule["lower_wick_x_body_max"] * body),
        rule["body_x_prev_min"]        is None or (pb is not None and pb > 0 and body >= rule["body_x_prev_min"] * pb),
        rule["body_x_prev_max"]        is None or (pb is not None and pb > 0 and body <= rule["body_x_prev_max"] * pb),
        # ── Bollinger ─────────────────────────────────────────────
        bb(rule["close_above_upper_band"],   lambda: candle["close"] > bb_u),
        bb(rule["close_below_lower_band"],   lambda: candle["close"] < bb_l),
        bb(rule["close_above_middle_band"],  lambda: candle["close"] > bb_m),
        bb(rule["close_below_middle_band"],  lambda: candle["close"] < bb_m),
        bb(rule["low_below_lower_band"],     lambda: candle["low"]   < bb_l),
        bb(rule["high_above_upper_band"],    lambda: candle["high"]  > bb_u),
        bb(rule["open_above_upper_band"],    lambda: candle["open"]  > bb_u),
        bb(rule["open_below_lower_band"],    lambda: candle["open"]  < bb_l),
        whole_candle_within(),
        body_within(),
        # ── Volume ───────────────────────────────────────────────
        rule["volume_gt_prev_candle"]  is None or (pv is not None and candle["volume"] >  pv),
        rule["volume_lt_prev_candle"]  is None or (pv is not None and candle["volume"] <  pv),
        rule["volume_x_prev_min"]      is None or (pv is not None and pv > 0 and candle["volume"] >= rule["volume_x_prev_min"] * pv),
        # ── % change ─────────────────────────────────────────────
        pct_min(rule["candle_change_pct_min"],        candle_change_pct),
        pct_max(rule["candle_change_pct_max"],        candle_change_pct),
        pct_min(rule["change_vs_prev_close_pct_min"], change_vs_prev_close_pct),
        pct_max(rule["change_vs_prev_close_pct_max"], change_vs_prev_close_pct),
        # ── Gap ──────────────────────────────────────────────────
        pct_min(rule["gap_up_pct_min"],   gap_up_pct),
        pct_min(rule["gap_down_pct_min"], gap_down_pct),
    ])


def pattern_matches(candles, end_idx, pattern_rules):
    """Check if the candle window ending at end_idx matches the full pattern."""
    pat_len      = len(pattern_rules)
    window_start = end_idx - pat_len + 1
    if window_start < 0:
        return False
    for j in range(pat_len):
        c_idx = window_start + j
        prev  = candles[c_idx - 1] if c_idx > 0 else None
        if not _candle_passes(candles[c_idx], pattern_rules[j], prev):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════
def _add_minutes(time_str, minutes):
    """Add minutes to a time string, returning same format."""
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(time_str, fmt)
            result = dt + datetime.timedelta(minutes=minutes)
            return result.strftime(fmt)
        except ValueError:
            continue
    return time_str

def run_backtest(candle_data, strategy):
    try:
        pat_len   = len(strategy["pattern_candles"])
        hold      = strategy["hold_candles"]
        direction = strategy["direction"]
    except KeyError as e:
        raise KeyError(
            f"[Backtest] ERROR: Strategy is missing required field: {e}\n"
            "  → Check that your strategy has: pattern_candles, hold_candles, direction"
        )

    if not candle_data:
        raise ValueError(
            "[Backtest] ERROR: No candle data provided. Cannot run backtest.\n"
            "  → Check that data was fetched successfully before running backtest."
        )

    if len(candle_data) < pat_len + hold:
        raise ValueError(
            f"[Backtest] ERROR: Not enough candles to backtest.\n"
            f"  → Have {len(candle_data)} candles, need at least {pat_len + hold}.\n"
            "  → Try increasing the number of history days."
        )

    trades = []
    # Index of the first candle we're allowed to use as a signal.
    # Advances to i+hold+1 after each trade to prevent overlap.
    next_allowed_signal = pat_len - 1

    for i in range(pat_len - 1, len(candle_data) - hold):

        # ── Skip if a previous trade is still running ─────────────
        if i < next_allowed_signal:
            continue

        if not pattern_matches(candle_data, i, strategy["pattern_candles"]):
            continue

        # Entry: open of the very next candle after the signal
        entry_candle = candle_data[i + 1]
        # Exit:  close of the candle `hold` candles after entry
        #        hold=1 → exit at close of i+1 (same candle as entry)
        #        hold=2 → exit at close of i+2, etc.
        exit_candle  = candle_data[i + hold]

        ep  = entry_candle["open"]
        xp  = exit_candle["close"]
        pnl = round((xp - ep) if direction == "long" else (ep - xp), 2)

        # at the top of run_backtest, get the interval in minutes
        interval_minutes = strategy.get("interval", 15)

        # then in the loop, replace the trades.append(...) with:
        # first candle of the pattern
        signal_start_candle = candle_data[i - pat_len + 1]

        # last candle of the pattern (the signal candle)
        signal_end_candle = candle_data[i]

        trades.append({
            "signal_start_time": signal_start_candle["time"],
            "signal_end_time": _add_minutes(signal_end_candle["time"], interval_minutes),
            "entry_time": entry_candle["time"],
            "exit_end_time": _add_minutes(exit_candle["time"], interval_minutes),
            "direction": direction,
            "entry_price": round(ep, 2),
            "exit_price": round(xp, 2),
            "pnl": pnl,
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT"),
        })

        # Block any new signal until this trade has fully closed
        next_allowed_signal = i + hold + 1

    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    total  = len(trades)
    nw     = len(wins)
    nl     = len(losses)
    twp    = round(sum(t["pnl"] for t in wins),        2)
    tlp    = round(abs(sum(t["pnl"] for t in losses)), 2)

    summary = {
        "strategy":      strategy["name"],
        "direction":     direction,
        "hold_candles":  hold,
        "total_trades":  total,
        "wins":          nw,
        "losses":        nl,
        "accuracy_%":    round(nw / total * 100, 2) if total else 0,
        "total_pnl":     round(sum(t["pnl"] for t in trades), 2),
        "avg_win":       round(twp / nw, 2)  if wins   else 0,
        "avg_loss":      round(tlp / nl, 2)  if losses else 0,
        "best_trade":    max((t["pnl"] for t in trades), default=0),
        "worst_trade":   min((t["pnl"] for t in trades), default=0),
        "profit_factor": round(twp / tlp, 2) if tlp   else float("inf"),
    }
    return summary, trades

# ═══════════════════════════════════════════════════════════════════
#  LIVE SIGNAL CHECK
# ═══════════════════════════════════════════════════════════════════

def check_live_signal(candles, strategy):
    pat_len = len(strategy["pattern_candles"])
    if len(candles) < pat_len:
        return None
    end = len(candles) - 1
    return candles[end] if pattern_matches(candles, end, strategy["pattern_candles"]) else None


# ═══════════════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════════════

def print_report(summary, trades):
    W = 58

    def row(label, value):
        print(f"  {label:<30} {str(value):>20}")

    print(f"\n{'═'*W}")
    print(f"  BACKTEST REPORT  ·  {summary['strategy']}")
    print(f"{'═'*W}")
    row("Direction",      summary["direction"].upper())
    row("Hold (candles)", summary["hold_candles"])
    print(f"{'─'*W}")
    row("Total Trades",   summary["total_trades"])
    row("Wins  ✓",        summary["wins"])
    row("Losses ✗",       summary["losses"])
    row("Accuracy",       f"{summary['accuracy_%']} %")
    print(f"{'─'*W}")
    row("Total PnL",      summary["total_pnl"])
    row("Avg Win",        summary["avg_win"])
    row("Avg Loss",       summary["avg_loss"])
    row("Best Trade",     summary["best_trade"])
    row("Worst Trade",    summary["worst_trade"])
    row("Profit Factor",  summary["profit_factor"])
    print(f"{'═'*W}\n")

    if trades:
        print(f"  {'#':>4}  {'Signal Time':<20} {'Entry':>8} {'Exit':>8} {'PnL':>8}  Result")
        print(f"  {'─'*56}")
        for i, t in enumerate(trades, 1):
            tag = "✓" if t["result"] == "WIN" else ("✗" if t["result"] == "LOSS" else "–")
            print(
                f"  {i:>4}  {t['signal_time']:<20} "
                f"{t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
                f"{t['pnl']:>8.2f}  {tag} {t['result']}"
            )
        print(f"  {'─'*56}\n")


def print_comparison_report(results: list):
    COL_KEYS = [
        ("Total Trades",  "total_trades"),
        ("Wins",          "wins"),
        ("Losses",        "losses"),
        ("Accuracy %",    "accuracy_%"),
        ("Total PnL",     "total_pnl"),
        ("Avg Win",       "avg_win"),
        ("Avg Loss",      "avg_loss"),
        ("Best Trade",    "best_trade"),
        ("Worst Trade",   "worst_trade"),
        ("Profit Factor", "profit_factor"),
    ]

    W_SYM = 12
    W_COL = 13
    SEP   = "─"

    header_parts  = [f"{'Symbol':<{W_SYM}}"]
    divider_parts = [SEP * W_SYM]
    for label, _ in COL_KEYS:
        header_parts.append(f"{label:>{W_COL}}")
        divider_parts.append(SEP * W_COL)

    spacer  = "  "
    header  = spacer.join(header_parts)
    divider = spacer.join(divider_parts)
    total_w = len(divider)

    print(f"\n\n{'═' * total_w}")
    print("  COMPARATIVE PERFORMANCE ACROSS ALL STOCKS")
    print(f"{'═' * total_w}")
    print(header)
    print(divider)

    for r in results:
        sym  = r["symbol"]
        summ = r["summary"]

        if summ is None:
            row_parts = [f"{sym:<{W_SYM}}"]
            msg = "ERROR / NO DATA"
            row_parts.append(
                f"{msg:>{W_COL * len(COL_KEYS) + 2 * (len(COL_KEYS) - 1)}}"
            )
            print(spacer.join(row_parts))
            continue

        row_parts = [f"{sym:<{W_SYM}}"]
        for _, key in COL_KEYS:
            val = summ.get(key, "N/A")
            if isinstance(val, float):
                cell = f"{val:>{W_COL}.2f}"
            elif isinstance(val, int):
                cell = f"{val:>{W_COL}}"
            else:
                cell = f"{str(val):>{W_COL}}"
            row_parts.append(cell)
        print(spacer.join(row_parts))

    print(divider)

    valid = [r for r in results if r["summary"] is not None]
    if valid:
        pnl_key = "total_pnl"
        best  = max(valid, key=lambda r: r["summary"].get(pnl_key, float("-inf")))
        worst = min(valid, key=lambda r: r["summary"].get(pnl_key, float("inf")))
        print(f"\n  🏆  Best  PnL : {best['symbol']:<12}  {best['summary'].get(pnl_key, 'N/A')}")
        print(f"  ⚠️   Worst PnL : {worst['symbol']:<12}  {worst['summary'].get(pnl_key, 'N/A')}")

    print(f"{'═' * total_w}\n")


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY SAVE / LOAD
# ═══════════════════════════════════════════════════════════════════

STRATEGY_DIR = Path("./Assets/strategies")


def save_strategy(strategy, force_name=None):
    """
    Save strategy to ./Assets/strategies/<name>.json
    Called from the web UI — never prompts interactively.
    If the file already exists it is silently overwritten.
    (Interactive rename logic is preserved for CLI use via force_name.)
    """
    try:
        STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
        name = force_name or strategy["name"]

        def _path(n):
            return STRATEGY_DIR / f"{n.replace(' ', '_').replace('/', '-')}.json"

        path = _path(name)

        with open(path, "w") as f:
            json.dump(strategy, f, indent=2)
        print(f"[Save] ✓ Strategy saved → {path}")
        return path

    except PermissionError:
        raise PermissionError(
            f"[Save] ERROR: Cannot write to '{STRATEGY_DIR}'.\n"
            "  → Check folder permissions."
        )
    except Exception as e:
        raise RuntimeError(f"[Save] ERROR saving strategy: {e}")


def load_all_strategies():
    """
    Load and return all saved strategies from ./Assets/strategies/
    Returns a list of strategy dicts. Never raises — logs warnings instead.
    """
    try:
        STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(STRATEGY_DIR.glob("*.json"))
        if not files:
            print("[Strategy] No saved strategy files found in ./Assets/strategies/")
            return []
        strategies = []
        for f in files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                strategies.append(data)
                print(f"[Strategy] ✓ Loaded → {f.name}")
            except json.JSONDecodeError as e:
                print(f"[Strategy] WARNING: Could not parse {f.name}: {e}  (skipping)")
        return strategies
    except Exception as e:
        print(f"[Strategy] ERROR loading strategies: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════

def send_telegram(message, cred_path="All_cred.json"):
    try:
        c       = load_creds(cred_path)
        token   = c.get("telegram_bot_token", "").strip()
        chat_id = c.get("telegram_chat_id", "").strip()
        if not token or not chat_id:
            print(
                "[Notify] ERROR: 'telegram_bot_token' or 'telegram_chat_id' is missing "
                "in All_Cred.json."
            )
            return False
        url     = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        ).encode()
        resp = urllib.request.urlopen(
            urllib.request.Request(url, data=payload, method="POST"), timeout=10
        )
        result = json.loads(resp.read())
        if result.get("ok"):
            return True
        else:
            print(f"[Notify] WARNING: Telegram returned ok=False: {result}")
            return False
    except urllib.error.URLError as e:
        print(f"[Notify] ERROR: Could not reach Telegram API (network issue): {e}")
        return False
    except Exception as e:
        print(f"[Notify] ERROR sending Telegram message: {e}")
        return False


def build_alert_message(strategy, signal_candle):
    bb_u    = signal_candle.get("bb_upper")
    bb_lines = ""
    if bb_u is not None:
        bb_lines = (
            f"\n📊 <b>Bollinger Bands</b>"
            f"\n   Upper : {bb_u}"
            f"\n   Middle: {signal_candle.get('bb_middle')}"
            f"\n   Lower : {signal_candle.get('bb_lower')}"
        )
    return (
        f"🚨 <b>TRADE SIGNAL</b> 🚨"
        f"\n\n<b>Strategy :</b> {strategy['name']}"
        f"\n<b>Stock    :</b> {strategy['exchange']} : {strategy['stock_token_id']}"
        f"\n<b>Direction:</b> {'🟢 LONG' if strategy['direction'] == 'long' else '🔴 SHORT'}"
        f"\n<b>Signal at:</b> {signal_candle['time']}"
        f"\n\n📈 <b>Last Candle</b>"
        f"\n   O: {signal_candle['open']}  H: {signal_candle['high']}"
        f"\n   L: {signal_candle['low']}   C: {signal_candle['close']}"
        f"\n   Vol: {signal_candle['volume']}"
        f"{bb_lines}"
        f"\n\n⚡ Enter <b>{strategy['direction'].upper()}</b> on next candle open"
        f"\n   Hold <b>{strategy['hold_candles']}</b> candle(s)"
    )
