"""
utils_get_data.py  —  Zerodha login and candle data fetching

LOGIN METHOD: Fully automated (no browser required)
  - Uses requests + pyotp (TOTP) to log in headlessly
  - Extracts request_token from the redirect URL
  - Caches the access_token for the rest of the trading day

All_cred.json structure:
{
  "broker": "zerodha",
  "zerodha": {
    "user_id":              "YOUR_USER_ID",
    "password":             "YOUR_PASSWORD",
    "totp_secret":          "YOUR_TOTP_SECRET",
    "api_key":              "YOUR_API_KEY",
    "api_secret":           "YOUR_API_SECRET",
    "zerodha_access_token": ""        <-- optional: paste a token to skip login entirely
  }
}

Dependencies:
    pip install kiteconnect requests pyotp
"""

import json
import math
import datetime
import urllib.parse
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════
#  BROKER CONSTANTS
# ═══════════════════════════════════════════════════════════════════

BROKER_ZERODHA    = "zerodha"
SUPPORTED_BROKERS = [BROKER_ZERODHA]

# Zerodha's API accepts specific string values for interval.
# We map our integer minutes to those strings here.
ZERODHA_INTERVAL_MAP = {
    1:    "minute",
    3:    "3minute",
    5:    "5minute",
    10:   "10minute",
    15:   "15minute",
    30:   "30minute",
    60:   "60minute",
    120:  "2hour",
    240:  "4hour",
    "D":  "day",
}

# Natively supported minute values (integers only, excludes "D").
# Used to find the best base interval for custom resampling.
ZERODHA_NATIVE_INTERVALS = [1, 3, 5, 10, 15, 30, 60, 120, 240]


# ═══════════════════════════════════════════════════════════════════
#  CREDENTIALS
# ═══════════════════════════════════════════════════════════════════

