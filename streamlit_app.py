"""
台指期 (TXFF) / 台股大盤 (^TWII) 即時多空力道看盤系統 - Streamlit Cloud 版 (streamlit_app.py)
支援手機響應式介面、雙標的切換、自訂更新頻率 (10s/20s/30s/1m/5m) 與 ZigZag 參數微調。
"""

import asyncio
import threading
from datetime import timedelta
from typing import Dict, Tuple

import streamlit as st

from crawler import TaiexMarketService
from indicators import DEFAULT_THRESHOLDS, TIMEFRAME_CONFIG, evaluate_all_timeframes

st.set_page_config(
    page_title="台指期 / 大盤即時多空力道",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 深色看盤室樣式與手機響應式排版
st.markdown(
    """
    <style>
      .stApp {
        background-color: #070b14;
        color: #f8fafc;
      }
      /* 強制所有原生元件文字在手機深色背景上保持高對比亮色 */
      .stRadio label, .stRadio p, .stRadio span,
      .stSelectbox label, .stSelectbox p,
      .stExpander summary, .stExpander summary p, .stExpander label,
      .stButton button, .stCaption, .stCaption p {
        color: #f8fafc !important;
      }
      .stButton button {
        background-color: #1e293b !important;
        border: 1px solid #334155 !important;
      }
      .stButton button:hover {
        background-color: #334155 !important;
      }
      .block-container {
        padding-top: 3.2rem;
        padding-bottom: 1.5rem;
        max-width: 1100px;
      }
      .quote-card {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 14px;
      }
      .matrix-card {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 12px;
        overflow: hidden;
      }
      .tf-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 10px;
        padding: 12px 16px;
        border-bottom: 1px solid #1e293b;
      }
      .tf-row:last-child { border-bottom: none; }
      .tf-col-name { flex: 1 1 110px; min-width: 100px; }
      .tf-col-badge { flex: 0 0 60px; text-align: center; }
      .tf-col-ratio { flex: 1 1 180px; min-width: 160px; }
      .tf-col-bar { flex: 2 1 280px; min-width: 240px; }
      .badge-bull {
        display: inline-block; width: 46px; padding: 3px 0; border-radius: 6px;
        background: #e11d48; color: #fff; font-weight: 700; font-size: 12px; text-align: center;
      }
      .badge-bear {
        display: inline-block; width: 46px; padding: 3px 0; border-radius: 6px;
        background: #059669; color: #fff; font-weight: 700; font-size: 12px; text-align: center;
      }
      .badge-flat {
        display: inline-block; width: 46px; padding: 3px 0; border-radius: 6px;
        background: #334155; color: #e2e8f0; font-weight: 700; font-size: 12px; text-align: center;
      }
      .bar-track {
        position: relative;
        height: 30px;
        background: #020617;
        border: 1px solid #1e293b;
        border-radius: 8px;
        overflow: hidden;
      }
      .bar-zero {
        position: absolute; top: 0; bottom: 0; left: 50%;
        width: 2px; background: rgba(148,163,184,0.8); transform: translateX(-50%); z-index: 2;
      }
      .bar-fill-bull {
        position: absolute; top: 4px; bottom: 4px; left: 50%;
        background: linear-gradient(90deg, #e11d48, #f43f5e);
        border-radius: 0 4px 4px 0;
        display: flex; align-items: center; justify-content: flex-end; padding-right: 6px;
        color: #fff; font-size: 11px; font-weight: 700; white-space: nowrap;
      }
      .bar-fill-bear {
        position: absolute; top: 4px; bottom: 4px; right: 50%;
        background: linear-gradient(270deg, #059669, #10b981);
        border-radius: 4px 0 0 4px;
        display: flex; align-items: center; justify-content: flex-start; padding-left: 6px;
        color: #fff; font-size: 11px; font-weight: 700; white-space: nowrap;
      }
      .bar-weak-bull {
        position: absolute; top: 6px; bottom: 6px; left: 50%; width: 6.25%;
        background: rgba(71,85,105,0.8); border-right: 2px solid #fb7185; border-radius: 0 3px 3px 0;
      }
      .bar-weak-bear {
        position: absolute; top: 6px; bottom: 6px; right: 50%; width: 6.25%;
        background: rgba(71,85,105,0.8); border-left: 2px solid #34d399; border-radius: 3px 0 0 3px;
      }
      .tabular { font-variant-numeric: tabular-nums; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_market_runtime() -> Tuple[TaiexMarketService, asyncio.AbstractEventLoop, threading.Lock]:
    """全域共用爬蟲實例與持久事件迴圈，避免多次 asyncio.run() 造成連線池關閉"""
    loop = asyncio.new_event_loop()
    lock = threading.Lock()
    svc = TaiexMarketService()
    with lock:
        loop.run_until_complete(svc.initialize_history())
    return svc, loop, lock


service, _bg_loop, _loop_lock = get_market_runtime()


def run_async(coro):
    with _loop_lock:
        return _bg_loop.run_until_complete(coro)

# 初始化 ZigZag 閾值狀態
if "thresholds" not in st.session_state:
    st.session_state.thresholds = dict(DEFAULT_THRESHOLDS)

# 頂部控制列（手機自動堆疊）
INTERVAL_OPTIONS = {
    "10 秒": 10,
    "20 秒": 20,
    "30 秒": 30,
    "1 分鐘": 60,
    "5 分鐘": 300,
}

c1, c2, c3 = st.columns([2, 1.5, 1])
with c1:
    target_choice = st.radio(
        "監控標的",
        options=["TXFF", "TWII"],
        format_func=lambda x: "台指期 (TXFF)" if x == "TXFF" else "台股大盤 (^TWII)",
        horizontal=True,
        label_visibility="collapsed",
    )
with c2:
    interval_label = st.selectbox(
        "更新週期",
        options=list(INTERVAL_OPTIONS.keys()),
        index=1,  # 預設 20 秒
        label_visibility="collapsed",
    )
with c3:
    force_clicked = st.button("立即更新", use_container_width=True)

interval_sec = INTERVAL_OPTIONS[interval_label]

# ZigZag 參數微調折疊區
with st.expander("ZigZag 轉折閾值參數 (%)"):
    cols = st.columns(len(TIMEFRAME_CONFIG))
    updated_thresholds: Dict[str, float] = {}
    for idx, cfg in enumerate(TIMEFRAME_CONFIG):
        tf_key = cfg["key"]
        with cols[idx]:
            pct_val = st.number_input(
                cfg["label"],
                min_value=0.05,
                max_value=30.0,
                value=round(st.session_state.thresholds[tf_key] * 100, 2),
                step=0.1,
                key=f"zz_{tf_key}",
            )
            updated_thresholds[tf_key] = pct_val / 100.0
    st.session_state.thresholds = updated_thresholds
    if st.button("恢復預設參數"):
        st.session_state.thresholds = dict(DEFAULT_THRESHOLDS)
        st.rerun()


def _fmt(val) -> str:
    if val is None:
        return "--"
    return f"{val:,.2f}"


@st.fragment(run_every=timedelta(seconds=interval_sec))
def render_live_board(target: str, force: bool) -> None:
    if service.active_target != target:
        run_async(service.set_target(target))
    else:
        run_async(service.poll_realtime_quote(target, force=force))

    snap = run_async(service.get_snapshot())
    quote = snap["quote"]
    timeframes = evaluate_all_timeframes(
        candles_by_tf=snap["candles_by_tf"],
        current_price=quote["price"],
        thresholds=st.session_state.thresholds,
    )

    chg = quote["change"]
    chg_pct = quote["change_pct"]
    sign = "+" if chg > 0 else ""
    price_color = "#fb7185" if chg > 0 else ("#34d399" if chg < 0 else "#f1f5f9")
    status_bg = "rgba(16,185,129,0.18)" if (quote["is_market_open"] and not quote["is_cached"]) else "rgba(245,158,11,0.18)"
    status_fg = "#6ee7b7" if (quote["is_market_open"] and not quote["is_cached"]) else "#fcd34d"

    # 1. 報價總覽卡片
    st.markdown(
        f"""
        <div class="quote-card tabular">
          <div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:8px;">
            <div>
              <span style="font-size:18px; font-weight:800; color:#f8fafc;">{quote['name']}</span>
              <span style="margin-left:8px; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; background:{status_bg}; color:{status_fg};">
                {quote['status_text']}
              </span>
            </div>
            <div style="font-size:12px; color:#94a3b8;">
              資料：{quote['market_time'] or '--'} ｜ 更新：{quote['last_fetched_at'] or '--'} ｜ 每 {interval_sec}s
            </div>
          </div>
          <div style="display:flex; align-items:baseline; gap:14px; margin-top:8px;">
            <span style="font-size:36px; font-weight:900; color:{price_color};">{_fmt(quote['price'])}</span>
            <span style="font-size:18px; font-weight:700; color:{price_color};">{sign}{_fmt(chg)} ({sign}{chg_pct:.2f}%)</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2. 7 週期多空力道矩陣
    rows_html = []
    for tf in timeframes:
        st_info = tf["streak"]
        ratio = tf["ratio"]

        if st_info["status"] == "陽":
            badge = '<span class="badge-bull">陽</span>'
        elif st_info["status"] == "陰":
            badge = '<span class="badge-bear">陰</span>'
        else:
            badge = '<span class="badge-flat">盤整</span>'

        ratio_color = "#fb7185" if ratio["state"] == "breakout" else ("#34d399" if ratio["state"] == "breakdown" else "#f1f5f9")
        abs_c = min(st_info["abs_streak"] or 0, 8)
        w_pct = (abs_c / 8) * 50

        if st_info["streak"] >= 2:
            bar_inner = f'<div class="bar-fill-bull" style="width:{w_pct}%">{st_info["streak_label"]}</div>'
        elif st_info["streak"] <= -2:
            bar_inner = f'<div class="bar-fill-bear" style="width:{w_pct}%">{st_info["streak_label"]}</div>'
        elif st_info["streak"] == 1:
            bar_inner = '<div class="bar-weak-bull"></div><span style="position:absolute;left:57.5%;top:50%;transform:translateY(-50%);font-size:11px;color:#94a3b8;">1 紅</span>'
        elif st_info["streak"] == -1:
            bar_inner = '<div class="bar-weak-bear"></div><span style="position:absolute;right:57.5%;top:50%;transform:translateY(-50%);font-size:11px;color:#94a3b8;">1 黑</span>'
        else:
            bar_inner = '<span style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-size:11px;color:#64748b;">十字盤整</span>'

        row_str = (
            f'<div class="tf-row tabular">'
            f'<div class="tf-col-name">'
            f'<span style="font-size:16px;font-weight:700;color:#f1f5f9;">{tf["label"]}</span>'
            f'<span style="margin-left:6px;font-size:11px;color:#94a3b8;background:#1e293b;padding:1px 6px;border-radius:4px;">ZZ {tf["threshold_pct"]}%</span>'
            f'</div>'
            f'<div class="tf-col-badge">{badge}</div>'
            f'<div class="tf-col-ratio">'
            f'<div style="font-size:15px;font-weight:700;color:{ratio_color};">{ratio["display"]}</div>'
            f'<div style="font-size:11px;color:#94a3b8;">前高 {_fmt(tf["swing_high"])} / 前低 {_fmt(tf["swing_low"])}</div>'
            f'</div>'
            f'<div class="tf-col-bar"><div class="bar-track"><div class="bar-zero"></div>{bar_inner}</div></div>'
            f'</div>'
        )
        rows_html.append(row_str)

    st.markdown(f'<div class="matrix-card">{"".join(rows_html)}</div>', unsafe_allow_html=True)
    st.caption(f"資料來源：{quote['source']} ｜ 十字線延續前一根 K 線顏色 ｜ 前波位置 = (現價 - 前波底) / (前波高 - 前波底)")


render_live_board(target_choice, force_clicked)
