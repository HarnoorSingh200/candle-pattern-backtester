# live.py
"""
live.py  —  Live Signal Monitor + Optional Auto-Trader  (Zerodha)
──────────────────────────────────────────────────────────────────
Loads ALL saved strategies from ./Assets/strategies/
Monitors stocks from watched_stocks.json every candle interval for pattern matches
Sends Telegram alerts when a pattern is detected
Optionally places and exits orders via Zerodha Kite API

How it works
────────────
1. Load all strategies from ./Assets/strategies/*.json
2. Group strategies by candle interval
3. Every minute, check if a new candle has just closed for any interval
4. On new candle → fetch fresh data for all stocks × that interval
5. Check each stock against every strategy using that interval
6. On match → send Telegram alert
7. If live_trading=True → place entry order on next candle open,
   exit after hold_candles candles

[Rest of the docstring unchanged...]
"""

import time
import datetime
import threading
import traceback
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json

from utils import (
    check_live_signal,
    load_all_strategies,
    send_telegram,
    build_alert_message,
)

from utils_get_data import (
    login_zerodha,
    get_live,
    BROKER_ZERODHA,
)


# ── Stocks to watch ────────────────────────────────────────────────
STOCKS_FILE = "all_stocks.json"


# ════════════════════════════════════════════════════════════════════
#  MONEY MANAGEMENT SETTINGS
#  ─────────────────────────────────────────────────────────────────
#  TOTAL_CAPITAL   : Your total trading capital across ALL open trades
#                    combined.  The engine will never deploy more than
#                    this amount at any point in time.
#                    Example: 100000  means ₹1,00,000 total.
#
#  MONEY_PER_STOCK : Maximum rupee amount to allocate to a SINGLE trade
#                    on ANY stock.  The quantity is auto-calculated as:
#                        quantity = floor(MONEY_PER_STOCK / current_price)
#                    Example: 10000  means at most ₹10,000 per trade.
#
#  MONEY_PER_STOCK_MAP : Optional dict to override MONEY_PER_STOCK for
#                    specific symbols.
#                    Example: {"TCS": 20000, "INFY": 5000}
#                    Symbols NOT listed here use MONEY_PER_STOCK.
#
#  How it works before every entry order:
#    ① deployed = sum of (entry_price × quantity) for ALL open positions
#    ② remaining = TOTAL_CAPITAL - deployed
#    ③ allocated = MONEY_PER_STOCK (or per-symbol override)
#    ④ if remaining < allocated  →  order skipped; Telegram alert sent
#    ⑤ else  quantity = floor(remaining_for_this_trade / current_price)
#            (capped so spend ≤ allocated and ≤ remaining)
#       if quantity < 1  →  order skipped
# ════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  LOAD CONFIGURATION FROM JSON FILE
# ═══════════════════════════════════════════════════════════════════

CONFIG_FILE = Path("live_config.json")


def load_live_config() -> dict:
    """Load settings from live_config.json, with fallback defaults."""
    defaults = {
        "LIVE_TRADING": False,
        "TOTAL_CAPITAL": 100000,
        "MONEY_PER_STOCK": 10000,
        "MONEY_PER_STOCK_MAP": {},
        "DEFAULT_QUANTITY": 1,
        "SYMBOL_QUANTITY": {},
        "ORDER_PRODUCT": "MIS",
        "ORDER_TYPE_ENTRY": "MARKET",
        "ORDER_TYPE_EXIT": "MARKET",
        "ORDER_EXCHANGE": "NSE",
        "FETCH_DELAY_SECONDS": 3,
        "MAX_WORKERS": 3,
        "MARKET_OPEN": "09:15",
        "MARKET_CLOSE": "15:30",
        "ORDER_MAX_RETRIES": 2,
        "ORDER_RETRY_DELAY": 3,
        "TRADE_LOG_PATH": "./Assets/trade_log.json",
    }

    if not CONFIG_FILE.exists():
        print(f"\n[!] Config file {CONFIG_FILE} not found. Creating with default values.")
        with open(CONFIG_FILE, "w") as f:
            json.dump(defaults, f, indent=2)
        return defaults

    try:
        with open(CONFIG_FILE, "r") as f:
            user_config = json.load(f)
        # Merge with defaults (missing keys get default values)
        for key, val in defaults.items():
            if key not in user_config:
                user_config[key] = val
        return user_config
    except Exception as e:
        print(f"\n[!] Error loading {CONFIG_FILE}: {e}. Using defaults.")
        return defaults


# Load configuration
_config = load_live_config()

# Assign to module-level variables (so existing code works without changes)
LIVE_TRADING = _config["LIVE_TRADING"]
TOTAL_CAPITAL = _config["TOTAL_CAPITAL"]
MONEY_PER_STOCK = _config["MONEY_PER_STOCK"]
# MONEY_PER_STOCK_MAP = _config["MONEY_PER_STOCK_MAP"]
MONEY_PER_STOCK_MAP = {}
DEFAULT_QUANTITY = _config["DEFAULT_QUANTITY"]
SYMBOL_QUANTITY = _config["SYMBOL_QUANTITY"]
ORDER_PRODUCT = _config["ORDER_PRODUCT"]
ORDER_TYPE_ENTRY = _config["ORDER_TYPE_ENTRY"]
ORDER_TYPE_EXIT = _config["ORDER_TYPE_EXIT"]
ORDER_EXCHANGE = _config["ORDER_EXCHANGE"]
FETCH_DELAY_SECONDS = _config["FETCH_DELAY_SECONDS"]
MAX_WORKERS = _config["MAX_WORKERS"]
ORDER_MAX_RETRIES = _config["ORDER_MAX_RETRIES"]
ORDER_RETRY_DELAY = _config["ORDER_RETRY_DELAY"]
TRADE_LOG_PATH = Path(_config["TRADE_LOG_PATH"])

# Convert MARKET_OPEN and MARKET_CLOSE from strings to datetime.time objects
try:
    market_open_h, market_open_m = map(int, _config["MARKET_OPEN"].split(":"))
    MARKET_OPEN = datetime.time(market_open_h, market_open_m)
except:
    MARKET_OPEN = datetime.time(9, 15)
    print("[!] Invalid MARKET_OPEN format in config, using 09:15")

try:
    market_close_h, market_close_m = map(int, _config["MARKET_CLOSE"].split(":"))
    MARKET_CLOSE = datetime.time(market_close_h, market_close_m)
except:
    MARKET_CLOSE = datetime.time(15, 30)
    print("[!] Invalid MARKET_CLOSE format in config, using 15:30")

# ═══════════════════════════════════════════════════════════════════
#  LOAD STOCKS FROM JSON FILE (replaces hardcoded WATCHED_STOCKS)
# ═══════════════════════════════════════════════════════════════════

