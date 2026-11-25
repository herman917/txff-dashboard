"""
即時報價與歷史 K 線爬蟲模組 (crawler.py)
負責：
1. 系統啟動時抓取台灣加權指數 (^TWII) 7 個時框的歷史 K 線作為基底
2. 盤中每 5 秒非同步抓取最新即時成交點位（含 User-Agent 輪替、重試與雙資料源備援）
3. 即時將最新點位合成 / 更新至 7 個時框的最後一根 K 線 (開、高、低、收)
4. 非盤中時段或爬蟲暫時異常時，自動回傳最後一筆有效快取報價，確保系統不崩潰
"""

import asyncio
import copy
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("taiex_crawler")

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 常見瀏覽器 User-Agent 清單，用於輪替請求以降低被阻擋風險
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]

# 各時框對應 Yahoo Finance Chart API 的抓取範圍
# 確保長週期（日/週/月）有足夠根數能算出 ZigZag 轉折點
YAHOO_HISTORY_PARAMS: Dict[str, Dict[str, str]] = {
    "5m": {"interval": "5m", "range": "1mo"},
    "15m": {"interval": "15m", "range": "1mo"},
    "30m": {"interval": "30m", "range": "1mo"},
    "60m": {"interval": "60m", "range": "3mo"},
    "1d": {"interval": "1d", "range": "2y"},
    "1wk": {"interval": "1wk", "range": "5y"},
    "1mo": {"interval": "1mo", "range": "10y"},
}

YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]


def is_taiwan_market_open(now: Optional[datetime] = None) -> bool:
    """
    判斷目前是否為台股集中市場（加權指數 ^TWII）交易時段（週一至週五 09:00 ~ 13:30，台北時間）。
    """
    dt = now or datetime.now(TAIPEI_TZ)
    if dt.weekday() >= 5:
        return False
    hm = dt.hour * 60 + dt.minute
    return (9 * 60) <= hm <= (13 * 60 + 30)


