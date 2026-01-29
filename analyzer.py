# analyzer.py
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any
from decimal import Decimal, ROUND_DOWN
import config_console as config
from pulse_client import PricePoint



@dataclass
class RangeStat:
    idx: int
    start_ts: int
    end_ts: int
    points_count: int

    volume_sales: int           # sum(count)
    volume_used: int            # sales or points
    volume_used_name: str       # "sales" or "points"

    min_share: float
    min_window_volume: int
    percentile_q: float
    percentile_price: float
    valid: bool
    invalid_reason: str = ""
    ignored_by_violation: bool = False


@dataclass
class SupportResult:
    method: str                 # "COUNT_WEIGHTED" / "POINTS_ONLY"
    graph_hours: float
    min_range_hours: float
    min_points_share_per_hour: float
    ranges_count: int
    range_hours: float
    required_points_per_range: int
    p_now: float

    selected_price: float       # если нет кандидата -> math.inf
    selected_range_idx: Optional[int]
    has_candidate: bool

    notes: List[str]
    stats: List[RangeStat]


@dataclass
class DualSupportResult:
    res_count_weighted: SupportResult
    res_points_only: SupportResult
    min_support_price: float
    chosen_method: str          # "COUNT_WEIGHTED" / "POINTS_ONLY" / "FALLBACK_CURRENT"
    used_fallback_current: bool

def trunc_price_2(x: float) -> float:
    """
    Оставляет ровно 2 знака после запятой, остальные ОТБРАСЫВАЕТ (без округления).
    ROUND_DOWN = усечение к нулю (для положительных цен это обычное "отбросить хвост").
    """
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))



def _filter_last_hours(points: List[PricePoint], hours: float) -> List[PricePoint]:
    if not points:
        return []
    t_last = points[-1].ts
    t_min = t_last - int(hours * 3600)
    return [p for p in points if p.ts >= t_min]


def _weighted_quantile(prices: List[float], weights: List[int], q: float) -> float:
    if not prices:
        return 0.0
    if q <= 0:
        return min(prices)
    if q >= 1:
        return max(prices)

    pairs = sorted(zip(prices, weights), key=lambda x: x[0])
    total_w = sum(w for _, w in pairs if w > 0)
    if total_w <= 0:
        return 0.0

    target = q * total_w
    cum = 0.0
    for price, w in pairs:
        if w <= 0:
            continue
        cum += w
        if cum >= target:
            return float(price)
    return float(pairs[-1][0])


def _pick_range_partition(
    points: List[PricePoint],
    graph_hours: float,
    min_range_hours: float,
    density_share: float,
) -> Tuple[int, float, int, List[str]]:
    notes: List[str] = []

    if graph_hours <= 0:
        return 1, max(1.0, min_range_hours), 1, ["GRAPH_ANALYS_HOURS <= 0, forced fallback."]

    n_max = int(math.floor(graph_hours / max(1e-9, min_range_hours)))
    if n_max < 1:
        n_max = 1

    t_end = points[-1].ts
    t_start = t_end - int(graph_hours * 3600)

    notes.append(
        f"Partition search: Graph={graph_hours:.6g}h, min_range={min_range_hours:.6g}h, "
        f"density_share={density_share:.6g} -> N_max={n_max} (min range ~{(graph_hours / n_max):.6g}h)"
    )

    for N in range(n_max, 0, -1):
        range_hours = graph_hours / N
        if range_hours + 1e-9 < min_range_hours:
            notes.append(f"Try N={N}: range_hours={range_hours:.6g}h -> SKIP (below min_range)")
            continue

        required_points = int(math.ceil(range_hours * density_share))
        range_seconds = (graph_hours * 3600) / N

        counts: List[int] = []
        failed_details: List[str] = []

        for i in range(N):
            a = t_start + int(i * range_seconds)
            b = t_start + int((i + 1) * range_seconds) if i < N - 1 else t_end + 1
            cnt = sum(1 for p in points if a <= p.ts < b)
            counts.append(cnt)

            if cnt < required_points:
                start_h = (a - t_start) / 3600.0
                end_h = (b - t_start) / 3600.0
                failed_details.append(
                    f"idx={i} ({start_h:.2f}..{end_h:.2f}h): points={cnt} < required={required_points}"
                )

        if failed_details:
            notes.append(
                f"Try N={N}: range_hours={range_hours:.6g}h, required_points={required_points} -> FAIL "
                f"({len(failed_details)}/{N} ranges below requirement)"
            )
            for d in failed_details:
                notes.append(f"  FAIL_RANGE: {d}")
            notes.append(f"  COUNTS: {counts}")
            continue

        notes.append(
            f"Try N={N}: range_hours={range_hours:.6g}h, required_points={required_points} -> OK (selected)"
        )
        notes.append(f"  COUNTS: {counts}")
        return N, range_hours, required_points, notes

    notes.append("No partition satisfied density condition; fallback to 1 range.")
    N = 1
    range_hours = graph_hours
    required_points = int(math.ceil(range_hours * density_share))
    return N, range_hours, required_points, notes


