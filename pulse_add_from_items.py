#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Особый запуск:

- Читает items.txt из корня проекта (по одному предмету в строке)
- Удаляет повторы (сохраняя исходный порядок)
- Для каждого предмета считает "рек цену" через ваш граф-анализатор (compute_support_dual)
- Создаёт НОВЫЙ список в Pulse
- Загружает все предметы в этот список (Steam -> Steam) с параметрами:
    marketHashName = <название>
    count = 1
    firstMarket = "Steam"
    secondMarket = "Steam"
    firstPrice = 1
    secondPrice = <rec_price>

Зависимости:
- использует API/эндпоинты и разбор конфига по образцу pulse_add_from_db.py
- использует ваши модули pulse_client.py + analyzer.py + config_console.py для расчёта rec_price
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

# Import from steam_market_client (new format with currency_id)
from steam_market_client import (
    SteamAccount,
    read_steam_accs_txt,
    validate_accounts_or_exit,
    compute_steam_rec_prices,
    SteamRecResult,
)
from steam_inventory import fetch_account_name_flags


# --- граф-анализатор ---
import config_console as graph_cfg
from analyzer import compute_support_dual


# ==========================
# ЗАГРУЗКА КОНФИГА (как в pulse_add_from_db.py)
# ==========================

DEFAULT_CONFIG_PATHS = [
    "config_pulse_add.txt",
    "config_pulse_add — копия.txt",
    "config_pulse_add — копия (2).txt",
    "config_pulse_add — копия (3).txt",
]

API_BASE_URL = "https://api-pulse.tradeon.space"
ORIGIN = "https://pulse.tradeon.space"
REFERER = "https://pulse.tradeon.space/app/"


def load_config(path: str) -> Dict[str, object]:
    """
    Простейший парсер конфига формата:
      KEY = "value"
      NUM = 0.01
      delay = 250
    Пустые строки и строки, начинающиеся с #, игнорируются.
    """
    cfg: Dict[str, object] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = map(str.strip, line.split("=", 1))
            if not key:
                continue

            # убираем комментарий после значения
            if "#" in value:
                value, _ = value.split("#", 1)
                value = value.strip()

            # строки в кавычках
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            else:
                # пробуем int/float
                try:
                    if "." in value:
                        value = float(value)
                    else:
                        value = int(value)
                except ValueError:
                    pass

            cfg[key] = value

    return cfg


def load_cfg_any() -> Dict[str, object]:
    for p in DEFAULT_CONFIG_PATHS:
        if Path(p).exists():
            return load_config(p)
    raise RuntimeError("Файл конфига не найден. Ожидается один из: " + ", ".join(DEFAULT_CONFIG_PATHS))


CFG = load_cfg_any()

# Имя списка (база)
NAME_LIST: str = str(CFG.get("name_list", "") or CFG.get("NAME_LIST", "")).strip()

# задержка между запросами в миллисекундах (например 250 = 0.25 секунды)
DELAY_MS: int = int(CFG.get("delay", 0) or 0)

# авторизация Pulse (для списков)
DEVICE_ID: str = str(CFG.get("DEVICE_ID", "")).strip()
AUTHORIZATION: str = str(CFG.get("AUTHORIZATION", "")).strip()
COOKIE: str = str(CFG.get("COOKIE", "")).strip()

# размер батча
BATCH_SIZE: int = int(CFG.get("BATCH_SIZE", 50) or 50)

# рынки: firstMarket фиксируем как Steam, secondMarket выбирается по сравнению Steam vs TM
FIRST_MARKET = "Steam"

# optional sticker для списка
STICKER: str = str(CFG.get("STICKER", "😀") or "😀")


def apply_delay() -> None:
    if DELAY_MS > 0:
        time.sleep(DELAY_MS / 1000.0)


def build_headers() -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    if DEVICE_ID:
        headers["device-id"] = DEVICE_ID
    if AUTHORIZATION:
        headers["authorization"] = AUTHORIZATION
    if COOKIE:
        headers["cookie"] = COOKIE
    return headers


# ==========================
# API Pulse: create list + fetch lists + mass-change
# (как в pulse_add_from_db.py)
# ==========================

def create_list(list_name: str, sticker: str = "😀") -> None:
    """POST /api/table/purchase/history/explorer/CsGo/list"""
    url = f"{API_BASE_URL}/api/table/purchase/history/explorer/CsGo/list"
    payload = {"sticker": sticker, "name": list_name}
    resp = requests.post(url, json=payload, headers=build_headers(), timeout=30)
    apply_delay()
    if not resp.ok:
        raise RuntimeError(f"Ошибка при создании списка '{list_name}': HTTP {resp.status_code} – {resp.text}")


