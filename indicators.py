"""
核心量化計算模組 (indicators.py)
包含：
1. 7 個時框的定義與預設 ZigZag 轉折閾值
2. 標準 ZigZag 轉折點演算法（找出最近確認的前波高點與前波低點）
3. 高低點比例位置公式與邊界處理
4. 多空陰陽狀態判定（十字線延續前一根顏色、連續紅/黑 K 計數）
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 7 個時框設定（由短到長排列，對應前端顯示順序）
TIMEFRAME_CONFIG: List[Dict[str, Any]] = [
    {"key": "5m", "label": "5分", "default_threshold": 0.003},    # 0.3%
    {"key": "15m", "label": "15分", "default_threshold": 0.004},  # 0.4%
    {"key": "30m", "label": "30分", "default_threshold": 0.005},  # 0.5%
    {"key": "60m", "label": "時", "default_threshold": 0.008},    # 0.8%
    {"key": "1d", "label": "日", "default_threshold": 0.030},     # 3.0%
    {"key": "1wk", "label": "週", "default_threshold": 0.040},    # 4.0%
    {"key": "1mo", "label": "月", "default_threshold": 0.050},    # 5.0%
]

DEFAULT_THRESHOLDS: Dict[str, float] = {
    item["key"]: item["default_threshold"] for item in TIMEFRAME_CONFIG
}


def calculate_zigzag_pivots(
    candles: List[Dict[str, Any]],
    threshold: float = 0.03,
) -> Tuple[Optional[float], Optional[float], bool]:
    """
    標準百分比 ZigZag 轉折點演算法。

    參數：
        candles: K 線列表，每根需包含 'high', 'low', 'timestamp'
        threshold: 轉折閾值（小數表示，例如 0.03 代表 3%，0.005 代表 0.5%）

    回傳：
        (swing_high, swing_low, is_fallback)
        - swing_high: 最近一次「已確認」的前波高點
        - swing_low: 最近一次「已確認」的前波低點
        - is_fallback: 若歷史資料內尚未形成完整高低轉折，則以區間極值代替並標記 True
    """
    if not candles:
        return None, None, True

    if len(candles) < 2:
        return candles[0]["high"], candles[0]["low"], True

    # 確保閾值為正數，避免除以 0 或無窮迴圈
    threshold = max(float(threshold), 0.0001)

    # 轉折點紀錄列表：{"type": "H" | "L", "price": float, "index": int}
    confirmed_pivots: List[Dict[str, Any]] = []

    # 初始階段：先找出第一段超過閾值的方向，確立初始趨勢 (1=向上找高點, -1=向下找低點)
    trend = 0
    init_high = candles[0]["high"]
    init_high_idx = 0
    init_low = candles[0]["low"]
    init_low_idx = 0

    last_pivot_price = 0.0
    last_pivot_idx = 0

    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]

        if trend == 0:
            if h > init_high:
                init_high = h
                init_high_idx = i
            if l < init_low:
                init_low = l
                init_low_idx = i

            # 檢查是否從初始高點回檔超過閾值 -> 確認初始高點，轉為向下找低點
            if init_high > 0 and (init_high - l) / init_high >= threshold and init_high_idx < i:
                confirmed_pivots.append({"type": "H", "price": init_high, "index": init_high_idx})
                trend = -1
                last_pivot_price = l
                last_pivot_idx = i
            # 檢查是否從初始低點反彈超過閾值 -> 確認初始低點，轉為向上找高點
            elif init_low > 0 and (h - init_low) / init_low >= threshold and init_low_idx < i:
                confirmed_pivots.append({"type": "L", "price": init_low, "index": init_low_idx})
                trend = 1
                last_pivot_price = h
                last_pivot_idx = i

        elif trend == 1:
            # 目前處於上升段，持續追蹤更高點
            if h >= last_pivot_price:
                last_pivot_price = h
                last_pivot_idx = i
            # 若從目前追蹤高點回檔超過閾值，則「確認」該高點為 Swing High，並反轉為下降段
            elif last_pivot_price > 0 and (last_pivot_price - l) / last_pivot_price >= threshold:
                confirmed_pivots.append({"type": "H", "price": last_pivot_price, "index": last_pivot_idx})
                trend = -1
                last_pivot_price = l
                last_pivot_idx = i

        elif trend == -1:
            # 目前處於下降段，持續追蹤更低點
            if l <= last_pivot_price:
                last_pivot_price = l
                last_pivot_idx = i
            # 若從目前追蹤低點反彈超過閾值，則「確認」該低點為 Swing Low，並反轉為上升段
            elif last_pivot_price > 0 and (h - last_pivot_price) / last_pivot_price >= threshold:
                confirmed_pivots.append({"type": "L", "price": last_pivot_price, "index": last_pivot_idx})
                trend = 1
                last_pivot_price = h
                last_pivot_idx = i

    # 從已確認的轉折點中，由後往前找出最近的 Swing High 與 Swing Low
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None

    for p in reversed(confirmed_pivots):
        if p["type"] == "H" and swing_high is None:
            swing_high = p["price"]
        elif p["type"] == "L" and swing_low is None:
            swing_low = p["price"]
        if swing_high is not None and swing_low is not None:
            break

    # 邊界保護：若閾值過大導致某側尚未出現確認轉折點，改用區間極值並標記 fallback
    is_fallback = False
    if swing_high is None:
        swing_high = max(c["high"] for c in candles)
        is_fallback = True
    if swing_low is None:
        swing_low = min(c["low"] for c in candles)
        is_fallback = True

    return round(swing_high, 2), round(swing_low, 2), is_fallback


def calculate_price_ratio(
    current_price: float,
    swing_high: Optional[float],
    swing_low: Optional[float],
) -> Dict[str, Any]:
    """
    計算目前點位在最近前波區間的位置比例：
        位置 = (目前點位 - 前波底) / (前波高 - 前波底)

    結果以百分比呈現：
    - 0% ~ 100%：位於前波底～前波高區間內（例如 50.0% 代表中軸）
    - > 100%：突破前波高，顯示「XXX.X% (創高)」
    - < 0%：跌破前波底，顯示「-XX.X% (破底)」
    """
    if swing_high is None or swing_low is None or current_price <= 0:
        return {
            "value": None,
            "display": "--",
            "state": "unknown",
        }

    span = swing_high - swing_low
    if span <= 0:
        return {
            "value": None,
            "display": "--",
            "state": "unknown",
        }

    pos = (current_price - swing_low) / span
    pct = pos * 100.0

    if pos > 1.0:
        return {
            "value": round(pos, 4),
            "display": f"{pct:.1f}% (創高)",
            "state": "breakout",
        }

    if pos < 0.0:
        return {
            "value": round(pos, 4),
            "display": f"{pct:.1f}% (破底)",
            "state": "breakdown",
        }

    return {
        "value": round(pos, 4),
        "display": f"{pct:.1f}%",
        "state": "normal",
    }


def calculate_candle_streak(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    多空陰陽狀態判定：
    - 收盤 > 開盤：紅 K (+1)
    - 收盤 < 開盤：黑 K (-1)
    - 收盤 == 開盤（十字線）：延續前一根 K 線顏色狀態

    判定規則：
    - 連續 2 根以上紅 K (>= +2)：【陽】
    - 連續 2 根以上黑 K (<= -2)：【陰】
    - 僅 1 根紅 K 或 1 根黑 K (+1 / -1)：【盤整】
    """
    if not candles:
        return {
            "streak": 0,
            "abs_streak": 0,
            "status": "盤整",
            "direction": "neutral",
            "streak_label": "無資料",
        }

    # 先由舊到新計算每根 K 線的等效顏色 (+1, -1, 0)
    states: List[int] = []
    prev_state = 0
    for c in candles:
        o = c["open"]
        cl = c["close"]
        if cl > o:
            state = 1
        elif cl < o:
            state = -1
        else:
            # 十字線延續前一根顏色
            state = prev_state
        states.append(state)
        if state != 0:
            prev_state = state

    latest_state = states[-1]
    if latest_state == 0:
        return {
            "streak": 0,
            "abs_streak": 0,
            "status": "盤整",
            "direction": "neutral",
            "streak_label": "十字盤整",
        }

    # 由最新一根 K 線往回計算連續同色根數
    count = 0
    for s in reversed(states):
        if s == latest_state:
            count += 1
        else:
            break

    signed_streak = count if latest_state == 1 else -count

    if signed_streak >= 2:
        status = "陽"
        direction = "bull"
        streak_label = f"連 {count} 紅"
    elif signed_streak <= -2:
        status = "陰"
        direction = "bear"
        streak_label = f"連 {count} 黑"
    else:
        status = "盤整"
        direction = "bull_weak" if signed_streak == 1 else "bear_weak"
        streak_label = "1 紅" if signed_streak == 1 else "1 黑"

    return {
        "streak": signed_streak,
        "abs_streak": count,
        "status": status,
        "direction": direction,
        "streak_label": streak_label,
    }