def load_creds(path="All_cred.json"):
    """Load broker/API credentials from a JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"[Auth] ERROR: Credentials file '{path}' not found.\n"
            "  → Please create All_cred.json with your broker credentials.\n"
            "  → See the module docstring for the required JSON structure."
        )
    except json.JSONDecodeError as e:
        raise ValueError(
            f"[Auth] ERROR: Could not parse '{path}' as JSON.\n"
            f"  → Check the file for formatting mistakes: {e}"
        )


def get_broker_from_creds(cred_path="All_cred.json"):
    """
    Read 'broker' key from credentials file.
    Returns the broker name string (lowercased), or None if not set.
    """
    try:
        c   = load_creds(cred_path)
        val = c.get("broker", "").strip().lower()
        return val if val else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
#  ZERODHA LOGIN  —  fully automated, no browser required
# ═══════════════════════════════════════════════════════════════════

def login_zerodha(cred_path="All_cred.json"):
    """
    Log in to Zerodha (Kite Connect) fully automatically.
    Returns an authenticated KiteConnect object.

    ─────────────────────────────────────────────────────────────────
    TOKEN PRIORITY ORDER
    ─────────────────────────────────────────────────────────────────
    1. Manual token in All_cred.json  ("zerodha_access_token" key)
       → Used immediately, zero network calls.

    2. Cached token in ./Assets/zerodha_token.json dated today
       → Reused silently; no login needed.

    3. Auto-login via requests + TOTP (runs when 1 and 2 are unavailable)
       a. POST credentials to Zerodha's login endpoint.
       b. POST TOTP pin to the 2FA endpoint.
       c. GET the Kite Connect redirect URL — the request_token is
          extracted directly from the URL (no browser, no local server).
       d. Exchange request_token for access_token.
       e. Cache the access_token for the rest of the trading day.

    Required keys in All_cred.json (under "zerodha" block):
        api_key      : Kite Connect API key
        api_secret   : Kite Connect API secret
        user_id      : Zerodha client ID  (e.g. "AB1234")
        password     : Zerodha login password
        totp_secret  : TOTP secret from Kite → Settings → Security
                       (the plain-text secret, not a generated OTP)

    Optional key:
        zerodha_access_token : paste a valid token to skip login entirely
    ─────────────────────────────────────────────────────────────────
    """
    try:
        import requests
    except ImportError:
        raise ImportError(
            "[Auth] ERROR: 'requests' is not installed.\n"
            "  → Run:  pip install requests"
        )

    try:
        import pyotp
    except ImportError:
        raise ImportError(
            "[Auth] ERROR: 'pyotp' is not installed.\n"
            "  → Run:  pip install pyotp"
        )

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        raise ImportError(
            "[Auth] ERROR: 'kiteconnect' is not installed.\n"
            "  → Run:  pip install kiteconnect"
        )

    c  = load_creds(cred_path)
    zc = c.get("zerodha", c)  # support both flat and nested JSON

    # Validate required keys
    required_keys = ["api_key", "api_secret", "user_id", "password", "totp_secret"]
    missing = [k for k in required_keys if not zc.get(k, "").strip()]
    if missing:
        raise KeyError(
            f"[Auth] ERROR: Missing or empty key(s) in All_cred.json "
            f"(zerodha section): {missing}\n"
            f"  → Required keys: {required_keys}"
        )

    api_key      = zc["api_key"].strip()
    api_secret   = zc["api_secret"].strip()
    user_id      = zc["user_id"].strip()
    password     = zc["password"].strip()
    totp_secret  = zc["totp_secret"].strip()

    kite = KiteConnect(api_key=api_key)

    # # ──────────────────────────────────────────────────────────────
    # # PRIORITY 1 — manual token in creds file
    # # ──────────────────────────────────────────────────────────────
    # manual_token = zc.get("zerodha_access_token", "").strip()
    # if manual_token:
    #     kite.set_access_token(manual_token)
    #     print("[Auth] ✓ Zerodha: using access_token from All_cred.json")
    #     return kite
    #
    # # ──────────────────────────────────────────────────────────────
    # # PRIORITY 2 — today's cached token
    # # ──────────────────────────────────────────────────────────────
    # token_cache = Path("./Assets/zerodha_token.json")
    # token_cache.parent.mkdir(parents=True, exist_ok=True)
    # today_str = datetime.date.today().isoformat()
    #
    # if token_cache.exists():
    #     try:
    #         with open(token_cache) as f:
    #             cached = json.load(f)
    #         if cached.get("date") == today_str and cached.get("access_token"):
    #             kite.set_access_token(cached["access_token"])
    #             print("[Auth] ✓ Zerodha: reusing today's cached access_token")
    #             return kite
    #         else:
    #             print("[Auth] Zerodha token cache is from a previous day — refreshing.")
    #     except (json.JSONDecodeError, KeyError):
    #         print("[Auth] WARNING: Zerodha token cache is corrupted — ignoring it.")

    # ──────────────────────────────────────────────────────────────
    # PRIORITY 3 — auto-login via requests + TOTP (fully headless)
    # ──────────────────────────────────────────────────────────────
    print()
    print("─" * 62)
    print("  ZERODHA AUTO-LOGIN (headless, no browser required) …")
    print("─" * 62)

    session = requests.Session()

    # Step 1 — POST user ID + password
    print("[Auth] Step 1/4: Posting credentials …")
    try:
        login_res = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            timeout=15,
        )
        login_res.raise_for_status()
        login_data = login_res.json()
    except requests.RequestException as e:
        raise RuntimeError(
            f"[Auth] ERROR: Zerodha login step 1 failed: {e}\n"
            "  → Check your internet connection and Zerodha credentials."
        )

    if login_data.get("status") != "success":
        raise RuntimeError(
            f"[Auth] ERROR: Zerodha rejected credentials.\n"
            f"  → Response: {login_data}\n"
            "  → Check user_id and password in All_cred.json."
        )

    request_id = login_data["data"]["request_id"]

    # Step 2 — POST TOTP pin (2FA)
    print("[Auth] Step 2/4: Submitting TOTP 2FA …")
    try:
        totp_pin = pyotp.TOTP(totp_secret).now()
        twofa_res = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "request_id":  request_id,
                "twofa_value": totp_pin,
                "user_id":     user_id,
            },
            timeout=15,
        )
        twofa_res.raise_for_status()
        twofa_data = twofa_res.json()
    except requests.RequestException as e:
        raise RuntimeError(
            f"[Auth] ERROR: Zerodha 2FA step failed: {e}\n"
            "  → Check your internet connection."
        )

    if twofa_data.get("status") != "success":
        raise RuntimeError(
            f"[Auth] ERROR: Zerodha 2FA rejected.\n"
            f"  → Response: {twofa_data}\n"
            "  → Check totp_secret in All_cred.json.\n"
            "  → The TOTP secret is the plain-text base-32 string from\n"
            "    Kite → Settings → Security → 'External TOTP' setup."
        )

    print("[Auth] Step 3/4: Fetching request_token from redirect URL …")

    # Step 3 — Follow the Kite Connect login redirect to capture request_token.
    # Zerodha redirects to your registered redirect URL with ?request_token=…
    # We catch the ConnectionError that fires when it tries to hit that URL
    # (since we don't have a server running there) and pull the token out of it.
    kite_connect_url = f"https://kite.trade/connect/login?api_key={api_key}&v=3"
    request_token = None

    try:
        auth_res      = session.get(kite_connect_url, allow_redirects=True, timeout=15)
        redirect_url  = auth_res.url
        parsed        = urllib.parse.urlparse(redirect_url)
        params        = urllib.parse.parse_qs(parsed.query)
        request_token = params.get("request_token", [None])[0]

    except requests.exceptions.ConnectionError as e:
        # The redirect landed on our (non-running) redirect URL — expected.
        # Grab the URL from the exception and parse it.
        try:
            redirect_url  = str(e.request.url)
            parsed        = urllib.parse.urlparse(redirect_url)
            params        = urllib.parse.parse_qs(parsed.query)
            request_token = params.get("request_token", [None])[0]
        except Exception:
            pass

    except requests.RequestException as e:
        raise RuntimeError(
            f"[Auth] ERROR: Failed to follow Kite Connect redirect: {e}"
        )

    if not request_token:
        raise RuntimeError(
            "[Auth] ERROR: Could not extract request_token from redirect URL.\n"
            "  → Make sure your Kite Connect app's redirect URL is set to\n"
            "    http://127.0.0.1:5051  (or any localhost URL).\n"
            "  → Verify that api_key matches the app in your Kite developer console."
        )

    # Step 4 — Exchange request_token for access_token
    print("[Auth] Step 4/4: Generating session (access_token) …")
    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session_data["access_token"]
    except Exception as e:
        raise RuntimeError(
            f"[Auth] ERROR: Could not generate Zerodha session: {e}\n"
            "  → The request_token may have expired (they are single-use).\n"
            "  → Also check api_secret in All_cred.json."
        )

    kite.set_access_token(access_token)

    # Cache the token for the rest of today
    try:
        with open(token_cache, "w") as f:
            json.dump({"date": today_str, "access_token": access_token}, f, indent=2)
        print(f"[Auth] ✓ Zerodha login successful — token cached at {token_cache}")
    except OSError as e:
        print(f"[Auth] WARNING: Could not write token cache: {e}")

    print("─" * 62)
    return kite


# ═══════════════════════════════════════════════════════════════════
#  UNIFIED LOGIN
# ═══════════════════════════════════════════════════════════════════

def login(broker=None, cred_path="All_cred.json"):
    """
    Unified login function.

    Parameters
    ──────────
    broker   : str or None
               "zerodha"
               If None, reads the 'broker' key from All_cred.json.
               If that is missing, defaults to "zerodha".
    cred_path: path to credentials JSON file.

    Returns
    ───────
    A tuple  (api_object, broker_name_str)
    """
    if broker is None:
        broker = get_broker_from_creds(cred_path) or BROKER_ZERODHA

    broker = broker.strip().lower()

    if broker == BROKER_ZERODHA:
        api = login_zerodha(cred_path)
        return api, BROKER_ZERODHA

    else:
        raise ValueError(
            f"[Auth] ERROR: Unknown broker '{broker}'.\n"
            f"  → Supported brokers: {', '.join(SUPPORTED_BROKERS)}\n"
            f"  → Set the 'broker' key in All_cred.json to one of those values."
        )


# ═══════════════════════════════════════════════════════════════════
#  SYMBOL SEARCH
# ═══════════════════════════════════════════════════════════════════

def search(exchange, search_stock_name, broker=None, cred_path="All_cred.json"):
    """
    Search for a stock symbol on the broker and print matching tokens.
    """
    try:
        api, broker_name = login(broker=broker, cred_path=cred_path)

        if broker_name == BROKER_ZERODHA:
            instruments = api.instruments(exchange)
            results = [
                i for i in instruments
                if search_stock_name.upper() in i["tradingsymbol"].upper()
            ]
            if results:
                for i in results:
                    print(
                        f"{i['tradingsymbol']:<20}  "
                        f"token: {i['instrument_token']:<12}  "
                        f"name: {i.get('name', '')}"
                    )
            else:
                print(
                    f"[Search] No results for '{search_stock_name}' on {exchange}.\n"
                    f"  → Search is case-insensitive substring match on trading symbol."
                )

    except Exception as e:
        print(f"[Search] ERROR while searching: {e}")


# ═══════════════════════════════════════════════════════════════════
#  CANDLE NORMALISATION
# ═══════════════════════════════════════════════════════════════════

def _parse_time(time_str):
    """Parse time string to datetime for chronological sorting."""
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(time_str, fmt)
        except ValueError:
            continue
    return time_str  # fallback


def normalize(raw_candles, broker=BROKER_ZERODHA):
    """
    Convert raw broker response into clean candle dicts.
    Returns [] (empty list) on total failure — never None.
    """
    if not raw_candles:
        print("[Candle] WARNING: normalize() received empty or None input.")
        return []

    candles = []

    for idx, c in enumerate(raw_candles):
        try:
            raw_time = c.get("date", "")
            o  = float(c["open"])
            h  = float(c["high"])
            l  = float(c["low"])
            cl = float(c["close"])
            v  = float(c.get("volume", 0) or 0)

            if hasattr(raw_time, "strftime"):
                time_str = raw_time.strftime("%Y-%m-%d %H:%M")
            else:
                time_str = str(raw_time)

        except (KeyError, ValueError, TypeError) as e:
            print(f"[Candle] WARNING: Skipping malformed candle at index {idx}: {e}")
            print(f"         Candle data: {c}")
            continue

        body       = round(abs(cl - o), 2)
        upper_wick = round(h - max(o, cl), 2)
        lower_wick = round(min(o, cl) - l, 2)
        color      = "green" if cl > o else ("red" if cl < o else "neutral")

        candles.append({
            "time":         time_str,
            "open":         o,
            "high":         h,
            "low":          l,
            "close":        cl,
            "volume":       v,
            "body_length":  body,
            "upper_wick":   upper_wick,
            "lower_wick":   lower_wick,
            "candle_color": color,
        })

    if not candles:
        print(f"[Candle] WARNING: normalize() produced 0 candles from {len(raw_candles)} raw records.")
        return []

    candles.sort(key=lambda c: _parse_time(c["time"]))

    print(f"[Candle] Sorted {len(candles)} candles: "
          f"{candles[0]['time']}  →  {candles[-1]['time']}")

    return candles


# ═══════════════════════════════════════════════════════════════════
#  CACHE HELPERS  (internal)
# ═══════════════════════════════════════════════════════════════════

DATA_DIR           = Path("./Assets/data")
CACHE_MAX_AGE_DAYS = 7


def _cache_filename(exchange, stock_token_id, days, interval, broker):
    today = datetime.date.today().strftime("%Y-%m-%d")
    return (
        f"{broker}_{exchange}_{stock_token_id}_"
        f"{days}days_{interval}min_fetched{today}.json"
    )


def _find_existing_cache(exchange, stock_token_id, days, interval, broker):
    """
    Scan DATA_DIR for any cache file matching broker / stock / days / interval.
    Returns (path, fetch_date) or (None, None).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    prefix  = f"{broker}_{exchange}_{stock_token_id}_{days}days_{interval}min_fetched"
    matches = sorted(DATA_DIR.glob(f"{prefix}*.json"))

    if not matches:
        # Backward-compat: look for old-style files without broker prefix
        old_prefix = f"{exchange}_{stock_token_id}_{days}days_{interval}min_fetched"
        matches    = sorted(DATA_DIR.glob(f"{old_prefix}*.json"))
        if not matches:
            return None, None
        print(
            f"[Data] ℹ  Found old-style cache (no broker prefix) → {matches[-1].name}\n"
            f"       It will be reused this time, but replaced on next fetch."
        )

    newest = matches[-1]

    try:
        date_str   = newest.stem.split("fetched")[-1]
        fetch_date = datetime.date.fromisoformat(date_str)
    except (IndexError, ValueError):
        return newest, datetime.date.min

    return newest, fetch_date


