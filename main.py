"""
台股大盤（加權指數）即時多空力道看盤系統 - FastAPI 後端主程式 (main.py)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from crawler import TaiexMarketService
from indicators import DEFAULT_THRESHOLDS, evaluate_all_timeframes, evaluate_taiji_resonance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("taiex_app")

BASE_DIR = Path(__file__).resolve().parent
market_service = TaiexMarketService()

# 儲存各時框當前的 ZigZag 轉折閾值（小數形式，如 0.03 = 3%），支援執行期動態調整
current_thresholds: Dict[str, float] = dict(DEFAULT_THRESHOLDS)

# 支援的自動更新秒數選項：10秒, 20秒, 30秒, 1分鐘(60秒), 5分鐘(300秒)
ALLOWED_INTERVALS = {10, 20, 30, 60, 300}
poll_interval_sec: int = 20
_interval_changed_event = asyncio.Event()


class ThresholdUpdatePayload(BaseModel):
    """前端傳入的百分比閾值更新格式，例如 {"thresholds": {"5m": 0.3, "1d": 3.0}}"""
    thresholds: Dict[str, float]


class IntervalUpdatePayload(BaseModel):
    """前端傳入的更新頻率（秒）"""
    interval_sec: int


class TargetUpdatePayload(BaseModel):
    """前端傳入的監控標的：'TXFF'、'TWII' 或 'STOCK' 搭配 stock_code"""
    target: str
    stock_code: Optional[str] = None


async def _background_poll_loop() -> None:
    """背景非同步排程：依設定秒數定期抓取最新 TXFF 即時報價並更新 7 週期 K 線"""
    while True:
        try:
            _interval_changed_event.clear()
            try:
                # 若使用者切換更短的更新秒數，可立即喚醒重算等待時間
                await asyncio.wait_for(_interval_changed_event.wait(), timeout=poll_interval_sec)
                continue
            except asyncio.TimeoutError:
                pass
            await market_service.poll_realtime_quote()
        except asyncio.CancelledError:
            logger.info("背景即時報價排程已停止")
            break
        except Exception as exc:
            logger.error(f"背景排程發生未預期錯誤: {exc}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動時先抓取 7 個時框的歷史 K 線基底與 TXFF 即時報價
    await market_service.initialize_history()
    poll_task = asyncio.create_task(_background_poll_loop())
    logger.info("台指期 (TXFF) 即時多空力道看盤系統已啟動，每 20 秒自動更新報價")
    try:
        yield
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await market_service.close()


app = FastAPI(
    title="台股大盤即時多空力道看盤系統",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _build_market_response() -> Dict:
    snapshot = await market_service.get_snapshot()
    quote = snapshot["quote"]
    candles_by_tf = snapshot["candles_by_tf"]

    timeframes = evaluate_all_timeframes(
        candles_by_tf=candles_by_tf,
        current_price=quote["price"],
        thresholds=current_thresholds,
    )

    return {
        "active_target": snapshot["active_target"],
        "quote": quote,
        "taiji_resonance": evaluate_taiji_resonance(timeframes),
        "timeframes": timeframes,
        "poll_interval_sec": poll_interval_sec,
        "thresholds_pct": {k: round(v * 100, 3) for k, v in current_thresholds.items()},
        "default_thresholds_pct": {k: round(v * 100, 3) for k, v in DEFAULT_THRESHOLDS.items()},
    }


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    """提供單頁式前端看盤室介面"""
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/market")
async def get_market_data():
    """取得即時大盤報價與 7 個時框的 ZigZag 比例及多空陰陽力道"""
    return await _build_market_response()


@app.post("/api/refresh")
async def force_refresh():
    """手動立即觸發一次爬蟲更新並回傳最新計算結果"""
    await market_service.poll_realtime_quote(force=True)
    return await _build_market_response()


@app.post("/api/thresholds")
async def update_thresholds(payload: ThresholdUpdatePayload):
    """動態調整各時框 ZigZag 轉折閾值（傳入百分比數值，例如 3.0 代表 3%）"""
    for tf_key, pct_val in payload.thresholds.items():
        if tf_key in current_thresholds:
            # 限制合理範圍 0.05% ~ 30%
            clamped_pct = max(0.05, min(30.0, float(pct_val)))
            current_thresholds[tf_key] = clamped_pct / 100.0
    return await _build_market_response()


@app.post("/api/thresholds/reset")
async def reset_thresholds():
    """將各時框 ZigZag 轉折閾值重設為系統預設值"""
    current_thresholds.update(DEFAULT_THRESHOLDS)
    return await _build_market_response()


@app.post("/api/interval")
async def update_interval(payload: IntervalUpdatePayload):
    """動態設定背景爬蟲與前端同步的更新秒數（10, 20, 30, 60, 300）"""
    global poll_interval_sec
    if payload.interval_sec in ALLOWED_INTERVALS:
        poll_interval_sec = payload.interval_sec
        _interval_changed_event.set()
        logger.info(f"背景報價更新頻率已調整為 {poll_interval_sec} 秒")
    return await _build_market_response()


@app.post("/api/target")
async def update_target(payload: TargetUpdatePayload):
    """切換監控標的：'TXFF' (台指期)、'TWII' (台股大盤) 或 'STOCK' (台股個股代號)"""
    if payload.target in ("TXFF", "TWII", "STOCK"):
        await market_service.set_target(payload.target, payload.stock_code)
        logger.info(f"監控標的已切換為 {payload.target} {payload.stock_code or ''}")
    return await _build_market_response()
