# backtest.py  —  Web UI for the Backtest Strategy Builder
"""
backtest.py  —  Web UI for the Backtest Strategy Builder
──────────────────────────────────────────────────────────
Run this instead of backtest_ask.py.
It opens a browser at http://localhost:5050 automatically.
"""

import json
import sys
import os
import threading
import webbrowser
import traceback
import queue
import time
from flask import Flask, jsonify, request, send_from_directory

try:
    from utils import make_candle_rule, run_backtest, save_strategy, load_all_strategies
    from utils_get_data import (
        get_historical,
        BROKER_ZERODHA,
        SUPPORTED_BROKERS,
        get_broker_from_creds,
    )
except ImportError as e:
    print(f"\n[!] Import error: {e}")
    print("    Make sure backtest.py is in the same folder as utils.py and utils_get_data.py")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
#  STOCK LIST – LOADED FROM JSON FILE (only Zerodha tokens, NSE exchange)
# ─────────────────────────────────────────────────────────────────────────────
WATCHED_STOCKS_FILE = "all_stocks.json"

def load_watched_stocks():
    """Load symbol -> token mapping from JSON file."""
    if not os.path.exists(WATCHED_STOCKS_FILE):
        print(f"[!] Warning: {WATCHED_STOCKS_FILE} not found. No stocks available.")
        return {}
    try:
        with open(WATCHED_STOCKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"[!] Warning: {WATCHED_STOCKS_FILE} must contain a JSON object. Found {type(data)}")
            return {}
        # Validate each symbol's token is a string
        for sym, tok in data.items():
            if not isinstance(tok, str):
                print(f"[!] Warning: token for {sym} is not a string: {tok}")
                return {}
        return data
    except json.JSONDecodeError as e:
        print(f"[!] Warning: invalid JSON in {WATCHED_STOCKS_FILE}: {e}")
        return {}
    except Exception as e:
        print(f"[!] Warning: could not read {WATCHED_STOCKS_FILE}: {e}")
        return {}

def get_stock_exchange_token(symbol, broker):
    """
    Given a symbol and a broker name, return (exchange, token).
    Only Zerodha + NSE is supported (the JSON file contains NSE tokens).
    """
    stocks = load_watched_stocks()
    token = stocks.get(symbol)
    if token is None:
        return None
    # Only Zerodha is allowed
    if broker.lower() != "zerodha":
        raise ValueError(f"Only 'zerodha' broker is supported. Received '{broker}'")
    return ("NSE", token)

# Compatibility alias (the old code used _resolve_stock)
_resolve_stock = get_stock_exchange_token


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS & DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
BROKER = "zerodha"          # fixed – only Zerodha supported

DEFAULTS = {
    "strategy_name":   "my_strategy",
    "days_of_data":    500,
    "candle_interval": 15,
    "bb_period":       20,
    "bb_stddev":       2.0,
    "direction":       "long",
    "hold_candles":    1,
}

FILTERS = [
    ( 1,  "Green candle",                                         "color_green",                    "flag",   None),
    ( 2,  "Red candle",                                           "color_red",                      "flag",   None),
    ( 3,  "Upper wick at least X times the body",                 "upper_wick_x_body_min",          "float",  None),
    ( 4,  "Upper wick at most X times the body",                  "upper_wick_x_body_max",          "float",  None),
    ( 5,  "Lower wick at least X times the body",                 "lower_wick_x_body_min",          "float",  None),
    ( 6,  "Lower wick at most X times the body",                  "lower_wick_x_body_max",          "float",  None),
    ( 7,  "Body at least X times the previous candle",            "body_x_prev_min",                "float",  None),
    ( 8,  "Body at most X times the previous candle",             "body_x_prev_max",                "float",  None),
    ( 9,  "Close above upper Bollinger Band",                     "close_above_upper_band",         "flag",   None),
    (10,  "Close below lower Bollinger Band",                     "close_below_lower_band",         "flag",   None),
    (11,  "Close above middle Bollinger Band",                    "close_above_middle_band",        "flag",   None),
    (12,  "Close below middle Bollinger Band",                    "close_below_middle_band",        "flag",   None),
    (13,  "Low touched below lower Bollinger Band",               "low_below_lower_band",           "flag",   None),
    (14,  "High touched above upper Bollinger Band",              "high_above_upper_band",          "flag",   None),
    (15,  "Opened above upper Bollinger Band",                    "open_above_upper_band",          "flag",   None),
    (16,  "Opened below lower Bollinger Band",                    "open_below_lower_band",          "flag",   None),
    (17,  "Entire candle within Bollinger Bands",                 "whole_candle_within_bands",      "flag",   None),
    (18,  "Open and close within Bollinger Bands",                "body_within_bands",              "flag",   None),
    (19,  "Volume higher than previous candle",                   "volume_gt_prev_candle",          "flag",   None),
    (20,  "Volume lower than previous candle",                    "volume_lt_prev_candle",          "flag",   None),
    (21,  "Volume at least X times the previous candle",          "volume_x_prev_min",              "float",  None),
    (22,  "Candle gained at least X% (open to close)",            "candle_change_pct_min",          "float",  None),
    (23,  "Candle gained at most X% (open to close)",             "candle_change_pct_max",          "float",  None),
    (24,  "Moved at least X% from previous close",               "change_vs_prev_close_pct_min",   "float",  None),
    (25,  "Moved at most X% from previous close",                "change_vs_prev_close_pct_max",   "float",  None),
    (26,  "Gapped up at least X% from previous close",           "gap_up_pct_min",                 "float",  None),
    (27,  "Gapped down at least X% from previous close",         "gap_down_pct_min",               "float",  None),
]


