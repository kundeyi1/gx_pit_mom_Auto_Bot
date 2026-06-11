#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
update_data.py — Auto-update pipeline for gx_pit_mom_Auto_Bot.

数据源:
  - CITIC 行业指数: Tushare ci_daily API (真正的中信行业指数, 非代理)
  - 基准指数 000985: 东方财富 API (优先) / 腾讯财经 API (兜底)

工作流:
  ┌──────────────────────────────────────────────────────────────┐
  │  Tushare ci_daily  (CITIC 行业指数日线, 28 L1 + 108 L2)     │
  │  Eastmoney / 腾讯财经 (000985 中证全指)                      │
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
  python update_data.py              # 完整更新 (拉取 CITIC 数据 + 生成 CSV)
  python update_data.py --skip-csv   # 跳过 CSV 生成 (仅更新数据文件)
  python update_data.py --check      # 仅检查是否有新数据
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
import tushare as ts

# ── Tushare 配置 ──────────────────────────────────────────────────────
_TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if _TUSHARE_TOKEN:
    ts.set_token(_TUSHARE_TOKEN)
_PRO = ts.pro_api() if _TUSHARE_TOKEN else None

# ── 路径配置 ──────────────────────────────────────────────────────────
_TARGET_DATA_DIR = Path(__file__).resolve().parent / "data"

# ── 基准指数 ──────────────────────────────────────────────────────────
_BENCHMARK_CODE = "000985"
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

# ── CITIC 行业指数代码 (来自 Tushare ci_index_member) ────────────────
_CITIC_L1_CODES: dict[str, str] = {
    "CI005001.CI": "石油石化",
    "CI005002.CI": "煤炭",
    "CI005003.CI": "有色金属",
    "CI005004.CI": "电力及公用事业",
    "CI005005.CI": "钢铁",
    "CI005006.CI": "基础化工",
    "CI005007.CI": "建筑",
    "CI005008.CI": "建材",
    "CI005009.CI": "轻工制造",
    "CI005010.CI": "机械",
    "CI005011.CI": "电力设备及新能源",
    "CI005012.CI": "国防军工",
    "CI005013.CI": "汽车",
    "CI005014.CI": "商贸零售",
    "CI005015.CI": "消费者服务",
    "CI005016.CI": "家电",
    "CI005017.CI": "纺织服装",
    "CI005018.CI": "医药",
    "CI005019.CI": "食品饮料",
    "CI005020.CI": "农林牧渔",
    "CI005021.CI": "银行",
    "CI005022.CI": "非银行金融",
    "CI005023.CI": "房地产",
    "CI005024.CI": "交通运输",
    "CI005025.CI": "电子",
    "CI005026.CI": "通信",
    "CI005027.CI": "计算机",
    "CI005028.CI": "传媒",
    # CI005029: 综合, CI005030: 综合金融 — 原 Wind 数据只有 28 个, 保持一致
}

