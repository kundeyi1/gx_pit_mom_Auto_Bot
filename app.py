import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime
from src.analysis import GXPitMomActions

# --- Page Config ---
st.set_page_config(
    page_title="Quant_Auto_Bot Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- Custom CSS for Dark Theme and Layout ---
st.markdown("""
<style>
    .stApp { background-color: #0d1117; color: #c9d1d9; }
    .metric-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 20px;
        margin-bottom: 20px;
    }
    .column-label { color: #8b949e; font-size: 0.9em; }
    .value-label { font-size: 1.5em; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# --- Data Loading & Analysis ---
@st.cache_data(ttl=3600)  # 缩短缓存失效时间为 60 分钟
def get_analysis_results():
    analyzer = GXPitMomActions(data_dir='./data/')
    index_data = analyzer.dp.get_wide_table('000985_prices.xlsx')
    zx_yj_prices = analyzer.dp.get_wide_table('ZX_YJHY.xlsx')
    zx_ej_prices = analyzer.dp.get_wide_table('ZX_EJHY.xlsx')
    
    sig_breakout = analyzer.gx_pit_breakout(index_data)
    sig_rebound = analyzer.gx_pit_rebound(index_data)
    sig_rotation = analyzer.gx_pit_rotation(index_data, zx_yj_prices)
    
    signals_dict = {'breakout': sig_breakout, 'rebound': sig_rebound, 'rotation': sig_rotation}
    results = {
        '中信一级行业': analyzer.calculate_fused_signals(zx_yj_prices, signals_dict),
        '中信二级行业': analyzer.calculate_fused_signals(zx_ej_prices, signals_dict)
    }

    # 读取历史分组业绩明细 CSV
    def _load_group_detail(path):
        try:
            df = pd.read_csv(path, parse_dates=['date'])
            return df
        except Exception:
            return pd.DataFrame()

    hist_detail = {
        '中信一级行业': _load_group_detail('./data/group_assignment_details_zx_yjhy.csv'),
        '中信二级行业': _load_group_detail('./data/group_assignment_details_zx_ejhy.csv'),
    }
    return results, index_data, zx_yj_prices, zx_ej_prices, hist_detail

try:
    results, index_data, zx_yj_prices, zx_ej_prices, hist_detail = get_analysis_results()
    
    if index_data.empty:
        st.warning("📊 暂无中证全指数据。")
        st.stop()
        
    latest_date_dt = index_data.index[-1]
    latest_date_str = latest_date_dt.strftime('%Y-%m-%d')
    current_price = index_data['close'].iloc[-1]
    price_pct = (index_data['close'].pct_change().iloc[-1] * 100)

    # --- Header ---
    st.markdown(f"### 中证全指 <span style='font-size:0.6em; color:#8b949e'>000985.SH | {latest_date_str}</span>", unsafe_allow_html=True)

    # --- Task 3: 40D K-Line Chart (Enhanced) ---
    chart_data = index_data.tail(40)
    fig_k = go.Figure(data=[go.Candlestick(
        x=chart_data.index.strftime('%Y-%m-%d'),
        open=chart_data['open'], high=chart_data['high'],
        low=chart_data['low'], close=chart_data['close'],
        increasing_line_color='#f85149', decreasing_line_color='#3fb950',
        increasing_fillcolor='#f85149', decreasing_fillcolor='#3fb950'
    )])
    fig_k.update_layout(
        height=400, 
        margin=dict(l=10, r=10, t=10, b=0), 
        template="plotly_dark",
        xaxis_rangeslider_visible=False, 
        paper_bgcolor='rgba(0,0,0,0)', 
        plot_bgcolor='rgba(0,0,0,0)',
        xaxis=dict(
            type='category',
            showgrid=False,
            tickangle=-45,
            nticks=12,
            linecolor='#30363d'
        ),
        yaxis=dict(
            showgrid=False,
            zeroline=False,
            linecolor='#30363d',
            tickformat='.0f'
        )
    )
    st.plotly_chart(fig_k, use_container_width=True)

    # --- Metrics Section ---
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f'<div class="metric-card"><div class="column-label">最新价格</div><div class="value-label" style="color:{"#3fb950" if price_pct < 0 else "#f85149"}">{current_price:.2f}</div></div>', unsafe_allow_html=True)
    with col2:
        st.markdown(f'<div class="metric-card"><div class="column-label">日内涨跌指数</div><div class="value-label" style="color:{"#3fb950" if price_pct < 0 else "#f85149"}">{price_pct:+.2f}%</div></div>', unsafe_allow_html=True)

    # --- Task 4: 今日信号独立模块 ---
    st.markdown("### 🔔 今日策略信号")
    current_has_signal = False
    current_signal_type = "今日无信号"
    for sector in ['中信一级行业', '中信二级行业']:
        if results[sector] and results[sector][-1]['date'].date() == latest_date_dt.date():
            current_has_signal = True
            current_signal_type = f"触发: {results[sector][-1]['type']}"
    
    st.markdown(f"""
    <div class="metric-card" style="border-left: 5px solid {'#58a6ff' if current_has_signal else '#30363d'}; text-align: center;">
        <div class="value-label" style="color: {'#58a6ff' if current_has_signal else '#8b949e'}">{current_signal_type}</div>
    </div>
    """, unsafe_allow_html=True)

    # --- Today's Ranking ---
    if current_has_signal:
        st.markdown("### 📊 本次触发行业评分")
        tabs = st.tabs(["中信一级", "中信二级"])
        for i, sector in enumerate(['中信一级行业', '中信二级行业']):
            with tabs[i]:
                if results[sector] and results[sector][-1]['date'].date() == latest_date_dt.date():
                    latest_res = results[sector][-1]
                    limit = 10
                    for name, val in latest_res['series'].head(limit).items():
                        st.markdown(f'<div style="padding: 8px 15px; margin-bottom: 5px; background-color: #1c2128; border-left: 4px solid #58a6ff; border-radius: 4px;"><b style="color: #c9d1d9;">{name}</b> <span style="color: #58a6ff; margin-left:10px;">({val:.4f})</span></div>', unsafe_allow_html=True)

    # --- Task 1 & 2: History Trace with Benchmarking ---
    st.markdown("<br><h3>🕒 历史信号溯源</h3>", unsafe_allow_html=True)
    
    overall_last_sig_date, overall_last_sig_type = None, "N/A"
    for s in ['中信一级行业', '中信二级行业']:
        valid_res = [r for r in results[s] if r['date'] <= latest_date_dt]
        if valid_res:
            overall_last_sig_date, overall_last_sig_type = valid_res[-1]['date'], valid_res[-1]['type']
            break

    idx_ret = 0.0
    if overall_last_sig_date:
        st.markdown(f"""
        <div style="padding: 12px 20px; margin-bottom: 5px; background-color: #1c2128; border: 1px solid #30363d; border-radius: 8px; text-align: center;">
            <span style="color: #8b949e;">触发日期: <b style="color: #c9d1d9;">{overall_last_sig_date.strftime('%Y-%m-%d')}</b></span>
            <span style="color: #333; margin: 0 15px;">|</span>
            <span style="color: #8b949e;">信号类型: <b style="color: #58a6ff;">{overall_last_sig_type}</b></span>
        </div>
        """, unsafe_allow_html=True)
        
        # 计算中证同期
        idx_after_sig = index_data.loc[overall_last_sig_date:]
        if len(idx_after_sig) > 1:
            i_target_idx = min(20, len(idx_after_sig) - 1)
            idx_ret = (idx_after_sig['close'].iloc[i_target_idx] / idx_after_sig['close'].iloc[0] - 1) * 100
            st.markdown(f'<div style="text-align: right; font-size: 0.8em; margin-bottom: 15px; color: #8b949e; padding-right:10px;">同期中证全指表现: <span style="color: {"#f85149" if idx_ret>=0 else "#3fb950"}; font-weight: bold;">{idx_ret:+.2f}%</span> (20D或至今)</div>', unsafe_allow_html=True)

    h_col1, h_col2 = st.columns(2)
    sector_price_map = {'中信一级行业': zx_yj_prices, '中信二级行业': zx_ej_prices}

    def _compute_hist_portfolio_stats(detail_df):
        """基于 CSV 历史分组明细，按 date 聚合多头/多空 Top5/Top10 的期收益率。

        约定：factor_value 越大越多头。period_return/excess_return 为小数（×100 显示为 %）。
        """
        if detail_df is None or detail_df.empty:
            return pd.DataFrame()
        rows = []
        for dt, grp in detail_df.groupby('date'):
            g = grp.sort_values('factor_value', ascending=False)
            n = len(g)
            if n < 2:
                continue
            rets = g['period_return'].values * 100
            excess = g['excess_return'].values * 100
            def _avg(arr):
                return float(np.mean(arr)) if len(arr) > 0 else np.nan
            k5_ls = min(5, n // 2)
            k10_ls = min(10, n // 2)
            rows.append({
                'date': dt,
                'long5': _avg(rets[:min(5, n)]),
                'long10': _avg(rets[:min(10, n)]),
                'long5_ex': _avg(excess[:min(5, n)]),
                'long10_ex': _avg(excess[:min(10, n)]),
                'ls5': _avg(rets[:k5_ls]) - _avg(rets[-k5_ls:]) if k5_ls > 0 else np.nan,
                'ls10': _avg(rets[:k10_ls]) - _avg(rets[-k10_ls:]) if k10_ls > 0 else np.nan,
            })
        return pd.DataFrame(rows).sort_values('date').reset_index(drop=True)

    def _color(v):
        return '#f85149' if v >= 0 else '#3fb950'

    for i, sector in enumerate(['中信一级行业', '中信二级行业']):
        with [h_col1, h_col2][i]:
            st.markdown(f"#### {sector}")
            # 历史统计卡片
            hist_df = _compute_hist_portfolio_stats(hist_detail.get(sector, pd.DataFrame()))
            if not hist_df.empty:
                n_total = len(hist_df)

                def _stat(col):
                    s = hist_df[col].dropna()
                    if len(s) == 0:
                        return 0.0, 0.0
                    return float(s.mean()), float((s > 0).mean() * 100)

                lg5_avg, lg5_wr = _stat('long5')
                lg10_avg, lg10_wr = _stat('long10')
                ls5_avg, ls5_wr = _stat('ls5')
                ls10_avg, ls10_wr = _stat('ls10')
                lg5_ex, _ = _stat('long5_ex')
                lg10_ex, _ = _stat('long10_ex')

                st.markdown(f"""
                <div style="padding: 12px 14px; margin-bottom: 12px; background: linear-gradient(180deg,#161b22 0%,#12171e 100%); border: 1px solid #30363d; border-radius: 8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <span style="color:#c9d1d9; font-size:0.9em; font-weight:600;">📈 历史组合表现</span>
                        <span style="color:#8b949e; font-size:0.75em;">样本 {n_total} 次</span>
                    </div>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px;">
                        <div style="background-color:#1c2128; padding:10px; border-radius:6px; border-left:3px solid #58a6ff;">
                            <div style="color:#8b949e; font-size:0.72em; margin-bottom:4px;">多头 Top5 · 平均</div>
                            <div style="color:{_color(lg5_avg)}; font-weight:bold; font-size:1.1em;">{lg5_avg:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.7em; margin-top:2px;">超额 <span style="color:{_color(lg5_ex)};">{lg5_ex:+.2f}%</span> · 胜率 <span style="color:#c9d1d9;">{lg5_wr:.0f}%</span></div>
                        </div>
                        <div style="background-color:#1c2128; padding:10px; border-radius:6px; border-left:3px solid #58a6ff;">
                            <div style="color:#8b949e; font-size:0.72em; margin-bottom:4px;">多头 Top10 · 平均</div>
                            <div style="color:{_color(lg10_avg)}; font-weight:bold; font-size:1.1em;">{lg10_avg:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.7em; margin-top:2px;">超额 <span style="color:{_color(lg10_ex)};">{lg10_ex:+.2f}%</span> · 胜率 <span style="color:#c9d1d9;">{lg10_wr:.0f}%</span></div>
                        </div>
                        <div style="background-color:#1c2128; padding:10px; border-radius:6px; border-left:3px solid #d29922;">
                            <div style="color:#8b949e; font-size:0.72em; margin-bottom:4px;">多空 Top5 · 平均</div>
                            <div style="color:{_color(ls5_avg)}; font-weight:bold; font-size:1.1em;">{ls5_avg:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.7em; margin-top:2px;">胜率 <span style="color:#c9d1d9;">{ls5_wr:.0f}%</span></div>
                        </div>
                        <div style="background-color:#1c2128; padding:10px; border-radius:6px; border-left:3px solid #d29922;">
                            <div style="color:#8b949e; font-size:0.72em; margin-bottom:4px;">多空 Top10 · 平均</div>
                            <div style="color:{_color(ls10_avg)}; font-weight:bold; font-size:1.1em;">{ls10_avg:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.7em; margin-top:2px;">胜率 <span style="color:#c9d1d9;">{ls10_wr:.0f}%</span></div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

            # 最近一次信号详情
            valid_results = [r for r in results[sector] if r['date'] <= latest_date_dt]
            if valid_results:
                last_sig = valid_results[-1]
                after_prices = sector_price_map[sector].loc[last_sig['date']:]
                if len(after_prices) > 1:
                    t_idx = min(20, len(after_prices) - 1)
                    returns = (after_prices.iloc[t_idx] / after_prices.iloc[0] - 1) * 100
                else:
                    returns = pd.Series(0.0, index=sector_price_map[sector].columns)

                # 当期多头/多空组合收益率（基于因子值降序）
                sorted_series = last_sig['series'].dropna()
                aligned_ret = returns.reindex(sorted_series.index).dropna()
                n_avail = len(aligned_ret)
                def _avg2(sub):
                    return float(sub.mean()) if len(sub) > 0 else 0.0
                cur_long5 = _avg2(aligned_ret.head(min(5, n_avail)))
                cur_long10 = _avg2(aligned_ret.head(min(10, n_avail)))
                k5c = min(5, n_avail // 2)
                k10c = min(10, n_avail // 2)
                cur_ls5 = _avg2(aligned_ret.head(k5c)) - _avg2(aligned_ret.tail(k5c)) if k5c > 0 else 0.0
                cur_ls10 = _avg2(aligned_ret.head(k10c)) - _avg2(aligned_ret.tail(k10c)) if k10c > 0 else 0.0
                hold_days = min(20, max(len(after_prices) - 1, 0))

                st.markdown(f"""
                <div style="padding:10px 14px; margin-bottom:8px; background:linear-gradient(180deg,#161b22 0%,#12171e 100%); border:1px solid #30363d; border-left:3px solid #58a6ff; border-radius:6px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                        <div style="color:#c9d1d9; font-size:0.85em;">
                            <span style="color:#8b949e;">最近触发</span>
                            &nbsp;<b>{last_sig['date'].strftime('%Y-%m-%d')}</b>
                            &nbsp;·&nbsp;<span style="color:#58a6ff;">{last_sig['type']}</span>
                        </div>
                        <span style="color:#8b949e; font-size:0.72em;">当期 · 持有 {hold_days}D</span>
                    </div>
                    <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:6px;">
                        <div style="background-color:#1c2128; padding:6px 4px; border-radius:4px; text-align:center;">
                            <div style="color:#8b949e; font-size:0.7em;">多头 T5</div>
                            <div style="color:{_color(cur_long5)}; font-weight:bold; font-size:0.95em;">{cur_long5:+.2f}%</div>
                        </div>
                        <div style="background-color:#1c2128; padding:6px 4px; border-radius:4px; text-align:center;">
                            <div style="color:#8b949e; font-size:0.7em;">多头 T10</div>
                            <div style="color:{_color(cur_long10)}; font-weight:bold; font-size:0.95em;">{cur_long10:+.2f}%</div>
                        </div>
                        <div style="background-color:#1c2128; padding:6px 4px; border-radius:4px; text-align:center;">
                            <div style="color:#8b949e; font-size:0.7em;">多空 T5</div>
                            <div style="color:{_color(cur_ls5)}; font-weight:bold; font-size:0.95em;">{cur_ls5:+.2f}%</div>
                        </div>
                        <div style="background-color:#1c2128; padding:6px 4px; border-radius:4px; text-align:center;">
                            <div style="color:#8b949e; font-size:0.7em;">多空 T10</div>
                            <div style="color:{_color(cur_ls10)}; font-weight:bold; font-size:0.95em;">{cur_ls10:+.2f}%</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                # Add Table Header
                st.markdown("""
                <div style="padding: 10px; background-color: #21262d; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; font-size: 0.9em; margin-bottom:6px; font-weight: bold; color: #8b949e;">
                    <div style="flex:2.2;">行业名称</div>
                    <div style="flex:1; text-align:center;">因子值</div>
                    <div style="flex:1.8; text-align:right;">收益率(超额)</div>
                </div>""", unsafe_allow_html=True)

                for name, val in last_sig['series'].head(10).items():
                    r_val = returns.get(name, 0.0)
                    excess = r_val - idx_ret
                    st.markdown(f"""
                    <div style="padding: 10px; background-color: #161b22; border-left: 3px solid #30363d; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; font-size: 0.9em; margin-bottom:4px;">
                        <div style="flex:2.2; color:#c9d1d9;"><b>{name}</b></div>
                        <div style="flex:1; text-align:center; color:#58a6ff;">{val:.4f}</div>
                        <div style="flex:1.8; text-align:right;">
                            <span style="color:{_color(r_val)}; font-weight:bold;">{r_val:+.2f}%</span>
                            <span style="color:#8b949e; font-size:0.8em; margin-left:5px;">[<span style="color:{_color(excess)}">{excess:+.2f}%</span>]</span>
                        </div>
                    </div>""", unsafe_allow_html=True)
except Exception as e: st.error(f"Error: {e}")
st.caption(f"Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
