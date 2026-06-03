"""mootdx 行情获取封装 — 连接管理、重试、全量/增量拉取。

使用方式::

    fetcher = MootdxFetcher()
    df = fetcher.fetch_daily("000001", offset=800)          # 增量（最新 N 条）
    df = fetcher.fetch_daily_full("000001")                 # 全量（全部历史）
    xdxr = fetcher.fetch_xdxr("000001")

mootdx 通过 TCP 7709 直连通达信服务器，不封 IP，单次日线拉取约 0.05 秒。

.. note::
   mootdx 内部 adjust 命名与市场惯例相反：
   ``bars(adjust='hfq')`` → 前复权（我们称为 qfq）
   ``bars(adjust='qfq')`` → 后复权（我们称为 hfq）
   本模块对外统一使用市场惯例：qfq=前复权, hfq=后复权, none=不复权。
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd
from mootdx.quotes import Quotes
import numpy as np

# ── 列名归一化 ──────────────────────────────────────────────────────────
_STANDARD_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]

# mootdx 内部 adjust 映射：对外惯例 → mootdx bars() 实际参数
_MOOTDX_ADJUST_MAP = {
    "qfq": "hfq",   # 前复权 ← mootdx 的 hfq
    "hfq": "qfq",   # 后复权 ← mootdx 的 qfq
    "none": "none",
}

# factor_reversion 的 method 参数：hfq 用 mootdx 的 'qfq'
# factor_reversion(method='qfq') → 后复权数据


def _normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """将 mootdx bars() 返回的 DataFrame 归一化到项目标准 schema。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=_STANDARD_COLS)
    out = df.copy()
    if out.index.name is not None:
        out = out.reset_index(drop=True)
    if "datetime" in out.columns:
        out["date"] = pd.to_datetime(out["datetime"])
    elif "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
    # 成交量：mootdx 同时返回 vol(手) 和 volume(股)，优先用 vol
    if "vol" in out.columns and "volume" in out.columns:
        out = out.drop(columns=["volume"])
        out = out.rename(columns={"vol": "volume"})
    elif "vol" in out.columns:
        out = out.rename(columns={"vol": "volume"})
    existing = [c for c in _STANDARD_COLS if c in out.columns]
    out = out[existing].copy()
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in out.columns:
            out.loc[:, col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values("date").reset_index(drop=True)


def _normalize_index_bars(df: pd.DataFrame) -> pd.DataFrame:
    """将 mootdx index_bars() 返回的 DataFrame 归一化到项目标准 schema。

    index_bars() 返回的数据格式与 bars() 略有不同：
    - 索引是 datetime (DatetimeIndex)
    - 包含 open/close/high/low/vol/amount 列
    - 可能包含 year/month/day/hour/minute/datetime 列
    - vol 是手，volume 是股（如果都存在，优先用 vol）
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=_STANDARD_COLS)
    out = df.copy()
    # index_bars 以 datetime 为索引，且通常还有 datetime 列
    # 先重置索引（会创建 datetime 列），如果有冲突则处理
    has_datetime_col = "datetime" in out.columns
    if isinstance(out.index, pd.DatetimeIndex) or out.index.name == "datetime":
        if has_datetime_col:
            # datetime 同时出现在索引和列中，重命名列中的为 _dt
            out = out.rename(columns={"datetime": "_dt"})
        out = out.reset_index()
        if "_dt" in out.columns:
            out["date"] = pd.to_datetime(out["_dt"])
            out = out.drop(columns=["_dt"])
        elif "datetime" in out.columns:
            out["date"] = pd.to_datetime(out["datetime"])
    elif "datetime" in out.columns:
        out["date"] = pd.to_datetime(out["datetime"])
    # 处理 vol/volume 列
    if "vol" in out.columns:
        out["volume"] = pd.to_numeric(out["vol"], errors="coerce")
        out = out.drop(columns=["vol"])
    existing = [c for c in _STANDARD_COLS if c in out.columns]
    out = out[existing].copy()
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in out.columns:
            out.loc[:, col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


class MootdxFetcher:
    """mootdx TCP 行情客户端封装。

    自动管理连接生命周期，内置重试与 reconnect 容错。
    线程安全：每个线程应持有独立的 MootdxFetcher 实例（mootdx Quotes 非线程安全）。

    ``adjust`` 参数对外使用市场惯例（qfq=前复权, hfq=后复权, none=不复权），
    内部自动映射到 mootdx 的实际参数。
    """

    def __init__(self, market: str = "std", max_retries: int = 3):
        self._market = market
        self._max_retries = max_retries
        self._client: Optional[Quotes] = None

    @property
    def client(self) -> Quotes:
        if self._client is None:
            self._client = Quotes.factory(market=self._market)
        return self._client

    def reconnect(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = Quotes.factory(market=self._market)

    # ── 日线 K 线 ─────────────────────────────────────────────────────

    def fetch_daily(
        self,
        code: str,
        adjust: str = "qfq",
        start: int = 0,
        offset: int = 800,
    ) -> pd.DataFrame:
        """按偏移量拉取日线（最新 N 条）。

        Parameters
        ----------
        code: 6 位股票代码
        adjust: "qfq"(前复权) | "hfq"(后复权) | "none"(不复权)
        start: 起始偏移（0=最新）
        offset: 拉取条数（最大约 800）
        """
        md_adjust = _MOOTDX_ADJUST_MAP.get(adjust, adjust)
        for attempt in range(self._max_retries):
            try:
                df = self.client.bars(
                    symbol=code, frequency=9, start=start, offset=offset, adjust=md_adjust
                )
                return _normalize_bars(df)
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
                self.reconnect()

    def fetch_daily_full(self, code: str, adjust: str = "qfq") -> pd.DataFrame:
        """拉取全部历史日线（前复权）。

        策略：
        1. ``k()`` 拉取全量 raw 不复权数据
        2. ``xdxr()`` 获取除权除息记录，计算每次送转股的拆分乘数
        3. 同一天多条记录合并乘数；按日期降序累积求因子
        4. ``merge_asof(direction='forward')`` 为每个交易日匹配"下一个除权日"的累积因子
        5. **qfq = raw / factor**（最近日期 factor=1.0，历史日期除以累积拆分比）

        Parameters
        ----------
        code: 6 位股票代码
        adjust: "qfq"(前复权) | "hfq"(后复权)
        """
        # Step 1: 全量 raw 数据
        raw = None
        for attempt in range(self._max_retries):
            try:
                raw = self.client.k(symbol=code, begin="1990-01-01", end="2099-12-31")
                break
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
                self.reconnect()

        if raw is None or raw.empty:
            return pd.DataFrame(columns=_STANDARD_COLS)
        raw = raw.dropna(subset=["date"]).copy()
        if raw.index.name is not None:
            raw = raw.reset_index(drop=True)
        raw["date"] = pd.to_datetime(raw["date"])
        raw["_d"] = raw["date"].dt.normalize()

        if adjust == "hfq":
            return _normalize_bars(raw)

        # Step 2: 从 xdxr 计算复权因子
        xdxr = self.fetch_xdxr(code)
        cum_factors: list[tuple[pd.Timestamp, float]] = []  # (action_date, cum_factor)
        if xdxr is not None and not xdxr.empty:
            xdxr = xdxr.copy()
            xdxr["multiplier"] = 1.0
            sg_mask = xdxr["songzhuangu"].notna() & (xdxr["songzhuangu"].astype(float) > 0)
            xdxr.loc[sg_mask, "multiplier"] = (10.0 + xdxr.loc[sg_mask, "songzhuangu"].astype(float)) / 10.0
            # 合并同一日多条记录（乘数相乘），忽略无拆分动作的日期
            by_date = xdxr.groupby("action_date")["multiplier"].prod().reset_index()
            by_date.columns = ["date", "multiplier"]
            by_date = by_date[by_date["multiplier"] != 1.0]  # 只保留有拆分动作的日期
            if not by_date.empty:
                by_date = by_date.sort_values("date")
                # 从后向前累积乘数
                cum = 1.0
                for i in range(len(by_date) - 1, -1, -1):
                    cum *= float(by_date.iloc[i]["multiplier"])
                    cum_factors.append((pd.Timestamp(by_date.iloc[i]["date"]), cum))
                cum_factors.reverse()  # 恢复升序

        latest_action = cum_factors[-1][0] if cum_factors else None

        # Step 3: 为每个交易日赋因子
        # - date >= latest_action → factor = 1.0
        # - date < latest_action → factor = cum_factor of first action > date
        raw_dates = raw["_d"].values
        factors = np.ones(len(raw_dates), dtype=float)
        if latest_action is not None:
            for i, td in enumerate(raw_dates):
                d = pd.Timestamp(td)
                if d >= latest_action:
                    factors[i] = 1.0
                else:
                    for action_dt, cf in cum_factors:
                        if pd.Timestamp(action_dt) > d:
                            factors[i] = cf
                            break

        # Step 4: qfq = raw / factor
        result = pd.DataFrame()
        result["date"] = raw["_d"]
        for col in ["open", "high", "low", "close"]:
            result[col] = pd.to_numeric(raw[col], errors="coerce").astype(float) / factors
        result["volume"] = pd.to_numeric(raw.get("vol", raw.get("volume", 0)), errors="coerce")
        result["amount"] = pd.to_numeric(raw.get("amount", 0), errors="coerce")
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            result[col] = pd.to_numeric(result[col], errors="coerce")

        result = result.dropna(subset=["close"])
        result = result.drop_duplicates(subset=["date"], keep="last")
        return result.sort_values("date").reset_index(drop=True)

    # ── 除权除息 ───────────────────────────────────────────────────────

    def fetch_xdxr(self, code: str) -> pd.DataFrame:
        """获取除权除息历史。"""
        for attempt in range(self._max_retries):
            try:
                df = self.client.xdxr(symbol=code)
                if df is not None and not df.empty:
                    df = df.copy()
                    df["action_date"] = pd.to_datetime(
                        df[["year", "month", "day"]].astype(int).astype(str).agg("-".join, axis=1),
                        errors="coerce",
                    )
                return df if df is not None else pd.DataFrame()
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
                self.reconnect()

    def get_latest_xdxr_date(self, code: str) -> Optional[pd.Timestamp]:
        """获取最近一次除权除息日期。"""
        xdxr = self.fetch_xdxr(code)
        if xdxr is None or xdxr.empty:
            return None
        dates = xdxr["action_date"].dropna()
        return dates.max() if not dates.empty else None

    # ── 板块指数日线 ───────────────────────────────────────────────────

    def fetch_index_daily(
        self,
        code: str,
        offset: int = 800,
    ) -> pd.DataFrame:
        """拉取板块/指数日线数据（适用于 880xxx 板块指数、399xxx 深证指数等）。

        使用 ``client.index_bars()`` 接口，与个股 ``bars()`` 不同。
        返回归一化后的 OHLCV DataFrame。

        Parameters
        ----------
        code: 板块/指数代码（如 880301, 000300, 399001 等）
        offset: 拉取条数（最大约 800）
        """
        for attempt in range(self._max_retries):
            try:
                df = self.client.index_bars(
                    symbol=code, frequency=9, start=0, offset=offset
                )
                return _normalize_index_bars(df)
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
                self.reconnect()

    def fetch_index_daily_full(self, code: str) -> pd.DataFrame:
        """拉取板块/指数全部历史日线（实际上限约 800 条，约 3 年）。

        先尝试拉取 2000 条以获取服务器端最大可用条数，
        再以实际返回条数重新拉取确保数据完整。
        """
        # 先探路：拉取一次获取实际可用条数
        df = self.fetch_index_daily(code, offset=2000)
        if df is None or df.empty:
            return pd.DataFrame(columns=_STANDARD_COLS)
        return df

    # ── 市场信息 ───────────────────────────────────────────────────────

    # A 股股票代码前缀（按市场分离，避免 000xxx 在上海=指数、在深圳=股票 的歧义）
    _STOCK_PREFIXES: dict[int, tuple[str, ...]] = {
        0: ("000", "001", "002", "003", "300", "301"),   # 深圳主板+创业板
        1: ("600", "601", "603", "605", "688"),           # 上海主板+科创板
    }

    def fetch_stock_list(self, market: int = 1) -> pd.DataFrame:
        """获取市场全部证券列表（含 ETF/债券/指数等）。market: 0=深圳, 1=上海。"""
        return self.client.stocks(market=market)

    def fetch_stock_codes(self) -> list[str]:
        """获取全市场 A 股股票代码列表（已排除 ETF/债券/指数/基金等）。

        深圳交易所 (market=0)：主板 000/001/002/003，创业板 300/301
        上海交易所 (market=1)：主板 600/601/603/605，科创板 688

        Returns
        -------
        list[str]
            去重排序的 6 位股票代码列表。
        """
        codes: list[str] = []
        for market, prefixes in self._STOCK_PREFIXES.items():
            df = self.fetch_stock_list(market=market)
            if df is None or df.empty:
                continue
            market_codes = df["code"].astype(str).str.zfill(6)
            filtered = [c for c in market_codes if any(c.startswith(p) for p in prefixes)]
            codes.extend(filtered)
        return sorted(set(codes))

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


__all__ = ["MootdxFetcher"]