def _is_cache_stale(fetch_date):
    return (datetime.date.today() - fetch_date).days >= CACHE_MAX_AGE_DAYS


def _delete_stale_cache(path):
    try:
        path.unlink()
        print(f"[Data] 🗑  Deleted stale cache → {path.name}")
    except OSError as e:
        print(f"[Data] WARNING: Could not delete stale cache file '{path.name}': {e}")


# ═══════════════════════════════════════════════════════════════════
#  CUSTOM INTERVAL RESAMPLING
# ═══════════════════════════════════════════════════════════════════

def _find_base_interval(custom_interval):
    """
    Find the best Zerodha-native interval to use as the fetch base
    when the requested interval is not natively supported.

    Strategy: pick the LARGEST native interval that divides evenly
    into custom_interval.  This minimises the number of raw candles
    fetched while still allowing perfect aggregation.

    Examples
    ────────
    custom=20  → divisors in ZERODHA_NATIVE_INTERVALS: 1,5,10 → base=10
    custom=25  → divisors: 1,5                                → base=5
    custom=45  → divisors: 1,5,15                             → base=15
    custom=7   → divisors: 1                                  → base=1
    custom=90  → divisors: 1,5,10,15,30                       → base=30
    """
    candidates = [
        m for m in ZERODHA_NATIVE_INTERVALS
        if custom_interval % m == 0 and m <= custom_interval
    ]
    if not candidates:
        return 1  # fall back to 1-minute as the universal base
    return max(candidates)


