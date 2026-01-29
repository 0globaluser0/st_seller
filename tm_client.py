# tm_client.py
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

import config_console as config
from pulse_client import PricePoint


_tm_session: Optional[requests.Session] = None
_tm_lock = threading.Lock()

_tm_name_to_id: Optional[Dict[str, int]] = None
_tm_mapping_loaded_at: float = 0.0


def _get_session() -> requests.Session:
    global _tm_session
    with _tm_lock:
        if _tm_session is None:
            _tm_session = requests.Session()
        return _tm_session


def _cache_path() -> Path:
    # cache file рядом с проектом/скриптом (чтобы сохранялось между запусками)
    # можно переопределить в config_console.py через TM_ALL_CACHE_PATH
    p = getattr(config, "TM_ALL_CACHE_PATH", "") or ""
    if p:
        return Path(p).expanduser()
    return Path(__file__).resolve().parent / "tm_full_history_all_cache.json"


def _cache_ttl_sec() -> int:
    try:
        return int(getattr(config, "TM_ALL_CACHE_TTL_SEC", 3600) or 3600)
    except Exception:
        return 3600


def _tm_base() -> str:
    return str(getattr(config, "TM_API_BASE_URL", "https://market.csgo.com/api/v2")).rstrip("/")


def _download_all_mapping() -> Dict[str, int]:
    """
    GET /full-history/all.json
    Response: {"history": {"item_name": item_id, ...}}
    """
    url = f"{_tm_base()}/full-history/all.json"
    sess = _get_session()
    resp = sess.get(url, timeout=getattr(config, "HTTP_TIMEOUT", 20))
    resp.raise_for_status()
    data = resp.json()
    hist = data.get("history")
    if not isinstance(hist, dict):
        raise RuntimeError("TM all.json: unexpected response format (missing 'history' dict)")
    # values can be int-like
    out: Dict[str, int] = {}
    for k, v in hist.items():
        try:
            out[str(k)] = int(v)
        except Exception:
            continue
    return out


def _load_mapping_from_disk() -> Optional[Dict[str, int]]:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        # TTL check by mtime
        ttl = _cache_ttl_sec()
        age = time.time() - p.stat().st_mtime
        if ttl > 0 and age > ttl:
            return None
        obj = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        hist = obj.get("history") if isinstance(obj, dict) else None
        if not isinstance(hist, dict):
            return None
        out: Dict[str, int] = {}
        for k, v in hist.items():
            try:
                out[str(k)] = int(v)
            except Exception:
                continue
        return out
    except Exception:
        return None


def _save_mapping_to_disk(mapping: Dict[str, int]) -> None:
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # сохраняем в формате близком к оригинальному
        payload = {"history": mapping}
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # кэш — опциональный
        pass


def get_tm_item_id(item_name: str, *, force_refresh: bool = False) -> Optional[int]:
    """
    Возвращает item_id для market.csgo.com (TM) из full-history/all.json.
    Использует in-memory + disk cache.
    """
    global _tm_name_to_id, _tm_mapping_loaded_at

    name = str(item_name)

    with _tm_lock:
        if not force_refresh and _tm_name_to_id is not None:
            return _tm_name_to_id.get(name)

    # вне lock грузим/качем, чтобы не держать lock на сетевых операциях
    mapping: Optional[Dict[str, int]] = None
    if not force_refresh:
        mapping = _load_mapping_from_disk()

    if mapping is None:
        mapping = _download_all_mapping()
        _save_mapping_to_disk(mapping)

    with _tm_lock:
        _tm_name_to_id = mapping
        _tm_mapping_loaded_at = time.time()
        return _tm_name_to_id.get(name)


def fetch_tm_history(item_name: str) -> List[PricePoint]:
    """
    Получает историю продаж с market.csgo.com (TM) в USD.

    - Каждая точка = одна продажа (count=1)
    - Возвращает List[PricePoint] отсортированный по ts
    """
    item_id = get_tm_item_id(item_name)
    if item_id is None:
        raise RuntimeError(f"TM: item not found in full-history/all.json: {item_name!r}")

    url = f"{_tm_base()}/full-history/{int(item_id)}.json"
    sess = _get_session()
    resp = sess.get(url, timeout=getattr(config, "HTTP_TIMEOUT", 20))
    resp.raise_for_status()
    data = resp.json()
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        raise RuntimeError("TM detail: unexpected response format (missing 'data')")
    hist = d.get("history")
    if not isinstance(hist, list):
        raise RuntimeError("TM detail: unexpected response format (missing 'history' list)")

    points: List[PricePoint] = []
    for row in hist:
        # row: [timestamp, price_rub, price_usd, price_eur]
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            ts = int(row[0])
            price_usd = float(row[2])
        except Exception:
            continue
        if price_usd <= 0:
            continue
        points.append(PricePoint(ts=ts, price=price_usd, count=1))

    if not points:
        raise RuntimeError(f"TM: no valid USD history points for {item_name!r}")

    points.sort(key=lambda p: p.ts)
    return points


def count_sales_last_days(points: List[PricePoint], days: float = 2.0, *, now_ts: Optional[int] = None) -> int:
    """
    Для TM: каждая точка = 1 продажа (count всегда 1),
    но считаем по точкам (не по count), чтобы не зависеть от структуры.
    """
    if not points:
        return 0
    now = int(time.time()) if now_ts is None else int(now_ts)
    t_min = now - int(days * 86400)
    return sum(1 for p in points if p.ts >= t_min)