def fetch_lists() -> List[dict]:
    """GET /api/table/purchase/history/explorer/CsGo"""
    url = f"{API_BASE_URL}/api/table/purchase/history/explorer/CsGo"
    resp = requests.get(url, headers=build_headers(), timeout=30)
    apply_delay()
    if not resp.ok:
        raise RuntimeError(f"Ошибка при получении списков: HTTP {resp.status_code} – {resp.text}")
    data = resp.json()
    return data.get("explorerItems", []) or []


def get_list_id_by_name(list_name: str) -> Optional[int]:
    for item in fetch_lists():
        list_info = item.get("listInfo")
        if not isinstance(list_info, dict):
            continue
        if list_info.get("name") == list_name:
            return int(list_info["id"])
    return None


def get_list_id_after_create(list_name: str, retries: int = 10, delay_sec: float = 0.4) -> int:
    for _ in range(retries):
        list_id = get_list_id_by_name(list_name)
        if list_id is not None:
            return list_id
        time.sleep(delay_sec)
    raise RuntimeError(f"Список '{list_name}' не найден после создания (retries exhausted)")


def chunked(items: List[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def push_items_to_list(list_id: int, add_items: List[dict]) -> None:
    """
    POST /api/table/purchase/history/CsGo/mass-change
    """
    url = f"{API_BASE_URL}/api/table/purchase/history/CsGo/mass-change"
    total = len(add_items)
    sent = 0

    for batch in chunked(add_items, max(1, BATCH_SIZE)):
        payload = {
            "listId": list_id,
            "addItems": batch,
            "removeItems": [],
            "changeItems": [],
            "useActualPrice": False,
            "isBuffer": False,
        }
        resp = requests.post(url, json=payload, headers=build_headers(), timeout=60)
        apply_delay()
        if not resp.ok:
            raise RuntimeError(f"Ошибка при отправке батча в список {list_id}: HTTP {resp.status_code} – {resp.text}")

        sent += len(batch)
        print(f"Отправлено {sent}/{total} предметов...")


# ==========================
# items.txt + dedupe
# ==========================

def read_items_txt(path: Path) -> List[str]:
    """
    Читает items.txt, убирает повторы БЕЗ повторной проверки/анализа,
    при этом сохраняет исходный порядок первых вхождений.
    """
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    items: List[str] = []
    seen = set()

    for line in raw_lines:
        name = line.strip()
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        items.append(name)

    return items


def ensure_graph_auth_from_list_auth() -> None:
    """
    В проекте часто список и граф живут на одном домене, но в разных конфиг-файлах.
    Если в config_console.py токены не заданы, подставим из config_pulse_add.
    """
    if not getattr(graph_cfg, "PULSE_DEVICE_ID", "") and DEVICE_ID:
        graph_cfg.PULSE_DEVICE_ID = DEVICE_ID
    if not getattr(graph_cfg, "PULSE_AUTHORIZATION", "") and AUTHORIZATION:
        graph_cfg.PULSE_AUTHORIZATION = AUTHORIZATION
    if not getattr(graph_cfg, "PULSE_COOKIE", "") and COOKIE:
        graph_cfg.PULSE_COOKIE = COOKIE

    # по ТЗ: запрос графика с currencyOverride = 1
    graph_cfg.PULSE_CURRENCY_OVERRIDE = 1


def _choose_market(steam_rec: float, tm_rec: Optional[float]) -> tuple[str, float, float, Optional[float]]:
    """
    Возвращает:
      chosen_market: "Steam" или "Tm"
      chosen_rec_price: raw rec_price выбранной площадки
      cmp_steam: steam_rec * 0.87 * DIFF_ST_TM
      cmp_tm: tm_rec * 0.95 (или None если TM не считали)
    ВАЖНО: cmp_* используется ТОЛЬКО для сравнения (как в ТЗ).
    """
    steam_cmp = float(steam_rec) * 0.87 * float(getattr(graph_cfg, "DIFF_ST_TM", 1.0) or 1.0)
    if tm_rec is None:
        return "Steam", float(steam_rec), steam_cmp, None

    tm_cmp = float(tm_rec) * 0.95
    if tm_cmp > steam_cmp:
        return "Tm", float(tm_rec), steam_cmp, tm_cmp
    return "Steam", float(steam_rec), steam_cmp, tm_cmp


def compute_rec_prices_and_choose(
    item_name: str,
    *,
    steam_account: SteamAccount,
    can_sell_steam: bool = True,
    can_sell_tm: bool = True,
) -> dict:
    """
    Computes rec prices using Steam Market API (not Pulse) and optional TM.

    NEW LOGIC (per spec):
    1) Fetch Steam data from pricehistory/priceoverview in NATIVE currency:
       - steam_rec_from_graph_native: rec_price from graph analysis
       - steam_lowest_native: lowest_price from priceoverview
       - steam_rec_native = max(steam_rec_from_graph_native, steam_lowest_native)
       (comparison in native currency WITHOUT conversion)

    2) Convert to USD using Steam-implied FX rate (benchmark priceoverview):
       - steam_rec_usd = steam_rec_native / local_per_usd

    3) TM rec (if applicable) is already in USD.

    4) Comparison for market choice:
       - steam_cmp = steam_rec_usd * 0.87 * DIFF_ST_TM
       - tm_cmp = tm_rec * 0.95
       - Choose market with higher cmp value

    5) rec_price for Pulse upload is always in USD:
       - If Steam chosen: steam_rec_usd
       - If TM chosen: tm_rec (already USD)
    """
    from tm_client import fetch_tm_history, count_sales_last_days

    if not can_sell_steam and not can_sell_tm:
        raise RuntimeError("item is neither marketable nor tradable on this account")

    # --- Steam (direct API, not Pulse) ---
    steam_result: SteamRecResult = compute_steam_rec_prices(steam_account, item_name)

    # steam_rec_usd is used for comparison with TM and for Pulse upload
    steam_rec_usd = float(steam_result.steam_rec_usd)

    cmp_steam = steam_rec_usd * 0.87 * float(getattr(graph_cfg, "DIFF_ST_TM", 1.0) or 1.0)

    # --- TM gating ---
    tm_rec: Optional[float] = None
    cmp_tm: Optional[float] = None
    tm_status = "TM skipped"

    if not can_sell_tm:
        tm_status = "TM skipped: not tradable"
    else:
        thr = float(getattr(graph_cfg, "TM_MIN_STEAM_REC_PRICE_TO_CHECK_TM", 0.0) or 0.0)
        min_sales_2d = int(getattr(graph_cfg, "TM_MIN_SALES_LAST_2DAYS", 0) or 0)

        # Use steam_rec_usd for threshold check (comparison in USD)
        if can_sell_steam and steam_rec_usd < thr:
            tm_status = f"TM skipped: steam_rec_usd={steam_rec_usd:.6g} < threshold={thr:.6g}"
        else:
            try:
                tm_points = fetch_tm_history(item_name)
                sales_2d = count_sales_last_days(tm_points, 2.0)
                if sales_2d < min_sales_2d:
                    tm_status = f"TM skipped: sales_2d={sales_2d} < required={min_sales_2d}"
                else:
                    tm_dual = compute_support_dual(tm_points, density_share_override=0.0)
                    tm_rec = float(tm_dual.min_support_price)
                    cmp_tm = float(tm_rec) * 0.95
                    tm_status = f"TM OK: sales_2d={sales_2d}"
            except Exception as e:
                tm_status = f"TM skipped: {e}"

    # --- выбор площадки с учётом доступности ---
    if not can_sell_steam:
        if tm_rec is None:
            raise RuntimeError(f"Steam not sellable and TM not available: {tm_status}")
        chosen_market, chosen_rec = "Tm", float(tm_rec)
    elif tm_rec is None or not can_sell_tm:
        chosen_market, chosen_rec = "Steam", steam_rec_usd
    else:
        # Compare in USD
        chosen_market, chosen_rec, _, _ = _choose_market(steam_rec_usd, tm_rec)

    return {
        # USD values (for comparison and Pulse)
        "steam_rec": steam_rec_usd,  # This is steam_rec_usd for backward compatibility
        "steam_rec_usd": steam_rec_usd,
        "tm_rec": tm_rec,
        "chosen_market": chosen_market,
        "chosen_rec": chosen_rec,
        "cmp_steam": cmp_steam,
        "cmp_tm": cmp_tm,
        "tm_status": tm_status,
        # Native currency values (for debugging/logging)
        "steam_rec_native": steam_result.steam_rec_native,
        "steam_rec_from_graph_native": steam_result.steam_rec_from_graph_native,
        "steam_lowest_native": steam_result.steam_lowest_native,
        "fx_local_per_usd": steam_result.fx_local_per_usd,
        "currency_id": steam_result.currency_id,
    }



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--accs",
        default=None,
        help=r"Путь к steam_accs.txt. Формат строки: name\\currency_id\\http_proxy(user:pass@ip:port)\\sessionid\\SteamLoginSecure",
    )
    ap.add_argument("--count", type=int, default=2000, help="Steam inventory page size (обычно 2000)")
    ap.add_argument("--language", default="english", help="Steam inventory language")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent

    if not (DEVICE_ID and AUTHORIZATION):
        raise RuntimeError("В конфиге для Pulse списков должны быть DEVICE_ID и AUTHORIZATION")

    ensure_graph_auth_from_list_auth()

    # Load Steam accounts for API access (REQUIRED for new Steam Market API logic)
    # Try to find steam_accs.txt if not specified
    accs_path = None
    if args.accs:
        accs_path = Path(args.accs)
        if not accs_path.is_absolute():
            accs_path = root / args.accs
    else:
        # Try default locations
        for default_path in ["steam_accs.txt", "steam_accounts.txt"]:
            candidate = root / default_path
            if candidate.exists():
                accs_path = candidate
                break

    all_steam_accounts: List[SteamAccount] = []
    if accs_path and accs_path.exists():
        all_steam_accounts = read_steam_accs_txt(accs_path)
        validate_accounts_or_exit(all_steam_accounts)
        print(f"Loaded {len(all_steam_accounts)} Steam account(s) from {accs_path}")
    else:
        raise RuntimeError(
            "steam_accs.txt not found. Steam Market API requires authenticated accounts. "
            "Provide --accs path or place steam_accs.txt in project root. "
            "Format: name\\\\currency_id\\\\proxy\\\\sessionid\\\\steamLoginSecure"
        )

    # Default account for MODE B (items.txt) - use first valid account
    default_steam_account = all_steam_accounts[0]

    # кэш на время запуска, чтобы не пересчитывать одинаковые item_name между аккаунтами
    # Key now includes steam account name for currency-specific caching
    rec_cache: Dict[tuple[str, bool, bool, str], dict] = {}

    # ==========================
    # MODE A: из инвентарей аккаунтов (steam_accs.txt)
    # ==========================
    if args.accs:
        # accounts already loaded above (all_steam_accounts)
        accounts = all_steam_accounts

        base = NAME_LIST or "inventory"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        for acc_idx, acc in enumerate(accounts, 1):
            print()
            print("=" * 60)
            print(f"[{acc_idx}/{len(accounts)}] ACCOUNT: {acc.name} (currency_id={acc.currency_id})")
            print("=" * 60)

            steamid64, name_flags = fetch_account_name_flags(
                acc,
                language=args.language,
                count=args.count,
            )
            names = list(name_flags.keys())
            print(f"steamid64={steamid64} | уникальных unlocked предметов: {len(names)}")

            if not names:
                print("Нет unlocked предметов — пропускаю аккаунт.")
                continue

            list_name = f"{base} [{acc.name}] [{ts}]"
            print(f"Создаём НОВЫЙ список Pulse: {list_name!r}")
            create_list(list_name, sticker=STICKER)
            list_id = get_list_id_after_create(list_name)
            print(f"list_id = {list_id}")

            add_items: List[dict] = []
            failed: List[str] = []

            for idx, name in enumerate(names, 1):
                flags = name_flags[name]
                can_sell_steam = bool(flags.marketable)   # Steam market
                can_sell_tm = bool(flags.tradable)        # TM (trade)

                # Cache key includes account name for currency-specific results
                key = (name, can_sell_steam, can_sell_tm, acc.name)
                try:
                    if key in rec_cache:
                        res = rec_cache[key]
                    else:
                        res = compute_rec_prices_and_choose(
                            name,
                            steam_account=acc,
                            can_sell_steam=can_sell_steam,
                            can_sell_tm=can_sell_tm,
                        )
                        rec_cache[key] = res
                except Exception as e:
                    print(f"[{idx}/{len(names)}] {name}  [SKIP] rec_price error: {e}")
                    failed.append(name)
                    continue

                chosen_market = res["chosen_market"]
                chosen_rec = res["chosen_rec"]

                second_market = "Tm" if chosen_market == "Tm" else "Steam"

                add_items.append(
                    {
                        "marketHashName": name,
                        "firstMarket": FIRST_MARKET,
                        "secondMarket": second_market,
                        "firstPrice": 1,
                        "secondPrice": float(chosen_rec),
                        "count": 1,
                    }
                )

                # Extended log with native and USD values
                steam_rec_usd = res.get("steam_rec_usd", res["steam_rec"])
                steam_rec_native = res.get("steam_rec_native", steam_rec_usd)
                steam_lowest = res.get("steam_lowest_native")
                tm_rec = res.get("tm_rec")

                log_parts = [
                    f"[{idx}/{len(names)}] {name}",
                    f"chosen={chosen_market}->{second_market}",
                    f"steam_rec_usd={steam_rec_usd:.2f}",
                    f"steam_rec_native={steam_rec_native:.2f}",
                ]
                if steam_lowest is not None:
                    log_parts.append(f"steam_lowest={steam_lowest:.2f}")
                if tm_rec is not None:
                    log_parts.append(f"tm_rec={tm_rec:.2f}")
                else:
                    log_parts.append(f"TM: {res['tm_status']}")

                print(" | ".join(log_parts))

            if not add_items:
                print("Не удалось подготовить ни одного предмета для загрузки.")
                continue

            print(f"Загружаем {len(add_items)} предметов в list_id={list_id} ...")
            push_items_to_list(list_id, add_items)

            print("Готово.")
            if failed:
                print(f"Пропущено (ошибка расчёта rec_price/доступности): {len(failed)}")

        return 0

    # ==========================
    # MODE B: режим items.txt (uses default Steam account for API access)
    # ==========================
    items_path = root / "items.txt"
    if not items_path.exists():
        raise RuntimeError(f"items.txt не найден в корне проекта: {items_path}")

    items = read_items_txt(items_path)
    if not items:
        print("items.txt пуст — нечего загружать.")
        return 0

    base = NAME_LIST or "items"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    list_name = f"{base} [{ts}]"

    print(f"Уникальных предметов: {len(items)} (повторы в items.txt автоматически удалены)")
    print(f"Using Steam account: {default_steam_account.name} (currency_id={default_steam_account.currency_id})")
    print(f"Создаём НОВЫЙ список Pulse: {list_name!r}")

    create_list(list_name, sticker=STICKER)
    list_id = get_list_id_after_create(list_name)
    print(f"list_id = {list_id}")

    add_items: List[dict] = []
    failed: List[str] = []

    for idx, name in enumerate(items, 1):
        try:
            res = compute_rec_prices_and_choose(
                name,
                steam_account=default_steam_account,
            )
        except Exception as e:
            print(f"[{idx}/{len(items)}] {name}  [SKIP] rec_price error: {e}")
            failed.append(name)
            continue

        chosen_market = res["chosen_market"]
        chosen_rec = res["chosen_rec"]
        second_market = "Tm" if chosen_market == "Tm" else "Steam"

        add_items.append(
            {
                "marketHashName": name,
                "firstMarket": FIRST_MARKET,
                "secondMarket": second_market,
                "firstPrice": 1,
                "secondPrice": float(chosen_rec),
                "count": 1,
            }
        )

        # Extended log with native and USD values
        steam_rec_usd = res.get("steam_rec_usd", res["steam_rec"])
        steam_rec_native = res.get("steam_rec_native", steam_rec_usd)
        steam_lowest = res.get("steam_lowest_native")
        tm_rec = res.get("tm_rec")

        log_parts = [
            f"[{idx}/{len(items)}] {name}",
            f"chosen={chosen_market}->{second_market}",
            f"steam_rec_usd={steam_rec_usd:.2f}",
            f"steam_rec_native={steam_rec_native:.2f}",
        ]
        if steam_lowest is not None:
            log_parts.append(f"steam_lowest={steam_lowest:.2f}")
        if tm_rec is not None:
            log_parts.append(f"tm_rec={tm_rec:.2f}")
        else:
            log_parts.append(f"TM: {res['tm_status']}")

        print(" | ".join(log_parts))

    if not add_items:
        print("Не удалось подготовить ни одного предмета для загрузки.")
        return 0

    print(f"Загружаем {len(add_items)} предметов в list_id={list_id} ...")
    push_items_to_list(list_id, add_items)

    print("Готово.")
    if failed:
        print(f"Пропущено (ошибка расчёта rec_price): {len(failed)}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