def resample_candles(candles, target_interval, base_interval):
    """
    Aggregate base-interval candles into a custom target interval.

    Alignment: buckets are anchored to the FIRST candle of each
    trading day, so the 9:15 AM open is always the start of bucket 0
    regardless of the target interval size.

    Parameters
    ──────────
    candles         : list of normalised candle dicts (from normalize())
    target_interval : int  desired interval in minutes  (e.g. 20)
    base_interval   : int  interval of the input candles (e.g. 10)

    Returns
    ───────
    list of resampled candle dicts (same schema as normalize() output)
    """
    if not candles:
        return []

    if target_interval == base_interval:
        return candles  # nothing to do

    n = target_interval // base_interval  # candles per output bucket

    from collections import defaultdict

    # Group by date — each trading day resets the bucket index
    by_date = defaultdict(list)
    for c in candles:
        dt = _parse_time(c["time"])
        if isinstance(dt, str):
            continue  # unparseable timestamp — skip
        by_date[dt.date()].append((dt, c))

    resampled = []

    for date in sorted(by_date.keys()):
        day_entries = sorted(by_date[date], key=lambda x: x[0])

        # Slice into n-candle chunks aligned to market open
        for i in range(0, len(day_entries), n):
            chunk = day_entries[i : i + n]
            if not chunk:
                continue

            times  = [dt for dt, _  in chunk]
            cands  = [c  for _,  c  in chunk]

            open_  = cands[0]["open"]
            high_  = max(c["high"]   for c in cands)
            low_   = min(c["low"]    for c in cands)
            close_ = cands[-1]["close"]
            vol_   = sum(c["volume"] for c in cands)
            time_  = times[0].strftime("%Y-%m-%d %H:%M")

            body       = round(abs(close_ - open_), 2)
            upper_wick = round(high_ - max(open_, close_), 2)
            lower_wick = round(min(open_, close_) - low_, 2)
            color      = ("green"   if close_ > open_ else
                          "red"     if close_ < open_ else
                          "neutral")

            resampled.append({
                "time":         time_,
                "open":         open_,
                "high":         high_,
                "low":          low_,
                "close":        close_,
                "volume":       vol_,
                "body_length":  body,
                "upper_wick":   upper_wick,
                "lower_wick":   lower_wick,
                "candle_color": color,
            })

    print(
        f"[Resample] {len(candles)} × {base_interval}min  →  "
        f"{len(resampled)} × {target_interval}min candles"
    )
    return resampled


