# steam_market_client.py
"""
Steam Community Market JSON client.

Endpoints used (JSON only, NO HTML parsing):
  - pricehistory:  GET /market/pricehistory/?appid=&market_hash_name=&currency=
  - priceoverview: GET /market/priceoverview/?appid=&market_hash_name=&currency=&country=US

All requests go through account proxy + cookies (sessionid, steamLoginSecure).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

import config_console as config
from pulse_client import PricePoint
from steam_inventory import SteamAccount, make_session
from analyzer import compute_support_dual

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# A) parse_price_str
# ──────────────────────────────────────────────

def parse_price_str(s: str) -> float:
    """
    Parse a Steam price string like "$0.12", "0,12€", "1 234,56 pуб." etc.
    - Strip everything except digits, spaces, ',' and '.'
    - Determine decimal separator (the one that appears LAST among '.' and ',')
    - The other separator and spaces are treated as thousands grouping and removed.
    - Return float.
    """
    if not s:
        raise ValueError("parse_price_str: empty/None input")

    # keep only digits, spaces, commas, dots
    cleaned = re.sub(r"[^\d\s,.]", "", s)
    # collapse multiple spaces / nbsp
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        raise ValueError(f"parse_price_str: no numeric chars in {s!r}")

    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")

    if last_dot >= 0 and last_comma >= 0:
        # both present — last one is decimal separator
        if last_dot > last_comma:
            # '.' is decimal, ',' is thousands
            cleaned = cleaned.replace(",", "").replace(" ", "")
        else:
            # ',' is decimal, '.' is thousands
            cleaned = cleaned.replace(".", "").replace(" ", "").replace(",", ".")
    elif last_comma >= 0:
        # only comma — treat as decimal
        cleaned = cleaned.replace(" ", "").replace(",", ".")
    else:
        # only dot or neither
        cleaned = cleaned.replace(" ", "")

    return float(cleaned)


# ──────────────────────────────────────────────
# Internal HTTP helpers
# ──────────────────────────────────────────────

# Per-account session cache (thread-safe)
_sessions: Dict[str, requests.Session] = {}
_sessions_lock = threading.Lock()


def _get_session(acc: SteamAccount) -> requests.Session:
    """Return (or create) a requests.Session for the given account."""
    key = acc.name
    with _sessions_lock:
        s = _sessions.get(key)
        if s is not None:
            return s
        s = make_session(acc)
        _sessions[key] = s
        return s


def _steam_get(acc: SteamAccount, url: str, params: dict) -> requests.Response:
    """
    GET request to Steam with retry/backoff.
    Raises on non-retryable errors.
    """
    if not acc.http_proxy:
        raise RuntimeError(
            f"[SKIP] Steam request blocked: proxy is required (account={acc.name})"
        )

    session = _get_session(acc)

    max_retries = int(getattr(config, "STEAM_MAX_RETRIES", 4))
    backoff_mult = float(getattr(config, "STEAM_BACKOFF_MULT", 1.7))
    delay_429 = float(getattr(config, "STEAM_429_DELAY_SEC", 3.0))
    timeout = getattr(config, "STEAM_HTTP_TIMEOUT", 25)
    delay_sec = float(getattr(config, "STEAM_DELAY_SEC", 0.0))

    last_error: Optional[str] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = delay_429 * (backoff_mult ** (attempt - 1))
            time.sleep(wait)
        elif delay_sec > 0:
            time.sleep(delay_sec)

        try:
            resp = session.get(url, params=params, timeout=timeout)

            if resp.status_code == 429:
                last_error = f"429 Too Many Requests (attempt {attempt + 1})"
                continue

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} server error (attempt {attempt + 1})"
                continue

            if 400 <= resp.status_code < 500:
                resp.raise_for_status()

            return resp

        except requests.RequestException as e:
            last_error = f"Network error: {e} (attempt {attempt + 1})"
            continue

    raise RuntimeError(f"Steam request failed after retries: {last_error}")


# ──────────────────────────────────────────────
# B) fetch_priceoverview
# ──────────────────────────────────────────────

def fetch_priceoverview(
    acc: SteamAccount,
    market_hash_name: str,
    appid: int = 730,
    currency_id: Optional[int] = None,
) -> Tuple[float, Optional[float], float]:
    """
    GET /market/priceoverview/
    Returns: (lowest, median_or_None, chosen)
    chosen = median if available else lowest.
    """
    cid = currency_id if currency_id is not None else acc.currency_id

    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": appid,
        "market_hash_name": market_hash_name,
        "currency": cid,
        "country": "US",
    }

    resp = _steam_get(acc, url, params)
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(
            f"priceoverview success=false for {market_hash_name!r} (currency={cid}): {data}"
        )

    lowest: Optional[float] = None
    median: Optional[float] = None

    raw_lowest = data.get("lowest_price")
    raw_median = data.get("median_price")

    if raw_lowest:
        try:
            lowest = parse_price_str(raw_lowest)
        except ValueError:
            pass

    if raw_median:
        try:
            median = parse_price_str(raw_median)
        except ValueError:
            pass

    if lowest is None and median is None:
        raise RuntimeError(
            f"priceoverview: no parseable price for {market_hash_name!r} (currency={cid}): {data}"
        )

    chosen = median if median is not None else lowest  # type: ignore[assignment]
    if lowest is None:
        lowest = median  # type: ignore[assignment]

    return float(lowest), median, float(chosen)  # type: ignore[arg-type]


# ──────────────────────────────────────────────
# C) fetch_pricehistory_pricepoints
# ──────────────────────────────────────────────

# Regex to strip timezone suffix like " +0" or " -0300"
_TZ_SUFFIX_RE = re.compile(r"\s[+-]\d+$")


def _parse_history_date(date_str: str) -> int:
    """
    Parse Steam pricehistory date string.
    Example: "Nov 27 2013 01: +0"
    1) Strip tz suffix (regex)
    2) Strip trailing colon from hour ("01:" -> "01")
    3) strptime "%b %d %Y %H"
    4) Return Unix timestamp (UTC)
    """
    s = _TZ_SUFFIX_RE.sub("", date_str).strip()
    # Remove trailing colon from hour part: "01:" -> "01"
    s = re.sub(r":$", "", s).strip()
    dt = datetime.strptime(s, "%b %d %Y %H")
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_pricehistory_pricepoints(
    acc: SteamAccount,
    market_hash_name: str,
    appid: int = 730,
    currency_id: Optional[int] = None,
) -> List[PricePoint]:
    """
    GET /market/pricehistory/
    Returns sorted list[PricePoint].
    """
    cid = currency_id if currency_id is not None else acc.currency_id

    url = "https://steamcommunity.com/market/pricehistory/"
    params = {
        "appid": appid,
        "market_hash_name": market_hash_name,
        "currency": cid,
    }

    resp = _steam_get(acc, url, params)
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(
            f"pricehistory success=false for {market_hash_name!r} (currency={cid}): {data}"
        )

    raw_prices = data.get("prices")
    if not raw_prices:
        raise RuntimeError(f"pricehistory: empty prices for {market_hash_name!r}")

    points: List[PricePoint] = []
    for entry in raw_prices:
        try:
            date_str = str(entry[0])
            price = float(entry[1])
            volume = int(entry[2].replace(",", "")) if isinstance(entry[2], str) else int(entry[2])

            if price <= 0 or volume <= 0:
                continue

            ts = _parse_history_date(date_str)
            points.append(PricePoint(ts=ts, price=price, count=volume))
        except Exception:
            continue

    points.sort(key=lambda p: p.ts)

    if not points:
        raise RuntimeError(f"pricehistory: no valid points for {market_hash_name!r}")

    return points


# ──────────────────────────────────────────────
# D) FX cache  (Steam-implied exchange rate)
# ──────────────────────────────────────────────

# _fx_cache: currency_id -> (local_per_usd, timestamp_cached)
_fx_cache: Dict[int, Tuple[float, float]] = {}
_fx_lock = threading.Lock()


def get_local_per_usd(acc: SteamAccount) -> float:
    """
    Compute Steam-implied FX rate: how many local currency units per 1 USD.
    Uses benchmark item priceoverview in both local currency and USD.
    Cached in-memory with TTL = config.STEAM_FX_CACHE_TTL_SEC.

    For USD accounts (currency_id=1) returns 1.0 immediately.
    """
    cid = acc.currency_id
    if cid == 1:
        return 1.0

    ttl = float(getattr(config, "STEAM_FX_CACHE_TTL_SEC", 6 * 3600))
    now = time.time()

    with _fx_lock:
        cached = _fx_cache.get(cid)
        if cached is not None:
            rate, ts = cached
            if now - ts < ttl:
                return rate

    benchmark = str(getattr(config, "STEAM_FX_BENCHMARK_NAME", "Fracture Case"))

    # local quote
    _, _, local_chosen = fetch_priceoverview(acc, benchmark, currency_id=cid)
    # usd quote
    _, _, usd_chosen = fetch_priceoverview(acc, benchmark, currency_id=1)

    if local_chosen <= 0 or usd_chosen <= 0:
        raise RuntimeError(
            f"FX benchmark quotes invalid: local={local_chosen}, usd={usd_chosen} "
            f"(benchmark={benchmark!r}, currency_id={cid})"
        )

    local_per_usd = local_chosen / usd_chosen

    with _fx_lock:
        _fx_cache[cid] = (local_per_usd, time.time())

    return local_per_usd


# ──────────────────────────────────────────────
# E) compute_steam_rec_prices  (main entry point)
# ──────────────────────────────────────────────

def compute_steam_rec_prices(acc: SteamAccount, market_hash_name: str) -> dict:
    """
    Full Steam rec-price computation:
    1) pricehistory -> analyzer -> steam_rec_from_graph_native
    2) priceoverview -> steam_lowest_native
    3) steam_rec_native = max(graph, lowest)
    4) FX -> steam_rec_usd

    Returns dict with all intermediate values.
    """
    # 1) Graph analysis
    points = fetch_pricehistory_pricepoints(acc, market_hash_name)
    steam_dual = compute_support_dual(points)
    steam_rec_from_graph_native = float(steam_dual.min_support_price)

    # 2) Lowest listing
    lowest, median, _ = fetch_priceoverview(acc, market_hash_name, currency_id=acc.currency_id)
    steam_lowest_native = float(lowest)

    # 3) Native rec = max
    steam_rec_native = max(steam_rec_from_graph_native, steam_lowest_native)

    # 4) Convert to USD
    fx_local_per_usd = get_local_per_usd(acc)
    steam_rec_usd = steam_rec_native / fx_local_per_usd

    return {
        "points": points,
        "steam_dual": steam_dual,
        "steam_rec_from_graph_native": steam_rec_from_graph_native,
        "steam_lowest_native": steam_lowest_native,
        "steam_rec_native": steam_rec_native,
        "fx_local_per_usd": fx_local_per_usd,
        "steam_rec_usd": steam_rec_usd,
        "currency_id": acc.currency_id,
    }