_CITIC_L2_CODES: dict[str, str] = {
    "CI005101.CI": "石油开采Ⅱ",
    "CI005187.CI": "油服工程",
    "CI005102.CI": "石油化工",
    "CI005104.CI": "煤炭开采洗选",
    "CI005105.CI": "煤炭化工",
    "CI005106.CI": "贵金属",
    "CI005107.CI": "工业金属",
    "CI005188.CI": "稀有金属",
    "CI005109.CI": "发电及电网",
    "CI005110.CI": "环保及公用事业",
    "CI005111.CI": "普钢",
    "CI005189.CI": "其他钢铁",
    "CI005190.CI": "特材",
    "CI005113.CI": "农用化工",
    "CI005191.CI": "化学纤维",
    "CI005192.CI": "化学原料",
    "CI005193.CI": "其他化学制品Ⅱ",
    "CI005194.CI": "塑料及制品",
    "CI005195.CI": "橡胶及制品",
    "CI005117.CI": "建筑施工",
    "CI005196.CI": "建筑装修Ⅱ",
    "CI005197.CI": "建筑设计及服务Ⅱ",
    "CI005198.CI": "结构材料",
    "CI005199.CI": "装饰材料",
    "CI005800.CI": "专用材料Ⅱ",
    "CI005122.CI": "造纸Ⅱ",
    "CI005801.CI": "包装印刷",
    "CI005802.CI": "家居",
    "CI005803.CI": "文娱轻工Ⅱ",
    "CI005804.CI": "其他轻工Ⅱ",
    "CI005124.CI": "工程机械Ⅱ",
    "CI005805.CI": "专用机械",
    "CI005806.CI": "通用设备",
    "CI005127.CI": "运输设备",
    "CI005807.CI": "仪器仪表Ⅱ",
    "CI005129.CI": "金属制品Ⅱ",
    "CI005808.CI": "电气设备",
    "CI005809.CI": "电源设备",
    "CI005810.CI": "新能源动力系统",
    "CI005133.CI": "航空航天",
    "CI005134.CI": "兵器兵装Ⅱ",
    "CI005135.CI": "其他军工Ⅱ",
    "CI005136.CI": "乘用车Ⅱ",
    "CI005137.CI": "商用车",
    "CI005138.CI": "汽车零部件Ⅱ",
    "CI005139.CI": "汽车销售及服务Ⅱ",
    "CI005140.CI": "摩托车及其他Ⅱ",
    "CI005811.CI": "一般零售",
    "CI005812.CI": "贸易Ⅱ",
    "CI005813.CI": "专营连锁",
    "CI005814.CI": "电商及服务Ⅱ",
    "CI005815.CI": "专业市场经营Ⅱ",
    "CI005143.CI": "旅游及休闲",
    "CI005144.CI": "酒店及餐饮",
    "CI005816.CI": "教育",
    "CI005817.CI": "综合服务",
    "CI005145.CI": "白色家电Ⅱ",
    "CI005146.CI": "黑色家电Ⅱ",
    "CI005818.CI": "小家电Ⅱ",
    "CI005819.CI": "照明电工及其他",
    "CI005820.CI": "厨房电器Ⅱ",
    "CI005185.CI": "纺织制造",
    "CI005821.CI": "品牌服饰",
    "CI005152.CI": "化学制药",
    "CI005153.CI": "中药生产",
    "CI005154.CI": "生物医药Ⅱ",
    "CI005155.CI": "其他医药医疗",
    "CI005156.CI": "酒类",
    "CI005822.CI": "饮料",
    "CI005823.CI": "食品",
    "CI005824.CI": "种植业",
    "CI005160.CI": "畜牧业",
    "CI005825.CI": "林业",
    "CI005162.CI": "渔业",
    "CI005826.CI": "农产品加工Ⅱ",
    "CI005163.CI": "国有大型银行Ⅱ",
    "CI005164.CI": "全国性股份制银行Ⅱ",
    "CI005827.CI": "区域性银行",
    "CI005165.CI": "证券Ⅱ",
    "CI005166.CI": "保险Ⅱ",
    "CI005828.CI": "多元金融",
    "CI005168.CI": "房地产开发和运营",
    "CI005829.CI": "房地产服务",
    "CI005830.CI": "资产管理Ⅱ",
    "CI005831.CI": "多领域控股Ⅱ",
    "CI005832.CI": "新兴金融服务Ⅱ",
    "CI005170.CI": "公路铁路",
    "CI005171.CI": "物流",
    "CI005172.CI": "航运港口",
    "CI005173.CI": "航空机场",
    "CI005834.CI": "半导体",
    "CI005835.CI": "元器件",
    "CI005836.CI": "光学光电",
    "CI005837.CI": "消费电子",
    "CI005838.CI": "其他电子零组件Ⅱ",
    "CI005839.CI": "电信运营Ⅱ",
    "CI005181.CI": "通信设备制造",
    "CI005840.CI": "增值服务Ⅱ",
    "CI005841.CI": "通讯工程服务",
    "CI005842.CI": "计算机设备",
    "CI005843.CI": "计算机软件",
    "CI005844.CI": "云服务",
    "CI005845.CI": "产业互联网",
    "CI005846.CI": "媒体",
    "CI005847.CI": "广告营销",
    "CI005848.CI": "文化娱乐",
    "CI005849.CI": "互联网媒体",
    "CI005178.CI": "综合Ⅱ",
}


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


def _write_excel_wide(
    df: pd.DataFrame,
    path: Path,
    index_label: str = "Date",
    col0_label: str = "Wind",
    source_label: str = "中信证券股份有限公司",
    id_prefix: str = "M0331",
) -> None:
    """将宽表 DataFrame 写入 Excel, 格式兼容 WindLocalProvider.get_wide_table()."""
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


