# steam_inventory.py
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

INV_URL = "https://steamcommunity.com/inventory/{steamid}/{appid}/{contextid}"


@dataclass(frozen=True)
class SteamAccount:
    """
    Данные из steam_accs.txt (5 полей):
      name\\currency_id\\http_proxy(user:pass@ip:port)\\sessionid\\SteamLoginSecure
    currency_id: 37=KZT, 5=RUB, 1=USD и т.д.
    """
    name: str
    currency_id: int
    http_proxy: str
    sessionid: str
    steam_login_secure: str


@dataclass
class NameFlags:
    market_hash_name: str
    tradable: bool
    marketable: bool


def _normalize_proxy(proxy: str) -> str:
    """Нормализует proxy: добавляет http:// если нет схемы."""
    p = proxy.strip()
    if not p:
        return ""
    if not p.startswith("http://") and not p.startswith("https://"):
        p = "http://" + p
    return p


def read_steam_accs_txt(path) -> list[SteamAccount]:
    """
    Парсит steam_accs.txt, одна строка = один аккаунт (5 полей):
      name\\currency_id\\http_proxy(user:pass@ip:port)\\sessionid\\SteamLoginSecure
    Пустые строки и строки, начинающиеся с #, игнорируются.
    Аккаунты с пустым proxy/sessionid/steamLoginSecure пропускаются (warning).
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[SteamAccount] = []

    for ln, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue

        parts = s.split("\\\\")
        if len(parts) != 5:
            parts = s.split("\\")
        if len(parts) != 5:
            print(f"[WARN] {path}: line {ln}: expected 5 fields separated by \\\\ ; got {len(parts)}: {raw!r}  -> SKIP")
            continue

        name, currency_id_str, proxy_raw, sessionid, loginsecure = (p.strip() for p in parts)

        # currency_id
        try:
            currency_id = int(currency_id_str)
        except (ValueError, TypeError):
            print(f"[WARN] {path}: line {ln}: invalid currency_id={currency_id_str!r} -> SKIP")
            continue

        # proxy обязателен
        proxy = _normalize_proxy(proxy_raw)
        if not proxy:
            print(f"[WARN] {path}: line {ln}: proxy is empty for account={name!r} -> SKIP")
            continue

        # sessionid / steamLoginSecure обязательны
        if not sessionid:
            print(f"[WARN] {path}: line {ln}: sessionid is empty for account={name!r} -> SKIP")
            continue
        if not loginsecure:
            print(f"[WARN] {path}: line {ln}: steamLoginSecure is empty for account={name!r} -> SKIP")
            continue

        out.append(SteamAccount(
            name=name,
            currency_id=currency_id,
            http_proxy=proxy,
            sessionid=sessionid,
            steam_login_secure=loginsecure,
        ))

    return out


def make_session(acc: SteamAccount) -> requests.Session:
    if not acc.http_proxy:
        raise ValueError(
            f"[FATAL] Proxy is required for account={acc.name!r} but is empty/missing. "
            "All Steam requests MUST go through a proxy."
        )

    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    s.cookies.set("steamLoginSecure", acc.steam_login_secure, domain="steamcommunity.com", path="/")
    s.cookies.set("sessionid", acc.sessionid, domain="steamcommunity.com", path="/")

    s.proxies.update({"http": acc.http_proxy, "https": acc.http_proxy})

    return s


def _ckey(classid: Any, instanceid: Any) -> Tuple[str, str]:
    return (str(classid), str(instanceid))


def resolve_steamid64(session: requests.Session, steam_login_secure: str) -> str:
    """
    1) steamLoginSecure часто начинается с 17-значного steamid64
    2) /my/inventory/ -> редирект на /profiles/<steamid64>/...
    3) парсинг HTML на /my/inventory/
    """
    m = re.match(r"^(\d{17})", (steam_login_secure or "").strip())
    if m:
        return m.group(1)

    r = session.get("https://steamcommunity.com/my/inventory/", timeout=30, allow_redirects=True)
    r.raise_for_status()

    m = re.search(r"/profiles/(\d{17})", r.url)
    if m:
        return m.group(1)

    html = r.text
    m = re.search(r'g_steamID\s*=\s*"(\d{17})"', html)
    if m:
        return m.group(1)
    m = re.search(r'"steamid"\s*:\s*"(\d{17})"', html)
    if m:
        return m.group(1)

    raise RuntimeError("Не удалось определить SteamID64: проверь steamLoginSecure/sessionid и прокси")


def fetch_account_name_flags(
    acc: SteamAccount,
    *,
    appid: int = 730,
    contextid: int = 2,
    language: str = "english",
    count: int = 2000,
    max_retries: int = 6,
    base_sleep: float = 1.0,
) -> tuple[str, Dict[str, NameFlags]]:
    """
    Возвращает:
      steamid64, dict[market_hash_name] -> NameFlags(tradable, marketable)
    Повторы market_hash_name автоматически "схлопываются" (OR по флагам).
    ВАЖНО: все запросы к Steam идут через прокси и cookies конкретного аккаунта.
    """
    s = make_session(acc)
    steamid64 = resolve_steamid64(s, acc.steam_login_secure)

    start_assetid: Optional[str] = None
    all_assets: list[dict] = []
    desc_map: Dict[Tuple[str, str], dict] = {}

    while True:
        params: Dict[str, Any] = {"l": language, "count": str(count)}
        if start_assetid:
            params["start_assetid"] = start_assetid

        url = INV_URL.format(steamid=steamid64, appid=appid, contextid=contextid)

        data = None
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                r = s.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    time.sleep(min(base_sleep * (2 ** attempt), 60.0))
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                last_exc = e
                time.sleep(min(base_sleep * (2 ** attempt), 30.0))

        if data is None:
            raise RuntimeError(f"[{acc.name}] Failed to fetch inventory: {last_exc}")

        if not data.get("success"):
            raise RuntimeError(
                f"[{acc.name}] Inventory endpoint returned success={data.get('success')}. "
                f"Likely private/no access or temporary issue. keys={list(data.keys())}"
            )

        assets = data.get("assets") or []
        descriptions = data.get("descriptions") or []

        all_assets.extend(assets)
        for d in descriptions:
            desc_map[_ckey(d.get("classid"), d.get("instanceid"))] = d

        more_items = data.get("more_items")
        last_assetid = data.get("last_assetid")
        if more_items is None:
            more_items = data.get("more")
        if last_assetid is None:
            last_assetid = data.get("more_start")

        if more_items and last_assetid:
            start_assetid = str(last_assetid)
        else:
            break

    out: Dict[str, NameFlags] = {}
    for a in all_assets:
        classid = str(a.get("classid"))
        instanceid = str(a.get("instanceid"))
        d = desc_map.get((classid, instanceid), {}) or {}

        name = d.get("market_hash_name")
        if not name:
            continue

        tradable = int(d.get("tradable", 0) or 0) == 1
        marketable = int(d.get("marketable", 0) or 0) == 1

        prev = out.get(name)
        if prev is None:
            out[name] = NameFlags(market_hash_name=name, tradable=tradable, marketable=marketable)
        else:
            prev.tradable = prev.tradable or tradable
            prev.marketable = prev.marketable or marketable

    # оставляем только "разблокированные" (хотя бы один флаг доступности)
    out = {k: v for k, v in out.items() if v.tradable or v.marketable}

    return steamid64, out