def _candle_metrics(c: Dict[str, Any]) -> Dict[str, float]:
    """計算單根 K 線實體、上下影線與振幅"""
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body = abs(cl - o)
    rng = max(h - l, 0.0)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l
    return {
        "body": body,
        "rng": rng,
        "upper": upper_wick,
        "lower": lower_wick,
        "is_bull": cl > o,
        "is_bear": cl < o,
    }


def _detect_reversal_at_tip(
    candles: List[Dict[str, Any]],
    ratio_info: Dict[str, Any],
    swing_high: Optional[float],
    swing_low: Optional[float],
) -> Dict[str, Any]:
    """檢查 candles 最後一根位置是否符合底部轉陽或高檔轉陰反轉型態（單點判定）"""
    pos = ratio_info.get("value")
    if not candles or pos is None or swing_high is None or swing_low is None:
        return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False}

    span = swing_high - swing_low
    c1 = candles[-1]
    recent_low = min(c["low"] for c in candles[-2:]) if len(candles) >= 2 else c1["low"]
    recent_high = max(c["high"] for c in candles[-2:]) if len(candles) >= 2 else c1["high"]
    low_pos = (recent_low - swing_low) / span if span > 0 else pos
    high_pos = (recent_high - swing_low) / span if span > 0 else pos

    # 底部附近：現價在 -20%~25%，或近兩根最低點有觸及底部區 (<=15%) 且現價尚未反彈超過 45%
    is_near_bottom = (-0.20 <= pos <= 0.25) or (-0.30 <= low_pos <= 0.15 and pos <= 0.45)
    # 高檔附近：現價在 75%~120%，或近兩根最高點有觸及高檔區 (>=85%) 且現價尚未回落低於 55%
    is_near_top = (0.75 <= pos <= 1.20) or (0.85 <= high_pos <= 1.30 and pos >= 0.55)

    if not is_near_bottom and not is_near_top:
        return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False}

    m1 = _candle_metrics(c1)
    c1_top, c1_bot = max(c1["open"], c1["close"]), min(c1["open"], c1["close"])

    if is_near_bottom:
        # 1. 多頭島型反轉（太極陰陽）：只看實體不管影線與紅黑，中間實體須有明顯向下跳空缺口 (>= max(15點, 0.05%))
        if len(candles) >= 3:
            c2, c3 = candles[-2], candles[-3]
            c2_top = max(c2["open"], c2["close"])
            c3_bot = min(c3["open"], c3["close"])
            min_gap = max(15.0, 0.0005 * c1["close"])
            if (c3_bot - c2_top) >= min_gap and (c1_bot - c2_top) >= min_gap:
                return {"signal": "bull_reversal", "pattern": "多頭島型反轉", "zone": "bottom", "is_taiji": True}

        if len(candles) >= 2:
            c2 = candles[-2]
            m2 = _candle_metrics(c2)
            c2_top, c2_bot = max(c2["open"], c2["close"]), min(c2["open"], c2["close"])
            # 2. 多頭吞噬（太極陰陽）：後陽實體「嚴格完全包含」前陰實體（或前一根破底後被後陽實體完全吞噬）
            if m1["is_bull"] and ((m2["is_bear"] and c1["open"] < c2["close"] and c1["close"] > c2["open"]) or (c2["low"] < swing_low and c1["open"] <= c2_bot and c1["close"] > c2_top)):
                return {"signal": "bull_reversal", "pattern": "多頭吞噬", "zone": "bottom", "is_taiji": True}
            # 3. 多頭貫穿（太極陰陽）：後陽實體由下而上從底部貫穿前陰實體超過一半以上
            c2_mid = (c2["open"] + c2["close"]) / 2.0
            if m2["is_bear"] and m1["is_bull"] and c1["open"] < c2["close"] and c1["close"] > c2_mid:
                return {"signal": "bull_reversal", "pattern": "多頭貫穿", "zone": "bottom", "is_taiji": True}
            # 4. 破底翻：跌破前波低後迅速站回前波低之上
            if c2["low"] < swing_low and c1["close"] > swing_low and m1["is_bull"]:
                return {"signal": "bull_reversal", "pattern": "破底翻", "zone": "bottom", "is_taiji": False}

        if c1["low"] < swing_low and c1["close"] > swing_low:
            return {"signal": "bull_reversal", "pattern": "破底翻", "zone": "bottom", "is_taiji": False}

        # 5. 鎚子（太極陰陽）：實體很小且在最頂端（不管紅黑），下影線 >= 實體兩倍，幾乎無上影線
        if (
            m1["rng"] > 0
            and m1["body"] <= 0.35 * m1["rng"]
            and m1["lower"] >= 2.0 * max(m1["body"], m1["rng"] * 0.05)
            and m1["upper"] <= 0.15 * m1["rng"]
        ):
            return {"signal": "bull_reversal", "pattern": "鎚子", "zone": "bottom", "is_taiji": True}

        return {"signal": "none", "pattern": None, "zone": "bottom", "is_taiji": False}

    # is_near_top: 轉陰（頭部型態）-> 綠色亮燈
    # 1. 空頭島型反轉（太極陰陽）：只看實體不管影線與紅黑，中間實體須有明顯向上跳空缺口 (>= max(15點, 0.05%))
    if len(candles) >= 3:
        c2, c3 = candles[-2], candles[-3]
        c2_bot = min(c2["open"], c2["close"])
        c3_top = max(c3["open"], c3["close"])
        min_gap = max(15.0, 0.0005 * c1["close"])
        if (c2_bot - c3_top) >= min_gap and (c2_bot - c1_top) >= min_gap:
            return {"signal": "bear_reversal", "pattern": "空頭島型反轉", "zone": "top", "is_taiji": True}

    if len(candles) >= 2:
        c2 = candles[-2]
        m2 = _candle_metrics(c2)
        c2_top, c2_bot = max(c2["open"], c2["close"]), min(c2["open"], c2["close"])
        # 2. 空頭吞噬（太極陰陽）：後陰實體「嚴格完全包含」前陽實體（或前一根創高後被後陰實體完全吞噬）
        if m1["is_bear"] and ((m2["is_bull"] and c1["open"] > c2["close"] and c1["close"] < c2["open"]) or (c2["high"] > swing_high and c1["open"] >= c2_top and c1["close"] < c2_bot)):
            return {"signal": "bear_reversal", "pattern": "空頭吞噬", "zone": "top", "is_taiji": True}
        # 3. 烏雲罩日（太極陰陽）：後陰實體由上而下從頂部罩住前陽實體超過一半以上
        c2_mid = (c2["open"] + c2["close"]) / 2.0
        if m2["is_bull"] and m1["is_bear"] and c1["open"] > c2["close"] and c1["close"] < c2_mid:
            return {"signal": "bear_reversal", "pattern": "烏雲罩日", "zone": "top", "is_taiji": True}
        # 4. 假突破翻黑
        if c2["high"] > swing_high and c1["close"] < swing_high and m1["is_bear"]:
            return {"signal": "bear_reversal", "pattern": "假突破翻黑", "zone": "top", "is_taiji": False}

    if c1["high"] > swing_high and c1["close"] < swing_high:
        return {"signal": "bear_reversal", "pattern": "假突破翻黑", "zone": "top", "is_taiji": False}

    # 4. 流星（實體在最底端，不管紅黑）/ 5. 吊人（同鎚子，出現在頭部）
    if m1["rng"] > 0 and m1["body"] <= 0.35 * m1["rng"]:
        if m1["upper"] >= 2.0 * max(m1["body"], m1["rng"] * 0.05) and m1["lower"] <= 0.15 * m1["rng"]:
            return {"signal": "bear_reversal", "pattern": "流星", "zone": "top", "is_taiji": True}
        if m1["lower"] >= 2.0 * max(m1["body"], m1["rng"] * 0.05) and m1["upper"] <= 0.15 * m1["rng"]:
            return {"signal": "bear_reversal", "pattern": "吊人", "zone": "top", "is_taiji": True}

    return {"signal": "none", "pattern": None, "zone": "top", "is_taiji": False}


