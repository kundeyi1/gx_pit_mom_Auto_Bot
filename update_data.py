#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
update_data.py — Auto-update pipeline for gx_pit_mom_Auto_Bot.

使用项目内 src.mootdx_fetcher.MootdxFetcher (mootdx 通达信 TCP 接口)
直接从通达信服务器拉取板块指数数据，不再依赖 Wind D:/DATA 本地导出。

工作流:
  ┌─────────────────────────────────────────────────────────────┐
  │  mootdx (TDX 通达信 TCP 7709)                               │
  │  ├── 000300  沪深300 (基准指数代理)                          │
  │  ├── 8803xx  行业板块指数 (~46 个一级板块)                   │
  │  └── 8804xx + 8805xx  概念板块指数 (~80+ 个二级板块)        │
  └──────────────────┬──────────────────────────────────────────┘
                     │  update_data.py
                     ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  gx_pit_mom_Auto_Bot/data/                                  │
  │  ├── 000985_prices.xlsx          (基准指数 OHLCV)           │
  │  ├── ZX_YJHY.xlsx                (一级行业板块)              │
  │  ├── ZX_EJHY.xlsx                (二级行业板块)              │
  │  ├── group_assignment_details_*.csv  (历史分组业绩)         │
  │  └── .last_update                (最后更新时间戳)           │
  └──────────────────────────────────────────────────────────────┘

用法:
  python update_data.py              # 完整更新 (拉取 + 生成)
  python update_data.py --daily      # 每日增量更新
  python update_data.py --check      # 仅检查是否有新数据
  python update_data.py --skip-csv   # 跳过 CSV 生成
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    from src.mootdx_fetcher import MootdxFetcher
except ImportError:
    print("[WARN] 无法导入 src.mootdx_fetcher.MootdxFetcher, mootdx 数据拉取将不可用")
    MootdxFetcher = None

# ── 路径配置 ──────────────────────────────────────────────────────────
_TARGET_DATA_DIR = Path(__file__).resolve().parent / "data"

# ── 基准指数 ──────────────────────────────────────────────────────────
_TDX_BENCHMARK_CODE = "000300"  # 沪深300 (代理 000985 中证全指)
_TDX_BENCHMARK_NAME = "沪深300"