def _load_existing_data(file_name: str) -> pd.DataFrame:
    """读取已有数据文件，作为在线数据源临时失败时的兜底."""
    try:
        from src.data_provider import WindLocalProvider

        df = WindLocalProvider(data_dir=str(_TARGET_DATA_DIR) + "/").get_wide_table(file_name)
        if not df.empty:
            _log(f"  [FALLBACK] 使用已有 {file_name}: {len(df)} 行 × {len(df.columns)} 列, "
                 f"{df.index[0].date()} ~ {df.index[-1].date()}")
        return df
    except Exception as exc:
        _log(f"  [WARN] 读取已有 {file_name} 失败: {exc}")
        return pd.DataFrame()


# ── Tushare CITIC 数据拉取 ────────────────────────────────────────────

def _fetch_citic_index_data(code_map: dict[str, str], label: str) -> pd.DataFrame:
    """通过 Tushare ci_daily 拉取 CITIC 行业指数 close 价格宽表.

    Parameters
    ----------
    code_map: CITIC 指数代码 → 行业名称映射
    label: 日志标签

    Returns
    -------
    pd.DataFrame
        以 date 为 index, 行业名称为 columns 的 close 价格宽表.
    """
    if _PRO is None:
        _log(f"  [FAIL] Tushare pro_api 不可用, 请设置 TUSHARE_TOKEN 环境变量")
        return pd.DataFrame()

    codes = list(code_map.keys())
    _log(f"[Tushare] 拉取 CITIC {label} ({len(codes)} 个指数)")

    all_series = {}
    success = 0

    for i, code in enumerate(codes):
        name = code_map[code]
        try:
            df = _PRO.query("ci_daily", ts_code=code,
                            start_date="20040101", end_date="20991231")
            if df is not None and not df.empty and "close" in df.columns:
                df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
                series = df.set_index("trade_date")["close"].sort_index()
                series = series[~series.index.duplicated(keep="last")]
                series.name = name
                series = pd.to_numeric(series, errors="coerce")
                all_series[name] = series
                success += 1
            else:
                _log(f"    [FAIL] {code} ({name}): 空数据")
        except Exception as e:
            _log(f"    [FAIL] {code} ({name}): {e}")

        # 进度 & 限速
        if (i + 1) % 30 == 0:
            _log(f"    进度: {i+1}/{len(codes)} (成功 {success})")
            time.sleep(1.0)  # Tushare 限速

    _log(f"  拉取完成: 成功 {success}/{len(codes)}")

    if not all_series:
        return pd.DataFrame()

    result = pd.DataFrame(all_series).sort_index()
    return result


# ── 基准指数 (在线) ────────────────────────────────────────────────────

def _request_eastmoney_klines(beg: str, end: str, max_retries: int = 4) -> list[str]:
    params = {
        "secid": _BENCHMARK_SECID,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "0", "beg": beg, "end": end,
    }
    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.get(_EASTMONEY_KLINE_URL, params=params,
                                    headers=_EASTMONEY_HEADERS, timeout=20)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            klines = data.get("klines") or []
            if klines:
                return klines
        except Exception as exc:
            last_error = exc
            time.sleep(1.0 + attempt * 1.5)
    raise RuntimeError(f"东方财富中证全指请求失败: {last_error}") from last_error


def _parse_eastmoney_klines(klines: list[str]) -> pd.DataFrame:
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
    df = df.dropna(subset=["date", "close"])
    result = df.set_index("date")[["open", "high", "low", "close", "volume", "amount"]]
    return result[~result.index.duplicated(keep="last")].sort_index()


def _request_tencent_klines(start_year: int, end_year: int, max_retries: int = 3) -> list[list]:
    all_rows: list[list] = []
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
            except Exception:
                time.sleep(1.0 + attempt)
    return all_rows


def _parse_tencent_klines(rows: list[list], start_date: str, end_date: str) -> pd.DataFrame:
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
    df = df.dropna(subset=["date", "close"])
    result = df.set_index("date")[["open", "high", "low", "close", "volume", "amount"]]
    return result[~result.index.duplicated(keep="last")].sort_index()


