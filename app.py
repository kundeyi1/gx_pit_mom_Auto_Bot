import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
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
    .market-summary-panel {
        background-color: #141414;
        border: 1px solid #2c2c2c;
        border-radius: 8px;
        padding: 18px 18px 16px 18px;
        min-height: 560px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 16px;
    }
    .market-summary-title { color: #c9d1d9; font-size: 1.15rem; font-weight: 700; }
    .market-summary-code { color: #8b949e; font-size: 0.82rem; margin-top: 4px; }
    .market-stat {
        border-top: 1px solid #2c2c2c;
        padding-top: 16px;
    }
    .market-signal {
        border-top: 1px solid #2c2c2c;
        padding-top: 16px;
    }
    .signal-value { font-size: 1.15rem; font-weight: 700; line-height: 1.35; }
    .column-label { color: #8b949e; font-size: 0.9em; }
    .value-label { font-size: 1.5em; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# --- Data Loading & Analysis ---
DATA_FILES = (
    '000985_prices.xlsx',
    'ZX_YJHY.xlsx',
    'ZX_EJHY.xlsx',
    'group_assignment_details_zx_yjhy.csv',
    'group_assignment_details_zx_ejhy.csv',
)

EXCLUDED_ASSET_KEYWORDS = ('资产管理',)

# 数据源检测: 通过 Excel 第一行识别数据来源 (Wind CITIC vs 通达信/东方财富/腾讯财经)
def _detect_data_source(file_path: str) -> str:
    """检测数据源类型: 'citic', 'tdx', 'eastmoney' 或 'tencent'."""
    try:
        df_raw = pd.read_excel(file_path, nrows=5, header=None)
        first_val = str(df_raw.iloc[0, 0]).strip()
        if '腾讯财经' in first_val or 'Tencent' in first_val:
            return 'tencent'
        if '东方财富' in first_val or 'Eastmoney' in first_val:
            return 'eastmoney'
        if '通达信' in first_val or 'TDX' in first_val or 'mootdx' in first_val:
            return 'tdx'
        if 'Wind' in first_val or '中信' in first_val or 'Wind' in str(df_raw.iloc[4, 0]) if len(df_raw) > 4 else False:
            return 'citic'
        # 检查表的来源行 (第 5 行, 0-indexed=4)
        if len(df_raw) > 4:
            source_row = str(df_raw.iloc[4, 0]).strip()
            if '腾讯财经' in source_row or 'Tencent' in source_row:
                return 'tencent'
            if '东方财富' in source_row or 'Eastmoney' in source_row:
                return 'eastmoney'
            if '通达信' in source_row or 'mootdx' in source_row:
                return 'tdx'
            if '中信' in source_row:
                return 'citic'
    except Exception:
        pass
    return 'citic'  # 默认假设为 CITIC


# 在模块加载时检测数据源
_DATA_SOURCE_YJHY = _detect_data_source('./data/ZX_YJHY.xlsx')
_DATA_SOURCE_BENCH = _detect_data_source('./data/000985_prices.xlsx')

# 根据数据源动态设置标签
if _DATA_SOURCE_YJHY == 'tdx':
    _LABEL_BENCHMARK = '中证全指'
    _LABEL_L1 = '行业板块 (一级)'
    _LABEL_L2 = '行业板块 (二级)'
    _LABEL_L1_SHORT = '一级板块'
    _LABEL_L2_SHORT = '二级板块'
else:
    _LABEL_BENCHMARK = '中证全指'
    _LABEL_L1 = '中信一级行业'
    _LABEL_L2 = '中信二级行业'
    _LABEL_L1_SHORT = '一级行业'
    _LABEL_L2_SHORT = '二级行业'


def get_data_signature(data_dir='./data/'):
    """Return a lightweight fingerprint so Streamlit cache refreshes after data updates."""
    signature = []
    for file_name in DATA_FILES:
        path = os.path.join(data_dir, file_name)
        try:
            stat = os.stat(path)
            signature.append((file_name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((file_name, None, None))
    return tuple(signature)


def _filter_group_detail_to_current_universe(detail_df, price_df):
    if detail_df is None or detail_df.empty or price_df is None or price_df.empty:
        return detail_df
    asset_col = next((c for c in ('sector', 'asset_code', 'asset_name') if c in detail_df.columns), None)
    if asset_col is None:
        return detail_df
    current_assets = set(price_df.columns)
    current_asset_names = {str(c).split(':')[-1].lower() for c in price_df.columns}
    detail_assets = detail_df[asset_col].astype(str)
    detail_asset_names = detail_assets.str.split(':').str[-1].str.lower()
    mask = detail_assets.isin(current_assets) | detail_asset_names.isin(current_asset_names)
    return detail_df.loc[mask].copy()


def _drop_excluded_assets(price_df):
    if price_df is None or price_df.empty:
        return price_df
    keep_cols = [
        c for c in price_df.columns
        if not any(keyword in str(c) for keyword in EXCLUDED_ASSET_KEYWORDS)
    ]
    return price_df.loc[:, keep_cols].copy()


def _drop_excluded_from_detail(detail_df):
    if detail_df is None or detail_df.empty:
        return detail_df
    asset_col = next((c for c in ('sector', 'asset_code', 'asset_name') if c in detail_df.columns), None)
    if asset_col is None:
        return detail_df
    asset_text = detail_df[asset_col].astype(str)
    mask = ~asset_text.apply(lambda x: any(keyword in x for keyword in EXCLUDED_ASSET_KEYWORDS))
    return detail_df.loc[mask].copy()


@st.cache_data(ttl=3600)  # 数据文件指纹变化时会立即刷新缓存
def get_analysis_results(data_signature):
    analyzer = GXPitMomActions(data_dir='./data/')
    index_data = analyzer.dp.get_wide_table('000985_prices.xlsx')
    zx_yj_prices = analyzer.dp.get_wide_table('ZX_YJHY.xlsx')
    zx_ej_prices = analyzer.dp.get_wide_table('ZX_EJHY.xlsx')
    zx_ej_prices = _drop_excluded_assets(zx_ej_prices)
    
    sig_breakout = analyzer.gx_pit_breakout(index_data)
    sig_rebound = analyzer.gx_pit_rebound(index_data)
    sig_rotation = analyzer.gx_pit_rotation(index_data, zx_yj_prices)

    signals_dict = {'breakout': sig_breakout, 'rebound': sig_rebound, 'rotation': sig_rotation}
    results = {
        _LABEL_L1: analyzer.calculate_fused_signals(zx_yj_prices, signals_dict),
        _LABEL_L2: analyzer.calculate_fused_signals(zx_ej_prices, signals_dict)
    }

    # 读取历史分组业绩明细 CSV
    def _load_group_detail(path):
        try:
            df = pd.read_csv(path, parse_dates=['date'])
            return df
        except Exception:
            return pd.DataFrame()

    hist_detail = {
        _LABEL_L1: _load_group_detail('./data/group_assignment_details_zx_yjhy.csv'),
        _LABEL_L2: _load_group_detail('./data/group_assignment_details_zx_ejhy.csv'),
    }
    hist_detail = {
        _LABEL_L1: _filter_group_detail_to_current_universe(hist_detail[_LABEL_L1], zx_yj_prices),
        _LABEL_L2: _drop_excluded_from_detail(
            _filter_group_detail_to_current_universe(hist_detail[_LABEL_L2], zx_ej_prices)
        ),
    }
    return results, index_data, zx_yj_prices, zx_ej_prices, hist_detail

try:
    results, index_data, zx_yj_prices, zx_ej_prices, hist_detail = get_analysis_results(get_data_signature())
    
    if index_data.empty:
        st.warning(f"📊 暂无{_LABEL_BENCHMARK}数据。")
        st.stop()
        
    latest_date_dt = index_data.index[-1]
    latest_date_str = latest_date_dt.strftime('%Y-%m-%d')
    current_price = index_data['close'].iloc[-1]
    price_pct = (index_data['close'].pct_change().iloc[-1] * 100)

    # --- Header ---
    bench_code = "000985.SH"
    st.markdown("### 股旭低位动量策略监控")
    st.caption("基于中证全指择时信号与行业相对动量，跟踪一级、二级行业板块的轮动机会。")

    current_has_signal = False
    current_signal_type = "今日无信号"
    for sector in [_LABEL_L1, _LABEL_L2]:
        if results[sector] and results[sector][-1]['date'].date() == latest_date_dt.date():
            current_has_signal = True
            current_signal_type = f"触发: {results[sector][-1]['type']}"

    # --- Benchmark K-Line Chart ---
    chart_data = index_data.copy()
    chart_data['ma5'] = chart_data['close'].rolling(5).mean()
    chart_data['ma20'] = chart_data['close'].rolling(20).mean()
    chart_data = chart_data.tail(120)
    chart_x = chart_data.index.strftime('%Y-%m-%d')
    volume_col = 'volume' if 'volume' in chart_data.columns else 'amt'
    prev_close = chart_data['close'].shift(1).fillna(chart_data['open'])
    volume_colors = np.where(chart_data['close'] >= prev_close, '#ff5722', '#00c875')

    fig_k = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.82, 0.18],
    )
    fig_k.add_trace(go.Candlestick(
        x=chart_x,
        open=chart_data['open'],
        high=chart_data['high'],
        low=chart_data['low'],
        close=chart_data['close'],
        increasing_line_color='#ff5722',
        increasing_fillcolor='rgba(0,0,0,0)',
        decreasing_line_color='#00c875',
        decreasing_fillcolor='#00c875',
        whiskerwidth=0.55,
        name='K线',
    ), row=1, col=1)
    fig_k.add_trace(go.Scatter(
        x=chart_x,
        y=chart_data['ma5'],
        mode='lines',
        line=dict(color='#e8e8e8', width=2),
        hoverinfo='skip',
        name='MA5',
    ), row=1, col=1)
    fig_k.add_trace(go.Scatter(
        x=chart_x,
        y=chart_data['ma20'],
        mode='lines',
        line=dict(color='#f0a51a', width=2),
        hoverinfo='skip',
        name='MA20',
    ), row=1, col=1)
    fig_k.add_trace(go.Bar(
        x=chart_x,
        y=chart_data[volume_col],
        marker_color=volume_colors,
        marker_line_width=0,
        opacity=1,
        hoverinfo='skip',
        name='成交量',
    ), row=2, col=1)
    fig_k.update_layout(
        height=560,
        margin=dict(l=4, r=4, t=8, b=4),
        template="plotly_dark",
        showlegend=False,
        hovermode='x unified',
        bargap=0.55,
        xaxis_rangeslider_visible=False,
        paper_bgcolor='#111111',
        plot_bgcolor='#111111',
        font=dict(color='#c9d1d9'),
        dragmode=False,
    )
    fig_k.update_xaxes(
        type='category',
        showgrid=False,
        showticklabels=False,
        linecolor='#2c2c2c',
        zeroline=False,
        rangeslider_visible=False,
        row=1,
        col=1,
    )
    fig_k.update_xaxes(
        type='category',
        showgrid=False,
        nticks=7,
        tickangle=0,
        linecolor='#2c2c2c',
        zeroline=False,
        row=2,
        col=1,
    )
    for row_idx in [1, 2]:
        fig_k.update_yaxes(
            showgrid=True,
            gridcolor='#2c2c2c',
            zeroline=False,
            showticklabels=False,
            fixedrange=True,
            row=row_idx,
            col=1,
        )

    price_color = "#3fb950" if price_pct < 0 else "#f85149"
    signal_color = "#58a6ff" if current_has_signal else "#8b949e"
    market_col, chart_col = st.columns([1, 2.8], gap="large")
    with market_col:
        st.markdown(f"""
        <div class="market-summary-panel">
            <div>
                <div class="market-summary-title">{_LABEL_BENCHMARK}</div>
                <div class="market-summary-code">{bench_code} · {latest_date_str}</div>
            </div>
            <div class="market-stat">
                <div class="column-label">最新价格</div>
                <div class="value-label" style="color:{price_color}; font-size:2rem;">{current_price:.2f}</div>
            </div>
            <div class="market-stat">
                <div class="column-label">当日涨跌幅</div>
                <div class="value-label" style="color:{price_color}; font-size:2rem;">{price_pct:+.2f}%</div>
            </div>
            <div class="market-signal">
                <div class="column-label">今日信号</div>
                <div class="signal-value" style="color:{signal_color};">{current_signal_type}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    with chart_col:
        st.plotly_chart(fig_k, use_container_width=True)

    # --- Task 1 & 2: History Trace with Benchmarking ---
    st.markdown("### 📊 当前、历史信号结果统计")
    
    overall_last_sig_date, overall_last_sig_type = None, "N/A"
    for s in [_LABEL_L1, _LABEL_L2]:
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
            bench_color = "#f85149" if idx_ret >= 0 else "#3fb950"
            st.markdown(f'<div style="text-align: right; font-size: 0.8em; margin-bottom: 15px; color: #8b949e; padding-right:10px;">同期{_LABEL_BENCHMARK}表现: <span style="color: {bench_color}; font-weight: bold;">{idx_ret:+.2f}%</span> (20D或至今)</div>', unsafe_allow_html=True)

    h_col1, h_col2 = st.columns(2)
    sector_price_map = {_LABEL_L1: zx_yj_prices, _LABEL_L2: zx_ej_prices}

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

    for i, sector in enumerate([_LABEL_L1, _LABEL_L2]):
        with [h_col1, h_col2][i]:
            top_n = 5 if sector == _LABEL_L1 else 10  # 一级→T5；二级→T10
            long_col = f'long{top_n}'
            long_ex_col = f'long{top_n}_ex'
            ls_col = f'ls{top_n}'
            tag = f'T{top_n}'

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

                lg_avg, lg_wr = _stat(long_col)
                lg_ex, _ = _stat(long_ex_col)
                ls_avg, ls_wr = _stat(ls_col)

                st.markdown(f"""
                <div style="padding: 12px 14px; margin-bottom: 12px; background: linear-gradient(180deg,#161b22 0%,#12171e 100%); border: 1px solid #30363d; border-radius: 8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <span style="color:#c9d1d9; font-size:0.9em; font-weight:600;">📈 历史组合表现</span>
                        <span style="color:#8b949e; font-size:0.75em;">样本 {n_total} 次</span>
                    </div>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px;">
                        <div style="background-color:#1c2128; padding:10px; border-radius:6px; border-left:3px solid #58a6ff;">
                            <div style="color:#8b949e; font-size:0.72em; margin-bottom:4px;">多头 {tag} · 平均</div>
                            <div style="color:{_color(lg_avg)}; font-weight:bold; font-size:1.1em;">{lg_avg:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.7em; margin-top:2px;">超额 <span style="color:{_color(lg_ex)};">{lg_ex:+.2f}%</span> · 胜率 <span style="color:#c9d1d9;">{lg_wr:.0f}%</span></div>
                        </div>
                        <div style="background-color:#1c2128; padding:10px; border-radius:6px; border-left:3px solid #d29922;">
                            <div style="color:#8b949e; font-size:0.72em; margin-bottom:4px;">多空 {tag} · 平均</div>
                            <div style="color:{_color(ls_avg)}; font-weight:bold; font-size:1.1em;">{ls_avg:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.7em; margin-top:2px;">胜率 <span style="color:#c9d1d9;">{ls_wr:.0f}%</span></div>
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
                cur_long = _avg2(aligned_ret.head(min(top_n, n_avail)))
                cur_long_ex = cur_long - idx_ret
                k_ls = min(top_n, n_avail // 2)
                cur_ls = _avg2(aligned_ret.head(k_ls)) - _avg2(aligned_ret.tail(k_ls)) if k_ls > 0 else 0.0
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
                    <div style="display:grid; grid-template-columns:repeat(2,1fr); gap:6px;">
                        <div style="background-color:#1c2128; padding:8px 6px; border-radius:4px; text-align:center; border-left:2px solid #58a6ff;">
                            <div style="color:#8b949e; font-size:0.7em;">多头 {tag}</div>
                            <div style="color:{_color(cur_long)}; font-weight:bold; font-size:1em;">{cur_long:+.2f}%</div>
                            <div style="color:#8b949e; font-size:0.68em; margin-top:2px;">超额 <span style="color:{_color(cur_long_ex)};">{cur_long_ex:+.2f}%</span></div>
                        </div>
                        <div style="background-color:#1c2128; padding:8px 6px; border-radius:4px; text-align:center; border-left:2px solid #d29922;">
                            <div style="color:#8b949e; font-size:0.7em;">多空 {tag}</div>
                            <div style="color:{_color(cur_ls)}; font-weight:bold; font-size:1em;">{cur_ls:+.2f}%</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                def _render_factor_table(title, factor_items, accent_color):
                    st.markdown(f"""
                    <div style="padding: 8px 10px; margin: 10px 0 6px; background-color:#21262d; border-left:3px solid {accent_color}; border-radius:4px; color:#c9d1d9; font-size:0.9em; font-weight:600;">
                        {title}
                    </div>
                    <div style="padding: 10px; background-color: #21262d; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; font-size: 0.9em; margin-bottom:6px; font-weight: bold; color: #8b949e;">
                        <div style="flex:2.2;">行业名称</div>
                        <div style="flex:1; text-align:center;">因子值</div>
                        <div style="flex:1.8; text-align:right;">收益率(超额)</div>
                    </div>""", unsafe_allow_html=True)

                    for name, val in factor_items.items():
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

                _render_factor_table(f"因子值前 {tag}", last_sig['series'].head(top_n), "#58a6ff")
                _render_factor_table(f"因子值后 {tag}", last_sig['series'].tail(top_n).sort_values(ascending=False), "#d29922")

    # --- 今日行业涨幅 Top10 ---
    st.markdown("### 🚀 当天行业涨幅 Top10")
    top_col1, top_col2 = st.columns(2)
    daily_ret_map = {
        _LABEL_L1: zx_yj_prices.pct_change().iloc[-1] * 100 if len(zx_yj_prices) > 1 else pd.Series(dtype=float),
        _LABEL_L2: zx_ej_prices.pct_change().iloc[-1] * 100 if len(zx_ej_prices) > 1 else pd.Series(dtype=float),
    }

    for i, sector in enumerate([_LABEL_L1, _LABEL_L2]):
        with [top_col1, top_col2][i]:
            sorted_daily_ret = daily_ret_map[sector].dropna().sort_values(ascending=False)
            top10 = sorted_daily_ret.head(10)
            bottom10 = sorted_daily_ret.tail(10).sort_values(ascending=False)
            st.markdown(f"""
            <div style="padding:10px 12px; margin-bottom:8px; background:linear-gradient(180deg,#161b22 0%,#12171e 100%); border:1px solid #30363d; border-radius:8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span style="color:#c9d1d9; font-weight:600; font-size:0.9em;">{sector}</span>
                    <span style="color:#8b949e; font-size:0.72em;">{latest_date_str}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            def _render_daily_return_list(title, items, accent_color):
                st.markdown(f"""
                <div style="padding:8px 10px; margin:10px 0 6px; background-color:#21262d; border-left:3px solid {accent_color}; border-radius:4px; color:#c9d1d9; font-size:0.88em; font-weight:600;">
                    {title}
                </div>
                """, unsafe_allow_html=True)

                for rank, (name, ret_val) in enumerate(items.items(), start=1):
                    st.markdown(f"""
                    <div style="padding:9px 10px; margin-bottom:4px; background-color:#161b22; border-left:3px solid #58a6ff; border-radius:4px; display:flex; justify-content:space-between; align-items:center;">
                        <div style="color:#c9d1d9; font-size:0.9em;"><span style="color:#8b949e; margin-right:8px;">#{rank}</span><b>{name}</b></div>
                        <div style="color:{'#f85149' if ret_val >= 0 else '#3fb950'}; font-weight:bold; font-size:0.92em;">{ret_val:+.2f}%</div>
                    </div>
                    """, unsafe_allow_html=True)

            if top10.empty:
                st.markdown('<div style="padding:10px; color:#8b949e; background-color:#161b22; border:1px dashed #30363d; border-radius:6px;">暂无可用数据</div>', unsafe_allow_html=True)
            else:
                _render_daily_return_list("涨幅前 Top10", top10, "#58a6ff")
                _render_daily_return_list("涨幅后 Top10", bottom10, "#d29922")
except Exception as e: st.error(f"Error: {e}")
st.caption(f"Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
