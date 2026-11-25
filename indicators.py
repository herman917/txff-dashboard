"""
核心量化計算模組 (indicators.py)
包含：
1. 7 個時框的定義與預設 ZigZag 轉折閾值
2. 標準 ZigZag 轉折點演算法（找出最近確認的前波高點與前波低點）
3. 高低點比例位置公式與邊界處理
4. 多空陰陽狀態判定（十字線延續前一根顏色、連續紅/黑 K 計數）
"""

from typing import Any, Dict, List, Optional, Tuple

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

        swing_high, swing_low, is_fallback = calculate_zigzag_pivots(candles, threshold)
        ratio_info = calculate_price_ratio(current_price, swing_high, swing_low)
        streak_info = calculate_candle_streak(candles)

        latest_candle = candles[-1] if candles else None

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
        })

    return results