app = Flask(__name__, static_folder="ui_static")

_backtest_running = False


# ═══════════════════════════════════════════════════════════════════
#  SAFE JSON SERIALISER
# ═══════════════════════════════════════════════════════════════════

def _safe_value(v):
    """Convert a single value to a JSON-safe primitive."""
    import math as _math
    if isinstance(v, float):
        if _math.isnan(v):
            return None
        if _math.isinf(v):
            return 1e308 if v > 0 else -1e308
    return v

def _sanitise_dict(d):
    """Recursively sanitise all values in a dict (or list of dicts)."""
    if isinstance(d, dict):
        return {k: _sanitise_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_sanitise_dict(item) for item in d]
    return _safe_value(d)

def _safe_jsonify(payload):
    safe = _sanitise_dict(payload)
    try:
        return app.response_class(
            response=json.dumps(safe),
            status=200,
            mimetype="application/json",
        )
    except (TypeError, ValueError) as e:
        print(f"\n[!] _safe_jsonify: still cannot serialise after sanitising: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════
#  TERMINAL LOGGING HELPERS
# ═══════════════════════════════════════════════════════════════════

def _log_section(title):
    bar = "═" * 60
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)

def _log_error(context, exc, tb_str=None):
    bar  = "─" * 60
    barE = "!" * 60
    print(f"\n{barE}")
    print(f"  ❌  ERROR  in  {context}")
    print(barE)
    print(f"  Type    : {type(exc).__name__}")
    print(f"  Message : {exc}")
    if tb_str:
        print(f"\n  — Traceback ——————————————————————————————————————")
        for line in tb_str.strip().splitlines():
            print(f"    {line}")
    print(barE + "\n")

def _log_stock_start(symbol, exchange, token, broker):
    print(f"\n  ┌─ {symbol}  ({broker.upper()}  {exchange}:{token})")

def _log_stock_ok(symbol, total_trades):
    print(f"  └─ ✓ {symbol}  →  {total_trades} trade(s) found")

def _log_stock_error(symbol, exc, tb_str):
    print(f"  └─ ✗ {symbol}  →  {type(exc).__name__}: {exc}")
    print(f"       — Traceback ─────────────────────────────────────")
    for line in tb_str.strip().splitlines():
        print(f"         {line}")
    print(f"       ──────────────────────────────────────────────────")


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def _effective_broker(override=None):
    # Only Zerodha supported
    return "zerodha"

def _serialise_trades(trades):
    safe_trades = []
    for idx, t in enumerate(trades):
        try:
            if hasattr(t, "_asdict"):
                td = t._asdict()
            elif isinstance(t, dict):
                td = dict(t)
            else:
                td = {"raw": str(t)}
            td = _sanitise_dict(td)
            safe_trades.append(td)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n  [!] Could not serialise trade #{idx}: {e}")
            print(tb)
            safe_trades.append({"error": f"serialisation failed: {e}", "index": idx})
    return safe_trades