def _is_candle_completed(candle_ts: int, tf_key: str, now_ts: Optional[int] = None) -> bool:
    """判斷最新一根 K 線是否已經收線完成（未完成則不參與反轉型態亮燈判定）"""
    now_dt = datetime.fromtimestamp(now_ts, tz=TAIPEI_TZ) if now_ts else datetime.now(TAIPEI_TZ)
    c_dt = datetime.fromtimestamp(candle_ts, tz=TAIPEI_TZ)

    if tf_key in ("5m", "15m", "30m", "60m"):
        sec = int(tf_key.replace("m", "")) * 60
        return int(now_dt.timestamp()) >= (candle_ts + sec)
    if tf_key == "1d":
        if now_dt.date() > c_dt.date():
            return True
        if now_dt.date() == c_dt.date():
            return (now_dt.hour * 60 + now_dt.minute) >= (13 * 60 + 45)
        return False
    if tf_key == "1wk":
        return now_dt.isocalendar()[:2] > c_dt.isocalendar()[:2]
    if tf_key == "1mo":
        return (now_dt.year, now_dt.month) > (c_dt.year, c_dt.month)
    return True


def detect_reversal_signal(
    candles: List[Dict[str, Any]],
    ratio_info: Dict[str, Any],
    swing_high: Optional[float],
    swing_low: Optional[float],
    lookback_bars: int = 6,
    tf_key: Optional[str] = None,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """
    掃描最近 6T（6 根已收線完成的 K 線）的反轉訊號並實作保留機制：
    - 若最新一根 K 線尚未收線完成，自動排除之，待該根 K 線時間完成確認後才亮燈。
    - 亮燈保留最多 6T，直到「超過 6T」或「波浪觸及對向頂/底（>=75% 或 <=25%）」為止。
    - 若 6T 內重複出現同向反轉型態，回傳 repeat_count 供前端於旁邊顯示次數（如 ×2）。
    """
    if not candles or swing_high is None or swing_low is None or swing_high <= swing_low:
        return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False, "repeat_count": 0, "bars_ago": 0}

    # 若指定 tf_key 且最新一根尚未收線完成，僅以已完成的 K 線判定型態
    eval_candles = candles
    if tf_key and not _is_candle_completed(candles[-1]["timestamp"], tf_key, now_ts):
        eval_candles = candles[:-1]

    if not eval_candles:
        return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False, "repeat_count": 0, "bars_ago": 0}

    span = swing_high - swing_low
    max_offset = min(lookback_bars - 1, len(eval_candles) - 1)
    hits: List[Dict[str, Any]] = []

    for offset in range(0, max_offset + 1):
        sub = eval_candles[: len(eval_candles) - offset]
        tip_price = sub[-1]["close"]
        sub_ratio = calculate_price_ratio(tip_price, swing_high, swing_low) if (offset > 0 or len(eval_candles) < len(candles)) else ratio_info
        sig = _detect_reversal_at_tip(sub, sub_ratio, swing_high, swing_low)
        if sig["signal"] != "none":
            hits.append({**sig, "offset": offset})

    if not hits:
        return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False, "repeat_count": 0, "bars_ago": 0}

    latest = hits[0]
    target_sig = latest["signal"]
    latest_offset = latest["offset"]

    # 檢查觸發該訊號之後（含最新未完成 K 線）是否已「波浪觸頂/觸底反轉」
    signal_end_idx = len(eval_candles) - latest_offset
    after_candles = candles[signal_end_idx:]
    if after_candles:
        if target_sig == "bull_reversal":
            if any((c["high"] - swing_low) / span >= 0.75 for c in after_candles):
                return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False, "repeat_count": 0, "bars_ago": 0}
        elif target_sig == "bear_reversal":
            if any((c["low"] - swing_low) / span <= 0.25 for c in after_candles):
                return {"signal": "none", "pattern": None, "zone": "mid", "is_taiji": False, "repeat_count": 0, "bars_ago": 0}

    # 計算 6T 內同向重複出現次數（遇到反向訊號即停止累計）
    repeat_count = 0
    for h in hits:
        if h["signal"] == target_sig:
            repeat_count += 1
        else:
            break

    return {
        "signal": latest["signal"],
        "pattern": latest["pattern"],
        "zone": latest["zone"],
        "is_taiji": latest["is_taiji"],
        "repeat_count": repeat_count,
        "bars_ago": latest_offset,
    }


