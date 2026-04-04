import streamlit as st
import pandas as pd
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
    /* Main Background */
    .stApp {
        background-color: #0d1117;
        color: #c9d1d9;
    }
    
    /* Card Styling */
    .metric-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 20px;
        margin-bottom: 20px;
    }
    
    .column-label {
        color: #8b949e;
        font-size: 0.9em;
    }
    
    .value-label {
        font-size: 1.5em;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

# --- Data Loading & Analysis ---
@st.cache_data(ttl=3600)
def get_analysis_results():
    analyzer = GXPitMomActions(data_dir='./data/')
    index_data = analyzer.dp.get_wide_table('000985_prices.xlsx')
    zx_yj_prices = analyzer.dp.get_wide_table('ZX_YJHY.xlsx')
    zx_ej_prices = analyzer.dp.get_wide_table('ZX_EJHY.xlsx')
    
    sig_breakout = analyzer.gx_pit_breakout(index_data)
    sig_rebound = analyzer.gx_pit_rebound(index_data)
    sig_rotation = analyzer.gx_pit_rotation(index_data, zx_yj_prices)
    
    signals_dict = {
        'breakout': sig_breakout,
        'rebound': sig_rebound,
        'rotation': sig_rotation,
    }
    
    results = {
        '中信一级行业': analyzer.calculate_fused_signals(zx_yj_prices, signals_dict),
        '中信二级行业': analyzer.calculate_fused_signals(zx_ej_prices, signals_dict)
    }
    
    return results, index_data

# --- Main App Logic ---
try:
    results, index_data = get_analysis_results()
    
    if index_data.empty:
        st.warning("📊 暂无中证全指数据。")
        st.stop()
        
    latest_date_dt = index_data.index[-1]
    latest_date_str = latest_date_dt.strftime('%Y-%m-%d')
    current_price = index_data['close'].iloc[-1]
    price_pct = (index_data['close'].pct_change().iloc[-1] * 100)

    # --- Header ---
    st.markdown(f"### 中证全指 <span style='font-size:0.6em; color:#8b949e'>000985.SH | {latest_date_str}</span>", unsafe_allow_html=True)

    # --- Metrics Section ---
    col1, col2, col3 = st.columns(3)

    # Calculate current signal status
    current_has_signal = False
    current_signal_type = "今日无信号"
    for sector in ['中信一级行业', '中信二级行业']:
        if results[sector] and results[sector][-1]['date'].date() == latest_date_dt.date():
            current_has_signal = True
            current_signal_type = f"触发: {results[sector][-1]['type']}"

    with col1:
        st.markdown(f'<div class="metric-card"><div class="column-label">最新价格</div><div class="value-label" style="color:{"#3fb950" if price_pct < 0 else "#f85149"}">{current_price:.2f}</div></div>', unsafe_allow_html=True)
    with col2:
        st.markdown(f'<div class="metric-card"><div class="column-label">日内涨跌</div><div class="value-label" style="color:{"#3fb950" if price_pct < 0 else "#f85149"}">{price_pct:+.2f}%</div></div>', unsafe_allow_html=True)
    with col3:
        st.markdown(f'<div class="metric-card"><div class="column-label">策略信号</div><div class="value-label">{current_signal_type}</div></div>', unsafe_allow_html=True)

    # --- Rank Tables: Factor Sorting (ONLY IF SIGNAL EXISTS) ---
    if current_has_signal:
        st.markdown("### 📊 中信行业动量因子排序")
        tabs = st.tabs(["中信一级", "中信二级"])

        for i, sector in enumerate(['中信一级行业', '中信二级行业']):
            with tabs[i]:
                if results[sector] and results[sector][-1]['date'].date() == latest_date_dt.date():
                    latest_res = results[sector][-1]
                    limit = 5 if sector == '中信一级行业' else 10
                    top_series = latest_res['series'].head(limit)
                    
                    # 采用行式布局显示
                    for name, val in top_series.items():
                        st.markdown(f"""
                        <div style="padding: 8px 15px; margin-bottom: 5px; background-color: #1c2128; border-left: 4px solid #58a6ff; border-radius: 4px;">
                            <span style="color: #8b949e;">中信行业指数:</span> 
                            <b style="color: #c9d1d9;">{name}</b> 
                            <span style="color: #58a6ff; margin-left:10px;">({val:.4f})</span>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.info("该级别行业今日未触发评分。")
    
    # --- History Window: Last Signal Information ---
    st.markdown("### 🕒 历史信号溯源")
    h_col1, h_col2 = st.columns(2)
    
    for i, sector in enumerate(['中信一级行业', '中信二级行业']):
        with [h_col1, h_col2][i]:
            st.markdown(f"**{sector}**")
            # Get the last signal before or on latest_date
            valid_results = [r for r in results[sector] if r['date'] <= latest_date_dt]
            if valid_results:
                # If today has signal, we might want the one BEFORE today, or just the very last one
                last_sig = valid_results[-1]
                
                # 历史回溯也同步限制显示数量
                h_limit = 5 if sector == '中信一级行业' else 10
                top_items = last_sig['series'].head(h_limit)
                
                # 构造历史信号卡片头部信息
                st.markdown(f"""
                <div class="metric-card" style="padding:15px; background-color:#1c2128">
                    <span class="column-label">上一次触发时间:</span> <b style="color:#c9d1d9">{last_sig['date'].strftime('%Y-%m-%d')}</b><br>
                    <span class="column-label">信号类型:</span> <span style="color:#58a6ff">{last_sig['type']}</span>
                </div>
                """, unsafe_allow_html=True)
                
                # 历史行业也采用同样的行式布局
                for name, val in top_items.items():
                    st.markdown(f"""
                    <div style="padding: 5px 12px; margin-bottom: 3px; background-color: #161b22; border-left: 3px solid #30363d; border-radius: 4px; font-size: 0.9em;">
                        <span style="color: #8b949e;">中信行业指数:</span> 
                        <b style="color: #c9d1d9;">{name}</b> 
                        <span style="color: #58a6ff; margin-left:8px;">({val:.4f})</span>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.write("暂无历史信号记录。")

except Exception as e:
    # Handle DLL or other low-level errors gracefully
    if "DLL load failed" in str(e):
        st.error("💻 环境配置异常：检测到系统组件缺失 (DLL Load Failed)。")
        st.info("这通常由于 Conda 环境中 numpy 或 mkl 库冲突引起。请在终端运行下方命令修复：\n`conda install mkl mkl-service` 或使用 pip 强制重装核心库。")
    else:
        st.warning(f"⚠️ 运行提示: {e}")

st.caption(f"Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