# ── TDX 板块指数代码 (8803xx 基础行业 + 8804xx/8805xx 扩展概念) ────────
# 名称映射: 代码 → 中文名称
_TDX_INDUSTRY_NAME_MAP: dict[str, str] = {
    # ── 8803xx: 基础行业板块 ──
    "880301": "煤炭",
    "880302": "石油",
    "880303": "有色",
    "880304": "化纤",
    "880305": "电力",
    "880306": "供气供热",
    "880307": "水务",
    "880308": "环保",
    "880309": "钢铁",
    "880310": "石油加工",
    "880311": "矿物制品",
    "880312": "日用化工",
    "880313": "化工原料",
    "880314": "农药化肥",
    "880315": "塑料",
    "880316": "橡胶",
    "880317": "染料涂料",
    "880318": "造纸",
    "880319": "玻璃",
    "880320": "陶瓷",
    "880321": "水泥",
    "880322": "其他建材",
    "880323": "建筑工程",
    "880324": "装修装饰",
    "880325": "工程机械",
    "880326": "轻工机械",
    "880327": "纺织机械",
    "880328": "农用机械",
    "880329": "机床制造",
    "880330": "专用机械",
    "880331": "通用机械",
    "880332": "电器仪表",
    "880333": "运输设备",
    "880334": "摩托车",
    "880335": "汽车整车",
    "880336": "汽车配件",
    "880337": "家居用品",
    "880338": "文教休闲",
    "880339": "广告包装",
    "880340": "旅游景点",
    "880341": "酒店餐饮",
    "880342": "超市连锁",
    "880343": "百货",
    "880344": "商贸代理",
    "880345": "医药商业",
    "880346": "中成药",
    "880347": "生物制药",
    "880348": "化学制药",
    "880349": "医疗保健",
    "880350": "农药兽药",
    "880351": "食品",
    "880352": "饮料",
    "880353": "乳制品",
    "880354": "酿酒",
    "880355": "软饮料",
    "880356": "纺织",
    "880357": "服饰",
    "880358": "家用电器",
    "880359": "饲料",
    "880360": "农林牧渔",
    "880361": "种植业",
    "880362": "渔业",
    "880363": "畜牧业",
    "880364": "船舶",
    "880365": "航空",
    "880366": "运输服务",
    "880367": "港口",
    "880368": "路桥",
    "880369": "空运",
    "880370": "水运",
    "880371": "铁路",
    "880372": "公共交通",
    # ── 8804xx: 扩展主题板块 ──
    "880400": "医药",
    "880401": "医疗",
    "880402": "医疗服务",
    "880403": "医药制造",
    "880404": "中药",
    "880405": "生物医药",
    "880406": "商业连锁",
    "880407": "新零售",
    "880408": "跨境电商",
    "880409": "电子支付",
    "880410": "电子商务",
    "880411": "网红经济",
    "880412": "地摊经济",
    "880413": "C2M概念",
    "880414": "免税概念",
    "880415": "体育概念",
    "880416": "网络游戏",
    "880417": "知识产权",
    "880418": "传媒娱乐",
    "880419": "网络视听",
    "880420": "知识付费",
    "880421": "智能医疗",
    "880422": "智能交通",
    "880423": "智慧城市",
    "880424": "智能家居",
    "880425": "智能电网",
    "880426": "充电桩",
    "880427": "锂电池",
    "880428": "燃料电池",
    "880429": "新能源车",
    "880430": "特斯拉",
    "880431": "光伏",
    "880432": "风能",
    "880433": "核能",
    "880434": "可燃冰",
    "880435": "氢能源",
    "880436": "储能",
    "880437": "通用机械",
    "880438": "工业机械",
    "880439": "高端装备",
    "880440": "智能机器",
    "880441": "工业母机",
    "880442": "机器人概念",
    "880443": "数控机床",
    "880444": "国防军工",
    "880445": "军民融合",
    "880446": "卫星导航",
    "880447": "无人机",
    "880448": "安防服务",
    "880449": "信息安全",
    "880450": "国产软件",
    "880451": "云计算",
    "880452": "电信运营",
    "880453": "5G概念",
    "880454": "通信设备",
    "880455": "光通信",
    "880456": "量子科技",
    "880457": "物联网",
    "880458": "边缘计算",
    "880459": "大数据",
    "880460": "数据中心",
    "880461": "人工智能",
    "880462": "区块链",
    "880463": "数字货币",
    "880464": "元宇宙",
    "880465": "Web3概念",
    "880466": "东数西算",
    "880467": "算力概念",
    "880468": "半导体",
    "880469": "芯片",
    "880470": "光刻机",
    "880471": "银行",
    "880472": "证券",
    "880473": "保险",
    "880474": "多元金融",
    "880475": "互联金融",
    "880476": "建筑",
    "880477": "建材",
    "880478": "装配式建筑",
    "880479": "绿色建筑",
    "880480": "地下管网",
    "880481": "水利建设",
    "880482": "房地产",
    "880483": "物业管理",
    "880484": "租购同权",
    "880485": "保障房",
    "880486": "土地流转",
    "880487": "乡村振兴",
    "880488": "种业",
    "880489": "粮食概念",
    "880490": "通信设备",
    "880491": "半导体",
    "880492": "元器件",
    "880493": "消费电子",
    "880494": "互联网",
    "880495": "软件服务",
    "880496": "IT设备",
    "880497": "信创",
    "880498": "操作系统",
    "880499": "国资云",
    "880500": "数据要素",
    "880501": "数据确权",
    # ── 8805xx: 概念/主题板块 ──
    "880506": "5G概念",
    "880507": "国防军工",
    "880508": "军民融合",
    "880509": "央企改革",
    "880510": "一带一路",
    "880511": "雄安新区",
    "880512": "粤港澳",
    "880513": "长三角",
    "880514": "海南自贸",
    "880515": "上海自贸",
    "880516": "稀土永磁",
    "880517": "石墨烯",
    "880518": "碳纤维",
    "880519": "钛金属",
    "880520": "黄金概念",
    "880521": "稀缺资源",
    "880522": "维生素",
    "880523": "仿制药",
    "880524": "创新药",
    "880525": "新冠药",
    "880526": "检测试剂",
    "880527": "医美概念",
    "880528": "养老概念",
    "880529": "婴童概念",
    "880530": "职业教育",
    "880531": "在线教育",
    "880532": "信创",
    "880533": "东数西算",
    "880534": "算力概念",
    "880535": "ChatGPT概念",
    "880536": "AIGC概念",
    "880537": "CPO概念",
    "880538": "液冷服务器",
    "880539": "存储芯片",
    "880540": "先进封装",
    "880541": "汽车芯片",
    "880542": "MCU芯片",
    "880543": "第三代半导体",
    "880544": "碳化硅",
    "880545": "氮化镓",
    "880546": "卫星导航",
    "880547": "商业航天",
    "880548": "低空经济",
    "880549": "飞行汽车",
    "880550": "无人驾驶",
    "880551": "车路云",
    "880552": "固态电池",
    "880553": "钠电池",
    "880554": "钙钛矿电池",
    "880555": "TOPCon电池",
    "880556": "HJT电池",
    "880557": "复合铜箔",
    "880558": "一体化压铸",
    "880559": "热管理",
    "880560": "复合集流体",
    "880561": "超导概念",
    "880562": "可控核聚变",
    "880563": "人形机器人",
    "880564": "具身智能",
    "880565": "AI手机",
    "880566": "AI PC",
    "880567": "空间计算",
    "880568": "混合现实",
    "880569": "智能穿戴",
    "880570": "智能音箱",
    "880571": "虚拟现实",
    "880572": "增强现实",
    "880573": "脑机接口",
    "880574": "多模态AI",
    "880575": "AI智能体",
    "880576": "智谱AI",
    "880577": "DeepSeek概念",
    "880578": "数据安全",
    "880579": "跨境支付",
    "880580": "电子身份证",
    "880581": "数字水印",
    "880582": "毫米波雷达",
    "880583": "6G概念",
    "880584": "星闪概念",
    "880585": "时空大数据",
    "880586": "数字孪生",
    "880587": "新型工业化",
    "880588": "工业软件",
    "880589": "财税数字化",
    "880590": "数据要素",
    "880591": "首发经济",
    "880592": "银发经济",
    "880593": "冰雪经济",
    "880594": "谷子经济",
    "880595": "IP经济",
}

