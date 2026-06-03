#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
update_data.py — Auto-update pipeline for gx_pit_mom_Auto_Bot.

数据源:
  - CITIC 行业指数: D:/DATA/INDEX/ZX/ (Wind 导出, 主数据源)
  - 基准指数 000985: D:/DATA/INDEX/STOCK/000985.CSI.xlsx (Wind 导出, 优先)
                     或东方财富/腾讯财经 API (在线兜底)
  - TDX 通达信板块指数: mootdx (仅作为 Wind 数据未更新时的日度补充)

工作流:
  ┌──────────────────────────────────────────────────────────────┐
  │  D:/DATA/INDEX/ZX/  (CITIC 行业指数, Wind 导出)             │
  │  D:/DATA/INDEX/STOCK/ (000985 中证全指, Wind 导出)          │
  │  Eastmoney / 腾讯财经 (基准指数在线兜底)                     │
  └──────────────────┬───────────────────────────────────────────┘
                     │  update_data.py
                     ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  gx_pit_mom_Auto_Bot/data/                                  │
  │  ├── 000985_prices.xlsx          (基准指数 OHLCV)           │
  │  ├── ZX_YJHY.xlsx                (一级行业 28 个)           │
  │  ├── ZX_EJHY.xlsx                (二级行业 108 个)           │
  │  ├── group_assignment_details_*.csv  (历史分组业绩)         │
  │  └── .last_update                (最后更新时间戳)           │
  └──────────────────────────────────────────────────────────────┘

用法:
  python update_data.py              # 完整更新 (合并 Wind 数据 + 生成 CSV)
  python update_data.py --daily      # 每日增量更新
  python update_data.py --check      # 仅检查是否有新数据
  python update_data.py --skip-csv   # 跳过 CSV 生成
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

try:
    from src.mootdx_fetcher import MootdxFetcher
except ImportError:
    print("[WARN] 无法导入 src.mootdx_fetcher.MootdxFetcher")
    MootdxFetcher = None

# ── 路径配置 ──────────────────────────────────────────────────────────
_TARGET_DATA_DIR = Path(__file__).resolve().parent / "data"
_SOURCE_ZX_DIR = Path("D:/DATA/INDEX/ZX")                   # CITIC 行业指数
_SOURCE_INDEX_DIR = Path("D:/DATA/INDEX/STOCK")              # 基准指数

_FILE_MAP = {
    "ZX_YJHY.xlsx": _SOURCE_ZX_DIR / "ZX_YJHY.xlsx",
    "ZX_EJHY.xlsx": _SOURCE_ZX_DIR / "ZX_EJHY.xlsx",
}

# ── 基准指数 ──────────────────────────────────────────────────────────
_BENCHMARK_CODE = "000985"
_BENCHMARK_WIND_PATH = _SOURCE_INDEX_DIR / "000985.CSI.xlsx"
_BENCHMARK_SECID = "1.000985"
_BENCHMARK_NAME = "中证全指"
_EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
}
_TENCENT_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


# ── 辅助函数 ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
        safe_msg = msg.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        print(f"[{ts}] {safe_msg}")


def _read_excel_safe(path: Path) -> pd.DataFrame:
    """安全读取 Excel, 不自动推断表头 (保留原始多行表头结构)."""
    try:
        return pd.read_excel(str(path), header=None)
    except Exception as e:
        _log(f"  [FAIL] 读取失败 {path.name}: {e}")
        return pd.DataFrame()


def _detect_date_col(df: pd.DataFrame) -> Optional[str]:
    """检测 DataFrame 中的日期列."""
    for col in df.columns:
        col_lower = str(col).lower().strip()
        if col_lower in ("date", "日期", "tradingday"):
            return col
    if len(df.columns) > 0 and len(df) > 0:
        first_col = df.columns[0]
        try:
            test = pd.to_datetime(df[first_col].iloc[0], errors="coerce")
            if not pd.isna(test):
                return first_col
        except Exception:
            pass
    return None