def evaluate_taiji_resonance(timeframes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    依太極陰陽學「操作要領」判定 週K 與 日K 共振信號：
    - 週(+) and 日(+)：快買進
    - 週(-) and 日(-)：快賣出
    - 週(+) and 日(-)：賤價買
    - 週(-) and 日(+)：反彈謹慎 / 偏空看待
    """
    tf_map = {tf["timeframe"]: tf["streak"]["streak"] for tf in timeframes}
    wk_s = tf_map.get("1wk", 0)
    d_s = tf_map.get("1d", 0)
    wk_sign = 1 if wk_s > 0 else (-1 if wk_s < 0 else 0)
    d_sign = 1 if d_s > 0 else (-1 if d_s < 0 else 0)

    if wk_s > 0 and d_s > 0:
        return {"code": "buy_fast", "action": "快買進", "label": "週(+) 日(+)：快買進", "tone": "bull", "wk_sign": wk_sign, "d_sign": d_sign}
    if wk_s < 0 and d_s < 0:
        return {"code": "sell_fast", "action": "快賣出", "label": "週(-) 日(-)：快賣出", "tone": "bear", "wk_sign": wk_sign, "d_sign": d_sign}
    if wk_s > 0 and d_s < 0:
        return {"code": "buy_dip", "action": "賤價買", "label": "週(+) 日(-)：賤價買", "tone": "warn", "wk_sign": wk_sign, "d_sign": d_sign}
    if wk_s < 0 and d_s > 0:
        return {"code": "rebound_caution", "action": "反彈謹慎", "label": "週(-) 日(+)：反彈謹慎", "tone": "neutral", "wk_sign": wk_sign, "d_sign": d_sign}
    return {"code": "neutral", "action": "盤整觀望", "label": "週/日盤整觀望", "tone": "neutral", "wk_sign": wk_sign, "d_sign": d_sign}


def evaluate_all_timeframes(
    candles_by_tf: Dict[str, List[Dict[str, Any]]],
    current_price: float,
    thresholds: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """
    針對 7 個時框獨立計算 ZigZag 前波高低點、比例位置與多空陰陽狀態。
    """
    active_thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    results: List[Dict[str, Any]] = []

    for tf_cfg in TIMEFRAME_CONFIG:
        tf_key = tf_cfg["key"]
        tf_label = tf_cfg["label"]
        threshold = active_thresholds.get(tf_key, tf_cfg["default_threshold"])
        candles = candles_by_tf.get(tf_key, [])

        # 所有指標判定（ZigZag 轉折、連紅/連黑陰陽狀態、反轉型態）皆須等 K 線收線完成才算確認
        confirmed_candles = candles
        if candles and not _is_candle_completed(candles[-1]["timestamp"], tf_key):
            confirmed_candles = candles[:-1]

        swing_high, swing_low, is_fallback = calculate_zigzag_pivots(confirmed_candles, threshold)
        ratio_info = calculate_price_ratio(current_price, swing_high, swing_low)
        streak_info = calculate_candle_streak(confirmed_candles)
        reversal_info = detect_reversal_signal(candles, ratio_info, swing_high, swing_low, tf_key=tf_key)

        latest_candle = confirmed_candles[-1] if confirmed_candles else (candles[-1] if candles else None)

        def _candle_color(c: Dict[str, Any]) -> str:
            if c["close"] > c["open"]:
                return "red"
            if c["close"] < c["open"]:
                return "green"
            return "doji"

        recent_colors = [_candle_color(c) for c in confirmed_candles[-7:]]
        forming_color = _candle_color(candles[-1]) if (candles and len(confirmed_candles) < len(candles)) else None

        results.append({
            "timeframe": tf_key,
            "label": tf_label,
            "threshold_pct": round(threshold * 100, 2),
            "candle_count": len(candles),
            "latest_candle": latest_candle,
            "swing_high": swing_high,
            "swing_low": swing_low,
            "is_fallback_pivot": is_fallback,
            "ratio": ratio_info,
            "streak": streak_info,
            "reversal": reversal_info,
            "recent_colors": recent_colors,
            "forming_color": forming_color,
        })

    return results