def load_watched_stocks() -> dict:
    """
    Load symbol -> token mapping from STOCKS_FILE (watched_stocks.json).
    Expected format: { "TCS": "2953217", "INFY": "408065", ... }
    Exchange is always NSE (Zerodha only).
    Returns empty dict if file missing or invalid.
    """
    if not Path(STOCKS_FILE).exists():
        print(f"\n[!] Warning: {STOCKS_FILE} not found. No stocks will be monitored.")
        return {}
    try:
        with open(STOCKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"\n[!] Warning: {STOCKS_FILE} must contain a JSON object. Found {type(data)}")
            return {}
        # Validate each symbol's token is a string
        for sym, tok in data.items():
            if not isinstance(tok, str):
                print(f"[!] Warning: token for {sym} is not a string: {tok}")
                return {}
        return data
    except json.JSONDecodeError as e:
        print(f"\n[!] Warning: invalid JSON in {STOCKS_FILE}: {e}")
        return {}
    except Exception as e:
        print(f"\n[!] Warning: could not read {STOCKS_FILE}: {e}")
        return {}

def get_stocks() -> dict:
    """
    Returns a dictionary of symbols mapped to (exchange, token) tuples.
    Exchange is always "NSE" because only Zerodha is supported.
    """
    tokens = load_watched_stocks()
    stocks = {}
    for sym, token in tokens.items():
        stocks[sym] = ("NSE", token)
    return stocks

# Global variable that holds the current stock list (loaded once at startup)
WATCHED_STOCKS = get_stocks()

# ═══════════════════════════════════════════════════════════════════
#  (Do not edit below this line unless you know what you're doing)
# ═══════════════════════════════════════════════════════════════════

W = 68   # console print width

# ── In-memory open-position tracker ───────────────────────────────
# Key  : (symbol, strategy_name)
# Value: {
#     "entry_order_id"     : str,
#     "entry_price"        : float | None,
#     "entry_time"         : datetime,
#     "exit_at"            : datetime,
#     "direction"          : "long"|"short",
#     "quantity"           : int,
#     "allocated_capital"  : float,   ← rupee amount locked for this trade
#     "strategy_name"      : str,
#     "symbol"             : str,
#     "exchange"           : str,
#     "token"              : str,
#     "interval"           : int,
#     "hold_candles"       : int,
#     "signal_candle_time" : str,
# }
_open_positions: dict = {}
_positions_lock = threading.Lock()

# ── Trade log (persisted to TRADE_LOG_PATH) ───────────────────────
_trade_log: list = []
_trade_log_lock  = threading.Lock()


