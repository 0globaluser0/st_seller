# steam_market_client.py
"""
Steam Community Market API client.

Fetches price data directly from Steam's JSON endpoints:
- pricehistory: sales graph data (history points)
- priceoverview: current lowest listing and median price

All requests require:
- Cookies: sessionid + steamLoginSecure
- Proxy: MANDATORY (requests without proxy are blocked)
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

import config_console as config
from pulse_client import PricePoint


# ---------------------------------------------------------------------------
# SteamAccount dataclass (new format with currency_id)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SteamAccount:
    """
    Steam account data from steam_accs.txt.
    New format: name\\currency_id\\http_proxy(user:pass@ip:port)\\sessionid\\SteamLoginSecure

    currency_id: 37 = KZT, 5 = RUB, 1 = USD, etc.
    """
    name: str
    currency_id: int
    http_proxy: str
    sessionid: str
    steam_login_secure: str


def read_steam_accs_txt(path) -> List[SteamAccount]:
    """
    Parses steam_accs.txt, one line = one account:
      name\\currency_id\\http://user:pass@ip:port\\sessionid\\SteamLoginSecure

    Empty lines and lines starting with # are ignored.
    Returns only accounts with valid (non-empty) proxy.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: List[SteamAccount] = []
    invalid_proxy_count = 0

    for ln, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue

        parts = s.split("\\\\")
        if len(parts) != 5:
            # fallback: single backslash separator
            parts = s.split("\\")
        if len(parts) != 5:
            raise ValueError(
                f"{path}: line {ln}: expected 5 fields separated by \\\\ ; "
                f"got {len(parts)}: {raw!r}"
            )

        name, currency_str, proxy, sessionid, loginsecure = (p.strip() for p in parts)

        # Validate currency_id
        try:
            currency_id = int(currency_str)
        except ValueError:
            raise ValueError(
                f"{path}: line {ln}: currency_id must be int, got: {currency_str!r}"
            )

        # Validate proxy is non-empty (MANDATORY)
        if not proxy:
            print(f"[WARNING] {path}: line {ln}: account '{name}' has empty proxy - SKIPPED")
            invalid_proxy_count += 1
            continue

        # Validate sessionid/steamLoginSecure
        if not sessionid or not loginsecure:
            raise ValueError(
                f"{path}: line {ln}: sessionid and steamLoginSecure must be non-empty"
            )

        out.append(SteamAccount(
            name=name,
            currency_id=currency_id,
            http_proxy=proxy,
            sessionid=sessionid,
            steam_login_secure=loginsecure,
        ))

    if invalid_proxy_count > 0:
        print(f"[WARNING] Skipped {invalid_proxy_count} accounts with empty/invalid proxy")

    return out


def validate_accounts_or_exit(accounts: List[SteamAccount]) -> None:
    """
    Validates that at least one account with valid proxy exists.
    Exits with error if no valid accounts found.
    """
    if not accounts:
        raise SystemExit(
            "[FATAL] No valid Steam accounts found. "
            "All accounts must have non-empty proxy. "
            "Check steam_accs.txt format: name\\\\currency_id\\\\proxy\\\\sessionid\\\\steamLoginSecure"
        )


# ---------------------------------------------------------------------------
# HTTP Session management
# ---------------------------------------------------------------------------

_steam_sessions: Dict[str, requests.Session] = {}
_steam_lock = threading.Lock()


def _make_steam_session(acc: SteamAccount) -> requests.Session:
    """
    Creates authenticated requests.Session with cookies and proxy.
    Proxy is MANDATORY - if empty, raises error.
    """
    if not acc.http_proxy:
        raise RuntimeError(
            f"[SKIP] Steam request blocked: proxy is required (account={acc.name})"
        )

    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Set cookies
    s.cookies.set("sessionid", acc.sessionid, domain="steamcommunity.com", path="/")
    s.cookies.set("steamLoginSecure", acc.steam_login_secure, domain="steamcommunity.com", path="/")

    # Set proxy (MANDATORY)
    s.proxies.update({"http": acc.http_proxy, "https": acc.http_proxy})

    return s