# ═══════════════════════════════════════════════════════════════════
#  BROKER-SPECIFIC FETCH HELPERS  (internal)
# ═══════════════════════════════════════════════════════════════════

def _fetch_zerodha(api, exchange, stock_token_id, days, interval):
    """
    Fetch raw OHLCV candles from the Zerodha (KiteConnect) API.
    Returns a list of normalised candle dicts.

    Custom intervals (e.g. 20, 25, 45 min) are supported automatically:
      → The function detects that the interval is not native to Zerodha.
      → It fetches the largest natively-supported base interval that divides
        evenly into the requested interval (e.g. 20 min → fetch 10 min).
      → It resamples the base candles into the desired interval.

    Zerodha per-request day limits:
      < 60 min  → max 60 days per chunk
      60–1439   → max 400 days per chunk
      day       → max 2000 days per chunk
    """
    # ── Detect custom (non-native) interval ───────────────────────
    is_custom   = isinstance(interval, int) and interval not in ZERODHA_INTERVAL_MAP
    base_interval = interval  # will be overridden for custom intervals

    if is_custom:
        base_interval = _find_base_interval(interval)
        print(
            f"[Data] Custom interval {interval}min detected — "
            f"fetching {base_interval}min base, then resampling."
        )

    fetch_interval = base_interval  # what we actually ask Zerodha for

    if fetch_interval not in ZERODHA_INTERVAL_MAP:
        valid = ", ".join(str(k) for k in ZERODHA_INTERVAL_MAP)
        raise ValueError(
            f"[Data] Zerodha does not support a {fetch_interval}-minute interval.\n"
            f"  → Valid native intervals (minutes): {valid}"
        )
    z_interval = ZERODHA_INTERVAL_MAP[fetch_interval]

    if fetch_interval == "D" or fetch_interval >= 1440:
        max_days_per_chunk = 2000
    elif fetch_interval >= 60:
        max_days_per_chunk = 400
    else:
        max_days_per_chunk = 60

    token_int = int(stock_token_id)
    today     = datetime.date.today()
    end_date  = today
    all_raw   = []
    remaining = int(days)

    print(f"[Data] Fetching {exchange}:{stock_token_id} from Zerodha ({z_interval}) …")

    while remaining > 0:
        chunk     = min(remaining, max_days_per_chunk)
        from_date = end_date - datetime.timedelta(days=chunk)

        print(f"[Data]   chunk: {from_date}  →  {end_date}  ({chunk} days)")

        raw_chunk = api.historical_data(
            instrument_token=token_int,
            from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end_date.strftime("%Y-%m-%d %H:%M:%S"),
            interval=z_interval,
            continuous=False,
            oi=False,
        )

        if raw_chunk:
            all_raw.extend(raw_chunk)

        end_date   = from_date - datetime.timedelta(days=1)
        remaining -= chunk

    if not all_raw:
        raise ValueError(
            f"[Data] Zerodha returned no data for token {stock_token_id} "
            f"({exchange}) over the last {days} days.\n"
            f"  → Check that the instrument_token is correct and the market\n"
            f"    has traded data in this date range."
        )

    all_raw.sort(key=lambda c: c["date"])
    print(f"[Data] Raw candles received: {len(all_raw)}")

    candles = normalize(all_raw, broker=BROKER_ZERODHA)

    # Resample to the originally requested custom interval if needed
    if is_custom:
        candles = resample_candles(candles, interval, base_interval)

    return candles


