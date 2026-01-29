# main.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from pulse_client import fetch_history, PricePoint
from analyzer import compute_support_dual, DualSupportResult, SupportResult
from tm_client import fetch_tm_history, count_sales_last_days
import config_console as config

try:
    from zoneinfo import ZoneInfo  # py3.9+
    _LOCAL_TZ = ZoneInfo("Europe/Warsaw")
except Exception:
    _LOCAL_TZ = timezone.utc


STEAM_COMPARE_FEE = 0.87
TM_COMPARE_FEE = 0.95


def _fmt(x: float) -> str:
    return f"{x:,.6g}" if abs(x) < 1000 else f"{x:,.2f}"

def _fmt_price2(x: float) -> str:
    return f"{x:.2f}"


def _dt_local(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "N/A"


def _range_local_str(start_ts: int, end_ts: int) -> str:
    # end_ts в анализаторе используется как "верхняя граница (exclusive)"
    # Для отображения человеку показываем как "start .. end" (end exclusive).
    return f"{_dt_local(start_ts)} .. {_dt_local(end_ts)}"


def _print_header(market_label: str, item: str, points: list[PricePoint], *, density_share: float) -> None:
    print()
    print("=" * 150)
    print(f"MARKET: {market_label}")
    print(f"ITEM: {item}")
    print(f"Points: {len(points)} | ts_range: {points[0].ts} .. {points[-1].ts}")
    print(f"Current price: {_fmt(points[-1].price)}")
    print(
        f"Graph_Analys={config.GRAPH_ANALYS_HOURS}h | min_range={config.MIN_RANGE_HOURS}h | "
        f"density_share={density_share}"
    )
    print(f"PRICE_SUPPORT_PERIODS (count-weighted): {config.PRICE_SUPPORT_PERIODS}")
    print(f"PRICE_SUPPORT_PERIODS_POINTS (points-only): {config.PRICE_SUPPORT_PERIODS_POINTS}")
    print("=" * 150)


def _print_support(res: SupportResult) -> None:
    print()
    print(f"--- METHOD: {res.method} ---")
    print(
        f"Partition: N={res.ranges_count}, range_hours={res.range_hours:.6g}, "
        f"required_points_per_range={res.required_points_per_range}"
    )
    for n in res.notes:
        print(f"[NOTE] {n}")

    print("-" * 150)
    print("RANGES: time is LOCAL (Europe/Warsaw) as [start .. end) ")
    print(
        " idx | time_local(start..end)           | points | sales_vol | used_vol(type) | "
        "min_share | q=1-min_share | percentile | valid | ignored | reason"
    )
    print("-" * 150)

    for s in res.stats:
        valid = "Y" if s.valid else "N"
        ign = "Y" if s.ignored_by_violation else "N"
        reason = s.invalid_reason if not s.valid else ""
        used = f"{s.volume_used}({s.volume_used_name})"
        time_str = _range_local_str(s.start_ts, s.end_ts)

        print(
            f"{s.idx:>4d} | "
            f"{time_str:<30s} | "
            f"{s.points_count:>6d} | "
            f"{s.volume_sales:>9d} | "
            f"{used:>13s} | "
            f"{s.min_share:>8.3f} | "
            f"{s.percentile_q:>13.3f} | "
            f"{_fmt(s.percentile_price):>10s} | "
            f"{valid:>5s} | "
            f"{ign:>7s} | "
            f"{reason}"
        )

    print("-" * 150)

    if not res.has_candidate:
        print(f"RESULT({res.method}): NO_RESULT (no valid candidates after filters)")
        return

    dist_pct = (res.selected_price - res.p_now) / res.p_now * 100.0 if res.p_now > 0 else 0.0
    print(
        f"RESULT({res.method}): support_price={_fmt(res.selected_price)} from range idx={res.selected_range_idx} "
        f"(dist vs current: {dist_pct:+.2f}%)"
    )


def _try_compute_tm(item: str, steam_rec_price: float) -> Tuple[Optional[DualSupportResult], Optional[list[PricePoint]], str]:
    """
    Возвращает (tm_dual, tm_points, status_string).
    tm_points может быть не None даже если анализ не делали — чтобы можно было посмотреть историю.
    """
    thr = float(getattr(config, "TM_MIN_STEAM_REC_PRICE_TO_CHECK_TM", 0.0) or 0.0)
    min_sales_2d = int(getattr(config, "TM_MIN_SALES_LAST_2DAYS", 0) or 0)

    if steam_rec_price < thr:
        return None, None, f"SKIP TM: steam_rec_price={_fmt_price2(steam_rec_price)} < threshold={_fmt_price2(thr)}"

    try:
        tm_points = fetch_tm_history(item)
    except Exception as e:
        return None, None, f"SKIP TM: fetch failed: {e}"

    sales_2d = count_sales_last_days(tm_points, 2.0)
    if sales_2d < min_sales_2d:
        return None, tm_points, f"SKIP TM: sales_2d={sales_2d} < required={min_sales_2d}"

    try:
        tm_dual = compute_support_dual(tm_points, density_share_override=0.0)
    except Exception as e:
        return None, tm_points, f"SKIP TM: analyze failed: {e}"

    return tm_dual, tm_points, f"OK TM: sales_2d={sales_2d} (>= {min_sales_2d})"


def _choose_market(steam_rec: float, tm_rec: Optional[float]) -> Tuple[str, float, float, float]:
    """
    Возвращает: (chosen_market, chosen_rec_price, cmp_steam, cmp_tm)
    cmp_* — только для сравнения (как в ТЗ).
    """
    cmp_steam = float(steam_rec) * STEAM_COMPARE_FEE * float(getattr(config, "DIFF_ST_TM", 1.0) or 1.0)

    if tm_rec is None:
        return "Steam", float(steam_rec), cmp_steam, float("-inf")

    cmp_tm = float(tm_rec) * TM_COMPARE_FEE

    if cmp_tm > cmp_steam:
        return "Tm", float(tm_rec), cmp_steam, cmp_tm
    return "Steam", float(steam_rec), cmp_steam, cmp_tm


def main() -> int:
    print("Rec-price analyzer: Steam(Pulse) + optional TM (market.csgo.com). Type 'exit' to quit.")
    while True:
        try:
            item = input("\nItem name> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0

        if not item:
            continue
        if item.lower() in ("exit", "quit", "q"):
            print("Bye.")
            return 0

        # --- Steam (Pulse) ---
        try:
            steam_points = fetch_history(item)
        except Exception as e:
            print(f"[ERROR] Steam(Pulse) fetch: {e}")
            continue

        try:
            steam_dual: DualSupportResult = compute_support_dual(steam_points)
        except Exception as e:
            print(f"[ERROR] Steam(Pulse) analyze failed: {e}")
            continue

        steam_rec = float(steam_dual.min_support_price)

        _print_header("Steam (Pulse)", item, steam_points, density_share=float(config.MIN_POINTS_SHARE_PER_HOUR))
        _print_support(steam_dual.res_count_weighted)
        _print_support(steam_dual.res_points_only)

        print()
        print("=" * 150)
        print(f"STEAM REC_PRICE: {_fmt(steam_rec)}  (chosen method: {steam_dual.chosen_method})")
        print("=" * 150)

        # --- TM (market.csgo.com) ---
        tm_dual, tm_points, tm_status = _try_compute_tm(item, steam_rec)
        print()
        print(tm_status)

        tm_rec: Optional[float] = None
        if tm_dual is not None and tm_points is not None:
            tm_rec = float(tm_dual.min_support_price)
            _print_header("TM (market.csgo.com)", item, tm_points, density_share=0.0)
            _print_support(tm_dual.res_count_weighted)
            _print_support(tm_dual.res_points_only)
            print()
            print("=" * 150)
            print(f"TM REC_PRICE: {_fmt(tm_rec)}  (chosen method: {tm_dual.chosen_method})")
            print("=" * 150)

        # --- Compare / choose ---
        chosen_market, chosen_rec, cmp_steam, cmp_tm = _choose_market(steam_rec, tm_rec)

        print()
        print("=" * 150)
        print("COMPARE (only for choosing market):")
        print(f"  steam_cmp = steam_rec * {STEAM_COMPARE_FEE} * DIFF_ST_TM({getattr(config, 'DIFF_ST_TM', 1.0)}) = {_fmt(cmp_steam)}")
        if tm_rec is None:
            print("  tm_cmp    = (TM skipped) -> -inf")
        else:
            print(f"  tm_cmp    = tm_rec * {TM_COMPARE_FEE} = {_fmt(cmp_tm)}")
        print(f"CHOSEN MARKET: {chosen_market}  |  CHOSEN REC_PRICE (raw): {_fmt_price2(chosen_rec)}")
        print("=" * 150)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