# ═══════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("ui_static", "index.html")

@app.route("/api/config")
def api_config():
    filters_out = []
    for num, label, key, itype, choices in FILTERS:
        filters_out.append({
            "id":      num,
            "label":   label,
            "key":     key,
            "type":    itype,
            "choices": choices or [],
        })

    # Build stock list from JSON file
    stocks_out = []
    for sym, token in load_watched_stocks().items():
        stocks_out.append({
            "symbol": sym,
            "brokers": {
                "zerodha": {"exchange": "NSE", "token": token}
            }
        })

    return jsonify({
        "broker":            "ZERODHA",
        "supported_brokers": ["ZERODHA"],   # only Zerodha
        "stocks":            stocks_out,
        "defaults":          DEFAULTS,
        "filters":           filters_out,
    })

@app.route("/api/strategies")
def api_strategies():
    try:
        strategies = load_all_strategies()
        return jsonify({"strategies": strategies})
    except Exception as e:
        tb = traceback.format_exc()
        _log_error("/api/strategies", e, tb)
        return jsonify({"error": str(e), "strategies": []}), 500

@app.route("/api/run", methods=["POST"])
def api_run():
    global _backtest_running

    if _backtest_running:
        msg = "A backtest is already running. Please wait."
        print(f"\n[!] /api/run blocked: {msg}")
        return jsonify({"error": msg}), 429

    try:
        body = request.get_json(force=True)
    except Exception as e:
        tb = traceback.format_exc()
        _log_error("/api/run — parsing request JSON", e, tb)
        return jsonify({"error": f"Could not parse request body: {e}"}), 400

    if not body:
        print("\n[!] /api/run: received empty or non-JSON body")
        return jsonify({"error": "No JSON body received."}), 400

    _log_section("BACKTEST REQUEST RECEIVED")
    print(f"  Strategy  : {body.get('strategy_name', DEFAULTS['strategy_name'])}")
    print(f"  Direction : {body.get('direction', DEFAULTS['direction'])}")
    print(f"  Hold      : {body.get('hold_candles', DEFAULTS['hold_candles'])} candle(s)")
    print(f"  BB period : {body.get('bb_period', DEFAULTS['bb_period'])}")
    print(f"  BB stddev : {body.get('bb_stddev', DEFAULTS['bb_stddev'])}")
    print(f"  Candle rules in pattern: {len(body.get('candles', [{}]))}")

    # Broker is always Zerodha
    broker = "zerodha"
    print(f"  Broker    : {broker.upper()}")

    # Resolve stocks from JSON file
    requested = body.get("stocks", list(load_watched_stocks().keys()))
    print(f"  Stocks    : {requested}")

    stocks_to_run = {}
    no_token_stocks = []
    for sym in requested:
        resolved = get_stock_exchange_token(sym, broker)
        if resolved is None:
            no_token_stocks.append(sym)
            continue
        stocks_to_run[sym] = resolved

    if no_token_stocks:
        print(f"  [Warning] Skipped (no token): {no_token_stocks}")

    if not stocks_to_run:
        msg = "No valid stocks found. Please add tokens to watched_stocks.json."
        print(f"\n[!] /api/run: {msg}")
        return jsonify({"error": msg}), 400

    # Build pattern candle rules
    try:
        raw_candles = body.get("candles", [{}])
        pattern_candles = []
        for rc in raw_candles:
            clean = {k: v for k, v in rc.items() if v is not None and v != ""}
            pattern_candles.append(make_candle_rule(**clean))
        print(f"  Pattern candles built successfully ({len(pattern_candles)} rule(s))")
    except Exception as e:
        tb = traceback.format_exc()
        _log_error("/api/run — building pattern candles", e, tb)
        return jsonify({"error": f"Invalid candle rule: {e}"}), 400

    # Assemble strategy dict
    try:
        strategy = {
            "name": body.get("strategy_name", DEFAULTS["strategy_name"]),
            "interval": int(body.get("candle_interval", DEFAULTS["candle_interval"])),
            "bb_period": int(body.get("bb_period", DEFAULTS["bb_period"])),
            "bb_stddev": float(body.get("bb_stddev", DEFAULTS["bb_stddev"])),
            "pattern_candles": pattern_candles,
            "hold_candles": int(body.get("hold_candles", DEFAULTS["hold_candles"])),
            "direction": body.get("direction", DEFAULTS["direction"]),
            "days": int(body.get("days_of_data", DEFAULTS["days_of_data"])),
        }
    except (ValueError, TypeError) as e:
        tb = traceback.format_exc()
        _log_error("/api/run — assembling strategy", e, tb)
        return jsonify({"error": f"Strategy parameter error: {e}"}), 400

    # Run backtests
    results = []
    _backtest_running = True

    _log_section(f"RUNNING BACKTEST  ·  {len(stocks_to_run)} stock(s)")

    try:
        for symbol, (exchange, token) in stocks_to_run.items():
            _log_stock_start(symbol, exchange, token, broker)

            result_entry = {
                "symbol":   symbol,
                "exchange": exchange,
                "broker":   broker.upper(),
                "error":    None,
                "summary":  None,
                "trades":   [],
            }

            try:
                print(f"  │   Fetching candle data …")
                candles = get_historical(
                    exchange=exchange,
                    stock_token_id=token,
                    days=strategy["days"],
                    interval=strategy["interval"],
                    bb_period=strategy["bb_period"],
                    bb_stddev=strategy["bb_stddev"],
                    broker=broker,
                )

                result_entry["candles_fetched"] = len(candles)
                print(f"  │   {len(candles)} candles loaded")

                print(f"  │   Running pattern match …")
                run_cfg = {
                    **strategy,
                    "exchange":       exchange,
                    "stock_token_id": token,
                }
                summary, trades = run_backtest(candles, run_cfg)
                print(f"  │   Pattern matched — {len(trades)} trade(s) found")

                print(f"  │   Serialising summary …")
                if isinstance(summary, dict):
                    safe_summary = _sanitise_dict(summary)
                else:
                    safe_summary = _sanitise_dict(vars(summary))

                print(f"  │   Serialising {len(trades)} trade(s) …")
                safe_trades = _serialise_trades(trades)

                result_entry["summary"] = safe_summary
                result_entry["trades"]  = safe_trades

                _log_stock_ok(symbol, len(trades))

            except Exception as e:
                tb = traceback.format_exc()
                _log_stock_error(symbol, e, tb)
                result_entry["error"]     = str(e)
                result_entry["traceback"] = tb

            results.append(result_entry)

    finally:
        _backtest_running = False

    # Optionally save strategy
    if body.get("save_strategy", False):
        print("\n  Saving strategy …")
        try:
            save_strategy(strategy)
            print("  ✓ Strategy saved")
        except Exception as e:
            tb = traceback.format_exc()
            _log_error("save_strategy", e, tb)

    _log_section("BACKTEST COMPLETE — sending response")

    payload = {
        "results":  results,
        "strategy": strategy,
        "broker":   broker.upper(),
    }

    try:
        _ = json.dumps(_sanitise_dict(payload))
        print("  ✓ Response payload serialisation check passed")
    except (TypeError, ValueError) as e:
        tb = traceback.format_exc()
        _log_error("JSON serialisation of response payload", e, tb)
        for r in results:
            try:
                json.dumps(_sanitise_dict(r))
            except Exception as inner:
                print(f"\n  [!] Problem result entry → symbol: {r.get('symbol', '?')}")
                print(f"      Error: {inner}")
        return jsonify({
            "error":    f"Server could not serialise response: {e}",
            "detail":   "See terminal for full traceback.",
        }), 500

    print(f"  Total result entries: {len(results)}")
    for r in results:
        trades_count  = len(r.get("trades", []))
        summary_keys  = list(r.get("summary", {}).keys()) if r.get("summary") else []
        print(
            f"    {r['symbol']:<12}  trades={trades_count:<5}  "
            f"error={r['error'] is not None}  "
            f"summary_keys={len(summary_keys)}"
        )

    return _safe_jsonify(payload)


# ═══════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════

PORT = 5050

def _open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")

if __name__ == "__main__":
    os.makedirs("ui_static", exist_ok=True)

    print(f"\n{'═' * 54}")
    print(f"  Backtest UI  —  starting on http://localhost:{PORT}")
    print(f"  (Only Zerodha broker supported)")
    print(f"  Stock list from: {WATCHED_STOCKS_FILE}")
    print(f"  Press Ctrl+C to stop.")
    print(f"{'═' * 54}\n")

    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