def _fetch_and_cache(exchange, stock_token_id, days, interval, api, broker):
    """Fetch, normalise, and cache candles. Returns normalised candle list."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if broker != BROKER_ZERODHA:
        raise ValueError(
            f"[Data] Unsupported broker '{broker}'. Supported: {BROKER_ZERODHA}"
        )

    candles   = _fetch_zerodha(api, exchange, stock_token_id, days, interval)
    save_name = _cache_filename(exchange, stock_token_id, days, interval, broker)
    save_path = DATA_DIR / save_name

    with open(save_path, "w") as f:
        json.dump(candles, f, indent=2)

    print(f"[Data] ✓ {len(candles)} candles saved → {save_name}")
    return candles


# ═══════════════════════════════════════════════════════════════════
#  PUBLIC API  —  get_historical
# ═══════════════════════════════════════════════════════════════════

def get_historical(
    exchange, stock_token_id, days, interval, bb_period, bb_stddev,
    api=None, broker=None, cred_path="All_cred.json"
):
    """
    Return a fully-prepared candle list (with Bollinger Bands).

    Cache policy
    ────────────
    Cache files live in  ./Assets/data/
    Fresh cache  (< CACHE_MAX_AGE_DAYS old) → loaded directly (no login needed).
    Stale / missing → broker API is called; new cache written.

    Parameters
    ──────────
    exchange        : str   e.g. "NSE"
    stock_token_id  : str   Zerodha instrument token  e.g. "256265"
    days            : int   calendar days of history to request
    interval        : int   candle interval in minutes  e.g. 15
    bb_period       : int   Bollinger Band period
    bb_stddev       : float Bollinger Band std-dev multiplier
    api             : broker API object (optional — logged in automatically if None)
    broker          : "zerodha" | None
    cred_path       : path to credentials JSON
    """
    from utils import add_bollinger_bands

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if broker is None:
        broker = get_broker_from_creds(cred_path) or BROKER_ZERODHA
    broker = broker.strip().lower()

    cached_path, fetch_date = _find_existing_cache(
        exchange, stock_token_id, days, interval, broker
    )

    if cached_path is not None and not _is_cache_stale(fetch_date):
        age_days = (datetime.date.today() - fetch_date).days
        print(
            f"[Data] ✓ Cache fresh ({age_days}d old, limit={CACHE_MAX_AGE_DAYS}d)"
            f" → {cached_path.name}"
        )
        with open(cached_path) as f:
            candles = json.load(f)
        print(f"[Data] Loaded {len(candles)} candles from cache.")

    else:
        if cached_path is not None:
            age_days = (datetime.date.today() - fetch_date).days
            print(
                f"[Data] Cache stale ({age_days}d old, limit={CACHE_MAX_AGE_DAYS}d)"
                f" → refreshing …"
            )
            _delete_stale_cache(cached_path)
        else:
            print(
                f"[Data] No cache for {broker.upper()}:{exchange}:{stock_token_id}"
                f" — fetching …"
            )

        if api is None:
            api, broker = login(broker=broker, cred_path=cred_path)

        candles = _fetch_and_cache(
            exchange, stock_token_id, days, interval, api, broker
        )

    return add_bollinger_bands(candles, bb_period, bb_stddev)


# ═══════════════════════════════════════════════════════════════════
#  PUBLIC API  —  get_live
# ═══════════════════════════════════════════════════════════════════

def get_live(
    api, exchange, stock_token_id, interval, bb_period, bb_stddev,
    broker=None, cred_path="All_cred.json"
):
    """
    Fetch the most recent candles directly from the broker (no cache).
    Fetches enough days to cover the Bollinger Band warmup period.

    Parameters
    ──────────
    api             : authenticated broker API object
    exchange        : str   e.g. "NSE"
    stock_token_id  : str   Zerodha instrument token
    interval        : int   candle interval in minutes
    bb_period       : int   Bollinger Band period
    bb_stddev       : float Bollinger Band std-dev multiplier
    broker          : "zerodha" | None
    cred_path       : path to credentials JSON
    """
    from utils import add_bollinger_bands

    if broker is None:
        broker = get_broker_from_creds(cred_path) or BROKER_ZERODHA
    broker = broker.strip().lower()

    candles_per_day = int((6.5 * 60) / interval)
    lookback_days   = max(10, math.ceil(bb_period / candles_per_day) + 3)

    if broker != BROKER_ZERODHA:
        raise ValueError(
            f"[Data] Unsupported broker '{broker}'. Supported: {BROKER_ZERODHA}"
        )

    candles = _fetch_zerodha(api, exchange, stock_token_id, lookback_days, interval)
    return add_bollinger_bands(candles, bb_period, bb_stddev)
