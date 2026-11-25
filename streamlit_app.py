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
from indicators import DEFAULT_THRESHOLDS, TIMEFRAME_CONFIG, evaluate_all_timeframes, evaluate_taiji_resonance

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
        display: inline-block; width: 54px; padding: 4px 0; border-radius: 6px;
        background: #e11d48; color: #fff; font-weight: 800; font-size: 14px; text-align: center;
      }
      .badge-bear {
        display: inline-block; width: 54px; padding: 4px 0; border-radius: 6px;
        background: #059669; color: #fff; font-weight: 800; font-size: 14px; text-align: center;
      }
      .badge-flat {
        display: inline-block; width: 54px; padding: 4px 0; border-radius: 6px;
        background: #334155; color: #f8fafc; font-weight: 800; font-size: 14px; text-align: center;
      }
      .bar-track {
        position: relative;
        height: 34px;
        background: #020617;
        border: 1px solid #1e293b;
        border-radius: 8px;
        overflow: hidden;
      }
      .bar-zero {
        position: absolute; top: 0; bottom: 0; left: 50%;
        width: 2px; background: rgba(203,213,225,0.85); transform: translateX(-50%); z-index: 2;
      }
      .bar-fill-bull {
        position: absolute; top: 4px; bottom: 4px; left: 50%;
        background: linear-gradient(90deg, #e11d48, #f43f5e);
        border-radius: 0 4px 4px 0;
        display: flex; align-items: center; justify-content: flex-end; padding-right: 8px;
        color: #fff; font-size: 13px; font-weight: 800; white-space: nowrap;
      }
      .bar-fill-bear {
        position: absolute; top: 4px; bottom: 4px; right: 50%;
        background: linear-gradient(270deg, #059669, #10b981);
        border-radius: 4px 0 0 4px;
        display: flex; align-items: center; justify-content: flex-start; padding-left: 8px;
        color: #fff; font-size: 13px; font-weight: 800; white-space: nowrap;
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
      @keyframes pulseGlow {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.55; transform: scale(1.18); }
      }
      .rev-bull {
        display: inline-flex; align-items: center; gap: 6px; margin-top: 5px;
        padding: 3px 10px; border-radius: 999px; font-size: 13px; font-weight: 700;
        background: rgba(225,29,72,0.22); color: #fecdd3; border: 1px solid rgba(244,63,94,0.65);
      }
      .rev-bear {
        display: inline-flex; align-items: center; gap: 6px; margin-top: 5px;
        padding: 3px 10px; border-radius: 999px; font-size: 13px; font-weight: 700;
        background: rgba(5,150,105,0.22); color: #a7f3d0; border: 1px solid rgba(16,185,129,0.65);
      }
      .rev-taiji-bull {
        background: linear-gradient(90deg, rgba(225,29,72,0.38), rgba(244,63,94,0.2));
        border: 1.5px solid #fb7185;
        box-shadow: 0 0 12px rgba(244,63,94,0.45);
        color: #fff1f2;
      }
      .rev-taiji-bear {
        background: linear-gradient(90deg, rgba(5,150,105,0.38), rgba(16,185,129,0.2));
        border: 1.5px solid #34d399;
        box-shadow: 0 0 12px rgba(16,185,129,0.45);
        color: #ecfdf5;
      }
      .dot-red {
        width: 8px; height: 8px; border-radius: 50%; background: #f43f5e;
        box-shadow: 0 0 8px #f43f5e; animation: pulseGlow 1.3s infinite;
      }
      .dot-green {
        width: 8px; height: 8px; border-radius: 50%; background: #10b981;
        box-shadow: 0 0 8px #10b981; animation: pulseGlow 1.3s infinite;
      }
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


def _pattern_svg(pattern: str) -> str:
    """回傳對應太極陰陽 / 反轉型態的迷你 K 線與波浪 SVG 圖示 (32x22)"""
    svgs = {
        "鎚子": '<svg width="32" height="22" viewBox="0 0 28 20"><line x1="14" y1="2" x2="14" y2="18" stroke="#fb7185" stroke-width="1.8"/><rect x="10" y="2" width="8" height="5" rx="1" fill="#f43f5e"/></svg>',
        "多頭吞噬": '<svg width="32" height="22" viewBox="0 0 28 20"><rect x="5" y="6" width="6" height="7" rx="1" fill="#10b981"/><line x1="19" y1="1" x2="19" y2="19" stroke="#fb7185" stroke-width="1.5"/><rect x="15" y="2" width="8" height="16" rx="1" fill="#f43f5e"/></svg>',
        "多頭貫穿": '<svg width="32" height="22" viewBox="0 0 28 20"><rect x="5" y="3" width="6" height="12" rx="1" fill="#10b981"/><line x1="2" y1="9" x2="26" y2="9" stroke="#fbbf24" stroke-width="1" stroke-dasharray="2,2"/><rect x="16" y="6" width="7" height="12" rx="1" fill="#f43f5e"/></svg>',
        "多頭島型反轉": '<svg width="32" height="22" viewBox="0 0 28 20"><rect x="2" y="2" width="5" height="6" rx="1" fill="#10b981"/><rect x="11.5" y="13" width="5" height="5" rx="1" fill="#fbbf24"/><rect x="21" y="2" width="5" height="7" rx="1" fill="#f43f5e"/><line x1="1" y1="10.5" x2="27" y2="10.5" stroke="#fbbf24" stroke-width="1" stroke-dasharray="2,2"/></svg>',
        "破底翻": '<svg width="32" height="22" viewBox="0 0 28 20"><line x1="2" y1="12" x2="26" y2="12" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="2,2"/><path d="M3,8 Q10,19 16,13 T25,4" fill="none" stroke="#f43f5e" stroke-width="2" stroke-linecap="round"/><polyline points="21,4 25,4 25,8" fill="none" stroke="#f43f5e" stroke-width="1.8" stroke-linecap="round"/></svg>',
        "流星": '<svg width="32" height="22" viewBox="0 0 28 20"><line x1="14" y1="2" x2="14" y2="18" stroke="#34d399" stroke-width="1.8"/><rect x="10" y="13" width="8" height="5" rx="1" fill="#10b981"/></svg>',
        "吊人": '<svg width="32" height="22" viewBox="0 0 28 20"><line x1="14" y1="2" x2="14" y2="18" stroke="#34d399" stroke-width="1.8"/><rect x="10" y="2" width="8" height="5" rx="1" fill="#10b981"/></svg>',
        "空頭吞噬": '<svg width="32" height="22" viewBox="0 0 28 20"><rect x="5" y="6" width="6" height="7" rx="1" fill="#f43f5e"/><line x1="19" y1="1" x2="19" y2="19" stroke="#34d399" stroke-width="1.5"/><rect x="15" y="2" width="8" height="16" rx="1" fill="#10b981"/></svg>',
        "烏雲罩日": '<svg width="32" height="22" viewBox="0 0 28 20"><rect x="5" y="5" width="6" height="12" rx="1" fill="#f43f5e"/><line x1="2" y1="11" x2="26" y2="11" stroke="#fbbf24" stroke-width="1" stroke-dasharray="2,2"/><rect x="16" y="2" width="7" height="12" rx="1" fill="#10b981"/></svg>',
        "空頭島型反轉": '<svg width="32" height="22" viewBox="0 0 28 20"><rect x="2" y="12" width="5" height="6" rx="1" fill="#f43f5e"/><rect x="11.5" y="2" width="5" height="5" rx="1" fill="#fbbf24"/><rect x="21" y="11" width="5" height="7" rx="1" fill="#10b981"/><line x1="1" y1="9.5" x2="27" y2="9.5" stroke="#fbbf24" stroke-width="1" stroke-dasharray="2,2"/></svg>',
        "假突破翻黑": '<svg width="32" height="22" viewBox="0 0 28 20"><line x1="2" y1="8" x2="26" y2="8" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="2,2"/><path d="M3,12 Q10,1 16,7 T25,16" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round"/><polyline points="21,16 25,16 25,12" fill="none" stroke="#10b981" stroke-width="1.8" stroke-linecap="round"/></svg>',
    }
    return svgs.get(pattern, "")


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
    taiji_res = evaluate_taiji_resonance(timeframes)
    res_colors = {
        "bull": ("rgba(225,29,72,0.22)", "#fda4af", "rgba(244,63,94,0.5)"),
        "bear": ("rgba(5,150,105,0.22)", "#6ee7b7", "rgba(16,185,129,0.5)"),
        "warn": ("rgba(245,158,11,0.22)", "#fde68a", "rgba(245,158,11,0.5)"),
        "neutral": ("rgba(51,65,85,0.4)", "#cbd5e1", "rgba(100,116,139,0.4)"),
    }
    res_bg, res_fg, res_bd = res_colors.get(taiji_res["tone"], res_colors["neutral"])

    def _sign_dot(s: int) -> str:
        if s > 0:
            return '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f43f5e;box-shadow:0 0 8px #f43f5e;vertical-align:middle;margin:0 2px;"></span>'
        if s < 0:
            return '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981;vertical-align:middle;margin:0 2px;"></span>'
        return '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#94a3b8;vertical-align:middle;margin:0 2px;"></span>'

    wk_dot = _sign_dot(taiji_res.get("wk_sign", 0))
    d_dot = _sign_dot(taiji_res.get("d_sign", 0))
    res_action = taiji_res.get("action", "")

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
              <span style="font-size:20px; font-weight:800; color:#ffffff;">{quote['name']}</span>
              <span style="margin-left:8px; padding:3px 10px; border-radius:999px; font-size:13px; font-weight:700; background:{status_bg}; color:{status_fg};">
                {quote['status_text']}
              </span>
            </div>
            <div style="font-size:13.5px; color:#cbd5e1;">
              資料：{quote['market_time'] or '--'} ｜ 更新：{quote['last_fetched_at'] or '--'} ｜ 每 {interval_sec}s
            </div>
          </div>
          <div style="display:flex; flex-wrap:wrap; align-items:baseline; justify-content:space-between; gap:10px; margin-top:8px;">
            <div style="display:flex; align-items:baseline; gap:14px;">
              <span style="font-size:42px; font-weight:900; color:{price_color};">{_fmt(quote['price'])}</span>
              <span style="font-size:20px; font-weight:800; color:{price_color};">{sign}{_fmt(chg)} ({sign}{chg_pct:.2f}%)</span>
            </div>
            <div style="display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:8px; font-size:14px; font-weight:700; background:{res_bg}; color:{res_fg}; border:1px solid {res_bd};">
              <span>☯️ 太極共振：週</span>{wk_dot}<span>日</span>{d_dot}<span>{res_action}</span>
            </div>
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
        ratio_text = ratio["display"].replace(" (創高)", "").replace(" (破底)", "")
        ratio_badge = ""
        if ratio["state"] == "breakout":
            ratio_badge = (
                '<span style="display:inline-flex;align-items:center;gap:3px;margin-left:6px;padding:1px 7px;'
                'border-radius:6px;font-size:12px;font-weight:800;background:rgba(225,29,72,0.22);color:#fda4af;border:1px solid rgba(244,63,94,0.6);">'
                '<svg width="16" height="14" viewBox="0 0 18 14"><line x1="1" y1="9" x2="17" y2="9" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="2,2"/>'
                '<polyline points="3,12 9,6 15,2" fill="none" stroke="#f43f5e" stroke-width="2" stroke-linecap="round"/>'
                '<polyline points="11,2 15,2 15,6" fill="none" stroke="#f43f5e" stroke-width="1.8" stroke-linecap="round"/></svg>'
                '創高</span>'
            )
        elif ratio["state"] == "breakdown":
            ratio_badge = (
                '<span style="display:inline-flex;align-items:center;gap:3px;margin-left:6px;padding:1px 7px;'
                'border-radius:6px;font-size:12px;font-weight:800;background:rgba(5,150,105,0.22);color:#6ee7b7;border:1px solid rgba(16,185,129,0.6);">'
                '<svg width="16" height="14" viewBox="0 0 18 14"><line x1="1" y1="5" x2="17" y2="5" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="2,2"/>'
                '<polyline points="3,2 9,8 15,12" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round"/>'
                '<polyline points="11,12 15,12 15,8" fill="none" stroke="#10b981" stroke-width="1.8" stroke-linecap="round"/></svg>'
                '破底</span>'
            )
        abs_c = min(st_info["abs_streak"] or 0, 8)
        w_pct = (abs_c / 8) * 50

        if st_info["streak"] >= 2:
            bar_inner = f'<div class="bar-fill-bull" style="width:{w_pct}%">{st_info["streak_label"]}</div>'
        elif st_info["streak"] <= -2:
            bar_inner = f'<div class="bar-fill-bear" style="width:{w_pct}%">{st_info["streak_label"]}</div>'
        elif st_info["streak"] == 1:
            bar_inner = '<div class="bar-weak-bull"></div><span style="position:absolute;left:57.5%;top:50%;transform:translateY(-50%);font-size:13px;font-weight:700;color:#e2e8f0;">1 紅</span>'
        elif st_info["streak"] == -1:
            bar_inner = '<div class="bar-weak-bear"></div><span style="position:absolute;right:57.5%;top:50%;transform:translateY(-50%);font-size:13px;font-weight:700;color:#e2e8f0;">1 黑</span>'
        else:
            bar_inner = '<span style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-size:13px;font-weight:600;color:#cbd5e1;">十字盤整</span>'

        rev = tf.get("reversal") or {}
        rev_html = ""
        rep_n = rev.get("repeat_count", 1)
        bars_ago = rev.get("bars_ago", 0)
        rep_badge = f'<span style="margin-left:4px;padding:0 5px;border-radius:4px;background:rgba(255,255,255,0.22);color:#fff;font-weight:900;">×{rep_n}</span>' if rep_n > 1 else ""
        ago_text = f'<span style="margin-left:3px;font-size:11px;opacity:0.85;">({bars_ago}T前)</span>' if bars_ago > 0 else ""

        if rev.get("signal") == "bull_reversal":
            svg_icon = _pattern_svg(rev["pattern"])
            tj_cls = " rev-taiji-bull" if rev.get("is_taiji") else ""
            tj_tag = "☯️ 轉陽：" if rev.get("is_taiji") else "底部反轉："
            rev_html = f'<div class="rev-bull{tj_cls}"><span class="dot-red"></span>{svg_icon}<span>{tj_tag}{rev["pattern"]}{rep_badge}{ago_text}</span></div>'
        elif rev.get("signal") == "bear_reversal":
            svg_icon = _pattern_svg(rev["pattern"])
            tj_cls = " rev-taiji-bear" if rev.get("is_taiji") else ""
            tj_tag = "☯️ 轉陰：" if rev.get("is_taiji") else "高檔反轉："
            rev_html = f'<div class="rev-bear{tj_cls}"><span class="dot-green"></span>{svg_icon}<span>{tj_tag}{rev["pattern"]}{rep_badge}{ago_text}</span></div>'

        # 最近最多 7 根已收線紅黑方塊 + 進行中(未收線) K 線紅黑狀況
        recent_colors = tf.get("recent_colors") or []
        forming_color = tf.get("forming_color")
        color_hex = {"red": "#f43f5e", "green": "#10b981", "doji": "#94a3b8"}
        color_zh = {"red": "紅", "green": "黑", "doji": "十"}
        blocks_html = "".join(
            f'<span title="{color_zh.get(col, "")}" style="display:inline-block;width:16px;height:14px;border-radius:3px;background:{color_hex.get(col, "#64748b")};"></span>'
            for col in recent_colors
        )
        forming_html = ""
        if forming_color:
            fc_hex = color_hex.get(forming_color, "#94a3b8")
            fc_zh = color_zh.get(forming_color, "")
            forming_html = (
                f'<span style="color:#64748b;margin:0 4px;">|</span>'
                f'<span style="display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:700;color:{fc_hex};">'
                f'<span style="display:inline-block;width:16px;height:14px;border-radius:3px;border:2px dashed {fc_hex};background:rgba(255,255,255,0.06);"></span>'
                f'即時({fc_zh})</span>'
            )
        strip_html = (
            f'<div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;margin-top:6px;">'
            f'<span style="font-size:12px;color:#cbd5e1;margin-right:4px;">近{len(recent_colors)}T:</span>'
            f'{blocks_html}{forming_html}'
            f'</div>'
        )

        row_str = (
            f'<div class="tf-row tabular">'
            f'<div class="tf-col-name">'
            f'<span style="font-size:19px;font-weight:800;color:#ffffff;">{tf["label"]}</span>'
            f'<span style="margin-left:6px;font-size:13px;font-weight:600;color:#e2e8f0;background:#1e293b;padding:2px 7px;border-radius:4px;">ZZ {tf["threshold_pct"]}%</span>'
            f'</div>'
            f'<div class="tf-col-badge">{badge}</div>'
            f'<div class="tf-col-ratio">'
            f'<div style="display:flex;align-items:center;flex-wrap:wrap;font-size:18px;font-weight:800;color:{ratio_color};"><span style="font-size:14px;font-weight:600;color:#cbd5e1;margin-right:5px;">距前底:</span>{ratio_text}{ratio_badge}</div>'
            f'<div style="font-size:13.5px;font-weight:500;color:#cbd5e1;margin-top:2px;">前高 <span style="color:#fda4af;font-weight:700;">{_fmt(tf["swing_high"])}</span> / 前低 <span style="color:#6ee7b7;font-weight:700;">{_fmt(tf["swing_low"])}</span></div>'
            f'{rev_html}'
            f'</div>'
            f'<div class="tf-col-bar"><div class="bar-track"><div class="bar-zero"></div>{bar_inner}</div>{strip_html}</div>'
            f'</div>'
        )
        rows_html.append(row_str)

    st.markdown(f'<div class="matrix-card">{"".join(rows_html)}</div>', unsafe_allow_html=True)
    st.caption(f"資料來源：{quote['source']} ｜ 十字線延續前一根 K 線顏色 ｜ 前波位置 = (現價 - 前波底) / (前波高 - 前波底)")


render_live_board(target_choice, force_clicked)