def _get_steam_session(acc: SteamAccount) -> requests.Session:
    """Gets or creates a session for the given account."""
    key = f"{acc.name}:{acc.currency_id}"
    with _steam_lock:
        if key not in _steam_sessions:
            _steam_sessions[key] = _make_steam_session(acc)
        return _steam_sessions[key]


# ---------------------------------------------------------------------------
# Price string parsing
# ---------------------------------------------------------------------------

def parse_price_str(s: Optional[str]) -> Optional[float]:
    """
    Parses Steam price strings like "$0.12", "0,12 pуб.", "123,45 EUR", etc.

    Logic:
    - Remove everything except digits, '.', ',' and whitespace
    - If both '.' and ',' present: the LAST one is the decimal separator,
      the other is thousands separator (remove it)
    - Replace decimal separator with '.'
    - Remove whitespace/nbsp as thousands separators
    - Convert to float

    Returns None if parsing fails or string is empty.
    """
    if not s:
        return None

    # Remove currency symbols and non-numeric chars except . , and space
    cleaned = re.sub(r'[^\d.,\s]', '', s)
    cleaned = cleaned.strip()

    if not cleaned:
        return None

    # Remove spaces/nbsp (thousands separator)
    cleaned = re.sub(r'\s+', '', cleaned)

    has_dot = '.' in cleaned
    has_comma = ',' in cleaned

    if has_dot and has_comma:
        # Determine which is the decimal separator (the LAST one)
        last_dot = cleaned.rfind('.')
        last_comma = cleaned.rfind(',')

        if last_comma > last_dot:
            # Comma is decimal separator, dots are thousands
            cleaned = cleaned.replace('.', '')
            cleaned = cleaned.replace(',', '.')
        else:
            # Dot is decimal separator, commas are thousands
            cleaned = cleaned.replace(',', '')
    elif has_comma:
        # Comma is decimal separator
        cleaned = cleaned.replace(',', '.')
    # else: only dots or no separators - already in correct format

    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# pricehistory date parsing
# ---------------------------------------------------------------------------

def _parse_pricehistory_date(date_str: str) -> Optional[datetime]:
    """
    Parses Steam pricehistory date format: "Nov 27 2013 01: +0"

    The format is: "%b %d %Y %H:" followed by timezone offset.
    Returns datetime in UTC.
    """
    if not date_str:
        return None

    # Remove timezone suffix like " +0" or " -0"
    cleaned = re.sub(r'\s*[+-]\d+$', '', date_str.strip())

    try:
        # Format: "Nov 27 2013 01:" (hour with colon at the end)
        dt = datetime.strptime(cleaned, "%b %d %Y %H:")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    try:
        # Try without the trailing colon
        cleaned_no_colon = cleaned.rstrip(':')
        dt = datetime.strptime(cleaned_no_colon, "%b %d %Y %H")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    return None


# ---------------------------------------------------------------------------
# Sale dataclass
# ---------------------------------------------------------------------------

@dataclass
class Sale:
    """Single sale point from pricehistory."""
    dt: datetime
    ts: int  # Unix timestamp
    price: float
    volume: int


# ---------------------------------------------------------------------------
# API: pricehistory
# ---------------------------------------------------------------------------

