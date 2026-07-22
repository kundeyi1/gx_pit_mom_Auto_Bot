from dataclasses import dataclass
import re
import unicodedata

import numpy as np
import pandas as pd


@dataclass
class BacktestNavResult:
    """Daily NAV curves reconstructed from signal-active holding periods."""

    long: pd.Series
    short: pd.Series
    long_short: pd.Series
    n_groups: int
    signal_count: int = 0
    new_signal_count: int = 0
    max_endpoint_error: float = 0.0
    source: str = "computed"
    reason: str = ""


@dataclass(frozen=True)
class PerformanceMetrics:
    annualized_return: float
    max_drawdown: float
    annualized_volatility: float


def _normalise_asset_name(value: object) -> str:
    """Match historical CITIC names across Roman-numeral/case variants."""
    name = str(value).split(":")[-1].strip()
    name = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"\s+", "", name)


def build_latest_signal_groups(
    prices: pd.DataFrame,
    timing_signals: dict,
    n_groups: int,
    after_date: pd.Timestamp,
    half_life: int = 10,
) -> pd.DataFrame:
    """Build new trigger-date groups with the original rank-fusion method."""
    returns = prices.pct_change().dropna()
    all_dates = prices.index
    rank_cache = {}

    for signal_name, timing_series in timing_signals.items():
        trigger_dates = timing_series[timing_series.eq(1)].index
        if signal_name == "rebound":
            signal_values = returns - returns.shift(1).rolling(20).mean()
        else:
            signal_values = returns
        for trigger_date in trigger_dates:
            if trigger_date not in signal_values.index:
                continue
            raw_values = signal_values.loc[trigger_date].dropna()
            if not raw_values.empty:
                rank_cache[(trigger_date, signal_name)] = raw_values.rank(pct=True)

    potential_dates = sorted(
        set().union(
            *(set(series[series.eq(1)].index) for series in timing_signals.values())
        )
    )
    rows = []
    for trigger_date in potential_dates:
        if trigger_date <= after_date or trigger_date not in all_dates:
            continue
        trigger_pos = int(all_dates.get_loc(trigger_date))
        window_dates = all_dates[max(0, trigger_pos - half_life + 1) : trigger_pos + 1]
        combined_factor = pd.Series(0.0, index=prices.columns)
        total_weight = 0.0

        for signal_date in window_dates:
            days_ago = trigger_pos - int(all_dates.get_loc(signal_date))
            weight = 2 ** (-days_ago / half_life)
            for signal_name in timing_signals:
                cache_key = (signal_date, signal_name)
                if cache_key in rank_cache:
                    combined_factor = combined_factor.add(
                        rank_cache[cache_key] * weight,
                        fill_value=0.0,
                    )
                    total_weight += weight
        if total_weight <= 0:
            continue

        percentile_rank = (combined_factor / total_weight).dropna().rank(pct=True)
        for group_index in range(n_groups):
            lower = group_index / n_groups
            upper = (group_index + 1) / n_groups
            members = percentile_rank[
                percentile_rank.ge(lower) & percentile_rank.le(upper)
            ]
            for asset_code, factor_value in members.items():
                rows.append(
                    {
                        "date": trigger_date,
                        "asset_code": asset_code,
                        "group_id": group_index + 1,
                        "factor_value": factor_value,
                    }
                )
    return pd.DataFrame(rows)


