import pandas as pd
import numpy as np
import os
from datetime import datetime
from src.data_provider import WindLocalProvider

class GXPitMomActions:
    """
    Adapted version of GXPitMom for GitHub Actions (CSV-based)
    """
    def __init__(
        self,
        data_dir='./data/',
        start_date='2017-01-01',
        end_date=None,
        half_life=10,
    ):
        self.data_dir = data_dir
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        self.start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        self.end_date = pd.to_datetime(end_date or datetime.now()).strftime('%Y-%m-%d')
        self.half_life = half_life
        
        # 使用单独的数据提供者
        self.dp = WindLocalProvider(data_dir=self.data_dir, start_date=self.start_date, end_date=self.end_date)

    def _gx_atr(self, data, n=60):
        # Data must have 'high', 'low', 'close'
        p_close = data['close'].shift(1).replace(0, np.nan)
        tr = pd.concat(
            [
                (data['high'] - data['low']) / p_close,
                (data['high'] - p_close).abs() / p_close,
                (p_close - data['low']).abs() / p_close,
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(n, min_periods=1).mean()

    def gx_pit_rebound(self, data, u=0.005, d=0.05):
        if data.empty: return pd.Series()
        close, returns = data['close'], data['close'].pct_change()
        atr = self._gx_atr(data, n=60).fillna(0)
        scale = pd.Series(1.0, index=data.index)
        scale.loc[atr < 0.01] = np.sqrt(atr / 0.01)
        scale.loc[atr > 0.02] = np.sqrt(atr / 0.02)
        u_eff = u * scale
        d_eff = d * scale

        rebound_trigger = returns > u_eff
        signal = pd.Series(0, index=data.index)
        trigger_indices = np.where(rebound_trigger)[0]

        for t_idx in trigger_indices:
            if t_idx < 4: continue
            pre_returns = returns.iloc[:t_idx]
            last_rebound = np.where(pre_returns > u_eff.iloc[t_idx])[0]
            m_start_idx = last_rebound[-1] if len(last_rebound) > 0 else 0
            m_close = close.iloc[m_start_idx:t_idx]
            if len(m_close) <= 3: continue

            c_high = m_close.max()
            m_after_high = m_close.loc[m_close.idxmax():]
            if len(m_after_high) > 2 and (1 - m_after_high.min() / c_high) > d_eff.iloc[t_idx]:
                signal.iloc[t_idx] = 1
        return signal

    def gx_pit_rotation(self, benchmark_data, sector_prices, n_decrease=3):
        if benchmark_data.empty or sector_prices.empty: return pd.Series()
        common = benchmark_data.index.intersection(sector_prices.index)
        bench_data = benchmark_data.loc[common]
        bench_returns = bench_data['close'].pct_change()

        high_1y = sector_prices.loc[common].rolling(252, min_periods=1).max()
        new_high_diff = ((sector_prices.loc[common] >= high_1y).astype(int).sum(axis=1)).diff()
        atr = self._gx_atr(bench_data, n=60)
        return ((new_high_diff <= -n_decrease) & (bench_returns < -atr)).astype(int)

    def gx_pit_breakout(self, data, threshold_pre=0.01, threshold_break=0.01, window=5):
        if data.empty: return pd.Series()
        high, low = data['high'], data['low']
        returns = data['close'].pct_change()
        vola_compression = (returns.abs() < threshold_pre).rolling(window).sum() == window
        channel_width = high.rolling(window).max() - low.rolling(window).min()
        squeeze = (channel_width.shift(1) < channel_width.shift(2)).fillna(False)
        return (vola_compression.shift(1) & squeeze & (returns > threshold_break)).astype(int)

    def calculate_fused_signals(self, sector_prices, signals_dict):
        if sector_prices.empty: return []
        rets = sector_prices.pct_change().dropna()
        all_dates = sector_prices.index
        
        combined_trigger = pd.Series(False, index=all_dates)
        for sig_series in signals_dict.values():
            combined_trigger |= (sig_series.reindex(all_dates) == 1)

        potential_trigger_dates = all_dates[combined_trigger]
        potential_trigger_dates = potential_trigger_dates[potential_trigger_dates >= pd.to_datetime(self.start_date)]

        raw_value_cache = {}
        rank_cache = {}
        for sig_name, timing_series in signals_dict.items():
            trigger_days = timing_series[timing_series == 1].index
            for d in trigger_days:
                if d not in rets.index: continue
                if sig_name == 'rebound':
                    avg_prev = rets.shift(1).rolling(20).mean()
                    row_val = (rets - avg_prev).loc[d].dropna()
                else:
                    row_val = rets.loc[d].dropna()
                if not row_val.empty:
                    raw_value_cache[(d, sig_name)] = row_val
                    rank_cache[(d, sig_name)] = row_val.rank(pct=True)

        sector_signals = []
        for d in potential_trigger_dates:
            if d not in rets.index: continue
            d_idx = all_dates.get_loc(d)
            start_idx = max(0, d_idx - self.half_life + 1)
            window_dates = all_dates[start_idx:d_idx + 1]

            found_signals = []
            for sig_name in signals_dict.keys():
                if any((t, sig_name) in rank_cache for t in window_dates):
                    found_signals.append(sig_name)
            if not found_signals: continue

            if len(found_signals) == 1:
                sig_name = found_signals[0]
                latest_t = next((t for t in reversed(window_dates) if (t, sig_name) in raw_value_cache), None)
                final_series = raw_value_cache.get((latest_t, sig_name), pd.Series())
                sig_type_str = sig_name
            else:
                combined_factor = pd.Series(0.0, index=sector_prices.columns)
                total_weight = 0.0
                for t in window_dates:
                    n = d_idx - all_dates.get_loc(t)
                    weight = 2 ** (-n / self.half_life)
                    for sig_name in found_signals:
                        if (t, sig_name) in rank_cache:
                            combined_factor = combined_factor.add(rank_cache[(t, sig_name)] * weight, fill_value=0)
                            total_weight += weight
                final_series = (combined_factor / total_weight) if total_weight > 0 else pd.Series()
                sig_type_str = '+'.join(sorted(found_signals))

            if not final_series.empty:
                final_series = final_series.dropna().sort_values(ascending=False)
                sector_signals.append({'date': d, 'series': final_series, 'type': sig_type_str})
        return sector_signals

    def generate_report_markdown(self, all_results):
        # 汉化映射
        signal_name_cn = {
            'breakout': '三角形突破', 
            'rebound': '大跌反弹', 
            'rotation': '顶部切换',
            'breakout+rebound': '突破+反弹',
            'breakout+rotation': '突破+切换',
            'rebound+rotation': '反弹+切换',
            'breakout+rebound+rotation': '全信号触发'
        }
        
        # 寻找最新日期和各版块的最后一次信号
        latest_date = None
        for sector_res in all_results.values():
            if sector_res:
                d = sector_res[-1]['date']
                if latest_date is None or d > latest_date:
                    latest_date = d
        
        if not latest_date:
            return "### 量化报告\n暂无任何历史信号数据。"

        today = pd.Timestamp.now().normalize()
        # 这里的 latest_date 是数据中的最后日期
        is_today = (latest_date.date() >= today.date())
        
        report = [f"### 量化行业动量监控报告\n数据日期：{latest_date.strftime('%Y-%m-%d')}\n"]
        
        for sector_name, results in all_results.items():
            # 找到该版块最近的一个有信号的记录
            last_valid_signal = results[-1] if results else None
            
            if not last_valid_signal:
                continue

            sig_type = last_valid_signal['type']
            sig_cn = signal_name_cn.get(sig_type, sig_type)
            sig_date_str = last_valid_signal['date'].strftime('%Y-%m-%d')
            
            # 信号状态说明
            if last_valid_signal['date'] == latest_date:
                report.append(f"#### {sector_name} (今日信号: **{sig_cn}**)")
            else:
                report.append(f"#### {sector_name} (今日无信号)")
                report.append(f"> 上次触发日期: {sig_date_str}")
                report.append(f"> 上次信号类型: {sig_cn}")

            # 无论今日是否有信号，都返回评分结果
            series = last_valid_signal['series'].sort_values(ascending=False)
            
            if sector_name == '中信一级行业':
                report.append(f"**Top 5 行业评分:**")
                top5 = [f"{i+1}. {name}: {val:.4f}" for i, (name, val) in enumerate(series.head(5).items())]
                report.append("\n".join(top5) + "\n")
            else: # 中信二级行业
                report.append(f"**Top 10 行业评分:**")
                top10 = [f"{i+1}. {name}: {val:.4f}" for i, (name, val) in enumerate(series.head(10).items())]
                report.append("\n".join(top10) + "\n")
            
        report.append("---\n*报告由 GitHub Actions 自动生成*")
        return "\n".join(report)

    def run_analysis(self):
        # 使用 WindLocalProvider (dp) 读取数据
        index_data = self.dp.get_wide_table('000985_prices.xlsx')
        zx_yj_prices = self.dp.get_wide_table('ZX_YJHY.xlsx')
        zx_ej_prices = self.dp.get_wide_table('ZX_EJHY.xlsx')
        
        if index_data.empty:
            print("Error: 000985_prices.xlsx is empty or missing (checked by WindLocalProvider).")
            return None

        # Calculate base signals
        sig_breakout = self.gx_pit_breakout(index_data)
        sig_rebound = self.gx_pit_rebound(index_data)
        sig_rotation = self.gx_pit_rotation(index_data, zx_yj_prices)
        
        signals_dict = {
            'breakout': sig_breakout,
            'rebound': sig_rebound,
            'rotation': sig_rotation,
        }
        
        # Fuse signals for each sector
        results = {
            '中信一级行业': self.calculate_fused_signals(zx_yj_prices, signals_dict),
            '中信二级行业': self.calculate_fused_signals(zx_ej_prices, signals_dict)
        }
        
        return self.generate_report_markdown(results)