def fetch_pricehistory_sales(
    acc: SteamAccount,
    market_hash_name: str,
    appid: int = 730,
    currency_id: Optional[int] = None,
) -> List[Sale]:
    """
    Fetches price history (sales graph) from Steam Community Market.

    GET https://steamcommunity.com/market/pricehistory/
        ?appid=730&market_hash_name=<URL_ENCODED_NAME>&currency=<CURRENCY_ID>

    Response: {"success":true,"price_prefix":"...","price_suffix":"...",
               "prices":[[date_str, median_price_number, volume_str], ...]}

    Returns list of Sale sorted by timestamp.
    Raises RuntimeError on errors.
    """
    if not acc.http_proxy:
        raise RuntimeError(
            f"[SKIP] Steam request blocked: proxy is required (account={acc.name})"
        )

    if currency_id is None:
        currency_id = acc.currency_id

    session = _get_steam_session(acc)
    url = "https://steamcommunity.com/market/pricehistory/"
    params = {
        "appid": appid,
        "market_hash_name": market_hash_name,
        "currency": currency_id,
    }

    timeout = getattr(config, "STEAM_HTTP_TIMEOUT", 20)
    max_retries = getattr(config, "STEAM_MAX_RETRIES", 4)
    backoff_mult = getattr(config, "STEAM_BACKOFF_MULT", 2.0)
    delay_429 = getattr(config, "STEAM_429_DELAY_SEC", 5.0)
    base_delay = getattr(config, "STEAM_DELAY_SEC", 0.3)

    last_error: Optional[str] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = delay_429 * (backoff_mult ** (attempt - 1))
            time.sleep(delay)
        elif base_delay > 0:
            time.sleep(base_delay)

        try:
            resp = session.get(url, params=params, timeout=timeout)

            if resp.status_code == 429:
                last_error = f"429 Too Many Requests (attempt {attempt + 1})"
                continue

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} server error (attempt {attempt + 1})"
                continue

            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                body_preview = resp.text[:300] if resp.text else "(empty)"
                raise RuntimeError(
                    f"Steam pricehistory HTTP {resp.status_code}: {body_preview}"
                )

            data = resp.json()

            if not data.get("success"):
                raise RuntimeError(
                    f"Steam pricehistory success=false for {market_hash_name!r}"
                )

            prices = data.get("prices")
            if not prices:
                raise RuntimeError(
                    f"Steam pricehistory: no prices for {market_hash_name!r}"
                )

            sales: List[Sale] = []
            for row in prices:
                if not isinstance(row, (list, tuple)) or len(row) < 3:
                    continue

                date_str = str(row[0])
                dt = _parse_pricehistory_date(date_str)
                if dt is None:
                    continue

                try:
                    price = float(row[1])
                except (ValueError, TypeError):
                    continue

                try:
                    volume = int(row[2].replace(',', '')) if isinstance(row[2], str) else int(row[2])
                except (ValueError, TypeError):
                    volume = 1

                if price <= 0 or volume <= 0:
                    continue

                sales.append(Sale(
                    dt=dt,
                    ts=int(dt.timestamp()),
                    price=price,
                    volume=volume,
                ))

            if not sales:
                raise RuntimeError(
                    f"Steam pricehistory: no valid sales for {market_hash_name!r}"
                )

            sales.sort(key=lambda x: x.ts)
            return sales

        except requests.RequestException as e:
            last_error = f"Network error: {e} (attempt {attempt + 1})"
            continue

    raise RuntimeError(
        f"Steam pricehistory failed after retries. Last error: {last_error}"
    )


def sales_to_pricepoints(sales: List[Sale]) -> List[PricePoint]:
    """Converts Sale list to PricePoint list for analyzer compatibility."""
    return [
        PricePoint(ts=s.ts, price=s.price, count=s.volume)
        for s in sales
    ]


# ---------------------------------------------------------------------------
# API: priceoverview
# ---------------------------------------------------------------------------

@dataclass
class PriceOverview:
    """Result from priceoverview API."""
    lowest_price: Optional[float]
    median_price: Optional[float]
    volume: Optional[int]