def _compute_support_with_periods(
    points: List[PricePoint],
    periods: List[Dict[str, Any]],
    *,
    method: str,
    use_counts_for_share: bool,
    density_share_override: Optional[float] = None,
) -> SupportResult:
    notes: List[str] = []
    if not points:
        raise RuntimeError("Empty points")

    pts = _filter_last_hours(points, config.GRAPH_ANALYS_HOURS)
    if len(pts) < len(points):
        notes.append(f"Filtered to last {config.GRAPH_ANALYS_HOURS}h from last point: {len(pts)}/{len(points)} points.")
    if not pts:
        raise RuntimeError("No points in selected Graph_Analys window")

    p_now = float(pts[-1].price)

    used_density_share = float(
        config.MIN_POINTS_SHARE_PER_HOUR if density_share_override is None else density_share_override
    )

    N, range_hours, required_points, part_notes = _pick_range_partition(
        pts,
        graph_hours=float(config.GRAPH_ANALYS_HOURS),
        min_range_hours=float(config.MIN_RANGE_HOURS),
        density_share=float(used_density_share),
    )
    notes.extend(part_notes)

    if not isinstance(periods, list) or len(periods) < 2:
        raise RuntimeError(f"{method}: periods must be a list of at least 2 dicts")

    last_cfg = periods[0]
    other_cfg = periods[1]

    last_count = int(last_cfg.get("LAST_RANGE_COUNT", 0) or 0)
    if last_count < 0:
        last_count = 0
    if last_count > N:
        last_count = N

    if use_counts_for_share:
        notes.append(f"{method}: MIN_SHARE & MIN_WINDOW_VOLUME use SALES volume (sum(count)).")
    else:
        notes.append(f"{method}: MIN_SHARE & MIN_WINDOW_VOLUME use POINTS only (each point weight=1).")

    t_end = pts[-1].ts
    t_start = t_end - int(config.GRAPH_ANALYS_HOURS * 3600)
    range_seconds = (config.GRAPH_ANALYS_HOURS * 3600) / N

    stats: List[RangeStat] = []

    for i in range(N):
        a = t_start + int(i * range_seconds)
        b = t_start + int((i + 1) * range_seconds) if i < N - 1 else t_end + 1

        in_range = [p for p in pts if a <= p.ts < b]
        points_count = len(in_range)
        volume_sales = sum(p.count for p in in_range)

        is_last_group = (i >= N - last_count) if last_count > 0 else False
        cfg = last_cfg if is_last_group else other_cfg

        min_share = float(cfg["MIN_SHARE"])
        min_window_volume = int(cfg["MIN_WINDOW_VOLUME"])
        q = 1.0 - min_share

        if use_counts_for_share:
            volume_used = volume_sales
            volume_used_name = "sales"
        else:
            volume_used = points_count
            volume_used_name = "points"

        st = RangeStat(
            idx=i,
            start_ts=a,
            end_ts=b,
            points_count=points_count,
            volume_sales=volume_sales,
            volume_used=volume_used,
            volume_used_name=volume_used_name,
            min_share=min_share,
            min_window_volume=min_window_volume,
            percentile_q=q,
            percentile_price=0.0,
            valid=False,
        )

        if volume_used < min_window_volume:
            st.valid = False
            st.invalid_reason = f"{volume_used_name}<{min_window_volume}"
            stats.append(st)
            continue

        prices = [p.price for p in in_range]
        if not prices:
            st.valid = False
            st.invalid_reason = "no prices"
            stats.append(st)
            continue

        if use_counts_for_share:
            weights = [p.count for p in in_range]
            if sum(weights) <= 0:
                st.valid = False
                st.invalid_reason = "sum(count)<=0"
                stats.append(st)
                continue
            perc_price = _weighted_quantile(prices, weights, q)
        else:
            perc_price = _weighted_quantile(prices, [1] * len(prices), q)

        if perc_price <= 0:
            st.valid = False
            st.invalid_reason = "percentile<=0"
            stats.append(st)
            continue

        st.percentile_price = float(perc_price)
        st.valid = True
        stats.append(st)

    def apply_violations(group_stats: List[RangeStat], max_viol: int) -> None:
        valid = [s for s in group_stats if s.valid and s.percentile_price > 0]
        valid.sort(key=lambda s: s.percentile_price)
        for s in valid[:max(0, max_viol)]:
            s.ignored_by_violation = True

    last_group = [s for s in stats if last_count > 0 and s.idx >= N - last_count]
    other_group = [s for s in stats if not (last_count > 0 and s.idx >= N - last_count)]

    last_max_viol = int(last_cfg.get("MAX_ALLOWED_VIOLATIONS", 0) or 0)
    other_max_viol = int(other_cfg.get("MAX_ALLOWED_VIOLATIONS", 0) or 0)

    apply_violations(last_group, last_max_viol)
    apply_violations(other_group, other_max_viol)

    candidates = [
        s for s in stats
        if s.valid and s.percentile_price > 0 and not s.ignored_by_violation
    ]

    if not candidates:
        notes.append(f"{method}: No valid candidates after filters; NO_RESULT for this method.")
        return SupportResult(
            method=method,
            graph_hours=float(config.GRAPH_ANALYS_HOURS),
            min_range_hours=float(config.MIN_RANGE_HOURS),
            min_points_share_per_hour=float(used_density_share),
            ranges_count=N,
            range_hours=float(range_hours),
            required_points_per_range=int(required_points),
            p_now=p_now,
            selected_price=math.inf,          # <-- важно: НЕ fallback
            selected_range_idx=None,
            has_candidate=False,
            notes=notes,
            stats=stats,
        )

    candidates.sort(key=lambda s: (s.percentile_price, -s.idx))
    chosen = candidates[0]

    return SupportResult(
        method=method,
        graph_hours=float(config.GRAPH_ANALYS_HOURS),
        min_range_hours=float(config.MIN_RANGE_HOURS),
        min_points_share_per_hour=float(used_density_share),
        ranges_count=N,
        range_hours=float(range_hours),
        required_points_per_range=int(required_points),
        p_now=p_now,
        selected_price=float(chosen.percentile_price),
        selected_range_idx=int(chosen.idx),
        has_candidate=True,
        notes=notes,
        stats=stats,
    )