def build_time_spliced_nav(
    price_df: pd.DataFrame,
    detail_df: pd.DataFrame,
    n_groups: int,
    holding_days: int = 20,
) -> BacktestNavResult:
    """Reproduce the original SparseSignalTester long/short/hedge curves.

    Group ``n_groups`` is the long portfolio and group 1 is the short
    portfolio.  Each signal is held for at most ``holding_days`` trading days;
    a new signal ends the preceding holding period.  Point-to-point portfolio
    returns are converted to constant geometric daily returns, and only active
    signal periods are concatenated on the displayed timeline.
    """
    required = {"date", "asset_code", "group_id"}
    if price_df is None or price_df.empty:
        raise ValueError("行业价格为空，无法生成回测净值")
    if detail_df is None or detail_df.empty or not required.issubset(detail_df.columns):
        raise ValueError("信号分组明细缺少 date、asset_code 或 group_id")
    if n_groups < 2 or holding_days <= 0:
        raise ValueError("分组数和持有期必须为正数")

    prices = price_df.copy().sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    prices = prices.apply(pd.to_numeric, errors="coerce").ffill()

    detail = detail_df.loc[:, ["date", "asset_code", "group_id"]].copy()
    detail["date"] = pd.to_datetime(detail["date"], errors="coerce")
    detail["group_id"] = pd.to_numeric(detail["group_id"], errors="coerce")
    detail = detail.dropna(subset=["date", "asset_code", "group_id"])

    column_map = {_normalise_asset_name(column): column for column in prices.columns}
    grouped = {date: group for date, group in detail.groupby("date", sort=True)}
    trigger_dates = sorted(set(grouped).intersection(prices.index))
    if not trigger_dates:
        raise ValueError("行业价格与信号触发日期没有交集")

    daily_returns = {"long": [], "short": [], "long_short": []}
    dates_spliced = []
    segment_bases = {"long": 1.0, "short": 1.0, "long_short": 1.0}
    max_endpoint_error = 0.0
    used_signals = 0

    for signal_index, start_date in enumerate(trigger_dates):
        start_pos = int(prices.index.get_loc(start_date))
        if signal_index + 1 < len(trigger_dates):
            next_pos = int(prices.index.get_loc(trigger_dates[signal_index + 1]))
            end_pos = min(start_pos + holding_days, next_pos)
        else:
            end_pos = min(start_pos + holding_days, len(prices.index) - 1)
        if end_pos <= start_pos:
            continue

        group = grouped[start_date]
        long_names = group.loc[group["group_id"].eq(n_groups), "asset_code"]
        short_names = group.loc[group["group_id"].eq(1), "asset_code"]
        long_assets = [column_map.get(_normalise_asset_name(name)) for name in long_names]
        short_assets = [column_map.get(_normalise_asset_name(name)) for name in short_names]
        missing_assets = [
            str(name)
            for name, asset in zip(list(long_names) + list(short_names), long_assets + short_assets)
            if asset is None
        ]
        if missing_assets:
            preview = "、".join(missing_assets[:3])
            raise ValueError(f"历史行业名称无法映射到价格列：{preview}")
        if not long_assets or not short_assets:
            continue

        start_prices = prices.iloc[start_pos]
        end_prices = prices.iloc[end_pos]
        long_return = float(
            (end_prices.reindex(long_assets) / start_prices.reindex(long_assets) - 1.0).mean()
        )
        short_return = float(
            (end_prices.reindex(short_assets) / start_prices.reindex(short_assets) - 1.0).mean()
        )
        long_return = 0.0 if not np.isfinite(long_return) else long_return
        short_return = 0.0 if not np.isfinite(short_return) else short_return
        total_returns = {
            "long": long_return,
            "short": short_return,
            "long_short": long_return - short_return,
        }
        period_days = end_pos - start_pos

        for key, total_return in total_returns.items():
            if 1.0 + total_return <= 0:
                raise ValueError(f"{start_date:%Y-%m-%d} 的{key}区间收益无法几何日度化")
            daily_return = (1.0 + total_return) ** (1.0 / period_days) - 1.0
            daily_returns[key].extend([daily_return] * period_days)
            expected_endpoint = segment_bases[key] * (1.0 + total_return)
            actual_endpoint = segment_bases[key] * (1.0 + daily_return) ** period_days
            max_endpoint_error = max(max_endpoint_error, abs(actual_endpoint - expected_endpoint))
            segment_bases[key] = expected_endpoint

        dates_spliced.extend(prices.index[start_pos + 1 : end_pos + 1])
        used_signals += 1

    if not dates_spliced:
        raise ValueError("没有可拼接的信号活跃区间")

    nav = {
        key: pd.Series(
            np.cumprod(1.0 + np.asarray(values, dtype=float)),
            index=pd.DatetimeIndex(dates_spliced, name="date"),
            name=key,
        )
        for key, values in daily_returns.items()
    }
    return BacktestNavResult(
        long=nav["long"],
        short=nav["short"],
        long_short=nav["long_short"],
        n_groups=n_groups,
        signal_count=used_signals,
        max_endpoint_error=max_endpoint_error,
    )


def build_auto_updating_nav(
    price_df: pd.DataFrame,
    baseline_detail_df: pd.DataFrame,
    timing_signals: dict,
    n_groups: int,
    holding_days: int = 20,
) -> BacktestNavResult:
    """Extend the fixed historical groups with triggers found in current data."""
    if baseline_detail_df is None or baseline_detail_df.empty:
        raise ValueError("旧版回测信号分组基线为空")
    baseline = baseline_detail_df.copy()
    baseline["date"] = pd.to_datetime(baseline["date"], errors="coerce")
    baseline = baseline.dropna(subset=["date"])
    if baseline.empty:
        raise ValueError("旧版回测信号分组基线没有有效日期")

    latest_groups = build_latest_signal_groups(
        price_df,
        timing_signals,
        n_groups=n_groups,
        after_date=baseline["date"].max(),
    )
    combined = pd.concat([baseline, latest_groups], ignore_index=True, sort=False)
    result = build_time_spliced_nav(
        price_df,
        combined,
        n_groups=n_groups,
        holding_days=holding_days,
    )
    result.new_signal_count = (
        int(latest_groups["date"].nunique()) if not latest_groups.empty else 0
    )
    return result


def calculate_performance_metrics(
    nav: pd.Series,
    annualization_days: int = 252,
) -> PerformanceMetrics:
    """Calculate active-period annualized return, drawdown and volatility."""
    series = pd.to_numeric(nav, errors="coerce").dropna()
    if series.empty or (series <= 0).any():
        return PerformanceMetrics(np.nan, np.nan, np.nan)

    daily_returns = series.div(series.shift(1)).sub(1.0)
    daily_returns.iloc[0] = series.iloc[0] - 1.0
    annualized_return = series.iloc[-1] ** (annualization_days / len(series)) - 1.0
    annualized_volatility = daily_returns.std(ddof=1) * np.sqrt(annualization_days)
    nav_with_initial = pd.concat(
        [pd.Series([1.0], index=[series.index[0] - pd.Timedelta(days=1)]), series]
    )
    max_drawdown = (nav_with_initial / nav_with_initial.cummax() - 1.0).min()
    return PerformanceMetrics(
        annualized_return=float(annualized_return),
        max_drawdown=float(max_drawdown),
        annualized_volatility=float(annualized_volatility),
    )


def load_backtest_nav(path: str, n_groups: int) -> BacktestNavResult:
    """Load a precomputed, cloud-portable daily NAV artifact."""
    frame = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "long", "short", "long_short"}
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError(f"回测净值文件格式无效：{path}")
    frame = frame.loc[:, ["date", "long", "short", "long_short"]].copy()
    frame = frame.dropna(subset=["date"]).drop_duplicates("date", keep="last")
    frame = frame.sort_values("date").set_index("date")
    frame = frame.apply(pd.to_numeric, errors="coerce")
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError(f"回测净值包含无效值：{path}")
    return BacktestNavResult(
        long=frame["long"],
        short=frame["short"],
        long_short=frame["long_short"],
        n_groups=n_groups,
        source="snapshot",
    )