def fetch_priceoverview(
    acc: SteamAccount,
    market_hash_name: str,
    appid: int = 730,
    currency_id: Optional[int] = None,
    country: str = "US",
) -> PriceOverview:
    """
    Fetches price overview (current lowest listing and median) from Steam.

    GET https://steamcommunity.com/market/priceoverview/
        ?appid=730&market_hash_name=<URL_ENCODED_NAME>&currency=<CURRENCY_ID>&country=US

    Response: {"success":true,"lowest_price":"$0.12","median_price":"$0.13","volume":"123"}

    Returns PriceOverview with parsed float values.
    """
    if not acc.http_proxy:
        raise RuntimeError(
            f"[SKIP] Steam request blocked: proxy is required (account={acc.name})"
        )

    if currency_id is None:
        currency_id = acc.currency_id

    session = _get_steam_session(acc)
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": appid,
        "market_hash_name": market_hash_name,
        "currency": currency_id,
        "country": country,
    }

    timeout = getattr(config, "STEAM_HTTP_TIMEOUT", 20)
    max_retries = getattr(config, "STEAM_MAX_RETRIES", 4)
    backoff_mult = getattr(config, "STEAM_BACKOFF_MULT", 2.0)
    delay_429 = getattr(config, "STEAM_429_DELAY_SEC", 5.0)
    base_delay = getattr(config, "STEAM_DELAY_SEC", 0.3)

    last_error: Optional[str] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = delay_429 * (backoff_mult ** (attempt - 1))
            time.sleep(delay)
        elif base_delay > 0:
            time.sleep(base_delay)

        try:
            resp = session.get(url, params=params, timeout=timeout)

            if resp.status_code == 429:
                last_error = f"429 Too Many Requests (attempt {attempt + 1})"
                continue

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} server error (attempt {attempt + 1})"
                continue

            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                body_preview = resp.text[:300] if resp.text else "(empty)"
                raise RuntimeError(
                    f"Steam priceoverview HTTP {resp.status_code}: {body_preview}"
                )

            data = resp.json()

            if not data.get("success"):
                # This can happen for items with no listings
                return PriceOverview(
                    lowest_price=None,
                    median_price=None,
                    volume=None,
                )

            lowest = parse_price_str(data.get("lowest_price"))
            median = parse_price_str(data.get("median_price"))

            vol_str = data.get("volume")
            volume = None
            if vol_str:
                try:
                    volume = int(str(vol_str).replace(',', ''))
                except ValueError:
                    pass

            return PriceOverview(
                lowest_price=lowest,
                median_price=median,
                volume=volume,
            )

        except requests.RequestException as e:
            last_error = f"Network error: {e} (attempt {attempt + 1})"
            continue

    raise RuntimeError(
        f"Steam priceoverview failed after retries. Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# FX Rate calculation with caching
# ---------------------------------------------------------------------------

@dataclass
class FXCacheEntry:
    """Cached FX rate entry."""
    local_per_usd: float
    timestamp: float


_fx_cache: Dict[int, FXCacheEntry] = {}
_fx_lock = threading.Lock()


def get_local_per_usd(acc: SteamAccount) -> float:
    """
    Gets Steam-implied FX rate: how many units of local currency per 1 USD.

    Uses benchmark item (config.STEAM_FX_BENCHMARK_NAME, default "Fracture Case")
    to calculate the rate by comparing priceoverview in local currency vs USD.

    Result is cached per currency_id with TTL from config.STEAM_FX_CACHE_TTL_SEC.

    If account currency is already USD (currency_id=1), returns 1.0.
    """
    if acc.currency_id == 1:
        return 1.0

    cache_ttl = getattr(config, "STEAM_FX_CACHE_TTL_SEC", 6 * 3600)  # 6 hours default

    with _fx_lock:
        entry = _fx_cache.get(acc.currency_id)
        if entry is not None:
            age = time.time() - entry.timestamp
            if age < cache_ttl:
                return entry.local_per_usd

    # Need to fetch fresh rate
    benchmark_name = getattr(config, "STEAM_FX_BENCHMARK_NAME", "Fracture Case")

    # Fetch in local currency
    local_overview = fetch_priceoverview(
        acc,
        benchmark_name,
        currency_id=acc.currency_id,
    )

    # Fetch in USD
    usd_overview = fetch_priceoverview(
        acc,
        benchmark_name,
        currency_id=1,
    )

    # Prefer median_price, fallback to lowest_price
    local_px = local_overview.median_price or local_overview.lowest_price
    usd_px = usd_overview.median_price or usd_overview.lowest_price

    if not local_px or local_px <= 0:
        raise RuntimeError(
            f"Cannot get FX rate: no valid local price for benchmark {benchmark_name!r} "
            f"(currency_id={acc.currency_id})"
        )

    if not usd_px or usd_px <= 0:
        raise RuntimeError(
            f"Cannot get FX rate: no valid USD price for benchmark {benchmark_name!r}"
        )

    local_per_usd = local_px / usd_px

    with _fx_lock:
        _fx_cache[acc.currency_id] = FXCacheEntry(
            local_per_usd=local_per_usd,
            timestamp=time.time(),
        )

    return local_per_usd


# ---------------------------------------------------------------------------
# Main computation: Steam rec prices
# ---------------------------------------------------------------------------

@dataclass
class SteamRecResult:
    """Result of Steam rec price computation."""
    # Native currency values (no conversion)
    steam_rec_from_graph_native: float
    steam_lowest_native: Optional[float]
    steam_rec_native: float

    # FX conversion
    fx_local_per_usd: float
    currency_id: int

    # USD values (for comparison and Pulse upload)
    steam_rec_usd: float

    # Raw data for debugging
    sales_count: int
    pricepoints: List[PricePoint]


def compute_steam_rec_prices(
    acc: SteamAccount,
    market_hash_name: str,
    appid: int = 730,
) -> SteamRecResult:
    """
    Computes Steam rec prices using pricehistory and priceoverview.

    Logic:
    1. Fetch pricehistory -> get sales points -> compute rec_price using analyzer
       (steam_rec_from_graph_native, in native currency)
    2. Fetch priceoverview -> get lowest_price (steam_lowest_native, in native currency)
    3. steam_rec_native = max(steam_rec_from_graph_native, steam_lowest_native)
       This comparison is done WITHOUT conversion (per spec)
    4. Get FX rate (local_per_usd) via benchmark priceoverview
    5. steam_rec_usd = steam_rec_native / fx_local_per_usd

    Returns SteamRecResult with all values for logging.
    """
    from analyzer import compute_support_dual

    # 1. Fetch pricehistory and compute rec from graph
    sales = fetch_pricehistory_sales(acc, market_hash_name, appid=appid)
    pricepoints = sales_to_pricepoints(sales)

    dual_result = compute_support_dual(pricepoints)
    steam_rec_from_graph_native = float(dual_result.min_support_price)

    # 2. Fetch priceoverview for lowest_price
    overview = fetch_priceoverview(acc, market_hash_name, appid=appid)
    steam_lowest_native = overview.lowest_price

    # 3. Compute steam_rec_native = max(graph_rec, lowest) - comparison in native currency
    if steam_lowest_native is not None and steam_lowest_native > 0:
        steam_rec_native = max(steam_rec_from_graph_native, steam_lowest_native)
    else:
        steam_rec_native = steam_rec_from_graph_native

    # 4. Get FX rate
    fx_local_per_usd = get_local_per_usd(acc)

    # 5. Convert to USD
    steam_rec_usd = steam_rec_native / fx_local_per_usd

    return SteamRecResult(
        steam_rec_from_graph_native=steam_rec_from_graph_native,
        steam_lowest_native=steam_lowest_native,
        steam_rec_native=steam_rec_native,
        fx_local_per_usd=fx_local_per_usd,
        currency_id=acc.currency_id,
        steam_rec_usd=steam_rec_usd,
        sales_count=len(sales),
        pricepoints=pricepoints,
    )