# ── 一级行业板块 (8803xx 基础行业) ──
# 自动扫描时使用，此处仅作为期望列表
_LEVEL1_CANDIDATE_CODES = [f"8803{i:02d}" for i in range(1, 73)]

# ── 二级行业板块 (8804xx + 8805xx 扩展概念) ──
_LEVEL2_CANDIDATE_CODES = [f"8804{i:02d}" for i in range(0, 100)] + \
                          [f"8805{i:02d}" for i in range(6, 100)]


# ── 辅助函数 ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    """带时间戳的日志输出 (编码安全)."""
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
    col0_label: str = "通达信",
) -> None:
    """将宽表 DataFrame 写入 Excel, 格式兼容 WindLocalProvider.get_wide_table().

    输出格式 (与 CITIC Wind 导出格式兼容):
      Row 0: col0_label + 各列名 (如 '通达信', '煤炭', '石油', ...)
      Row 1: '指数名称' + 各列 '日' (频率)
      Row 2: '频率' + 各列 '点' (单位)
      Row 3: '指数ID' + 各列 'TDX' (来源标识)
      Row 4: '来源' + 各列 '通达信/mootdx'
      Row 5 (header): index_label + 各列名 (如 'Date', '煤炭', '石油', ...)
      Row 6+: 数据行 (日期 + OHLCV)
    """
    if df.empty:
        _log(f"  [WARN] 空数据, 跳过写入 {path.name}")
        return

    n_cols = len(df.columns)

    # 构建表头 (6 行元数据)
    rows = []
    rows.append([col0_label] + list(df.columns))               # Row 0
    rows.append(["指数名称"] + ["日"] * n_cols)                  # Row 1
    rows.append(["频率"] + ["点"] * n_cols)                      # Row 2
    rows.append(["指数ID"] + [f"TDX{i:04d}" for i in range(1, n_cols + 1)])  # Row 3
    rows.append(["来源"] + ["通达信/mootdx"] * n_cols)           # Row 4
    rows.append([index_label] + list(df.columns))               # Row 5

    # 数据行
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


