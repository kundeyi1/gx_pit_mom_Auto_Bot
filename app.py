import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
from html import escape
from pathlib import Path
from src.analysis import GXPitMomActions
from src.data_provider import WindLocalProvider
from src.backtest import (
    build_auto_updating_nav,
    calculate_max_drawdown,
    load_backtest_nav,
)

# --- Page Config ---
st.set_page_config(
    page_title="时点动量策略 · Quant Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.html(Path(__file__).with_name("dashboard.css"))


def signal_label(value):
    names = {"breakout": "突破", "rotation": "轮动", "rebound": "反弹"}
    for key, label in names.items():
        value = value.replace(key, label)
    return value


def rank_table(title, columns, rows):
    headers = "".join(f"<th scope='col'>{name}</th>" for name in columns)
    body = "".join(f"<tr>{row}</tr>" for row in rows)
    st.html(
        f"<section class='rank-panel'><div class='rank-title'>{title}</div>"
        f"<table class='rank-table'><thead><tr>{headers}</tr></thead>"
        f"<tbody>{body}</tbody></table></section>"
    )


# --- Data Loading & Analysis ---
DATA_FILES = (
    '000985_prices.xlsx',
    'ZX_YJHY.xlsx',
    'ZX_EJHY.xlsx',
    'group_assignment_details_zx_yjhy.csv',
    'group_assignment_details_zx_ejhy.csv',
    'backtest_nav_zx_yjhy.csv',
    'backtest_nav_zx_ejhy.csv',
    'backtest_signal_groups_zx_yjhy.csv',
    'backtest_signal_groups_zx_ejhy.csv',
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


def _build_backtest_nav_figure(nav_specs, trading_dates):
    """Render long and long-short NAV in stacked panels."""
    subplot_titles = [f"{label} · {result.n_groups}组" for label, result in nav_specs]
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.10,
        subplot_titles=subplot_titles,
    )
    latest_nav_date = max(result.long.index[-1] for _, result in nav_specs)
    window = trading_dates[trading_dates <= latest_nav_date][-60:]
    window_start, window_end = window[0], window[-1]
    colors = {"long": "#ef777d", "long_short": "#80bff1"}

    for row, (label, result) in enumerate(nav_specs, start=1):
        for key, legend_label in (
            ("long", "多头组合"),
            ("long_short", "多空"),
        ):
            trace_label = f"{label} {legend_label}"
            series = getattr(result, key)
            fig.add_trace(
                go.Scatter(
                    x=series.index.strftime("%Y-%m-%d"),
                    y=series.values,
                    mode="lines",
                    line=dict(color=colors[key], width=1.05),
                    name=legend_label,
                    legendgroup=key,
                    showlegend=row == 1,
                    hovertemplate=(
                        f"{trace_label}<br>%{{x}}<br>净值 %{{y:.4f}}"
                        "<extra></extra>"
                    ),
                ),
                row=row,
                col=1,
            )
        fig.add_hline(y=1.0, line=dict(color="#476584", width=0.8, dash="dot"), row=row, col=1)
        visible = pd.concat([result.long, result.long_short], axis=1).loc[window_start:window_end]
        if not visible.empty:
            low, high = visible.min().min(), visible.max().max()
            padding = max((high - low) * 0.08, 0.01)
            fig.update_yaxes(range=[low - padding, high + padding], row=row, col=1)

    fig.update_layout(
        height=350,
        margin=dict(l=4, r=4, t=68, b=12),
        template="plotly_dark",
        paper_bgcolor="#131c27",
        plot_bgcolor="#131c27",
        font=dict(color="#e9f0f7", size=10),
        hovermode="x unified",
        dragmode="zoom",
        legend=dict(
            orientation="h",
            x=1,
            xanchor="right",
            y=1.14,
            yanchor="top",
            font=dict(size=9, color="#97a8bb"),
        ),
    )
    fig.update_annotations(font=dict(size=10, color="#97a8bb"), x=0, xanchor="left")
    fig.update_xaxes(
        type="date",
        range=[window_start - pd.Timedelta(hours=12), window_end + pd.Timedelta(hours=12)],
        tickformat="%y-%m-%d",
        showgrid=False,
        linecolor="#283647",
        tickfont=dict(size=9, color="#97a8bb"),
        nticks=4,
        fixedrange=False,
    )
    fig.update_xaxes(showticklabels=False, row=1, col=1)
    fig.update_yaxes(
        showgrid=True,
        gridcolor="#223040",
        zeroline=False,
        tickformat=".2f",
        tickfont=dict(size=8, color="#97a8bb"),
        fixedrange=False,
        nticks=4,
    )
    return fig


@st.cache_data(ttl=3600)  # 数据文件指纹变化时会立即刷新缓存
def get_analysis_results(data_signature):
    analyzer = GXPitMomActions(data_dir='./data/')
    index_data = analyzer.dp.get_wide_table('000985_prices.xlsx')
    zx_yj_prices = analyzer.dp.get_wide_table('ZX_YJHY.xlsx')
    zx_ej_prices_all = analyzer.dp.get_wide_table('ZX_EJHY.xlsx')
    zx_ej_prices = _drop_excluded_assets(zx_ej_prices_all)
    backtest_provider = WindLocalProvider(data_dir='./data/', start_date='2013-01-01')
    backtest_yj_prices = backtest_provider.get_wide_table('ZX_YJHY.xlsx')
    backtest_ej_prices = backtest_provider.get_wide_table('ZX_EJHY.xlsx')
    
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
    def _load_signal_groups(path):
        return pd.read_csv(path, parse_dates=['date'])

    def _build_checked_nav(prices, group_path, snapshot_path, n_groups, expected):
        try:
            nav = build_auto_updating_nav(
                prices,
                _load_signal_groups(group_path),
                signals_dict,
                n_groups=n_groups,
                holding_days=20,
            )
            checkpoint = pd.Timestamp('2026-02-02')
            actual = (
                nav.long.loc[checkpoint],
                nav.short.loc[checkpoint],
                nav.long_short.loc[checkpoint],
            )
            if max(abs(value - target) for value, target in zip(actual, expected)) > 1e-9:
                raise ValueError("旧版回测检查点未通过")
            return nav
        except Exception as error:
            snapshot = load_backtest_nav(snapshot_path, n_groups=n_groups)
            snapshot.reason = str(error)
            return snapshot

    backtest_nav = {
        _LABEL_L1: _build_checked_nav(
            backtest_yj_prices,
            './data/backtest_signal_groups_zx_yjhy.csv',
            './data/backtest_nav_zx_yjhy.csv',
            5,
            (17.045276112646, 1.225478106637, 14.411344072385),
        ),
        _LABEL_L2: _build_checked_nav(
            backtest_ej_prices,
            './data/backtest_signal_groups_zx_ejhy.csv',
            './data/backtest_nav_zx_ejhy.csv',
            10,
            (18.308491495852, 1.245589257048, 15.209996496207),
        ),
    }
    return results, index_data, zx_yj_prices, zx_ej_prices, hist_detail, backtest_nav

try:
    results, index_data, zx_yj_prices, zx_ej_prices, hist_detail, backtest_nav = get_analysis_results(get_data_signature())
    
    if index_data.empty:
        st.warning(f"📊 暂无{_LABEL_BENCHMARK}数据。")
        st.stop()
        
    latest_date_dt = index_data.index[-1]
    latest_date_str = latest_date_dt.strftime('%Y-%m-%d')
    current_price = index_data['close'].iloc[-1]
    price_pct = (index_data['close'].pct_change().iloc[-1] * 100)

    # --- Header ---
    bench_code = "000985.CSI"
    st.html(f"""
        <header class="dash-head">
            <div><div class="dash-brand">QUANT DASHBOARD</div><h1>时点动量策略</h1></div>
            <span class="dash-asof">行情日期　{latest_date_str}</span>
        </header>
    """)
    current_has_signal = False
    current_signal_type = "今日无信号"
    for sector in [_LABEL_L1, _LABEL_L2]:
        if results[sector] and results[sector][-1]['date'].date() == latest_date_dt.date():
            current_has_signal = True
            current_signal_type = signal_label(results[sector][-1]["type"])

    # --- Benchmark K-Line Chart ---
    chart_data = index_data.copy()
    chart_data['ma5'] = chart_data['close'].rolling(5).mean()
    chart_data['ma20'] = chart_data['close'].rolling(20).mean()
    chart_data = chart_data.tail(120)
    chart_x = chart_data.index.strftime('%Y-%m-%d')
    volume_col = 'volume' if 'volume' in chart_data.columns else 'amt'
    prev_close = chart_data['close'].shift(1).fillna(chart_data['open'])
    volume_colors = np.where(chart_data['close'] >= prev_close, '#ef777d', '#48cba4')

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
        increasing_line_color='#ef777d',
        increasing_fillcolor='rgba(0,0,0,0)',
        decreasing_line_color='#48cba4',
        decreasing_fillcolor='#48cba4',
        whiskerwidth=0.55,
        name='K线',
    ), row=1, col=1)
    fig_k.add_trace(go.Scatter(
        x=chart_x,
        y=chart_data['ma5'],
        mode='lines',
        line=dict(color='#80bff1', width=2),
        hoverinfo='skip',
        name='MA5',
    ), row=1, col=1)
    fig_k.add_trace(go.Scatter(
        x=chart_x,
        y=chart_data['ma20'],
        mode='lines',
        line=dict(color='#e5bc77', width=2),
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
        height=471,
        margin=dict(l=4, r=4, t=32, b=4),
        template="plotly_dark",
        showlegend=True,
        legend=dict(orientation="h", y=1.08, x=0, font=dict(size=11)),
        hovermode='x unified',
        bargap=0.55,
        xaxis_rangeslider_visible=False,
        paper_bgcolor='#131c27',
        plot_bgcolor='#131c27',
        font=dict(color='#e9f0f7'),
        dragmode=False,
    )
    fig_k.update_xaxes(
        type='category',
        showgrid=False,
        showticklabels=False,
        linecolor='#283647',
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
        linecolor='#283647',
        zeroline=False,
        row=2,
        col=1,
    )
    for row_idx in [1, 2]:
        fig_k.update_yaxes(
            showgrid=True,
            gridcolor='#283647',
            zeroline=False,
            showticklabels=row_idx == 1,
            side="right",
            fixedrange=True,
            row=row_idx,
            col=1,
        )

    sector_price_map = {_LABEL_L1: zx_yj_prices, _LABEL_L2: zx_ej_prices}
    nav_specs = [
        (_LABEL_L1_SHORT, backtest_nav[_LABEL_L1]),
        (_LABEL_L2_SHORT, backtest_nav[_LABEL_L2]),
    ]
    fig_nav = _build_backtest_nav_figure(nav_specs, index_data.index)
    nav_latest_date = min(result.long.index[-1] for _, result in nav_specs)
    nav_is_dynamic = all(result.source == 'computed' for _, result in nav_specs)
    nav_mode = "自动重算" if nav_is_dynamic else "已验证快照"
    nav_mode_text = nav_mode
    performance_rows = []
    for label, result in nav_specs:
        for portfolio_label, series in (
            ("多头", result.long),
            ("多空", result.long_short),
        ):
            max_drawdown = calculate_max_drawdown(series)
            performance_rows.append(
                f'<tr><td>{label}{portfolio_label}</td>'
                f'<td class="drawdown">{max_drawdown:.1%}</td></tr>'
            )
    performance_table = ''.join(performance_rows)

    price_color = "#48cba4" if price_pct < 0 else "#ef777d"
    signal_color = "#80bff1" if current_has_signal else "#97a8bb"
    st.html(f"""
        <div class="metric-strip">
            <div><small>{_LABEL_BENCHMARK} · {bench_code}</small><strong>{current_price:,.2f}</strong></div>
            <div><small>当日涨跌幅</small><strong style="color:{price_color}">{price_pct:+.2f}%</strong></div>
            <div><small>今日信号</small><strong style="color:{signal_color}">{current_signal_type}</strong></div>
            <div><small>回测净值截至</small><strong>{nav_latest_date:%Y-%m-%d}</strong></div>
        </div>
    """)
    market_col, chart_col = st.columns([1, 1.65], gap="medium")
    with market_col:
        with st.container(key="nav_panel"):
            st.markdown(f"""
            <div class="backtest-header">
                <div class="backtest-title">回测净值</div>
                <div class="backtest-mode">默认近 60 个交易日 · {nav_mode_text}</div>
            </div>
            """, unsafe_allow_html=True)
            st.plotly_chart(
                fig_nav,
                theme=None,
                width="stretch",
                config={
                    "displayModeBar": True, "displaylogo": False, "scrollZoom": True,
                    "doubleClick": "reset",
                    "modeBarButtonsToRemove": ["select2d", "lasso2d", "toImage"],
                },
                key="backtest_nav_chart",
            )
            st.markdown(f"""
            <table class="performance-table">
                <thead><tr><th>组合</th><th>最大回撤</th></tr></thead>
                <tbody>{performance_table}</tbody>
            </table>
            """, unsafe_allow_html=True)
    with chart_col, st.container(key="price_panel"):
        st.html('<div class="backtest-header"><span class="backtest-title">中证全指行情</span>'
                '<span class="backtest-mode">近 120 个交易日</span></div>')
        st.plotly_chart(fig_k, width="stretch", theme=None)

    # --- Task 1 & 2: History Trace with Benchmarking ---
    st.markdown("### 信号与组合表现")
    
    overall_last_sig_date, overall_last_sig_type = None, "N/A"
    for s in [_LABEL_L1, _LABEL_L2]:
        valid_res = [r for r in results[s] if r['date'] <= latest_date_dt]
        if valid_res:
            overall_last_sig_date, overall_last_sig_type = valid_res[-1]['date'], valid_res[-1]['type']
            break

    idx_ret = 0.0
    if overall_last_sig_date:
        st.markdown(f"""
        <div style="padding: 12px 20px; margin-bottom: 5px; background-color: #192636; border: 1px solid #283647; border-radius: 8px; text-align: center;">
            <span style="color: #97a8bb;">触发日期: <b style="color: #e9f0f7;">{overall_last_sig_date.strftime('%Y-%m-%d')}</b></span>
            <span style="color: #283647; margin: 0 15px;">|</span>
            <span style="color: #97a8bb;">信号类型: <b style="color: #80bff1;">{signal_label(overall_last_sig_type)}</b></span>
        </div>
        """, unsafe_allow_html=True)
        
        # 计算中证同期
        idx_after_sig = index_data.loc[overall_last_sig_date:]
        if len(idx_after_sig) > 1:
            i_target_idx = min(20, len(idx_after_sig) - 1)
            idx_ret = (idx_after_sig['close'].iloc[i_target_idx] / idx_after_sig['close'].iloc[0] - 1) * 100
            bench_color = "#ef777d" if idx_ret >= 0 else "#48cba4"
            st.markdown(f'<div style="text-align: right; font-size: 0.8em; margin-bottom: 15px; color: #97a8bb; padding-right:10px;">同期{_LABEL_BENCHMARK}表现: <span style="color: {bench_color}; font-weight: bold;">{idx_ret:+.2f}%</span> (20D或至今)</div>', unsafe_allow_html=True)

    h_col1, h_col2 = st.columns(2)
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
        return "#ef777d" if v > 0 else "#48cba4" if v < 0 else "#97a8bb"

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
                <div style="padding: 12px 14px; margin-bottom: 12px; background: #131c27; border: 1px solid #283647; border-radius: 8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <span style="color:#e9f0f7; font-size:0.9em; font-weight:600;">历史组合表现</span>
                        <span style="color:#97a8bb; font-size:0.75em;">样本 {n_total} 次</span>
                    </div>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px;">
                        <div style="background-color:#192636; padding:10px; border-radius:6px; border-left:3px solid #80bff1;">
                            <div style="color:#97a8bb; font-size:0.72em; margin-bottom:4px;">多头 {tag} · 平均</div>
                            <div style="color:{_color(lg_avg)}; font-weight:bold; font-size:1.1em;">{lg_avg:+.2f}%</div>
                            <div style="color:#97a8bb; font-size:0.7em; margin-top:2px;">超额 <span style="color:{_color(lg_ex)};">{lg_ex:+.2f}%</span> · 胜率 <span style="color:#e9f0f7;">{lg_wr:.0f}%</span></div>
                        </div>
                        <div style="background-color:#192636; padding:10px; border-radius:6px; border-left:3px solid #b1bfd0;">
                            <div style="color:#97a8bb; font-size:0.72em; margin-bottom:4px;">多空 {tag} · 平均</div>
                            <div style="color:{_color(ls_avg)}; font-weight:bold; font-size:1.1em;">{ls_avg:+.2f}%</div>
                            <div style="color:#97a8bb; font-size:0.7em; margin-top:2px;">胜率 <span style="color:#e9f0f7;">{ls_wr:.0f}%</span></div>
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
                <div style="padding:10px 14px; margin-bottom:8px; background:#131c27; border:1px solid #283647; border-left:3px solid #80bff1; border-radius:6px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                        <div style="color:#e9f0f7; font-size:0.85em;">
                            <span style="color:#97a8bb;">最近触发</span>
                            &nbsp;<b>{last_sig['date'].strftime('%Y-%m-%d')}</b>
                            &nbsp;·&nbsp;<span style="color:#80bff1;">{signal_label(last_sig['type'])}</span>
                        </div>
                        <span style="color:#97a8bb; font-size:0.72em;">当期 · 持有 {hold_days}D</span>
                    </div>
                    <div style="display:grid; grid-template-columns:repeat(2,1fr); gap:6px;">
                        <div style="background-color:#192636; padding:8px 6px; border-radius:4px; text-align:center; border-left:2px solid #80bff1;">
                            <div style="color:#97a8bb; font-size:0.7em;">多头 {tag}</div>
                            <div style="color:{_color(cur_long)}; font-weight:bold; font-size:1em;">{cur_long:+.2f}%</div>
                            <div style="color:#97a8bb; font-size:0.68em; margin-top:2px;">超额 <span style="color:{_color(cur_long_ex)};">{cur_long_ex:+.2f}%</span></div>
                        </div>
                        <div style="background-color:#192636; padding:8px 6px; border-radius:4px; text-align:center; border-left:2px solid #b1bfd0;">
                            <div style="color:#97a8bb; font-size:0.7em;">多空 {tag}</div>
                            <div style="color:{_color(cur_ls)}; font-weight:bold; font-size:1em;">{cur_ls:+.2f}%</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                for label, items in (
                    ("前", last_sig["series"].head(top_n)),
                    ("后", last_sig["series"].tail(top_n).sort_values()),
                ):
                    rows = []
                    for name, value in items.items():
                        ret = returns.get(name, 0.0)
                        excess = ret - idx_ret
                        sector_name = escape(str(name).removeprefix("中信行业指数:"))
                        rows.append(
                            f"<td>{sector_name}</td><td>{value:.4f}</td>"
                            f"<td style='color:{_color(ret)}'>{ret:+.2f}%</td>"
                            f"<td style='color:{_color(excess)}'>{excess:+.2f}%</td>"
                        )
                    rank_table(f"因子值{label} {top_n}", ("行业", "因子值", "收益率", "超额收益"), rows)

    # --- 今日行业涨幅 Top10 ---
    st.markdown("### 行业涨跌幅排行")
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
            <div style="padding:10px 12px; margin-bottom:8px; background:#131c27; border:1px solid #283647; border-radius:8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span style="color:#e9f0f7; font-weight:600; font-size:0.9em;">{sector}</span>
                    <span style="color:#97a8bb; font-size:0.72em;">{latest_date_str}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            if top10.empty:
                st.caption("暂无行业涨跌幅数据")
            else:
                for label, items in (("涨幅前十", top10), ("涨幅后十", bottom10)):
                    rows = []
                    for rank, (name, ret) in enumerate(items.items(), start=1):
                        sector_name = escape(str(name).removeprefix("中信行业指数:"))
                        rows.append(
                            f"<td><span class='number'>{rank}</span>{sector_name}</td>"
                            f"<td style='color:{_color(ret)}'>{ret:+.2f}%</td>"
                        )
                    rank_table(label, ("行业", "当日涨跌幅"), rows)
    with st.expander("数据口径"):
        st.caption("2026-08-03 起，行业收益按 iFinD 行情续接 7 月末点位。11 月 23 日采用收盘快照。二级回测中的资产管理Ⅱ按同期唯一成分股续接，信号榜单沿用原有排除规则。")
except Exception as e: st.error(f"Error: {e}")