def _parse_wide_table(df: pd.DataFrame) -> pd.DataFrame:
    """解析 Wind 宽表格式 (多行表头 + 日期索引), 返回干净的 DataFrame.

    Wind CITIC 行业指数标准格式:
      Row 0: 来源标识 (如 'Wind')
      Row 1: '指数名称' + 行业全名 (如 '中信行业指数:石油石化')
      Row 2: '频率' + 各列 '日'
      Row 3: '单位' + 各列 '点'
      Row 4: '指数ID' + 各列 Wind ID (如 'M0331600')
      Row 5: '来源' + 各列 '中信证券股份有限公司'
      Row 6+: 数据行 (日期 + 数值)
    """
    if df.empty:
        return df

    # 检查第一个非 NaN 值以识别 Wind 格式
    first_cell = str(df.iloc[0, 0]).strip() if len(df) > 0 else ""
    is_wind_format = first_cell in ("Wind", "通达信", "腾讯财经", "东方财富")

    header_row_idx = None
    if is_wind_format:
        # Wind 标准格式: Row 1 = 行业名称, Row 6+ = 数据
        header_row_idx = 1
    else:
        # 通用格式: 扫描寻找表头
        header_keywords = ["日期", "Date", "指标名称", "指数名称", "TradingDay", "Trading Day"]
        for i in range(min(30, len(df))):
            row_vals = [str(v).strip() for v in df.iloc[i].values]
            if any(k in row_vals for k in header_keywords):
                header_row_idx = i
                break

    if header_row_idx is not None:
        df.columns = df.iloc[header_row_idx]
        # Wind 格式: header 后跳过 5 行元数据 (频率/单位/指数ID/来源共4行+1行冗余)
        # 通用格式: header 后跳过 1 行
        skip_rows = 5 if is_wind_format else 1
        df = df.iloc[header_row_idx + skip_rows:].reset_index(drop=True)
    else:
        # 备选: 寻找第一列中看起来像日期的行作为数据起始行
        data_start = None
        for i in range(min(30, len(df))):
            try:
                val = df.iloc[i, 0]
                test_date = pd.to_datetime(val, errors="coerce")
                if not pd.isna(test_date):
                    data_start = i
                    break
            except Exception:
                continue
        if data_start is not None and data_start > 0:
            df.columns = df.iloc[data_start - 1]
            df = df.iloc[data_start:].reset_index(drop=True)

    df.columns = [str(c).strip() for c in df.columns]
    date_col = _detect_date_col(df)
    if not date_col and len(df.columns) > 0 and len(df) > 0:
        first_col = df.columns[0]
        try:
            sample = df[first_col].dropna().head(3)
            if len(sample) >= 1:
                tests = pd.to_datetime(sample, errors="coerce")
                if tests.notna().all():
                    date_col = first_col
        except Exception:
            pass

    if date_col:
        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.set_index("date").sort_index()

    if not isinstance(df.index, pd.DatetimeIndex):
        _log("  [WARN] 无法解析日期索引, 返回空 DataFrame")
        return pd.DataFrame()

    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")
        except Exception:
            pass

    return df


