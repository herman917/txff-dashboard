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
from urllib.parse import quote as url_quote
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
        total_min = dt.hour * 60 + dt.minute
        bucket_min = (total_min // minutes) * minutes
        bucket_dt = dt.replace(
            hour=bucket_min // 60,
            minute=bucket_min % 60,
            second=0,
            microsecond=0,
        )
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
        symbol: str = "^TWII",
        retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """
        向 Yahoo Finance v8 Chart API 抓取指定標的資料，含主機切換、重試與 yfinance 備援。
        """
        client = await self._get_client()
        encoded_sym = url_quote(symbol, safe="")
        for attempt in range(retries):
            host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
            url = f"{host}/v8/finance/chart/{encoded_sym}"
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
                logger.warning(f"Yahoo Chart HTTP {resp.status_code} ({symbol} {interval}/{range_str})")
            except Exception as exc:
                logger.warning(f"Yahoo Chart 請求失敗 ({attempt + 1}/{retries}): {exc}")

            if attempt < retries - 1:
                await asyncio.sleep(0.8 * (attempt + 1))

        # 若 Yahoo API 持續 429 限流（常見於雲端共用 IP），自動切換 yfinance 備援
        try:
            import yfinance as yf

            def _yf_fetch() -> Optional[Dict[str, Any]]:
                df = yf.Ticker(symbol).history(period=range_str, interval=interval, auto_adjust=False)
                if df is None or df.empty:
                    return None
                ts_list = [int(idx.timestamp()) for idx in df.index]
                return {
                    "timestamp": ts_list,
                    "indicators": {
                        "quote": [{
                            "open": df["Open"].tolist(),
                            "high": df["High"].tolist(),
                            "low": df["Low"].tolist(),
                            "close": df["Close"].tolist(),
                        }]
                    },
                    "meta": {},
                }

            yf_res = await asyncio.to_thread(_yf_fetch)
            if yf_res:
                logger.info(f"yfinance 備援抓取成功 ({symbol} {interval}/{range_str})")
                return yf_res
        except Exception as exc:
            logger.warning(f"yfinance 備援失敗 ({symbol} {interval}/{range_str}): {exc}")

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
                            "symbol_id": sym_id,
                            "price": price,
                            "previous_close": prev,
                            "market_time": ts,
                            "contract": f"{contract_name} ({session_label})",
                            "source": f"TAIFEX 期交所 ({session_label})",
                        }
        except Exception as exc:
            logger.warning(f"TAIFEX TXF 報價抓取失敗 (MarketType={market_type}): {exc}")
        return None

    async def _fetch_taifex_1m_candles(self, symbol_id: str, is_night: bool) -> List[Dict[str, Any]]:
        """
        向 TAIFEX MIS getChartData1M 抓取當盤（日盤或夜盤）1 分 K 歷史資料。
        Ticks 格式：['HHMMSS', Open, High, Low, Close, Volume]
        """
        client = await self._get_client()
        url = "https://mis.taifex.com.tw/futures/api/getChartData1M"
        try:
            resp = await client.post(url, json={"SymbolID": symbol_id}, headers=self._random_headers())
            if resp.status_code != 200:
                return []
            ticks = resp.json().get("RtData", {}).get("Ticks", [])
            if not ticks:
                return []

            now = datetime.now(TAIPEI_TZ)
            candles: List[Dict[str, Any]] = []
            for row in ticks:
                if len(row) < 5:
                    continue
                t_str = str(row[0]).strip()
                if len(t_str) != 6 or not t_str.isdigit():
                    continue
                hh, mm, ss = int(t_str[:2]), int(t_str[2:4]), int(t_str[4:6])
                if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
                    continue
                dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                if is_night and hh >= 15 and now.hour < 6:
                    dt -= timedelta(days=1)
                elif not is_night and now.hour < 8:
                    dt -= timedelta(days=1)
                elif dt > now + timedelta(minutes=5):
                    dt -= timedelta(days=1)

                # TAIFEX getChartData1M 的 T 為該根 1 分 K「結束時間」(例：204600 代表 20:45~20:46)，
                # 減去 1 分鐘對齊成 K 線「起始時間」，才能正確歸類到對應的 5分/15分/30分/60分 K 線。
                dt -= timedelta(minutes=1)

                candles.append({
                    "timestamp": int(dt.timestamp()),
                    "open": round(float(row[1]), 2),
                    "high": round(float(row[2]), 2),
                    "low": round(float(row[3]), 2),
                    "close": round(float(row[4]), 2),
                })
            return candles
        except Exception as exc:
            logger.warning(f"TAIFEX 1M K線抓取失敗 ({symbol_id}): {exc}")
            return []

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
                        o_str, h_str, l_str = item.get("o"), item.get("h"), item.get("l")
                        return {
                            "price": price,
                            "open": float(o_str) if o_str and o_str != "-" else price,
                            "high": float(h_str) if h_str and h_str != "-" else price,
                            "low": float(l_str) if l_str and l_str != "-" else price,
                            "previous_close": prev,
                            "market_time": ts,
                            "source": "TWSE MIS",
                        }
        except Exception as exc:
            logger.warning(f"TWSE MIS 備援抓取失敗: {exc}")
        return None

    async def _fetch_twse_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """向 TWSE MIS 抓取上市 (tse) 或上櫃 (otc) 個股即時/收盤報價"""
        client = await self._get_client()
        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        params = {"ex_ch": f"tse_{stock_code}.tw|otc_{stock_code}.tw", "json": "1", "delay": "0"}
        try:
            resp = await client.get(url, params=params, headers=self._random_headers())
            if resp.status_code == 200:
                for item in resp.json().get("msgArray", []):
                    price_str = item.get("z") or item.get("y")
                    prev_str = item.get("y")
                    if price_str and price_str != "-":
                        price = float(price_str)
                        prev = float(prev_str) if prev_str and prev_str != "-" else price
                        ts_ms = int(item.get("tlong", 0))
                        ts = ts_ms // 1000 if ts_ms > 0 else int(datetime.now(TAIPEI_TZ).timestamp())
                        name = item.get("n") or stock_code
                        ex = "上市" if item.get("ex") == "tse" else "上櫃"
                        return {
                            "price": price,
                            "previous_close": prev,
                            "market_time": ts,
                            "name": f"{name} ({stock_code})",
                            "source": f"TWSE MIS ({ex})",
                        }
        except Exception as exc:
            logger.warning(f"TWSE 個股報價抓取失敗 ({stock_code}): {exc}")
        return None

    async def _fetch_finmind_txf_daily(self, days: int = 240) -> List[Dict[str, Any]]:
        """
        從 FinMind TaiwanFuturesDaily (TX) 抓取台指期近月「全盤 (夜盤+日盤)」真實日 K 歷史資料，
        使其與券商軟體「台指近全」日 K / 週 K 完全一致。
        """
        client = await self._get_client()
        start_date = (datetime.now(TAIPEI_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanFuturesDaily",
            "data_id": "TX",
            "start_date": start_date,
        }
        try:
            resp = await client.get(url, params=params, headers=self._random_headers())
            if resp.status_code != 200:
                return []
            raw = resp.json().get("data", [])
            rows = [
                r for r in raw
                if "/" not in str(r.get("contract_date", ""))
                and float(r.get("open", 0)) > 0
                and float(r.get("volume", 0)) > 100
            ]
            if not rows:
                return []

            by_date: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                by_date.setdefault(r["date"], []).append(r)

            candles: List[Dict[str, Any]] = []
            for d_str in sorted(by_date.keys()):
                items = by_date[d_str]
                # 挑選當日成交量最大的單月合約（近月主力合約）
                by_contract: Dict[str, Dict[str, Any]] = {}
                vol_by_contract: Dict[str, float] = {}
                for it in items:
                    c_id = it["contract_date"]
                    vol_by_contract[c_id] = vol_by_contract.get(c_id, 0.0) + float(it.get("volume", 0))
                    by_contract.setdefault(c_id, {})[it.get("trading_session", "position")] = it

                main_contract = max(vol_by_contract.keys(), key=lambda k: vol_by_contract[k])
                sess = by_contract[main_contract]
                am = sess.get("after_market")
                pm = sess.get("position")

                if am and pm:
                    o = float(am["open"])
                    h = max(float(am["max"]), float(pm["max"]))
                    l = min(float(am["min"]), float(pm["min"]))
                    cl = float(pm["close"])
                elif pm:
                    o, h, l, cl = float(pm["open"]), float(pm["max"]), float(pm["min"]), float(pm["close"])
                elif am:
                    o, h, l, cl = float(am["open"]), float(am["max"]), float(am["min"]), float(am["close"])
                else:
                    continue

                dt = datetime.strptime(d_str, "%Y-%m-%d").replace(hour=9, minute=0, tzinfo=TAIPEI_TZ)
                candles.append({
                    "timestamp": int(dt.timestamp()),
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(cl, 2),
                })
            return candles
        except Exception as exc:
            logger.warning(f"FinMind TXF 日K抓取失敗: {exc}")
            return []

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

        # 若 1d 仍因限流失敗，改由 5m 重採樣補齊日 K；若 1d 缺漏最近交易日（如 Yahoo 延遲），也用 5m 補齊
        if not base_candles["1d"] and base_candles["5m"]:
            base_candles["1d"] = self._resample_candles(base_candles["5m"], "1d")
            logger.warning(f"1d 由 5m 重採樣補齊，共 {len(base_candles['1d'])} 根")
        elif base_candles["1d"] and base_candles["5m"]:
            daily_from_5m = self._resample_candles(base_candles["5m"], "1d")
            last_1d_bucket = get_bucket_start_ts(base_candles["1d"][-1]["timestamp"], "1d")
            extra_days = [c for c in daily_from_5m if c["timestamp"] > last_1d_bucket]
            if extra_days:
                base_candles["1d"].extend(extra_days)
                logger.info(f"1d 已由 5m 補齊最新 {len(extra_days)} 個交易日")

        for sub_tf in ("15m", "30m", "60m"):
            if base_candles["5m"]:
                base_candles[sub_tf] = self._resample_candles(base_candles["5m"], sub_tf)
                logger.info(f"時框 {sub_tf} 合成完成，共 {len(base_candles[sub_tf])} 根")

        for macro_tf in ("1wk", "1mo"):
            if base_candles["1d"]:
                base_candles[macro_tf] = self._resample_candles(base_candles["1d"], macro_tf)
                logger.info(f"時框 {macro_tf} 合成完成，共 {len(base_candles[macro_tf])} 根")

        # 並行抓取 TWSE MIS (^TWII 今日官方報價)、TAIFEX MIS (TXFF 近月報價) 與 FinMind TXF 全盤日 K
        now = datetime.now(TAIPEI_TZ)
        txf_open, market_type = is_taifex_market_open(now)
        twse_task = self._fetch_twse_mis_quote()
        txf_task = self._fetch_taifex_txf_quote(market_type)
        txf_daily_task = self._fetch_finmind_txf_daily(days=240)
        twse_quote, txf_quote, txf_daily_candles = await asyncio.gather(twse_task, txf_task, txf_daily_task)

        if not txf_quote:
            other_mt = "0" if market_type == "1" else "1"
            txf_quote = await self._fetch_taifex_txf_quote(other_mt)

        # 抓取 TXFF 當盤（日盤 + 夜盤）真實 1 分 K，補足台指期盤中/夜盤走勢
        txf_1m_candles: List[Dict[str, Any]] = []
        if txf_quote and txf_quote.get("symbol_id"):
            sym_base = txf_quote["symbol_id"].rsplit("-", 1)[0]
            day_1m, night_1m = await asyncio.gather(
                self._fetch_taifex_1m_candles(f"{sym_base}-F", is_night=False),
                self._fetch_taifex_1m_candles(f"{sym_base}-M", is_night=True),
            )
            txf_1m_candles = sorted(day_1m + night_1m, key=lambda x: x["timestamp"])
            logger.info(f"TXFF 1分K 載入完成：日盤 {len(day_1m)} 根 + 夜盤 {len(night_1m)} 根")

        async with self._lock:
            self.candles_by_target["TXFF"] = copy.deepcopy(base_candles)
            self.candles_by_target["TWII"] = copy.deepcopy(base_candles)

            # 若取得 FinMind 真實台指近全 (夜盤+日盤) 日 K，優先覆寫 TXFF 的日 K 與週 K
            if txf_daily_candles:
                self.candles_by_target["TXFF"]["1d"] = txf_daily_candles
                self.candles_by_target["TXFF"]["1wk"] = self._resample_candles(txf_daily_candles, "1wk")
                logger.info(f"TXFF 全盤日K載入完成：{len(txf_daily_candles)} 根，週K：{len(self.candles_by_target['TXFF']['1wk'])} 根")

            # 將 TXFF 真實日/夜盤 1分K 重採樣併入 TXFF 的 5m / 15m / 30m / 60m
            if txf_1m_candles:
                first_txf_ts = txf_1m_candles[0]["timestamp"]
                for tf_key in ("5m", "15m", "30m", "60m"):
                    txf_tf = self._resample_candles(txf_1m_candles, tf_key)
                    # 保留早於 TXFF 當盤起始時間的歷史基底，後面銜接真實 TXFF K 線
                    hist_part = [
                        c for c in self.candles_by_target["TXFF"][tf_key]
                        if c["timestamp"] < get_bucket_start_ts(first_txf_ts, tf_key)
                    ]
                    self.candles_by_target["TXFF"][tf_key] = hist_part + txf_tf

            daily = base_candles["1d"]

            # 1) 初始化 ^TWII 報價（優先使用 TWSE MIS 官方即時/收盤報價）
            twii_open = is_taiwan_market_open(now)
            if twse_quote:
                twii_price = twse_quote["price"]
                twii_prev = twse_quote["previous_close"]
                twii_ts = twse_quote["market_time"]
                twii_source = twse_quote["source"]
                self._apply_tick_to_candles("TWII", twii_price, twii_ts)
                # 以 TWSE MIS 真實開高低更新當日日 K
                if self.candles_by_target["TWII"]["1d"]:
                    d_last = self.candles_by_target["TWII"]["1d"][-1]
                    if get_bucket_start_ts(d_last["timestamp"], "1d") == get_bucket_start_ts(twii_ts, "1d"):
                        d_last["open"] = round(twse_quote.get("open", twii_price), 2)
                        d_last["high"] = round(max(d_last["high"], twse_quote.get("high", twii_price)), 2)
                        d_last["low"] = round(min(d_last["low"], twse_quote.get("low", twii_price)), 2)
                        d_last["close"] = round(twii_price, 2)
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
        if target == "TXFF":
            symbol = "TXFF"
        elif target == "TWII":
            symbol = "^TWII"
        else:
            symbol = target.split(":", 1)[1] if ":" in target else target

        existing = self.quotes.get(target, {})
        self.quotes[target] = {
            "symbol": symbol,
            "name": name or existing.get("name", symbol),
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
            txf_1m: List[Dict[str, Any]] = []
            if txf_res and txf_res.get("symbol_id") and (market_open or force):
                txf_1m = await self._fetch_taifex_1m_candles(
                    txf_res["symbol_id"],
                    is_night=(market_type == "1"),
                )

            async with self._lock:
                if txf_res is not None:
                    fetched_price = txf_res["price"]
                    fetched_prev = txf_res["previous_close"]
                    fetched_ts = txf_res["market_time"]
                    if txf_1m:
                        first_ts = txf_1m[0]["timestamp"]
                        for tf_key in ("5m", "15m", "30m", "60m"):
                            txf_tf = self._resample_candles(txf_1m, tf_key)
                            hist_part = [
                                c for c in self.candles_by_target["TXFF"][tf_key]
                                if c["timestamp"] < get_bucket_start_ts(first_ts, tf_key)
                            ]
                            self.candles_by_target["TXFF"][tf_key] = hist_part + txf_tf
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

        # active == "TWII" 或 "STOCK:XXXX"
        is_stock = active.startswith("STOCK:")
        stock_code = active.split(":", 1)[1] if is_stock else ""
        market_open = is_taiwan_market_open(now)

        if not market_open and not force and self.quotes.get(active, {}).get("price", 0) > 0:
            async with self._lock:
                self.quotes[active]["is_market_open"] = False
                self.quotes[active]["is_cached"] = True
                self.quotes[active]["status_text"] = "非盤中/快取狀態"
                self.quotes[active]["last_fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            return

        fetched_price: Optional[float] = None
        fetched_prev: Optional[float] = None
        fetched_ts: Optional[int] = None
        source = "TWSE MIS"
        display_name = self.quotes.get(active, {}).get("name")

        if is_stock:
            st_res = await self._fetch_twse_stock_quote(stock_code)
            if st_res:
                fetched_price = st_res["price"]
                fetched_prev = st_res["previous_close"]
                fetched_ts = st_res["market_time"]
                source = st_res["source"]
                display_name = st_res["name"]
            else:
                for suffix in (".TW", ".TWO"):
                    chart_res = await self._fetch_yahoo_chart("1m", "1d", symbol=f"{stock_code}{suffix}", retries=1)
                    if chart_res and chart_res.get("meta", {}).get("regularMarketPrice"):
                        meta = chart_res["meta"]
                        fetched_price = float(meta["regularMarketPrice"])
                        fetched_prev = float(meta.get("previousClose") or fetched_price)
                        fetched_ts = int(meta.get("regularMarketTime") or now.timestamp())
                        source = "Yahoo Finance"
                        break
        else:
            twse_res = await self._fetch_twse_mis_quote()
            if twse_res:
                fetched_price = twse_res["price"]
                fetched_prev = twse_res["previous_close"]
                fetched_ts = twse_res["market_time"]
                source = twse_res["source"]
            else:
                chart_res = await self._fetch_yahoo_chart("1m", "1d", symbol="^TWII", retries=2)
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
                    self._apply_tick_to_candles(active, fetched_price, fetched_ts)
                self._update_quote_state(
                    target=active,
                    price=fetched_price,
                    previous_close=fetched_prev or fetched_price,
                    market_ts=fetched_ts,
                    is_market_open=market_open,
                    is_cached=not market_open,
                    status_text="盤中即時" if market_open else "非盤中/快取狀態",
                    source=source,
                    name=display_name or ("台灣加權指數 (^TWII)" if not is_stock else stock_code),
                )
            elif active in self.quotes:
                self.quotes[active]["is_market_open"] = market_open
                self.quotes[active]["is_cached"] = True
                self.quotes[active]["status_text"] = (
                    "快取狀態 (連線重試中)" if market_open else "非盤中/快取狀態"
                )
                self.quotes[active]["last_fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

    async def ensure_stock_loaded(self, stock_code: str) -> str:
        """載入指定台股代號（如 2330）的 15m/30m/60m/1d/1wk 歷史 K 線與報價，回傳 target_key"""
        code = stock_code.strip().upper().replace(".TW", "").replace(".TWO", "")
        target_key = f"STOCK:{code}"
        if target_key in self.candles_by_target and self.candles_by_target[target_key].get("1d"):
            return target_key

        res_5m = None
        res_1d = None
        for suffix in (".TW", ".TWO"):
            sym = f"{code}{suffix}"
            r5 = await self._fetch_yahoo_chart("5m", "1mo", symbol=sym, retries=2)
            await asyncio.sleep(0.25)
            r1 = await self._fetch_yahoo_chart("1d", "5y", symbol=sym, retries=2)
            if r5 or r1:
                res_5m, res_1d = r5, r1
                break

        candles_map: Dict[str, List[Dict[str, Any]]] = {tf: [] for tf in YAHOO_HISTORY_PARAMS}
        if isinstance(res_5m, dict):
            candles_map["5m"] = self._parse_yahoo_candles(res_5m)
        if isinstance(res_1d, dict):
            candles_map["1d"] = self._parse_yahoo_candles(res_1d)
        if not candles_map["1d"] and candles_map["5m"]:
            candles_map["1d"] = self._resample_candles(candles_map["5m"], "1d")

        for sub_tf in ("15m", "30m", "60m"):
            if candles_map["5m"]:
                candles_map[sub_tf] = self._resample_candles(candles_map["5m"], sub_tf)
        for macro_tf in ("1wk", "1mo"):
            if candles_map["1d"]:
                candles_map[macro_tf] = self._resample_candles(candles_map["1d"], macro_tf)

        st_quote = await self._fetch_twse_stock_quote(code)
        now = datetime.now(TAIPEI_TZ)
        market_open = is_taiwan_market_open(now)

        async with self._lock:
            self.candles_by_target[target_key] = candles_map
            daily = candles_map["1d"]
            if st_quote:
                price = st_quote["price"]
                prev = st_quote["previous_close"]
                ts = st_quote["market_time"]
                name = st_quote["name"]
                source = st_quote["source"]
                self._apply_tick_to_candles(target_key, price, ts)
            elif daily:
                price = daily[-1]["close"]
                prev = daily[-2]["close"] if len(daily) >= 2 else price
                ts = daily[-1]["timestamp"]
                name = code
                source = "Yahoo Finance"
            else:
                price, prev, ts = 0.0, 0.0, int(now.timestamp())
                name = f"{code} (查無資料)"
                source = "N/A"

            self._update_quote_state(
                target=target_key,
                price=price,
                previous_close=prev,
                market_ts=ts,
                is_market_open=market_open,
                is_cached=not market_open,
                status_text="盤中即時" if market_open else "非盤中/快取狀態",
                source=source,
                name=name,
            )
        return target_key

    async def set_target(self, target: str, stock_code: Optional[str] = None) -> None:
        """切換目前監控標的 ('TXFF', 'TWII' 或 'STOCK' 搭配 stock_code)"""
        if target == "STOCK" and stock_code:
            key = await self.ensure_stock_loaded(stock_code)
            self.active_target = key
            await self.poll_realtime_quote(key, force=True)
        elif target in ("TXFF", "TWII"):
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