def fetch_benchmark_data() -> pd.DataFrame:
    """获取基准指数 (000985 中证全指).

    优先级: 东方财富 API > 腾讯财经 API
    """
    _log("=" * 60)
    _log(f"[Benchmark] 获取基准指数: {_BENCHMARK_NAME}")

    # 1. 东方财富
    _log("  [Eastmoney] 在线拉取...")
    try:
        klines = _request_eastmoney_klines("20170101", "20500101")
        result = _parse_eastmoney_klines(klines)
        if not result.empty:
            _log(f"  ✓ 东方财富: {len(result)} 行, "
                 f"{result.index[0].date()} ~ {result.index[-1].date()}")
            return result
    except Exception as e:
        _log(f"  [WARN] 东方财富失败: {e}")

    # 2. 腾讯财经兜底
    _log("  [Tencent] 兜底拉取...")
    try:
        end_date = datetime.now().strftime("%Y-%m-%d")
        rows = _request_tencent_klines(2017, datetime.now().year)
        result = _parse_tencent_klines(rows, "2017-01-01", end_date)
        if not result.empty:
            _log(f"  ✓ 腾讯财经: {len(result)} 行, "
                 f"{result.index[0].date()} ~ {result.index[-1].date()}")
            return result
    except Exception as e:
        _log(f"  [FAIL] 腾讯财经失败: {e}")

    _log("  [FAIL] 所有基准指数数据源均不可用")
    return pd.DataFrame()


# ── 主流程 ──────────────────────────────────────────────────────────────

def update_all_data() -> dict[str, bool]:
    """更新全部数据文件."""
    results = {}
    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. CITIC 一级行业 (Tushare) ──
    level1_df = _fetch_citic_index_data(_CITIC_L1_CODES, "一级行业")
    if not level1_df.empty:
        _write_excel_wide(level1_df, _TARGET_DATA_DIR / "ZX_YJHY.xlsx")
        results["ZX_YJHY.xlsx"] = True
    else:
        level1_df = _load_existing_data("ZX_YJHY.xlsx")
        results["ZX_YJHY.xlsx"] = not level1_df.empty

    # ── 2. CITIC 二级行业 (Tushare) ──
    time.sleep(2.0)  # Tushare 限速
    level2_df = _fetch_citic_index_data(_CITIC_L2_CODES, "二级行业")
    if not level2_df.empty:
        _write_excel_wide(level2_df, _TARGET_DATA_DIR / "ZX_EJHY.xlsx")
        results["ZX_EJHY.xlsx"] = True
    else:
        level2_df = _load_existing_data("ZX_EJHY.xlsx")
        results["ZX_EJHY.xlsx"] = not level2_df.empty

    # ── 3. 基准指数 ──
    bench_df = fetch_benchmark_data()
    if not bench_df.empty:
        _write_excel_wide(bench_df, _TARGET_DATA_DIR / "000985_prices.xlsx")
        results["000985_prices.xlsx"] = True
    else:
        bench_df = _load_existing_data("000985_prices.xlsx")
        results["000985_prices.xlsx"] = not bench_df.empty

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

        _log(f"  基准指数: {len(index_data)} 行, "
             f"{index_data.index[0].date()} ~ {index_data.index[-1].date()}")
        _log(f"  一级行业: {len(zx_yj_prices)} 行 × {len(zx_yj_prices.columns)} 列")
        _log(f"  二级行业: {len(zx_ej_prices)} 行 × {len(zx_ej_prices.columns)} 列")

        for sector_name, sector_prices, output_name, n_groups in [
            ("一级行业", zx_yj_prices, "group_assignment_details_zx_yjhy.csv", 5),
            ("二级行业", zx_ej_prices, "group_assignment_details_zx_ejhy.csv", 10),
        ]:
            if sector_prices.empty:
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
                        bench_ret = (index_data["close"].iloc[bench_end]
                                     / index_data["close"].iloc[bench_loc] - 1)
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


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="gx_pit_mom_Auto_Bot 数据自动更新管道 (Tushare CITIC 版)",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--skip-csv", action="store_true")
    args = parser.parse_args()

    _log("[START] gx_pit_mom_Auto_Bot 数据更新管道 (Tushare CITIC)")
    _log(f"  目标目录: {_TARGET_DATA_DIR}")
    _log(f"  Tushare pro_api: {'可用' if _PRO else '不可用 (请设置 TUSHARE_TOKEN)'}")

    if _PRO is None:
        _log("[FAIL] Tushare pro_api 不可用, 请设置 TUSHARE_TOKEN 环境变量")
        return 1

    if args.check:
        _log("[CHECK] 检查更新... (暂未实现, 返回 0)")
        return 0

    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    success = True

    results = update_all_data()
    success = all(results.values())

    if not args.skip_csv:
        csv_ok = regenerate_group_details()
        if not csv_ok:
            success = False

    write_update_timestamp()

    if success:
        _log("[OK] 数据更新完成! Streamlit 网站将在文件变化时自动刷新.")
    else:
        _log("[WARN] 数据更新部分完成 (部分步骤失败).")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