def _write_excel_wide(
    df: pd.DataFrame,
    path: Path,
    index_label: str = "Date",
    col0_label: str = "Wind",
    source_label: str = "中信证券股份有限公司",
    id_prefix: str = "M0331",
) -> None:
    """将宽表 DataFrame 写入 Excel, 格式兼容 WindLocalProvider.get_wide_table().

    Row 0: col0_label + 各列名 (如 'Wind', '中信行业指数:石油石化', ...)
    Row 1: '指数名称' + 各列 '日'
    Row 2: '频率' + 各列 '点'
    Row 3: '指数ID' + 各列 id_prefix
    Row 4: '来源' + 各列 source_label
    Row 5 (header): index_label + 各列名
    Row 6+: 数据行
    """
    if df.empty:
        _log(f"  [WARN] 空数据, 跳过写入 {path.name}")
        return

    n_cols = len(df.columns)
    rows = [
        [col0_label] + list(df.columns),
        ["指数名称"] + ["日"] * n_cols,
        ["频率"] + ["点"] * n_cols,
        ["指数ID"] + [f"{id_prefix}{i:04d}" for i in range(1, n_cols + 1)],
        ["来源"] + [source_label] * n_cols,
        [index_label] + list(df.columns),
    ]

    data_rows = []
    for idx, row in df.iterrows():
        data_rows.append(
            [idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)]
            + [float(v) if not pd.isna(v) else "" for v in row.values]
        )

    header_df = pd.DataFrame(rows)
    data_df = pd.DataFrame(data_rows)
    output_df = pd.concat([header_df, data_df], ignore_index=True)
    output_df.to_excel(str(path), index=False, header=False)
    _log(f"  ✓ 写入 {path.name}: {len(df)} 行 × {len(df.columns)} 列, "
         f"日期范围 {df.index[0]} ~ {df.index[-1]}")


# ── 基准指数 (在线) ────────────────────────────────────────────────────

def _request_eastmoney_klines(beg: str, end: str, max_retries: int = 4) -> list[str]:
    """从东方财富拉取中证全指日线 kline 字符串列表."""
    params = {
        "secid": _BENCHMARK_SECID,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "beg": beg,
        "end": end,
    }
    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.get(_EASTMONEY_KLINE_URL, params=params,
                                    headers=_EASTMONEY_HEADERS, timeout=20)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            code = str(data.get("code") or "")
            klines = data.get("klines") or []
            if code != _BENCHMARK_CODE:
                raise ValueError(f"东方财富返回代码异常: code={code}")
            return klines
        except Exception as exc:
            last_error = exc
            time.sleep(1.0 + attempt * 1.5)
    raise RuntimeError(f"东方财富中证全指请求失败: {last_error}") from last_error


def _parse_eastmoney_klines(klines: list[str]) -> pd.DataFrame:
    """解析东方财富 kline 字符串为 OHLCV DataFrame."""
    if not klines:
        return pd.DataFrame()
    rows = []
    for item in klines:
        parts = str(item).split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "date": parts[0], "open": parts[1], "close": parts[2],
            "high": parts[3], "low": parts[4], "volume": parts[5], "amount": parts[6],
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df["_d"] = df["date"].dt.normalize()
    result = df.set_index("_d")[["open", "high", "low", "close", "volume", "amount"]]
    return result[~result.index.duplicated(keep="last")].sort_index()


def _request_tencent_klines(start_year: int, end_year: int, max_retries: int = 3) -> list[list]:
    """从腾讯财经拉取中证全指日线列表."""
    all_rows: list[list] = []
    last_error = None
    for year in range(start_year, end_year + 1):
        params = {
            "_var": "kline_dayqfq",
            "param": f"sh{_BENCHMARK_CODE},day,{year}-01-01,{year + 1}-12-31,640,qfq",
            "r": "0.8205512681390605",
        }
        for attempt in range(max_retries):
            try:
                response = requests.get(_TENCENT_KLINE_URL, params=params,
                                        headers=_EASTMONEY_HEADERS, timeout=20)
                response.raise_for_status()
                text = response.text
                json_start = text.find("={")
                payload_text = text[json_start + 1:] if json_start >= 0 else text
                payload = json.loads(payload_text)
                data = payload.get("data", {}).get(f"sh{_BENCHMARK_CODE}", {})
                rows = data.get("day") or data.get("qfqday") or []
                all_rows.extend(rows)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1.0 + attempt)
        else:
            raise RuntimeError(f"腾讯财经中证全指请求失败: {last_error}") from last_error
    return all_rows


def _parse_tencent_klines(rows: list[list], start_date: str, end_date: str) -> pd.DataFrame:
    """解析腾讯财经 kline 列表为 OHLCV DataFrame."""
    if not rows:
        return pd.DataFrame()
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        parsed.append({
            "date": row[0], "open": row[1], "close": row[2],
            "high": row[3], "low": row[4], "volume": row[5], "amount": np.nan,
        })
    df = pd.DataFrame(parsed)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)
    df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)]
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df["_d"] = df["date"].dt.normalize()
    result = df.set_index("_d")[["open", "high", "low", "close", "volume", "amount"]]
    return result[~result.index.duplicated(keep="last")].sort_index()