def is_taifex_market_open(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    判斷目前是否為台指期 (TXF) 交易時段（台北時間）：
    - 日盤 (MarketType='0')：週一至週五 08:45 ~ 13:45
    - 夜盤 (MarketType='1')：週一至週五 15:00 ~ 23:59，及週二至週六 00:00 ~ 05:00
    回傳 (is_open, market_type)
    """
    dt = now or datetime.now(TAIPEI_TZ)
    wd = dt.weekday()  # 0=Mon ... 5=Sat, 6=Sun
    hm = dt.hour * 60 + dt.minute

    # 日盤 08:45 ~ 13:45 (週一至週五)
    if wd < 5 and (8 * 60 + 45) <= hm <= (13 * 60 + 45):
        return True, "0"

    # 夜盤 15:00 ~ 23:59 (週一至週五)
    if wd < 5 and hm >= (15 * 60):
        return True, "1"

    # 夜盤跨日 00:00 ~ 05:00 (週二至週六凌晨)
    if 1 <= wd <= 5 and hm <= (5 * 60):
        return True, "1"

    # 非盤中時段：若在 13:45~15:00 優先查日盤收盤，其餘查夜盤最後報價
    fallback_mt = "0" if (wd < 5 and (13 * 60 + 45) < hm < (15 * 60)) else "1"
    return False, fallback_mt


def get_bucket_start_ts(ts: int, tf_key: str) -> int:
    """
    根據台北時間與週期類型，計算該時間戳所屬 K 線的起始時間戳 (Unix seconds)。
    用於判斷即時報價應「更新當前最後一根 K 線」或「開啟新的一根 K 線」。
    """
    dt = datetime.fromtimestamp(ts, tz=TAIPEI_TZ)

    if tf_key in ("5m", "15m", "30m", "60m"):
        minutes = int(tf_key.replace("m", ""))
        # 台股從 09:00 開盤對齊分 K 區間
        market_open = dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if dt < market_open:
            bucket_dt = market_open
        else:
            elapsed_min = int((dt - market_open).total_seconds() // 60)
            bucket_idx = elapsed_min // minutes
            bucket_dt = market_open + timedelta(minutes=bucket_idx * minutes)
        return int(bucket_dt.timestamp())

    if tf_key == "1d":
        bucket_dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
        return int(bucket_dt.timestamp())

    if tf_key == "1wk":
        # 對齊至該週的週一 09:00
        monday = dt - timedelta(days=dt.weekday())
        bucket_dt = monday.replace(hour=9, minute=0, second=0, microsecond=0)
        return int(bucket_dt.timestamp())

    if tf_key == "1mo":
        # 對齊至該月 1 號 09:00
        bucket_dt = dt.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        return int(bucket_dt.timestamp())

    return ts


class TaiexMarketService:
    """台指期 (TXFF) 與台股大盤 (^TWII) 雙標的爬蟲與多週期 K 線狀態管理中心"""

    def __init__(self) -> None:
        self.active_target: str = "TXFF"
        self.candles_by_target: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            "TXFF": {tf: [] for tf in YAHOO_HISTORY_PARAMS},
            "TWII": {tf: [] for tf in YAHOO_HISTORY_PARAMS},
        }
        self.quotes: Dict[str, Dict[str, Any]] = {
            "TXFF": {
                "symbol": "TXFF",
                "name": "台指期近月 (TXFF)",
                "price": 0.0,
                "previous_close": 0.0,
                "change": 0.0,
                "change_pct": 0.0,
                "market_time": None,
                "last_fetched_at": None,
                "is_market_open": False,
                "is_cached": True,
                "status_text": "初始化中...",
                "source": "init",
            },
            "TWII": {
                "symbol": "^TWII",
                "name": "台灣加權指數",
                "price": 0.0,
                "previous_close": 0.0,
                "change": 0.0,
                "change_pct": 0.0,
                "market_time": None,
                "last_fetched_at": None,
                "is_market_open": False,
                "is_cached": True,
                "status_text": "初始化中...",
                "source": "init",
            },
        }
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None

    async def _get_client(self) -> httpx.AsyncClient:
        current_loop = asyncio.get_running_loop()
        if (
            self._client is None
            or self._client.is_closed
            or self._client_loop is None
            or self._client_loop.is_closed()
            or self._client_loop is not current_loop
        ):
            self._client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)
            self._client_loop = current_loop
        return self._client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _random_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
        }

    async def _fetch_yahoo_chart(
        self,
        interval: str,
        range_str: str,
        retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """
        向 Yahoo Finance v8 Chart API 抓取 ^TWII 資料，含主機切換與重試機制。
        """
        client = await self._get_client()
        for attempt in range(retries):
            host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
            url = f"{host}/v8/finance/chart/%5ETWII"
            params = {
                "interval": interval,
                "range": range_str,
                "includePrePost": "false",
            }
            try:
                resp = await client.get(url, params=params, headers=self._random_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    result = data.get("chart", {}).get("result")
                    if result and len(result) > 0:
                        return result[0]
                logger.warning(f"Yahoo Chart HTTP {resp.status_code} ({interval}/{range_str})")
            except Exception as exc:
                logger.warning(f"Yahoo Chart 請求失敗 ({attempt + 1}/{retries}): {exc}")

            if attempt < retries - 1:
                await asyncio.sleep(0.8 * (attempt + 1))
        return None

    async def _fetch_taifex_txf_quote(self, market_type: str = "0") -> Optional[Dict[str, Any]]:
        """
        向臺灣期貨交易所 MIS API 抓取台指期近月 (TXF) 即時報價。
        market_type: '0' = 日盤 (08:45~13:45), '1' = 夜盤 (15:00~05:00)
        """
        client = await self._get_client()
        url = "https://mis.taifex.com.tw/futures/api/getQuoteList"
        payload = {
            "MarketType": market_type,
            "SymbolType": "F",
            "KindID": "1",
            "CID": "TXF",
            "ExpireMonth": "",
            "RowSize": "全部",
            "PageNo": "",
            "SortColumn": "",
            "AscDesc": "A",
        }
        try:
            resp = await client.post(url, json=payload, headers=self._random_headers())
            if resp.status_code == 200:
                data = resp.json()
                quote_list = data.get("RtData", {}).get("QuoteList", [])
                # 挑選近月台指期合約（排除現貨 TXF-S / TXF-P）
                for item in quote_list:
                    sym_id = item.get("SymbolID", "")
                    if sym_id.startswith("TXF") and (sym_id.endswith("-F") or sym_id.endswith("-M")):
                        last_str = (item.get("CLastPrice") or "").strip()
                        ref_str = (item.get("CRefPrice") or "").strip()
                        price_val = last_str if last_str and last_str != "-" else ref_str
                        if not price_val or price_val == "-":
                            continue
                        price = float(price_val)
                        prev = float(ref_str) if ref_str and ref_str != "-" else price

                        # 解析 HHMMSS 時間格式
                        now = datetime.now(TAIPEI_TZ)
                        ctime = (item.get("CTime") or "").strip()
                        if len(ctime) == 6 and ctime.isdigit():
                            hh, mm, ss = int(ctime[:2]), int(ctime[2:4]), int(ctime[4:6])
                            dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                            # 夜盤跨日若時間大於現在（例如 23:59 對比 00:05），視為前一日
                            if dt > now + timedelta(hours=1):
                                dt -= timedelta(days=1)
                            ts = int(dt.timestamp())
                        else:
                            ts = int(now.timestamp())

                        session_label = "夜盤" if market_type == "1" else "日盤"
                        contract_name = item.get("DispCName") or sym_id
                        return {
                            "price": price,
                            "previous_close": prev,
                            "market_time": ts,
                            "contract": f"{contract_name} ({session_label})",
                            "source": f"TAIFEX 期交所 ({session_label})",
                        }
        except Exception as exc:
            logger.warning(f"TAIFEX TXF 報價抓取失敗 (MarketType={market_type}): {exc}")
        return None

    async def _fetch_twse_mis_quote(self) -> Optional[Dict[str, Any]]:
        """備援資料源：臺灣證券交易所 MIS 加權指數即時資訊 (tse_t00.tw)"""
        client = await self._get_client()
        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        params = {"ex_ch": "tse_t00.tw", "json": "1", "delay": "0"}
        try:
            resp = await client.get(url, params=params, headers=self._random_headers())
            if resp.status_code == 200:
                data = resp.json()
                msg_array = data.get("msgArray", [])
                if msg_array:
                    item = msg_array[0]
                    price_str = item.get("z") or item.get("y")
                    prev_str = item.get("y")
                    if price_str and price_str != "-":
                        price = float(price_str)
                        prev = float(prev_str) if prev_str and prev_str != "-" else price
                        ts_ms = int(item.get("tlong", 0))
                        ts = ts_ms // 1000 if ts_ms > 0 else int(datetime.now(TAIPEI_TZ).timestamp())
                        return {
                            "price": price,
                            "previous_close": prev,
                            "market_time": ts,
                            "source": "TWSE MIS",
                        }
        except Exception as exc:
            logger.warning(f"TWSE MIS 備援抓取失敗: {exc}")
        return None

    @staticmethod
    def _parse_yahoo_candles(chart_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """將 Yahoo Chart JSON 解析為乾淨的 OHLC K 線列表"""
        timestamps = chart_result.get("timestamp") or []
        quote_list = chart_result.get("indicators", {}).get("quote", [])
        if not timestamps or not quote_list:
            return []

        q = quote_list[0]
        opens = q.get("open") or []
        highs = q.get("high") or []
        lows = q.get("low") or []
        closes = q.get("close") or []

        candles: List[Dict[str, Any]] = []
        for i, ts in enumerate(timestamps):
            o = opens[i] if i < len(opens) else None
            h = highs[i] if i < len(highs) else None
            l = lows[i] if i < len(lows) else None
            c = closes[i] if i < len(closes) else None
            if o is None or h is None or l is None or c is None:
                continue
            candles.append({
                "timestamp": int(ts),
                "open": round(float(o), 2),
                "high": round(float(h), 2),
                "low": round(float(l), 2),
                "close": round(float(c), 2),
            })
        return candles

    @staticmethod
    def _resample_candles(candles: List[Dict[str, Any]], target_tf: str) -> List[Dict[str, Any]]:
        """由較短週期 K 線重採樣合成目標週期 K 線（作為 API 限流時的自動備援）"""
        if not candles:
            return []
        buckets: Dict[int, Dict[str, Any]] = {}
        for c in candles:
            b_ts = get_bucket_start_ts(c["timestamp"], target_tf)
            if b_ts not in buckets:
                buckets[b_ts] = {
                    "timestamp": b_ts,
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                }
            else:
                b = buckets[b_ts]
                b["high"] = round(max(b["high"], c["high"]), 2)
                b["low"] = round(min(b["low"], c["low"]), 2)
                b["close"] = c["close"]
        return [buckets[k] for k in sorted(buckets.keys())]

    async def initialize_history(self) -> None:
        """
        系統啟動時抓取基底歷史 K 線，並以重採樣合成全部 7 個時框：
        - 僅向 Yahoo Finance 請求 5m(1mo) 與 1d(10y) 兩支端點，大幅降低 429 限流機率
        - 15m / 30m / 60m 由 5m 重採樣；1wk / 1mo 由 1d 重採樣
        - 同時以 TWSE MIS (tse_t00.tw) 與 TAIFEX MIS 初始化今日最新收盤/即時報價
        """
        logger.info("開始抓取 ^TWII 基底歷史 K 線資料 (5m & 1d)...")

        res_5m = await self._fetch_yahoo_chart("5m", "1mo", retries=4)
        await asyncio.sleep(0.5)
        res_1d = await self._fetch_yahoo_chart("1d", "10y", retries=4)

        base_candles: Dict[str, List[Dict[str, Any]]] = {tf: [] for tf in YAHOO_HISTORY_PARAMS}

        if isinstance(res_5m, dict):
            base_candles["5m"] = self._parse_yahoo_candles(res_5m)
            logger.info(f"基底時框 5m 載入完成，共 {len(base_candles['5m'])} 根 K 線")
        if isinstance(res_1d, dict):
            base_candles["1d"] = self._parse_yahoo_candles(res_1d)
            logger.info(f"基底時框 1d 載入完成，共 {len(base_candles['1d'])} 根 K 線")

        # 若 1d 仍因限流失敗，改由 5m 重採樣補齊日 K，確保日/週/月不為空
        if not base_candles["1d"] and base_candles["5m"]:
            base_candles["1d"] = self._resample_candles(base_candles["5m"], "1d")
            logger.warning(f"1d 由 5m 重採樣補齊，共 {len(base_candles['1d'])} 根")

        for sub_tf in ("15m", "30m", "60m"):
            if base_candles["5m"]:
                base_candles[sub_tf] = self._resample_candles(base_candles["5m"], sub_tf)
                logger.info(f"時框 {sub_tf} 合成完成，共 {len(base_candles[sub_tf])} 根")

        for macro_tf in ("1wk", "1mo"):
            if base_candles["1d"]:
                base_candles[macro_tf] = self._resample_candles(base_candles["1d"], macro_tf)
                logger.info(f"時框 {macro_tf} 合成完成，共 {len(base_candles[macro_tf])} 根")

        # 並行抓取 TWSE MIS (^TWII 今日官方報價) 與 TAIFEX MIS (TXFF 近月報價)
        now = datetime.now(TAIPEI_TZ)
        txf_open, market_type = is_taifex_market_open(now)
        twse_task = self._fetch_twse_mis_quote()
        txf_task = self._fetch_taifex_txf_quote(market_type)
        twse_quote, txf_quote = await asyncio.gather(twse_task, txf_task)

        if not txf_quote:
            other_mt = "0" if market_type == "1" else "1"
            txf_quote = await self._fetch_taifex_txf_quote(other_mt)

        async with self._lock:
            self.candles_by_target["TXFF"] = copy.deepcopy(base_candles)
            self.candles_by_target["TWII"] = copy.deepcopy(base_candles)

            daily = base_candles["1d"]

            # 1) 初始化 ^TWII 報價（優先使用 TWSE MIS 官方即時/收盤報價）
            twii_open = is_taiwan_market_open(now)
            if twse_quote:
                twii_price = twse_quote["price"]
                twii_prev = twse_quote["previous_close"]
                twii_ts = twse_quote["market_time"]
                twii_source = twse_quote["source"]
                self._apply_tick_to_candles("TWII", twii_price, twii_ts)
            elif daily:
                last_d = daily[-1]
                prev_d = daily[-2] if len(daily) >= 2 else last_d
                twii_price = last_d["close"]
                twii_prev = prev_d["close"]
                twii_ts = last_d["timestamp"]
                twii_source = "Yahoo Finance (基底)"
            else:
                twii_price, twii_prev, twii_ts = 0.0, 0.0, int(now.timestamp())
                twii_source = "init"

            self._update_quote_state(
                target="TWII",
                price=twii_price,
                previous_close=twii_prev,
                market_ts=twii_ts,
                is_market_open=twii_open,
                is_cached=not twii_open,
                status_text="盤中即時" if twii_open else "非盤中/快取狀態",
                source=twii_source,
                name="台灣加權指數 (^TWII)",
            )

            # 2) 初始化 TXFF 報價
            if txf_quote:
                txf_price = txf_quote["price"]
                txf_prev = txf_quote["previous_close"]
                txf_ts = txf_quote["market_time"]
                txf_source = txf_quote["source"]
                txf_name = f"台指期近月 {txf_quote['contract']}"
                self._apply_tick_to_candles("TXFF", txf_price, txf_ts)
            else:
                txf_price, txf_prev, txf_ts = twii_price, twii_prev, twii_ts
                txf_source = "Yahoo Finance (^TWII 備援基底)"
                txf_name = "台指期近月 (TXFF)"

            self._update_quote_state(
                target="TXFF",
                price=txf_price,
                previous_close=txf_prev,
                market_ts=txf_ts,
                is_market_open=txf_open,
                is_cached=not txf_open,
                status_text="盤中即時" if txf_open else "非盤中/快取狀態",
                source=txf_source,
                name=txf_name,
            )

    def _update_quote_state(
        self,
        target: str,
        price: float,
        previous_close: float,
        market_ts: int,
        is_market_open: bool,
        is_cached: bool,
        status_text: str,
        source: str,
        name: Optional[str] = None,
    ) -> None:
        now = datetime.now(TAIPEI_TZ)
        change = round(price - previous_close, 2) if previous_close else 0.0
        change_pct = round((change / previous_close) * 100, 2) if previous_close else 0.0
        symbol = "TXFF" if target == "TXFF" else "^TWII"

        self.quotes[target] = {
            "symbol": symbol,
            "name": name or self.quotes[target].get("name", symbol),
            "price": round(price, 2),
            "previous_close": round(previous_close, 2),
            "change": change,
            "change_pct": change_pct,
            "market_time": datetime.fromtimestamp(market_ts, tz=TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "last_fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "is_market_open": is_market_open,
            "is_cached": is_cached,
            "status_text": status_text,
            "source": source,
        }

    def _apply_tick_to_candles(self, target: str, price: float, tick_ts: int) -> None:
        """
        將最新成交點位即時合成到指定標的 7 個時框的最後一根 K 線（開、高、低、收）。
        若已跨入新週期區間，則自動新增一根 K 線；否則更新當根 K 線的 High/Low/Close。
        """
        if price <= 0:
            return

        for tf_key, candles in self.candles_by_target[target].items():
            bucket_ts = get_bucket_start_ts(tick_ts, tf_key)
            if not candles:
                candles.append({
                    "timestamp": bucket_ts,
                    "open": round(price, 2),
                    "high": round(price, 2),
                    "low": round(price, 2),
                    "close": round(price, 2),
                })
                continue

            last_candle = candles[-1]
            last_bucket = get_bucket_start_ts(last_candle["timestamp"], tf_key)

            if bucket_ts > last_bucket:
                candles.append({
                    "timestamp": bucket_ts,
                    "open": round(price, 2),
                    "high": round(price, 2),
                    "low": round(price, 2),
                    "close": round(price, 2),
                })
                if len(candles) > 1000:
                    del candles[0]
            else:
                last_candle["high"] = round(max(last_candle["high"], price), 2)
                last_candle["low"] = round(min(last_candle["low"], price), 2)
                last_candle["close"] = round(price, 2)

    async def poll_realtime_quote(self, target: Optional[str] = None, force: bool = False) -> None:
        """
        依目前選擇標的 (TXFF 或 TWII) 抓取最新即時報價並更新 7 週期 K 線。
        非交易時段保留最後一筆有效報價並標示「非盤中/快取狀態」（force=True 時仍強制向官方 API 校對一次）。
        """
        active = target or self.active_target
        now = datetime.now(TAIPEI_TZ)

        if active == "TXFF":
            market_open, market_type = is_taifex_market_open(now)
            if not market_open and not force and self.quotes["TXFF"]["price"] > 0:
                async with self._lock:
                    self.quotes["TXFF"]["is_market_open"] = False
                    self.quotes["TXFF"]["is_cached"] = True
                    self.quotes["TXFF"]["status_text"] = "非盤中/快取狀態"
                    self.quotes["TXFF"]["last_fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                return

            txf_res = await self._fetch_taifex_txf_quote(market_type)
            async with self._lock:
                if txf_res is not None:
                    fetched_price = txf_res["price"]
                    fetched_prev = txf_res["previous_close"]
                    fetched_ts = txf_res["market_time"]
                    if market_open:
                        self._apply_tick_to_candles("TXFF", fetched_price, fetched_ts)
                    self._update_quote_state(
                        target="TXFF",
                        price=fetched_price,
                        previous_close=fetched_prev or fetched_price,
                        market_ts=fetched_ts,
                        is_market_open=market_open,
                        is_cached=not market_open,
                        status_text="盤中即時" if market_open else "非盤中/快取狀態",
                        source=txf_res["source"],
                        name=f"台指期近月 {txf_res['contract']}",
                    )
                else:
                    self.quotes["TXFF"]["is_market_open"] = market_open
                    self.quotes["TXFF"]["is_cached"] = True
                    self.quotes["TXFF"]["status_text"] = (
                        "快取狀態 (連線重試中)" if market_open else "非盤中/快取狀態"
                    )
                    self.quotes["TXFF"]["last_fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            return

        # active == "TWII" (台股加權指數：優先使用 TWSE MIS 官方即時/收盤資訊)
        market_open = is_taiwan_market_open(now)
        if not market_open and not force and self.quotes["TWII"]["price"] > 0:
            async with self._lock:
                self.quotes["TWII"]["is_market_open"] = False
                self.quotes["TWII"]["is_cached"] = True
                self.quotes["TWII"]["status_text"] = "非盤中/快取狀態"
                self.quotes["TWII"]["last_fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            return

        fetched_price: Optional[float] = None
        fetched_prev: Optional[float] = None
        fetched_ts: Optional[int] = None
        source = "TWSE MIS"

        twse_res = await self._fetch_twse_mis_quote()
        if twse_res:
            fetched_price = twse_res["price"]
            fetched_prev = twse_res["previous_close"]
            fetched_ts = twse_res["market_time"]
            source = twse_res["source"]
        else:
            chart_res = await self._fetch_yahoo_chart("1m", "1d", retries=2)
            if chart_res:
                meta = chart_res.get("meta", {})
                if meta.get("regularMarketPrice"):
                    fetched_price = float(meta["regularMarketPrice"])
                    fetched_prev = float(
                        meta.get("previousClose")
                        or meta.get("chartPreviousClose")
                        or self.quotes["TWII"]["previous_close"]
                        or fetched_price
                    )
                    fetched_ts = int(meta.get("regularMarketTime") or now.timestamp())
                    source = "Yahoo Finance"

        async with self._lock:
            if fetched_price is not None and fetched_ts is not None:
                if market_open:
                    self._apply_tick_to_candles("TWII", fetched_price, fetched_ts)
                self._update_quote_state(
                    target="TWII",
                    price=fetched_price,
                    previous_close=fetched_prev or fetched_price,
                    market_ts=fetched_ts,
                    is_market_open=market_open,
                    is_cached=not market_open,
                    status_text="盤中即時" if market_open else "非盤中/快取狀態",
                    source=source,
                    name="台灣加權指數 (^TWII)",
                )
            else:
                self.quotes["TWII"]["is_market_open"] = market_open
                self.quotes["TWII"]["is_cached"] = True
                self.quotes["TWII"]["status_text"] = (
                    "快取狀態 (連線重試中)" if market_open else "非盤中/快取狀態"
                )
                self.quotes["TWII"]["last_fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

    async def set_target(self, target: str) -> None:
        """切換目前監控標的 ('TXFF' 或 'TWII') 並立即刷新該標的報價"""
        if target not in ("TXFF", "TWII"):
            return
        self.active_target = target
        await self.poll_realtime_quote(target, force=True)

    async def get_snapshot(self) -> Dict[str, Any]:
        """取得目前選擇標的之報價與 7 個時框 K 線快照"""
        async with self._lock:
            t = self.active_target
            return {
                "active_target": t,
                "quote": dict(self.quotes[t]),
                "candles_by_tf": {
                    tf: list(candles) for tf, candles in self.candles_by_target[t].items()
                },
            }