# ═══════════════════════════════════════════════════════════════════
#  DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _ts() -> str:
    """Current timestamp string for log lines."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(tag: str, msg: str):
    """Formatted log line: [YYYY-MM-DD HH:MM:SS] [TAG      ] message"""
    print(f"[{_ts()}] [{tag:<10}] {msg}")


def _banner(title: str):
    print(f"\n{'═' * W}")
    print(f"  {title}")
    print(f"{'═' * W}")


# ═══════════════════════════════════════════════════════════════════
#  TRADE LOG  (persisted to disk)
# ═══════════════════════════════════════════════════════════════════

def _load_trade_log():
    """Load existing trade log from disk on startup."""
    global _trade_log
    try:
        TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if TRADE_LOG_PATH.exists():
            with open(TRADE_LOG_PATH) as f:
                _trade_log = json.load(f)
            _log("TRADELOG", f"Loaded {len(_trade_log)} existing trade records.")
        else:
            _trade_log = []
    except Exception as e:
        _log("TRADELOG", f"WARNING: Could not load trade log: {e}  — starting fresh.")
        _trade_log = []


def _append_trade_log(record: dict):
    """Append one trade record and flush to disk."""
    global _trade_log
    with _trade_log_lock:
        _trade_log.append(record)
        try:
            TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TRADE_LOG_PATH, "w") as f:
                json.dump(_trade_log, f, indent=2, default=str)
        except Exception as e:
            _log("TRADELOG", f"WARNING: Could not write trade log: {e}")


# ═══════════════════════════════════════════════════════════════════
#  MONEY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

def _is_money_management_enabled() -> bool:
    """
    Returns True when money management is active.
    Money management is considered ENABLED when TOTAL_CAPITAL > 0
    AND MONEY_PER_STOCK > 0.
    """
    return TOTAL_CAPITAL > 0 and MONEY_PER_STOCK > 0


def _get_deployed_capital() -> float:
    """
    Calculate the total capital currently locked in open positions.

    For each open position we use:
        deployed = entry_price × quantity   (if entry_price is known)
        deployed = allocated_capital         (if fill price not yet available)

    This is called while NOT holding _positions_lock — the caller must
    ensure thread safety if needed, or we acquire it here briefly.
    """
    total_deployed = 0.0
    with _positions_lock:
        for pos in _open_positions.values():
            ep  = pos.get("entry_price")
            qty = pos.get("quantity", 0)
            if ep is not None and ep > 0:
                total_deployed += ep * qty
            else:
                # Fall back to the allocated amount reserved at order time
                total_deployed += pos.get("allocated_capital", 0.0)
    return total_deployed


def _get_remaining_capital() -> float:
    """
    Returns the undeployed capital remaining.
    remaining = TOTAL_CAPITAL - deployed
    Floor at 0 so it never goes negative in display/logic.
    """
    remaining = TOTAL_CAPITAL - _get_deployed_capital()
    return max(remaining, 0.0)


def _get_allocated_money_for_symbol(symbol: str) -> float:
    """
    Return the per-trade money limit for a given symbol.
    Uses MONEY_PER_STOCK_MAP override if present, else MONEY_PER_STOCK.
    """
    return float(MONEY_PER_STOCK_MAP.get(symbol, MONEY_PER_STOCK))


def _check_capital_and_calculate_quantity(
    symbol: str,
    current_price: float,
) -> tuple[int, float, str]:
    """
    Core money-management check performed before every entry order.

    Parameters
    ──────────
    symbol        : trading symbol  (e.g. "TCS")
    current_price : latest candle close price used to size the order

    Returns
    ───────
    (quantity, allocated_amount, reason)

    quantity         : number of shares to buy  (0 = do NOT place order)
    allocated_amount : rupee amount this order will consume
    reason           : human-readable explanation (for logs / Telegram)

    Decision logic
    ──────────────
    ① remaining_capital = TOTAL_CAPITAL − currently deployed capital
    ② allocated_for_symbol = MONEY_PER_STOCK (or per-symbol override)
    ③ usable = min(remaining_capital, allocated_for_symbol)
       → if usable < allocated_for_symbol  (not enough free capital)
         → return (0, 0, reason explaining shortfall)
    ④ quantity = floor(usable / current_price)
       → if quantity < 1  → return (0, 0, "price too high")
    ⑤ return (quantity, quantity × current_price, "ok")
    """
    if not _is_money_management_enabled():
        # Money management disabled — fall back to legacy quantity settings
        qty = SYMBOL_QUANTITY.get(symbol, DEFAULT_QUANTITY)
        return qty, 0.0, "money_management_disabled"

    if current_price <= 0:
        return 0, 0.0, f"Invalid current price ({current_price}) for {symbol}."

    allocated_for_symbol = _get_allocated_money_for_symbol(symbol)
    remaining_capital    = _get_remaining_capital()
    deployed_capital     = _get_deployed_capital()

    # ── ③ Sufficient free capital? ────────────────────────────────
    if remaining_capital < allocated_for_symbol:
        reason = (
            f"Insufficient capital for {symbol}.\n"
            f"  Total capital    : ₹{TOTAL_CAPITAL:,.2f}\n"
            f"  Deployed capital : ₹{deployed_capital:,.2f}\n"
            f"  Remaining capital: ₹{remaining_capital:,.2f}\n"
            f"  Required (per trade): ₹{allocated_for_symbol:,.2f}\n"
            f"  Shortfall        : ₹{allocated_for_symbol - remaining_capital:,.2f}\n"
            f"  → Skipping order for {symbol} until capital is freed."
        )
        return 0, 0.0, reason

    # ── ④ How many shares can we buy? ────────────────────────────
    # We use the lesser of remaining_capital and the per-symbol limit
    # so we never spend more than the per-symbol cap even if we have
    # lots of free capital.
    usable_amount = min(remaining_capital, allocated_for_symbol)
    quantity      = math.floor(usable_amount / current_price)

    if quantity < 1:
        reason = (
            f"Cannot buy even 1 share of {symbol}.\n"
            f"  Current price    : ₹{current_price:,.2f}\n"
            f"  Usable capital   : ₹{usable_amount:,.2f}\n"
            f"  Minimum cost     : ₹{current_price:,.2f}\n"
            f"  → Skipping order for {symbol} — price too high for budget."
        )
        return 0, 0.0, reason

    allocated_amount = round(quantity * current_price, 2)
    reason = (
        f"OK — ₹{allocated_amount:,.2f} allocated for {symbol}  "
        f"(qty={quantity} × ₹{current_price:,.2f})  "
        f"Remaining after: ₹{remaining_capital - allocated_amount:,.2f}"
    )
    return quantity, allocated_amount, reason


def _capital_summary_line() -> str:
    """
    One-liner capital status string for log output.
    """
    if not _is_money_management_enabled():
        return "Money management: DISABLED (using fixed quantity)"
    deployed  = _get_deployed_capital()
    remaining = _get_remaining_capital()
    open_pos  = len(_open_positions)
    return (
        f"Capital: total=₹{TOTAL_CAPITAL:,.0f}  "
        f"deployed=₹{deployed:,.0f}  "
        f"remaining=₹{remaining:,.0f}  "
        f"open_positions={open_pos}"
    )


# ═══════════════════════════════════════════════════════════════════
#  CANDLE TIMING
# ═══════════════════════════════════════════════════════════════════

def _next_candle_close_time(interval_minutes: int) -> datetime.datetime:
    """
    Calculate the next datetime when a candle of `interval_minutes`
    will close and we should fetch data.

    Candles align to market open (09:15).
    We add FETCH_DELAY_SECONDS so the broker has time to finalize.

    Example for 5-min interval:
        Candle closes at: 09:15, 09:20, 09:25 …
        We fetch at:      09:15:05, 09:20:05, 09:25:05 …
    """
    now = datetime.datetime.now()

    market_open_today = now.replace(
        hour=MARKET_OPEN.hour,
        minute=MARKET_OPEN.minute,
        second=0,
        microsecond=0,
    )

    interval_secs   = interval_minutes * 60
    elapsed_secs    = (now - market_open_today).total_seconds()

    if elapsed_secs < 0:
        # Before market open — first candle closes at open + interval
        next_close = market_open_today + datetime.timedelta(minutes=interval_minutes)
    else:
        intervals_done = int(elapsed_secs // interval_secs)
        next_close     = market_open_today + datetime.timedelta(
            seconds=(intervals_done + 1) * interval_secs
        )

    return next_close + datetime.timedelta(seconds=FETCH_DELAY_SECONDS)


def _candle_open_time_after(
    reference: datetime.datetime, interval_minutes: int, n: int = 1
) -> datetime.datetime:
    """
    Return the open time of the candle that starts `n` intervals
    after `reference`.  Used to schedule entry / exit.
    """
    return reference + datetime.timedelta(minutes=interval_minutes * n)


def _seconds_until(target: datetime.datetime) -> float:
    """Seconds remaining until `target`. Negative if already past."""
    return (target - datetime.datetime.now()).total_seconds()


def _is_market_open() -> bool:
    """Return True if the current local time is within market hours."""
    now_time = datetime.datetime.now().time()
    return MARKET_OPEN <= now_time <= MARKET_CLOSE


def _interruptible_sleep(
    seconds: float,
    stop_event: threading.Event,
    chunk: float = 5.0,
):
    """
    Sleep for `seconds` total but wake every `chunk` seconds to
    check if stop_event has been set.
    """
    remaining = max(seconds, 0.0)
    while remaining > 0 and not stop_event.is_set():
        sleep_for = min(chunk, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for


# ═══════════════════════════════════════════════════════════════════
#  ZERODHA ORDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def _kite_transaction_type(direction: str):
    """
    Return the KiteConnect transaction type constant for entry / exit.

    Entry:
        long  → BUY
        short → SELL

    Exit:
        long  → SELL
        short → BUY
    """
    try:
        from kiteconnect import KiteConnect
        if direction.lower() == "long":
            return KiteConnect.TRANSACTION_TYPE_BUY
        else:
            return KiteConnect.TRANSACTION_TYPE_SELL
    except ImportError:
        return "BUY" if direction.lower() == "long" else "SELL"


def _kite_exit_transaction_type(direction: str):
    """Opposite of entry transaction type."""
    try:
        from kiteconnect import KiteConnect
        if direction.lower() == "long":
            return KiteConnect.TRANSACTION_TYPE_SELL
        else:
            return KiteConnect.TRANSACTION_TYPE_BUY
    except ImportError:
        return "SELL" if direction.lower() == "long" else "BUY"


def _kite_product(product_str: str):
    """Map ORDER_PRODUCT string to KiteConnect constant."""
    try:
        from kiteconnect import KiteConnect
        mapping = {
            "MIS":  KiteConnect.PRODUCT_MIS,
            "CNC":  KiteConnect.PRODUCT_CNC,
            "NRML": KiteConnect.PRODUCT_NRML,
        }
        return mapping.get(product_str.upper(), KiteConnect.PRODUCT_MIS)
    except ImportError:
        return product_str.upper()


def _kite_order_type(order_type_str: str):
    """Map ORDER_TYPE string to KiteConnect constant."""
    try:
        from kiteconnect import KiteConnect
        mapping = {
            "MARKET": KiteConnect.ORDER_TYPE_MARKET,
            "LIMIT":  KiteConnect.ORDER_TYPE_LIMIT,
            "SL":     KiteConnect.ORDER_TYPE_SL,
            "SL-M":   KiteConnect.ORDER_TYPE_SLM,
        }
        return mapping.get(order_type_str.upper(), KiteConnect.ORDER_TYPE_MARKET)
    except ImportError:
        return order_type_str.upper()


def _place_order_with_retry(
    api,
    symbol: str,
    exchange: str,
    transaction_type,
    quantity: int,
    order_type,
    product,
    tag: str = "",
    order_label: str = "",          # "ENTRY" or "EXIT" — for cleaner log messages
) -> str | None:
    """
    Place a Zerodha order with automatic retry on transient errors.

    Retry behaviour
    ───────────────
    • Total attempts = ORDER_MAX_RETRIES + 1
      (i.e. 1 initial try + ORDER_MAX_RETRIES retries)
    • Waits ORDER_RETRY_DELAY seconds between each attempt.
    • On total failure: logs error + sends Telegram alert.

    Returns the order_id string on success, or None on total failure.
    Does NOT raise — all exceptions are caught and logged internally.
    """
    total_attempts = ORDER_MAX_RETRIES + 1   # e.g. 2 retries → 3 total tries
    last_error     = None
    label          = order_label or tag or "ORDER"

    for attempt in range(1, total_attempts + 1):
        try:
            order_id = api.place_order(
                variety=api.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=product,
                order_type=order_type,
                tag=tag[:20] if tag else "",   # Zerodha tag max 20 chars
            )
            _log(
                "ORDER",
                f"✓ [{label}] Placed  {symbol}  {transaction_type}  "
                f"qty={quantity}  order_id={order_id}  "
                f"(attempt {attempt}/{total_attempts})",
            )
            return str(order_id)

        except Exception as e:
            last_error = e
            _log(
                "ORDER",
                f"✗ [{label}] Attempt {attempt}/{total_attempts} FAILED "
                f"for {symbol}: {e}",
            )
            if attempt < total_attempts:
                _log(
                    "ORDER",
                    f"  Retrying in {ORDER_RETRY_DELAY}s …  "
                    f"({total_attempts - attempt} attempt(s) left)",
                )
                time.sleep(ORDER_RETRY_DELAY)

    # ── All attempts exhausted ────────────────────────────────────
    _log(
        "ORDER",
        f"✗ [{label}] All {total_attempts} attempt(s) FAILED for {symbol}. "
        f"Last error: {last_error}",
    )

    # Notify via Telegram that the order completely failed
    try:
        send_telegram(
            f"⚠️ <b>ORDER FAILED — {label}</b>\n"
            f"Symbol   : <b>{symbol}</b>\n"
            f"Tag      : {tag}\n"
            f"Qty      : {quantity}\n"
            f"Attempts : {total_attempts}\n"
            f"Error    : {last_error}"
        )
    except Exception:
        pass   # best-effort; don't let Telegram errors mask the real problem

    return None


def _place_entry_order(
    api,
    symbol: str,
    exchange: str,
    direction: str,
    quantity: int,
    strategy_name: str,
) -> str | None:
    """
    Place an entry order for the given signal.

    Returns order_id or None on failure.
    In dry-run mode (LIVE_TRADING=False) returns the sentinel "DRY_RUN"
    without touching the broker API.
    """
    if not LIVE_TRADING:
        _log(
            "ORDER",
            f"[DRY-RUN] Would place ENTRY {direction.upper()} "
            f"{symbol} qty={quantity}  (LIVE_TRADING=False)",
        )
        return "DRY_RUN"

    txn_type   = _kite_transaction_type(direction)
    order_type = _kite_order_type(ORDER_TYPE_ENTRY)
    product    = _kite_product(ORDER_PRODUCT)
    tag        = f"BT_{strategy_name[:12]}_E"

    return _place_order_with_retry(
        api=api,
        symbol=symbol,
        exchange=exchange,
        transaction_type=txn_type,
        quantity=quantity,
        order_type=order_type,
        product=product,
        tag=tag,
        order_label="ENTRY",
    )


def _place_exit_order(
    api,
    symbol: str,
    exchange: str,
    direction: str,
    quantity: int,
    strategy_name: str,
    entry_order_id: str,
) -> str | None:
    """
    Place an exit order (reverse of entry direction).

    Returns order_id or None on failure.
    In dry-run mode returns "DRY_RUN_EXIT" without touching the broker.
    """
    if not LIVE_TRADING or entry_order_id == "DRY_RUN":
        _log(
            "ORDER",
            f"[DRY-RUN] Would place EXIT {direction.upper()} "
            f"{symbol} qty={quantity}  (LIVE_TRADING=False)",
        )
        return "DRY_RUN_EXIT"

    txn_type   = _kite_exit_transaction_type(direction)
    order_type = _kite_order_type(ORDER_TYPE_EXIT)
    product    = _kite_product(ORDER_PRODUCT)
    tag        = f"BT_{strategy_name[:12]}_X"

    return _place_order_with_retry(
        api=api,
        symbol=symbol,
        exchange=exchange,
        transaction_type=txn_type,
        quantity=quantity,
        order_type=order_type,
        product=product,
        tag=tag,
        order_label="EXIT",
    )


def _get_order_fill_price(api, order_id: str) -> float | None:
    """
    Fetch the average fill price for a completed order from Zerodha.

    Returns the average_price float, or None if unavailable / not filled.
    Never raises.
    """
    if order_id in ("DRY_RUN", "DRY_RUN_EXIT", None):
        return None

    try:
        orders = api.orders()
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                status = o.get("status", "").upper()
                if status == "COMPLETE":
                    return float(o.get("average_price", 0)) or None
                else:
                    _log(
                        "ORDER",
                        f"Order {order_id} status={status} "
                        f"— fill price unavailable yet.",
                    )
                    return None
        _log("ORDER", f"Order {order_id} not found in order book.")
        return None
    except Exception as e:
        _log("ORDER", f"WARNING: Could not fetch fill price for {order_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
#  POSITION TRACKER
# ═══════════════════════════════════════════════════════════════════

def _register_open_position(
    symbol: str,
    exchange: str,
    token: str,
    direction: str,
    quantity: int,
    allocated_capital: float,
    entry_order_id: str,
    signal_candle_time: str,
    interval: int,
    hold_candles: int,
    strategy_name: str,
    signal_datetime: datetime.datetime,
) -> bool:
    """
    Record a newly opened position in the in-memory tracker.

    The scheduled exit candle open time = signal_datetime
    + interval × (hold_candles + 1) minutes.
    The extra +1 accounts for the fact that entry happens on the
    candle AFTER the signal.

    Returns True if registered successfully, False if a duplicate
    position already exists for (symbol, strategy_name).

    Parameters
    ──────────
    allocated_capital : rupee amount reserved for this trade — used for
                        capital accounting when entry fill price is
                        not yet available.
    """
    exit_at = signal_datetime + datetime.timedelta(
        minutes=interval * (hold_candles + 1)
    )

    pos_key = (symbol, strategy_name)
    with _positions_lock:
        if pos_key in _open_positions:
            _log(
                "POSITION",
                f"WARNING: Already have an open position for "
                f"{symbol}/{strategy_name} — skipping duplicate.",
            )
            return False

        _open_positions[pos_key] = {
            "entry_order_id":    entry_order_id,
            "entry_price":       None,          # filled in after order confirmation
            "entry_time":        datetime.datetime.now(),
            "exit_at":           exit_at,
            "direction":         direction,
            "quantity":          quantity,
            "allocated_capital": allocated_capital,
            "strategy_name":     strategy_name,
            "symbol":            symbol,
            "exchange":          exchange,
            "token":             token,
            "interval":          interval,
            "hold_candles":      hold_candles,
            "signal_candle_time": signal_candle_time,
        }

    _log(
        "POSITION",
        f"Registered open position  {symbol}  {direction.upper()}  "
        f"qty={quantity}  allocated=₹{allocated_capital:,.2f}  "
        f"exit_at={exit_at.strftime('%H:%M:%S')}",
    )
    _log("CAPITAL", _capital_summary_line())
    return True


def _check_and_exit_positions(api, stop_event: threading.Event):
    """
    Background thread: checks open positions every 30 seconds and
    places exit orders when the scheduled exit time has passed.
    Runs until stop_event is set.
    """
    _log("EXIT-MGR", "Exit manager thread started.")

    while not stop_event.is_set():
        now      = datetime.datetime.now()
        to_exit  = []

        with _positions_lock:
            for pos_key, pos in list(_open_positions.items()):
                if now >= pos["exit_at"]:
                    to_exit.append((pos_key, pos))

        for pos_key, pos in to_exit:
            symbol        = pos["symbol"]
            strategy_name = pos["strategy_name"]

            _log(
                "EXIT-MGR",
                f"Scheduled exit triggered  {symbol}  "
                f"strategy={strategy_name}  direction={pos['direction']}",
            )

            # Attempt to get entry fill price if not already fetched
            if pos["entry_price"] is None and pos["entry_order_id"] not in (
                "DRY_RUN", None
            ):
                pos["entry_price"] = _get_order_fill_price(
                    api, pos["entry_order_id"]
                )

            exit_order_id = _place_exit_order(
                api=api,
                symbol=symbol,
                exchange=pos["exchange"],
                direction=pos["direction"],
                quantity=pos["quantity"],
                strategy_name=strategy_name,
                entry_order_id=pos["entry_order_id"],
            )

            # Fetch exit fill price (brief wait for order to complete)
            exit_price = None
            if exit_order_id not in ("DRY_RUN_EXIT", None):
                time.sleep(2)  # give broker a moment to fill
                exit_price = _get_order_fill_price(api, exit_order_id)

            # Build trade record
            ep  = pos["entry_price"]
            xp  = exit_price
            pnl = None
            if ep and xp:
                raw_pnl = (xp - ep) if pos["direction"] == "long" else (ep - xp)
                pnl     = round(raw_pnl * pos["quantity"], 2)

            trade_record = {
                "symbol":             symbol,
                "strategy":           strategy_name,
                "direction":          pos["direction"],
                "quantity":           pos["quantity"],
                "allocated_capital":  pos.get("allocated_capital", 0.0),
                "entry_order_id":     pos["entry_order_id"],
                "exit_order_id":      exit_order_id,
                "entry_price":        ep,
                "exit_price":         xp,
                "pnl":                pnl,
                "signal_candle":      pos["signal_candle_time"],
                "entry_time":         str(pos["entry_time"]),
                "exit_time":          str(now),
                "live_trading":       LIVE_TRADING,
            }
            _append_trade_log(trade_record)

            # ── Remove from open positions BEFORE logging capital ──
            with _positions_lock:
                _open_positions.pop(pos_key, None)

            _log(
                "EXIT-MGR",
                f"Position closed  {symbol}  pnl={pnl}  "
                f"exit_order={exit_order_id}",
            )
            _log("CAPITAL", _capital_summary_line())

            # Telegram notification for exit
            try:
                direction_emoji = "🟢" if pos["direction"] == "long" else "🔴"
                pnl_str         = f"{pnl:+.2f}" if pnl is not None else "N/A"
                pnl_emoji       = "✅" if (pnl or 0) >= 0 else "❌"
                send_telegram(
                    f"{pnl_emoji} <b>TRADE CLOSED</b>\n"
                    f"Symbol    : <b>{symbol}</b>\n"
                    f"Strategy  : {strategy_name}\n"
                    f"Direction : {direction_emoji} {pos['direction'].upper()}\n"
                    f"Qty       : {pos['quantity']}\n"
                    f"Entry     : {ep or 'N/A'}\n"
                    f"Exit      : {xp or 'N/A'}\n"
                    f"PnL       : <b>{pnl_str}</b>\n"
                    f"Mode      : {'LIVE' if LIVE_TRADING else 'DRY-RUN'}\n"
                    f"Capital   : ₹{_get_remaining_capital():,.0f} remaining"
                )
            except Exception as tg_err:
                _log("TELEGRAM", f"WARNING: Could not send exit alert: {tg_err}")

        # Sleep in small chunks so stop_event is checked regularly
        _interruptible_sleep(30, stop_event, chunk=5)

    _log("EXIT-MGR", "Exit manager thread stopped.")


# ═══════════════════════════════════════════════════════════════════
#  PER-STOCK SIGNAL CHECK  (runs in thread pool)
# ═══════════════════════════════════════════════════════════════════

def _check_stock(
    symbol: str,
    exchange: str,
    token: str,
    strategies: list,
    interval: int,
    api,
    alert_lock: threading.Lock,
    signal_datetime: datetime.datetime,
) -> list:
    """
    Fetch the latest candles for one stock and check against all
    strategies that use `interval`.

    Returns a list of alert dicts for every match found.
    """
    alerts = []

    for strategy in strategies:
        bb_period = strategy.get("bb_period", 20)
        bb_stddev = strategy.get("bb_stddev", 2.0)

        # ── Fetch candles ──────────────────────────────────────────
        try:
            candles = get_live(
                api=api,
                exchange=exchange,
                stock_token_id=token,
                interval=interval,
                bb_period=bb_period,
                bb_stddev=bb_stddev,
                broker=BROKER_ZERODHA,
            )
        except Exception as e:
            _log(
                "DATA",
                f"ERROR fetching {symbol} "
                f"[{interval}min, strat={strategy.get('name','?')}]: {e}",
            )
            _log("DATA", traceback.format_exc())
            continue

        # ── Check pattern ──────────────────────────────────────────
        strat_with_stock = {
            **strategy,
            "exchange":       exchange,
            "stock_token_id": token,
        }

        try:
            signal = check_live_signal(candles, strat_with_stock)
        except Exception as e:
            _log(
                "SIGNAL",
                f"ERROR checking signal for {symbol} "
                f"[strat={strategy.get('name','?')}]: {e}",
            )
            _log("SIGNAL", traceback.format_exc())
            continue

        if signal is not None:
            with alert_lock:
                _log(
                    "MATCH",
                    f"🚨 Pattern matched!  {symbol}  |  "
                    f"strategy={strategy.get('name','?')}  |  "
                    f"{interval}min  |  "
                    f"candle={signal.get('time','?')}",
                )
            alerts.append({
                "symbol":           symbol,
                "exchange":         exchange,
                "token":            token,
                "strategy":         strat_with_stock,
                "signal_candle":    signal,
                "signal_datetime":  signal_datetime,
                # Pass the last candle close price for quantity calculation
                "current_price":    signal.get("close", 0.0),
            })
        else:
            _log(
                "SCAN",
                f"  No match  {symbol:<14}  "
                f"strat={strategy.get('name','?'):<22}  "
                f"{interval}min",
            )

    return alerts


# ═══════════════════════════════════════════════════════════════════
#  ALERT + OPTIONAL ORDER HANDLER
# ═══════════════════════════════════════════════════════════════════

def _handle_alert(alert: dict, api):
    """
    Process one matched signal:
      1. Send Telegram alert.
      2. Run money-management check.
      3. (If LIVE_TRADING and capital available) place entry order
         and register position.
    """
    symbol        = alert["symbol"]
    strategy      = alert["strategy"]
    signal_candle = alert["signal_candle"]
    signal_dt     = alert["signal_datetime"]
    exchange      = alert["exchange"]
    token         = alert["token"]
    direction     = strategy.get("direction", "long")
    hold_candles  = int(strategy.get("hold_candles", 1))
    interval      = int(strategy.get("interval", 15))
    strat_name    = strategy.get("name", "unknown")
    current_price = alert.get("current_price", 0.0)

    # ── 1. Telegram alert ─────────────────────────────────────────
    try:
        msg = build_alert_message(
            strategy=strategy,
            signal_candle=signal_candle,
        )
        mode_line = (
            "\n\n🤖 <b>Mode:</b> "
            + ("LIVE TRADING ✅" if LIVE_TRADING else "Alert only (dry-run)")
        )
        ok = send_telegram(msg + mode_line)
        if ok:
            _log("TELEGRAM", f"✓ Alert sent  {symbol}  |  {strat_name}")
        else:
            _log("TELEGRAM", f"✗ Alert FAILED  {symbol}  |  {strat_name}")
    except Exception as e:
        _log("TELEGRAM", f"ERROR sending alert for {symbol}: {e}")
        _log("TELEGRAM", traceback.format_exc())

    # ── 2. Guard: alert-only mode ─────────────────────────────────
    if not LIVE_TRADING:
        _log(
            "ORDER",
            f"[ALERT-ONLY]  No order placed for {symbol}  "
            f"(set LIVE_TRADING=True to enable).",
        )
        return

    # ── 3. Guard: no duplicate position ───────────────────────────
    pos_key = (symbol, strat_name)
    with _positions_lock:
        if pos_key in _open_positions:
            _log(
                "ORDER",
                f"Skipping entry for {symbol}/{strat_name} "
                f"— position already open.",
            )
            return

    # ── 4. Money management check + quantity calculation ──────────
    _log("CAPITAL", _capital_summary_line())

    quantity, allocated_amount, mm_reason = _check_capital_and_calculate_quantity(
        symbol=symbol,
        current_price=current_price,
    )

    if quantity < 1:
        # Not enough capital — log clearly and notify via Telegram
        _log("CAPITAL", f"⛔ Order SKIPPED for {symbol}:\n{mm_reason}")
        try:
            send_telegram(
                f"⛔ <b>ORDER SKIPPED — Insufficient Capital</b>\n"
                f"Symbol   : <b>{symbol}</b>\n"
                f"Strategy : {strat_name}\n"
                f"Price    : ₹{current_price:,.2f}\n\n"
                + mm_reason.replace("  →", "\n→")
            )
        except Exception:
            pass
        return

    _log("CAPITAL", f"✓ Capital check passed  {mm_reason}")

    # ── 5. Place entry order ───────────────────────────────────────
    entry_order_id = _place_entry_order(
        api=api,
        symbol=symbol,
        exchange=exchange,
        direction=direction,
        quantity=quantity,
        strategy_name=strat_name,
    )

    if entry_order_id is None:
        _log(
            "ORDER",
            f"Entry order FAILED for {symbol}/{strat_name} "
            f"— position NOT opened.",
        )
        return

    # Brief wait then fetch fill price
    if entry_order_id not in ("DRY_RUN",):
        time.sleep(2)
        entry_price = _get_order_fill_price(api, entry_order_id)
    else:
        # Dry-run: use signal candle close as approximate fill price
        entry_price = current_price if current_price > 0 else signal_candle.get("close")

    # ── 6. Register the position for exit tracking ────────────────
    registered = _register_open_position(
        symbol=symbol,
        exchange=exchange,
        token=token,
        direction=direction,
        quantity=quantity,
        allocated_capital=allocated_amount,
        entry_order_id=entry_order_id,
        signal_candle_time=signal_candle.get("time", ""),
        interval=interval,
        hold_candles=hold_candles,
        strategy_name=strat_name,
        signal_datetime=signal_dt,
    )

    if registered:
        # Update entry_price in position record once we have it
        with _positions_lock:
            if pos_key in _open_positions:
                _open_positions[pos_key]["entry_price"] = entry_price

        # Confirm via Telegram
        try:
            direction_emoji = "🟢" if direction == "long" else "🔴"
            exit_time_str   = (
                signal_dt + datetime.timedelta(
                    minutes=interval * (hold_candles + 1)
                )
            ).strftime("%H:%M:%S")

            send_telegram(
                f"✅ <b>ENTRY PLACED</b>\n"
                f"Symbol    : <b>{symbol}</b>\n"
                f"Strategy  : {strat_name}\n"
                f"Direction : {direction_emoji} {direction.upper()}\n"
                f"Qty       : {quantity}\n"
                f"Allocated : ₹{allocated_amount:,.2f}\n"
                f"Order ID  : {entry_order_id}\n"
                f"Fill ~    : {entry_price or 'pending'}\n"
                f"Hold      : {hold_candles} candle(s)\n"
                f"Exit at   : {exit_time_str}\n"
                f"Remaining : ₹{_get_remaining_capital():,.0f}"
            )
        except Exception as e:
            _log("TELEGRAM", f"WARNING: Could not send entry confirm: {e}")


# ═══════════════════════════════════════════════════════════════════
#  ONE SCAN CYCLE  (called every time a candle closes)
# ═══════════════════════════════════════════════════════════════════

def _run_scan_cycle(
    interval: int,
    strategies: list,
    api,
    alert_lock: threading.Lock,
    signal_datetime: datetime.datetime,
):
    """
    For a given interval, scan ALL watched stocks in parallel.
    Fires Telegram alerts (and optionally orders) for every match.
    """
    _log(
        "CYCLE",
        f"New {interval}-min candle closed — scanning "
        f"{len(WATCHED_STOCKS)} stock(s) × "
        f"{len(strategies)} strategy/strategies",
    )
    _log("CAPITAL", _capital_summary_line())

    futures: dict = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for symbol, (exchange, token) in WATCHED_STOCKS.items():
            future = pool.submit(
                _check_stock,
                symbol, exchange, token,
                strategies, interval,
                api, alert_lock, signal_datetime,
            )
            futures[future] = symbol

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                alerts = future.result()
                for alert in alerts:
                    _handle_alert(alert, api)
            except Exception as e:
                _log("ERROR", f"Unhandled error checking {symbol}: {e}")
                _log("ERROR", traceback.format_exc())


# ═══════════════════════════════════════════════════════════════════
#  INTERVAL SCHEDULER  (one thread per unique interval)
# ═══════════════════════════════════════════════════════════════════

def _interval_scheduler(
    interval: int,
    strategies: list,
    api,
    alert_lock: threading.Lock,
    stop_event: threading.Event,
):
    """
    Dedicated scheduler for one candle interval.
    Sleeps until the next candle close, fires the scan, then repeats.
    Exits cleanly when stop_event is set.
    """
    _log(
        "SCHED",
        f"Scheduler started  interval={interval}min  "
        f"strategies={len(strategies)}",
    )

    while not stop_event.is_set():

        # ── Wait if market is not open ─────────────────────────────
        if not _is_market_open():
            now_time = datetime.datetime.now().time()

            if now_time > MARKET_CLOSE:
                # After market close — sleep until tomorrow's open
                tomorrow_open = (
                    datetime.datetime.now().replace(
                        hour=MARKET_OPEN.hour,
                        minute=MARKET_OPEN.minute,
                        second=0,
                        microsecond=0,
                    ) + datetime.timedelta(days=1)
                )
                wait_secs = _seconds_until(tomorrow_open)
                _log(
                    "SCHED",
                    f"[{interval}min] Market closed. "
                    f"Next open in {wait_secs / 3600:.1f}h",
                )
                _interruptible_sleep(wait_secs, stop_event)
            else:
                # Before market open today
                today_open = datetime.datetime.now().replace(
                    hour=MARKET_OPEN.hour,
                    minute=MARKET_OPEN.minute,
                    second=0,
                    microsecond=0,
                )
                wait_secs = _seconds_until(today_open)
                _log(
                    "SCHED",
                    f"[{interval}min] Market not open yet. "
                    f"Waiting {wait_secs:.0f}s …",
                )
                _interruptible_sleep(wait_secs, stop_event)
            continue

        # ── Calculate next candle close ────────────────────────────
        next_fetch  = _next_candle_close_time(interval)
        wait_secs   = _seconds_until(next_fetch)

        if wait_secs > 0:
            _log(
                "SCHED",
                f"[{interval}min] Next fetch at "
                f"{next_fetch.strftime('%H:%M:%S')}  "
                f"({wait_secs:.0f}s away)",
            )
            _interruptible_sleep(wait_secs, stop_event)

        if stop_event.is_set():
            break

        # ── Fire scan ──────────────────────────────────────────────
        signal_datetime = datetime.datetime.now()
        try:
            _run_scan_cycle(
                interval, strategies, api, alert_lock, signal_datetime
            )
        except Exception as e:
            _log("ERROR", f"[{interval}min] Scan cycle error: {e}")
            _log("ERROR", traceback.format_exc())

        # Brief pause to avoid double-trigger on the same candle
        time.sleep(2)

    _log("SCHED", f"Scheduler stopped  interval={interval}min")


# ═══════════════════════════════════════════════════════════════════
#  STARTUP SUMMARY
# ═══════════════════════════════════════════════════════════════════

def _print_startup_summary(strategies_by_interval: dict):
    _banner("LIVE SIGNAL MONITOR  —  Zerodha  |  Starting Up")

    mode_line = (
        "  ⚡ LIVE TRADING  ON  — orders WILL be placed via Zerodha"
        if LIVE_TRADING else
        "  👁  Alert-only mode  — NO orders will be placed  (LIVE_TRADING=False)"
    )
    print(mode_line)

    print(f"\n  {'Stocks monitored':<36} {len(WATCHED_STOCKS)}")
    for sym, (exch, tok) in WATCHED_STOCKS.items():
        per_sym_money = _get_allocated_money_for_symbol(sym) if _is_money_management_enabled() else None
        money_str     = f"  limit=₹{per_sym_money:,.0f}" if per_sym_money else ""
        print(f"    • {sym:<16}  {exch}  token: {tok}{money_str}")

    total_strats = sum(len(v) for v in strategies_by_interval.values())
    print(f"\n  {'Strategies loaded':<36} {total_strats}")
    for interval, strats in sorted(strategies_by_interval.items()):
        print(f"\n    ── {interval}-min candle interval ──")
        for s in strats:
            print(
                f"       • {s.get('name', 'unnamed'):<32}  "
                f"dir={s.get('direction', '?'):<6}  "
                f"hold={s.get('hold_candles', '?')}  "
                f"BB={s.get('bb_period', '?')}/{s.get('bb_stddev', '?')}"
            )

    # ── Money management summary ───────────────────────────────────
    print(f"\n  ── Money Management ──")
    if _is_money_management_enabled():
        print(f"  {'Total capital':<36} ₹{TOTAL_CAPITAL:,.2f}")
        print(f"  {'Max per trade (default)':<36} ₹{MONEY_PER_STOCK:,.2f}")
        if MONEY_PER_STOCK_MAP:
            print(f"  {'Per-symbol overrides':<36}")
            for sym, amt in MONEY_PER_STOCK_MAP.items():
                print(f"       {sym:<16}  ₹{amt:,.2f}")
        max_concurrent = int(TOTAL_CAPITAL // MONEY_PER_STOCK)
        print(
            f"  {'Max concurrent trades':<36} "
            f"~{max_concurrent} "
            f"(= ₹{TOTAL_CAPITAL:,.0f} ÷ ₹{MONEY_PER_STOCK:,.0f})"
        )
    else:
        print(f"  Money management  : DISABLED (TOTAL_CAPITAL or MONEY_PER_STOCK = 0)")
        print(f"  Fixed quantity    : {DEFAULT_QUANTITY} share(s) per trade")
        if SYMBOL_QUANTITY:
            print(f"  Per-symbol qty    : {SYMBOL_QUANTITY}")

    if LIVE_TRADING:
        print(f"\n  {'Order product':<36} {ORDER_PRODUCT}")
        print(f"  {'Entry order type':<36} {ORDER_TYPE_ENTRY}")
        print(f"  {'Exit order type':<36} {ORDER_TYPE_EXIT}")
        print(f"  {'Order retries':<36} {ORDER_MAX_RETRIES} retry/retries "
              f"({ORDER_MAX_RETRIES + 1} total attempts, "
              f"{ORDER_RETRY_DELAY}s between retries)")

    print(f"\n  {'Market hours (IST)':<36} "
          f"{MARKET_OPEN.strftime('%H:%M')} – {MARKET_CLOSE.strftime('%H:%M')}")
    print(f"  {'Fetch delay':<36} {FETCH_DELAY_SECONDS}s after candle close")
    print(f"  {'Parallel workers':<36} {MAX_WORKERS}")
    print(f"  {'Trade log':<36} {TRADE_LOG_PATH}")
    print()


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def main():

    # ── Load strategies ────────────────────────────────────────────
    _log("INIT", "Loading strategies …")
    all_strategies = load_all_strategies()

    if not all_strategies:
        print(
            "\n[!] No strategies found in ./Assets/strategies/\n"
            "    → Run backtest.py first and save at least one strategy."
        )
        return

    # ── Check that we have stocks ─────────────────────────────────
    if not WATCHED_STOCKS:
        print(
            f"\n[!] No stocks loaded from {STOCKS_FILE}.\n"
            f"    Ensure the file exists and contains at least one entry like:\n"
            f'    {{"TCS": "2953217", "INFY": "408065"}}\n'
            f"    Exchange is always NSE (Zerodha only)."
        )
        return

    # ── Group strategies by candle interval ───────────────────────
    strategies_by_interval: dict[int, list] = {}
    for strat in all_strategies:
        iv = strat.get("interval")
        if iv is None:
            _log(
                "WARN",
                f"Strategy '{strat.get('name','?')}' has no 'interval' field "
                f"— skipping.",
            )
            continue
        strategies_by_interval.setdefault(int(iv), []).append(strat)

    if not strategies_by_interval:
        print(
            "\n[!] No valid strategies with an 'interval' field found.\n"
            "    → Check your strategy files in ./Assets/strategies/"
        )
        return

    _print_startup_summary(strategies_by_interval)

    # ── Validate money management settings ────────────────────────
    if _is_money_management_enabled():
        if MONEY_PER_STOCK > TOTAL_CAPITAL:
            print(
                f"\n[!] Configuration ERROR:\n"
                f"    MONEY_PER_STOCK (₹{MONEY_PER_STOCK:,.0f}) cannot be greater "
                f"than TOTAL_CAPITAL (₹{TOTAL_CAPITAL:,.0f}).\n"
                f"    → Fix these values at the top of live.py and restart."
            )
            return
        _log(
            "CAPITAL",
            f"Money management ENABLED  "
            f"total=₹{TOTAL_CAPITAL:,.0f}  "
            f"per_trade=₹{MONEY_PER_STOCK:,.0f}",
        )
    else:
        _log("CAPITAL", "Money management DISABLED — using fixed quantity mode.")

    # ── Load trade log from disk ───────────────────────────────────
    _load_trade_log()

    # ── Log in to Zerodha ─────────────────────────────────────────
    _log("INIT", "Logging in to Zerodha …")
    try:
        api = login_zerodha()
    except Exception as e:
        print(f"\n[!] Zerodha login failed: {e}")
        _log("INIT", traceback.format_exc())
        return

    _log("INIT", "✓ Zerodha login successful.")

    # ── Shared state ───────────────────────────────────────────────
    alert_lock = threading.Lock()
    stop_event = threading.Event()
    threads    = []

    # ── Exit manager thread ───────────────────────────────────────
    if LIVE_TRADING:
        exit_mgr_thread = threading.Thread(
            target=_check_and_exit_positions,
            args=(api, stop_event),
            name="exit-manager",
            daemon=True,
        )
        exit_mgr_thread.start()
        threads.append(exit_mgr_thread)
        _log("INIT", "Exit manager thread started.")
    else:
        _log("INIT", "Exit manager skipped (LIVE_TRADING=False).")

    # ── One scheduler thread per unique candle interval ───────────
    for interval, strategies in sorted(strategies_by_interval.items()):
        t = threading.Thread(
            target=_interval_scheduler,
            args=(interval, strategies, api, alert_lock, stop_event),
            name=f"sched-{interval}min",
            daemon=True,
        )
        t.start()
        threads.append(t)
        _log("INIT", f"Scheduler thread started → {interval}-min interval")

    _log(
        "INIT",
        f"Live monitor running  |  {len(threads)} thread(s) active  |  "
        f"Press Ctrl+C to stop",
    )

    # ── Keep main thread alive; handle Ctrl+C gracefully ──────────
    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print(f"\n\n{'═' * W}")
        print("  Shutting down — please wait …")
        print(f"{'═' * W}")
        stop_event.set()

        for t in threads:
            t.join(timeout=10)
            if t.is_alive():
                _log("WARN", f"Thread '{t.name}' did not stop cleanly.")
            else:
                _log("INIT", f"Thread '{t.name}' stopped.")

        print(f"\n{'═' * W}")
        print("  Live monitor stopped. ✓")
        print(f"{'═' * W}\n")


# ═══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n\n{'═' * W}")
        print("  UNEXPECTED ERROR — please share the following with your developer:")
        print(f"{'═' * W}")
        print(f"  {e}")
        traceback.print_exc()
        print(f"{'═' * W}\n")