# ── 主流程 ──────────────────────────────────────────────────────────────

def _merge_wind_file(source_path: Path, target_path: Path) -> bool:
    """从 D:/DATA 读取 Wind 导出的宽表, 写入目标路径."""
    if not source_path.exists():
        _log(f"  [SKIP] 源文件不存在: {source_path}")
        return False

    _log(f"  读取源: {source_path.name}")
    source_df = _read_excel_safe(source_path)
    if source_df.empty:
        return False

    parsed = _parse_wide_table(source_df)
    if parsed.empty:
        _log(f"  [WARN] 源数据解析后为空: {source_path.name}")
        return False

    _log(f"    源数据: {len(parsed)} 行, {len(parsed.columns)} 列, "
         f"{parsed.index[0].date()} ~ {parsed.index[-1].date()}")

    # 如果目标文件已存在，合并（保留目标中更早的日期）
    if target_path.exists():
        target_df = _read_excel_safe(target_path)
        target_parsed = _parse_wide_table(target_df) if not target_df.empty else pd.DataFrame()
        if not target_parsed.empty:
            target_end = target_parsed.index[-1]
            source_end = parsed.index[-1]
            _log(f"    目标已有: {len(target_parsed)} 行, 最后日期 {target_end.date()}")

            # 合并且去重
            merged = pd.concat([parsed, target_parsed]).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
            # 如果源数据更新，用源数据
            if source_end > target_end:
                _log(f"    源数据更新 ({source_end.date()} > {target_end.date()}), 使用源数据")
                merged.update(parsed)
            parsed = merged

    _write_excel_wide(parsed, target_path)
    return True


def merge_citic_from_source() -> dict[str, bool]:
    """从 D:/DATA 合并 CITIC 行业数据到目标 data 目录."""
    results = {}
    _log("=" * 60)
    _log("[Wind] 步骤 1: 合并 CITIC 行业数据 (D:/DATA → data/)")

    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    for target_name, source_path in _FILE_MAP.items():
        target_path = _TARGET_DATA_DIR / target_name
        results[target_name] = _merge_wind_file(source_path, target_path)

    return results


def fetch_benchmark_data() -> pd.DataFrame:
    """获取基准指数 (000985 中证全指).

    优先级: D:/DATA Wind 导出 > 东方财富 API > 腾讯财经 API
    """
    _log("=" * 60)
    _log(f"[Benchmark] 获取基准指数: {_BENCHMARK_NAME}")

    # 1. 优先使用 Wind 本地导出
    if _BENCHMARK_WIND_PATH.exists():
        _log(f"  [Wind] 读取本地: {_BENCHMARK_WIND_PATH.name}")
        raw = _read_excel_safe(_BENCHMARK_WIND_PATH)
        if not raw.empty:
            parsed = _parse_wide_table(raw)
            if not parsed.empty:
                _log(f"  ✓ Wind: {len(parsed)} 行, {parsed.index[0].date()} ~ {parsed.index[-1].date()}")
                return parsed

    # 2. 东方财富在线
    _log(f"  [Eastmoney] 在线拉取...")
    try:
        klines = _request_eastmoney_klines("20170101", "20500101")
        result = _parse_eastmoney_klines(klines)
        if not result.empty:
            _log(f"  ✓ 东方财富: {len(result)} 行, {result.index[0].date()} ~ {result.index[-1].date()}")
            return result
    except Exception as e:
        _log(f"  [WARN] 东方财富失败: {e}")

    # 3. 腾讯财经兜底
    _log(f"  [Tencent] 兜底拉取...")
    try:
        end_date = datetime.now().strftime("%Y-%m-%d")
        rows = _request_tencent_klines(2017, datetime.now().year)
        result = _parse_tencent_klines(rows, "2017-01-01", end_date)
        if not result.empty:
            _log(f"  ✓ 腾讯财经: {len(result)} 行, {result.index[0].date()} ~ {result.index[-1].date()}")
            return result
    except Exception as e:
        _log(f"  [FAIL] 腾讯财经失败: {e}")

    _log("  [FAIL] 所有基准指数数据源均不可用")
    return pd.DataFrame()