def _scan_available_codes(fetcher, candidate_codes: list[str]) -> list[str]:
    """扫描候选代码列表，返回实际可用的代码 (按原始顺序).

    每个代码尝试拉取 1 条数据验证可用性，不可用的跳过。
    """
    available = []
    for code in candidate_codes:
        try:
            df = fetcher.fetch_index_daily(code, offset=1)
            if df is not None and not df.empty and "close" in df.columns:
                available.append(code)
        except Exception:
            pass
    return available


# ── 核心数据拉取 ──────────────────────────────────────────────────────────

def _fetch_tdx_close_table(
    fetcher,
    codes: list[str],
    name_map: dict[str, str],
) -> pd.DataFrame:
    """从 TDX 拉取板块指数的 close 价格宽表.

    Parameters
    ----------
    fetcher: MootdxFetcher 实例
    codes: 板块代码列表
    name_map: 代码 → 中文名称映射

    Returns
    -------
    pd.DataFrame
        以 date 为 index, 板块名称为 columns 的 close 价格宽表.
    """
    all_series = {}
    success = 0
    fail = 0

    for i, code in enumerate(codes):
        name = name_map.get(code, f"TDX_{code}")
        try:
            df = fetcher.fetch_index_daily_full(code)
            if df is not None and not df.empty and "close" in df.columns:
                df["_d"] = pd.to_datetime(df["date"]).dt.normalize()
                close_series = df.set_index("_d")["close"]
                close_series.name = name
                # 去重: 对同一日期保留最后一个
                close_series = close_series[~close_series.index.duplicated(keep="last")]
                all_series[name] = close_series
                success += 1
            else:
                fail += 1
        except Exception as e:
            _log(f"    [FAIL] {code} ({name}): {e}")
            fail += 1

        # 进度
        if (i + 1) % 20 == 0:
            _log(f"    进度: {i+1}/{len(codes)} (成功 {success}, 失败 {fail})")

    _log(f"  TDX 拉取完成: 成功 {success}/{len(codes)}, 失败 {fail}")

    if not all_series:
        return pd.DataFrame()

    result = pd.DataFrame(all_series).sort_index()
    return result


def fetch_benchmark_data(fetcher) -> pd.DataFrame:
    """拉取基准指数 (000300 沪深300) 数据.

    Returns
    -------
    pd.DataFrame
        OHLCV 数据, 以 date 为 index.
    """
    _log("=" * 60)
    _log(f"[TDX] 拉取基准指数: {_TDX_BENCHMARK_NAME} ({_TDX_BENCHMARK_CODE})")

    try:
        df = fetcher.fetch_index_daily_full(_TDX_BENCHMARK_CODE)
        if df is None or df.empty:
            _log("  [FAIL] 基准指数返回空数据")
            return pd.DataFrame()

        df["_d"] = pd.to_datetime(df["date"]).dt.normalize()
        result = df.set_index("_d")[["open", "high", "low", "close", "volume", "amount"]]
        result = result[~result.index.duplicated(keep="last")].sort_index()
        _log(f"  ✓ {len(result)} 行, {result.index[0].date()} ~ {result.index[-1].date()}")
        return result
    except Exception as e:
        _log(f"  [FAIL] 基准指数拉取失败: {e}")
        return pd.DataFrame()


