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
    Данные из steam_accs.txt:
      name\\http_proxy\\sessionid\\SteamLoginSecure
    """
    name: str
    http_proxy: str
    sessionid: str
    steam_login_secure: str


@dataclass
class NameFlags:
    market_hash_name: str
    tradable: bool
    marketable: bool


def read_steam_accs_txt(path) -> list[SteamAccount]:
    """
    Парсит steam_accs.txt, одна строка = один аккаунт:
      acc_name\\http://user:pass@ip:port\\sessionid\\SteamLoginSecure
    Пустые строки и строки, начинающиеся с #, игнорируются.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[SteamAccount] = []

    for ln, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue

        parts = s.split("\\\\")
        if len(parts) != 4:
            # запасной вариант: если разделители случайно одиночные '\'
            parts = s.split("\\")
        if len(parts) != 4:
            raise ValueError(f"{path}: line {ln}: expected 4 fields separated by \\\\ ; got {len(parts)}: {raw!r}")

        name, proxy, sessionid, loginsecure = (p.strip() for p in parts)
        out.append(SteamAccount(
            name=name,
            http_proxy=proxy,
            sessionid=sessionid,
            steam_login_secure=loginsecure,
        ))

    return out


def make_session(acc: SteamAccount) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; cs2-inventory-bot/1.0)",
        "Accept": "application/json,text/plain,*/*",
    })

    if acc.steam_login_secure:
        s.cookies.set("steamLoginSecure", acc.steam_login_secure, domain="steamcommunity.com", path="/")
    if acc.sessionid:
        s.cookies.set("sessionid", acc.sessionid, domain="steamcommunity.com", path="/")

    if acc.http_proxy:
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
