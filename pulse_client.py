# pulse_client.py
from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

import config_console as config


@dataclass(frozen=True)
class PricePoint:
    ts: int
    price: float
    count: int


_pulse_session: Optional[requests.Session] = None
_pulse_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _pulse_session
    with _pulse_lock:
        if _pulse_session is None:
            _pulse_session = requests.Session()
        return _pulse_session


def _build_headers() -> Dict[str, str]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": config.PULSE_ORIGIN,
        "referer": config.PULSE_REFERER,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }
    if config.PULSE_DEVICE_ID:
        headers["device-id"] = config.PULSE_DEVICE_ID
    if config.PULSE_AUTHORIZATION:
        headers["authorization"] = config.PULSE_AUTHORIZATION
    if config.PULSE_COOKIE:
        headers["cookie"] = config.PULSE_COOKIE
    return headers


def fetch_pulse_item_info(item_name: str) -> dict:
    """
    POST {PULSE_API_BASE_URL}/api/item/info
    payload: currencyOverride/gameType/market/marketHashName/minTimestamp/maxTimestamp
    """
    session = _get_session()
    headers = _build_headers()

    now_ts = int(time.time())

    fetch_hours = float(config.GRAPH_ANALYS_HOURS) + float(getattr(config, "PULSE_FETCH_EXTRA_HOURS", 0) or 0)
    min_ts = now_ts - int(fetch_hours * 3600)
    max_ts = now_ts

    payload = {
        "currencyOverride": config.PULSE_CURRENCY_OVERRIDE,
        "gameType": config.PULSE_GAME_TYPE,
        "market": config.PULSE_MARKET,
        "marketHashName": item_name,
        "minTimestamp": min_ts,
        "maxTimestamp": max_ts,
    }

    url = f"{config.PULSE_API_BASE_URL}/api/item/info"
    last_error: Optional[str] = None

    for attempt in range(config.PULSE_MAX_RETRIES + 1):
        if attempt > 0:
            delay = config.PULSE_429_DELAY_SEC * (config.PULSE_BACKOFF_MULT ** (attempt - 1))
            time.sleep(delay)
        elif config.PULSE_DELAY_SEC > 0:
            time.sleep(config.PULSE_DELAY_SEC)

        try:
            resp = session.post(url, json=payload, headers=headers, timeout=config.HTTP_TIMEOUT)

            if resp.status_code == 429:
                last_error = f"429 Too Many Requests (attempt {attempt + 1})"
                continue

            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                body_preview = resp.text[:500] if resp.text else "(empty)"
                raise RuntimeError(f"Pulse client error HTTP {resp.status_code}: {body_preview}")

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} server error (attempt {attempt + 1})"
                continue

            ct = resp.headers.get("content-type", "")
            if "application/json" not in ct.lower():
                body_preview = resp.text[:500] if resp.text else "(empty)"
                last_error = f"Non-JSON response (content-type: {ct}): {body_preview}"
                continue

            try:
                return resp.json()
            except json.JSONDecodeError as e:
                body_preview = resp.text[:500] if resp.text else "(empty)"
                last_error = f"JSON parse error: {e}; body: {body_preview}"
                continue

        except requests.RequestException as e:
            last_error = f"Network error: {e} (attempt {attempt + 1})"
            continue

    raise RuntimeError(f"Pulse API failed after retries. Last error: {last_error}")


def extract_history_points(data: dict) -> List[dict]:
    history = data.get("history")
    if not history or not history.get("canUseHistory", False):
        return []
    points = history.get("historyPoints")
    if not points:
        return []
    return points


def history_points_to_pricepoints(points: List[dict]) -> List[PricePoint]:
    out: List[PricePoint] = []
    for p in points:
        try:
            ts = int(p.get("timeSpan", 0))
            price = float(p.get("averagePrice", 0))
            count = int(p.get("count", 0))

            if ts > 10**12:
                ts //= 1000

            if ts <= 0 or price <= 0 or count < 0:
                continue
            out.append(PricePoint(ts=ts, price=price, count=count))
        except Exception:
            continue

    out.sort(key=lambda x: x.ts)
    return out


def fetch_history(item_name: str) -> List[PricePoint]:
    data = fetch_pulse_item_info(item_name)
    raw_points = extract_history_points(data)
    if not raw_points:
        raise RuntimeError(f"No historyPoints for item: {item_name}")

    pp = history_points_to_pricepoints(raw_points)
    if not pp:
        raise RuntimeError(f"No valid points after parsing for item: {item_name}")

    return pp