def compute_support_dual(points: List[PricePoint], *, density_share_override: Optional[float] = None) -> DualSupportResult:
    res1 = _compute_support_with_periods(
        points,
        config.PRICE_SUPPORT_PERIODS,
        method="COUNT_WEIGHTED",
        use_counts_for_share=True,
        density_share_override=density_share_override,
    )
    res2 = _compute_support_with_periods(
        points,
        config.PRICE_SUPPORT_PERIODS_POINTS,
        method="POINTS_ONLY",
        use_counts_for_share=False,
        density_share_override=density_share_override,
    )

    # Если хоть один метод дал кандидата — выбираем минимум среди тех, кто дал.
    candidates = []
    if res1.has_candidate:
        candidates.append(res1)
    if res2.has_candidate:
        candidates.append(res2)

    if candidates:
        best = min(candidates, key=lambda r: r.selected_price)
        return DualSupportResult(
            res_count_weighted=res1,
            res_points_only=res2,
            min_support_price=trunc_price_2(float(best.selected_price)),
            chosen_method=best.method,
            used_fallback_current=False,
        )

    # И только если оба метода не дали кандидатов — fallback в текущую цену
    p_now = res1.p_now  # одинаково для обоих, т.к. одно окно
    return DualSupportResult(
        res_count_weighted=res1,
        res_points_only=res2,
        min_support_price=trunc_price_2(float(p_now)),
        chosen_method="FALLBACK_CURRENT",
        used_fallback_current=True,
    )
