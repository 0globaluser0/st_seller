# config_console.py
from __future__ import annotations

# ---- Pulse settings (как в вашей рабочей версии) ----
PULSE_API_BASE_URL = "https://api-pulse.tradeon.space"
PULSE_GAME_TYPE = "CsGo"
PULSE_MARKET = "Steam"
PULSE_CURRENCY_OVERRIDE = 1

# Оставьте ваши реальные значения (из вашей рабочей версии)
PULSE_DEVICE_ID = "2fff93d6c5319874"
PULSE_AUTHORIZATION = "Bearer eyJhbGciOiJIUzI1NiIsInR5YW1lIjoiNTczNzI0NWYwNSy9zY2Aub3JnL3dzLzIwMDUvMDUvaWRlbnRpdHkvY2xhaW1zL25hbWVpZGVudGlmaWVyIjoiNTczNiIsImlzQWRtaW4iOiJGYWxzZSIsImV4cCI6k4M30.SjbTRNGK4lRrLuen5kgmZto7K6J3M-EZYCM"
PULSE_COOKIE = ""                    # cookie (если нужен)
PULSE_ORIGIN = "https://pulse.tradeon.space"
PULSE_REFERER = "https://pulse.tradeon.space/"

# Retry/backoff
PULSE_DELAY_SEC = 0.25
PULSE_429_DELAY_SEC = 3.0
PULSE_MAX_RETRIES = 4
PULSE_BACKOFF_MULT = 1.7
HTTP_TIMEOUT = 20

# Запрашиваем данные с запасом назад, чтобы окно "от последней точки" было полным,
# даже если последняя точка в ответе отстаёт от now().
PULSE_FETCH_EXTRA_HOURS = 20



# ----------------------------
# Market.csgo.com (TM) settings
# ----------------------------
# API base: https://market.csgo.com/api/v2
TM_API_BASE_URL = "https://market.csgo.com/api/v2"

# Кэш маппинга item_name -> item_id (full-history/all.json)
# Если TM_ALL_CACHE_PATH пустой, будет использован файл рядом со скриптами: tm_full_history_all_cache.json
TM_ALL_CACHE_PATH = ""
TM_ALL_CACHE_TTL_SEC = 3600  # обновлять примерно раз в час (как и источник)

# Условия, при которых мы вообще считаем рек цену на TM:
# 1) продаж (точек) за последние 2 дня >= TM_MIN_SALES_LAST_2DAYS
# 2) рек цена на Steam (Pulse) >= TM_MIN_STEAM_REC_PRICE_TO_CHECK_TM
# Если любое из условий не выполняется -> TM НЕ считается и в сравнении автоматически побеждает Steam.
TM_MIN_SALES_LAST_2DAYS = 10
TM_MIN_STEAM_REC_PRICE_TO_CHECK_TM = 0.35

# Коэффициент для сравнения Steam vs TM:
# compare_steam = steam_rec_price * 0.87 * DIFF_ST_TM
# compare_tm    = tm_rec_price    * 0.95
DIFF_ST_TM = 0.91 * 0.95

# ----------------------------
# Анализ по вашей схеме
# ----------------------------

# 1) Берём график за последние GRAPH_ANALYS_HOURS часов (от последней точки)
GRAPH_ANALYS_HOURS = 24

# 2) Минимальная длина диапазона (range) в часах
MIN_RANGE_HOURS = 24

# 3) Плотность точек в каждом range:
#    points_in_range >= ceil(range_hours * MIN_POINTS_SHARE_PER_HOUR)
MIN_POINTS_SHARE_PER_HOUR = 0.50

# 4) Периоды №1: MIN_SHARE считается по ОБЪЁМУ продаж (вес = count)
PRICE_SUPPORT_PERIODS = [
    {
        "LAST_RANGE_COUNT": 1,
        "MIN_SHARE": 0.2,
        "MAX_ALLOWED_VIOLATIONS": 1,
        "MIN_WINDOW_VOLUME": 2,   # минимум продаж (sum(count)) в окне
    },
    {
        "MIN_SHARE": 0.2,
        "MAX_ALLOWED_VIOLATIONS": 9,
        "MIN_WINDOW_VOLUME": 4,   # минимум продаж (sum(count)) в окне
    },
]

# 5) Периоды №2: MIN_SHARE считается по ТОЧКАМ (без count; каждая точка вес=1)
PRICE_SUPPORT_PERIODS_POINTS = [
    {
        "LAST_RANGE_COUNT": 1,
        "MIN_SHARE": 0.2,
        "MAX_ALLOWED_VIOLATIONS": 0,
        "MIN_WINDOW_VOLUME": 2,   # минимум точек (points_count) в окне
    },
    {
        "MIN_SHARE": 0.3,
        "MAX_ALLOWED_VIOLATIONS": 9,
        "MIN_WINDOW_VOLUME": 4,   # минимум точек (points_count) в окне
    },
]

# ----------------------------
# Steam Market API settings (direct API, not via Pulse)
# ----------------------------
# HTTP settings for Steam Market requests
STEAM_HTTP_TIMEOUT = 20          # Connection/read timeout in seconds
STEAM_MAX_RETRIES = 4            # Max retry attempts on 429/5xx errors
STEAM_BACKOFF_MULT = 2.0         # Exponential backoff multiplier
STEAM_429_DELAY_SEC = 5.0        # Initial delay on 429 rate limit
STEAM_DELAY_SEC = 0.3            # Soft delay between requests

# FX rate benchmark item (used to calculate Steam-implied exchange rate)
# This item should be liquid enough on Steam Market
STEAM_FX_BENCHMARK_NAME = "Fracture Case"

# FX rate cache TTL in seconds (6 hours default)
# The rate is cached per currency_id
STEAM_FX_CACHE_TTL_SEC = 6 * 3600