def fetch_sector_data(fetcher, level: int = 1) -> pd.DataFrame:
    """拉取行业板块指数 close 价格宽表.

    Parameters
    ----------
    fetcher: MootdxFetcher 实例
    level: 1 = 一级板块 (8803xx), 2 = 二级板块 (8804xx + 8805xx)

    Returns
    -------
    pd.DataFrame
        close 价格宽表, index=date, columns=板块名称.
    """
    if level == 1:
        label = "一级行业板块 (8803xx)"
        candidate_codes = _LEVEL1_CANDIDATE_CODES
    else:
        label = "二级行业板块 (8804xx + 8805xx)"
        candidate_codes = _LEVEL2_CANDIDATE_CODES

    _log(f"[TDX] 拉取{label} ({len(candidate_codes)} 个候选)")

    # 先扫描可用代码
    _log(f"  扫描可用代码...")
    available = _scan_available_codes(fetcher, candidate_codes)
    _log(f"  可用: {len(available)}/{len(candidate_codes)} 个代码")

    if not available:
        _log("  [WARN] 无可用代码")
        return pd.DataFrame()

    # 拉取全量数据
    return _fetch_tdx_close_table(fetcher, available, _TDX_INDUSTRY_NAME_MAP)


# ── 主流程 ──────────────────────────────────────────────────────────────

def update_all_data(fetcher) -> dict[str, bool]:
    """使用 mootdx 拉取全部数据并写入目标文件.

    Returns
    -------
    dict[str, bool]
        各文件的写入状态.
    """
    results = {}
    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. 基准指数 ──
    bench_df = fetch_benchmark_data(fetcher)
    if not bench_df.empty:
        bench_path = _TARGET_DATA_DIR / "000985_prices.xlsx"
        _write_excel_wide(bench_df, bench_path, index_label="Date", col0_label="通达信")
        results["000985_prices.xlsx"] = True
    else:
        _log("  [WARN] 基准指数数据为空, 跳过")
        results["000985_prices.xlsx"] = False

    # ── 2. 一级行业板块 ──
    level1_df = fetch_sector_data(fetcher, level=1)
    if not level1_df.empty:
        level1_path = _TARGET_DATA_DIR / "ZX_YJHY.xlsx"
        _write_excel_wide(level1_df, level1_path, index_label="Date", col0_label="通达信")
        results["ZX_YJHY.xlsx"] = True
    else:
        _log("  [WARN] 一级行业板块数据为空, 跳过")
        results["ZX_YJHY.xlsx"] = False

    # ── 3. 二级行业板块 ──
    level2_df = fetch_sector_data(fetcher, level=2)
    if not level2_df.empty:
        level2_path = _TARGET_DATA_DIR / "ZX_EJHY.xlsx"
        _write_excel_wide(level2_df, level2_path, index_label="Date", col0_label="通达信")
        results["ZX_EJHY.xlsx"] = True
    else:
        _log("  [WARN] 二级行业板块数据为空, 跳过")
        results["ZX_EJHY.xlsx"] = False

    return results


def regenerate_group_details() -> bool:
    """运行 GXPitMomActions 分析, 重新生成 group_assignment_details CSV.

    这些 CSV 是 Streamlit 仪表盘中"历史组合表现"卡片的数据源,
    包含 factor_value, group_id, period_return, excess_return 等列.

    Returns
    -------
    bool
        是否成功生成.
    """
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
            _log("  [FAIL] 基准指数数据为空, 无法生成分组详情")
            return False

        _log(f"  基准指数: {len(index_data)} 行, {index_data.index[0].date()} ~ {index_data.index[-1].date()}")
        _log(f"  一级板块: {len(zx_yj_prices)} 行 × {len(zx_yj_prices.columns)} 列")
        _log(f"  二级板块: {len(zx_ej_prices)} 行 × {len(zx_ej_prices.columns)} 列")

        # 基于 20 日动量因子 (20-day return) 对行业分组
        for sector_name, sector_prices, output_name, n_groups in [
            ("一级板块", zx_yj_prices, "group_assignment_details_zx_yjhy.csv", 5),
            ("二级板块", zx_ej_prices, "group_assignment_details_zx_ejhy.csv", 10),
        ]:
            if sector_prices.empty:
                _log(f"  [WARN] {sector_name} 数据为空, 跳过")
                continue

            _log(f"  计算 {sector_name} 分组 ({len(sector_prices.columns)} 个板块)...")

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
    """写入最后更新时间戳文件."""
    ts_file = _TARGET_DATA_DIR / ".last_update"
    ts_file.write_text(datetime.now().isoformat(), encoding="utf-8")
    _log(f"  ✓ 更新时间戳: {ts_file}")