def update_all_data() -> dict[str, bool]:
    """更新全部数据文件.

    Returns
    -------
    dict[str, bool]
        各文件的写入状态.
    """
    results = {}
    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. CITIC 行业数据 (从 Wind D:/DATA) ──
    citic_results = merge_citic_from_source()
    results.update(citic_results)

    # ── 2. 基准指数 ──
    bench_df = fetch_benchmark_data()
    if not bench_df.empty:
        bench_path = _TARGET_DATA_DIR / "000985_prices.xlsx"
        _write_excel_wide(bench_df, bench_path, col0_label="Wind",
                          source_label="中信证券股份有限公司", id_prefix="M0331")
        results["000985_prices.xlsx"] = True
    else:
        _log("  [WARN] 基准指数数据为空")
        results["000985_prices.xlsx"] = False

    return results


def regenerate_group_details() -> bool:
    """运行 GXPitMomActions 分析, 重新生成 group_assignment_details CSV."""
    _log("=" * 60)
    _log("[REFRESH] 重新生成 group_assignment_details CSV")

    try:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

        from src.analysis import GXPitMomActions

        analyzer = GXPitMomActions(
            data_dir=str(_TARGET_DATA_DIR) + "/",
            start_date="2017-01-01",
            half_life=10,
        )

        index_data = analyzer.dp.get_wide_table("000985_prices.xlsx")
        zx_yj_prices = analyzer.dp.get_wide_table("ZX_YJHY.xlsx")
        zx_ej_prices = analyzer.dp.get_wide_table("ZX_EJHY.xlsx")

        if index_data.empty:
            _log("  [FAIL] 基准指数数据为空")
            return False

        _log(f"  基准指数: {len(index_data)} 行, {index_data.index[0].date()} ~ {index_data.index[-1].date()}")
        _log(f"  一级行业: {len(zx_yj_prices)} 行 × {len(zx_yj_prices.columns)} 列")
        _log(f"  二级行业: {len(zx_ej_prices)} 行 × {len(zx_ej_prices.columns)} 列")

        for sector_name, sector_prices, output_name, n_groups in [
            ("一级行业", zx_yj_prices, "group_assignment_details_zx_yjhy.csv", 5),
            ("二级行业", zx_ej_prices, "group_assignment_details_zx_ejhy.csv", 10),
        ]:
            if sector_prices.empty:
                _log(f"  [WARN] {sector_name} 数据为空, 跳过")
                continue

            _log(f"  计算 {sector_name} 分组 ({len(sector_prices.columns)} 个行业)...")

            returns_20d = sector_prices.pct_change(20)
            rows = []
            for dt in returns_20d.index:
                row = returns_20d.loc[dt].dropna()
                if len(row) < n_groups:
                    continue

                factor_values = row.sort_values(ascending=False)
                future_loc = sector_prices.index.get_loc(dt)
                future_end = min(future_loc + 20, len(sector_prices) - 1)
                if future_end <= future_loc:
                    continue
                future_price = sector_prices.iloc[future_end]
                current_price = sector_prices.loc[dt]
                period_return = (future_price / current_price - 1).dropna()

                if dt in index_data.index:
                    bench_loc = index_data.index.get_loc(dt)
                    bench_end = min(bench_loc + 20, len(index_data) - 1)
                    if bench_end > bench_loc and "close" in index_data.columns:
                        bench_ret = (index_data["close"].iloc[bench_end] / index_data["close"].iloc[bench_loc] - 1)
                    else:
                        bench_ret = 0.0
                else:
                    bench_ret = 0.0

                sorted_assets = factor_values.index.tolist()
                group_size = max(len(sorted_assets) // n_groups, 1)
                for i, asset in enumerate(sorted_assets):
                    group_id = min(i // group_size, n_groups - 1) + 1
                    fv = factor_values.get(asset, np.nan)
                    pr = period_return.get(asset, np.nan)
                    excess = pr - bench_ret if not pd.isna(pr) else np.nan
                    rows.append({
                        "date": dt.strftime("%Y-%m-%d"),
                        "asset_code": asset,
                        "factor_value": round(float(fv), 6) if not pd.isna(fv) else np.nan,
                        "group_id": group_id,
                        "period_return": round(float(pr), 6) if not pd.isna(pr) else np.nan,
                        "excess_return": round(float(excess), 6) if not pd.isna(excess) else np.nan,
                    })

            if not rows:
                _log(f"    [WARN] {sector_name}: 未生成任何分组数据")
                continue

            detail_df = pd.DataFrame(rows)
            detail_df = detail_df.dropna(subset=["factor_value", "period_return"])
            output_path = _TARGET_DATA_DIR / output_name
            detail_df.to_csv(output_path, index=False)
            _log(f"    ✓ {output_name}: {len(detail_df)} 条记录, "
                 f"{detail_df['date'].nunique()} 个截面日")

        return True

    except Exception as e:
        _log(f"  [FAIL] 生成分组详情失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def write_update_timestamp() -> None:
    ts_file = _TARGET_DATA_DIR / ".last_update"
    ts_file.write_text(datetime.now().isoformat(), encoding="utf-8")
    _log(f"  ✓ 更新时间戳: {ts_file}")


def check_for_updates() -> bool:
    """检查是否有新数据可用."""
    has_updates = False

    # 检查 D:/DATA 源文件是否比目标文件更新
    for target_name, source_path in _FILE_MAP.items():
        target_path = _TARGET_DATA_DIR / target_name
        if source_path.exists():
            if not target_path.exists():
                _log(f"  NEW: {target_name} (目标不存在)")
                has_updates = True
            elif source_path.stat().st_mtime > target_path.stat().st_mtime:
                _log(f"  NEWER: {target_name} (源文件更新)")
                has_updates = True

    # 检查基准指数
    bench_target = _TARGET_DATA_DIR / "000985_prices.xlsx"
    if _BENCHMARK_WIND_PATH.exists():
        if not bench_target.exists():
            has_updates = True
        elif _BENCHMARK_WIND_PATH.stat().st_mtime > bench_target.stat().st_mtime:
            _log(f"  NEWER: 000985_prices.xlsx (Wind 源文件更新)")
            has_updates = True

    if not has_updates:
        _log("  无新数据")
    return has_updates


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="gx_pit_mom_Auto_Bot 数据自动更新管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python update_data.py             完整运行: 合并 Wind CITIC 数据 + 生成 CSV
  python update_data.py --daily     每日增量更新
  python update_data.py --check     仅检查是否有新数据可用
  python update_data.py --skip-csv  跳过 CSV 生成
        """,
    )
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--skip-csv", action="store_true")
    args = parser.parse_args()

    _log("[START] gx_pit_mom_Auto_Bot 数据更新管道")
    _log(f"  目标目录: {_TARGET_DATA_DIR}")

    if args.check:
        _log("[CHECK] 检查更新...")
        has = check_for_updates()
        sys.exit(0 if has else 1)

    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    success = True

    # ── 拉取全部数据 ──
    results = update_all_data()
    success = all(results.values())

    # ── 生成分组详情 CSV ──
    if not args.skip_csv:
        csv_ok = regenerate_group_details()
        if not csv_ok:
            _log("  [WARN] CSV 生成部分失败")
            success = False

    write_update_timestamp()

    if success:
        _log("[OK] 数据更新完成! Streamlit 网站将在文件变化时自动刷新.")
    else:
        _log("[WARN] 数据更新部分完成 (部分步骤失败).")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