def check_for_updates() -> bool:
    """检查是否有新交易日数据.

    Returns
    -------
    bool
        True 如果有新数据.
    """
    if MootdxFetcher is None:
        _log("  [WARN] MootdxFetcher 不可用")
        return False

    try:
        fetcher = MootdxFetcher()
        df = fetcher.fetch_index_daily(_TDX_BENCHMARK_CODE, offset=5)
        if df is None or df.empty:
            fetcher.close()
            return False

        latest_tdx_date = pd.to_datetime(df["date"].iloc[-1]).date()

        target_path = _TARGET_DATA_DIR / "000985_prices.xlsx"
        if not target_path.exists():
            _log(f"  目标文件不存在: 000985_prices.xlsx")
            fetcher.close()
            return True

        # 读取已有数据的最后日期
        raw = pd.read_excel(str(target_path), header=None)
        if len(raw) > 6:
            dates = pd.to_datetime(raw.iloc[6:, 0], errors="coerce").dropna()
            if len(dates) > 0:
                last_date = dates.iloc[-1].date()
                if latest_tdx_date > last_date:
                    _log(f"  有新交易日: {latest_tdx_date} > {last_date}")
                    fetcher.close()
                    return True

        _log("  无新数据")
        fetcher.close()
        return False
    except Exception as e:
        _log(f"  [WARN] 检查更新失败: {e}")
        return False


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="gx_pit_mom_Auto_Bot 数据自动更新管道 (mootdx 通达信版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python update_data.py             完整运行: 拉取 TDX 数据 + 生成 CSV
  python update_data.py --daily     每日增量更新 (适合 GitHub Actions)
  python update_data.py --check     仅检查是否有新数据可用
  python update_data.py --skip-csv  跳过 CSV 生成 (仅更新数据文件)
        """,
    )
    parser.add_argument("--daily", action="store_true", help="每日增量更新")
    parser.add_argument("--check", action="store_true", help="仅检查更新, 不修改文件")
    parser.add_argument("--skip-csv", action="store_true", help="跳过 group_assignment_details CSV 生成")
    args = parser.parse_args()

    _log("[START] gx_pit_mom_Auto_Bot 数据更新管道 (mootdx)")
    _log(f"  目标目录: {_TARGET_DATA_DIR}")
    _log(f"  MootdxFetcher: {'可用' if MootdxFetcher else '不可用'}")

    if MootdxFetcher is None:
        _log("[FAIL] MootdxFetcher 不可用, 无法继续")
        return 1

    # --check 模式
    if args.check:
        _log("[CHECK] 检查更新...")
        has = check_for_updates()
        sys.exit(0 if has else 1)

    _TARGET_DATA_DIR.mkdir(parents=True, exist_ok=True)

    fetcher = MootdxFetcher()
    success = True

    try:
        # ── 拉取全部数据 ──
        results = update_all_data(fetcher)
        success = all(results.values())

        # ── 生成分组详情 CSV ──
        if not args.skip_csv:
            csv_ok = regenerate_group_details()
            if not csv_ok:
                _log("  [WARN] CSV 生成部分失败 (将继续)")
                success = False

        write_update_timestamp()

    finally:
        fetcher.close()

    if success:
        _log("[OK] 数据更新完成! Streamlit 网站将在文件变化时自动刷新.")
    else:
        _log("[WARN] 数据更新部分完成 (部分步骤失败).")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
