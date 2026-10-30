"""종목 유니버스, 장 운영 시간, 기저율, 후보 탐색. PROTECTED."""
import datetime as dt

from . import naver
from .config import UNIVERSE_CACHE, load_config, load_json, now_kst, save_json

EXCLUDE_NAME = ("스팩", "ETN", "리츠")


def _is_preferred(name):
    return name.endswith(("우", "우B", "우C")) or name.endswith(("1우", "2우B", "3우B"))


def universe(refresh=False):
    """{code: {name, market, rank, price, change_pct, value_mil}} — 하루 한 번 캐시."""
    """refresh=True 면 5분 넘은 캐시를 새로 받는다 (시세 갱신용)."""
    import time
    today = now_kst().date().isoformat()
    cache = load_json(UNIVERSE_CACHE, {})
    fresh = cache.get("date") == today and cache.get("stocks")
    if fresh and (not refresh or time.time() - cache.get("fetched", 0) < 300):
        return cache["stocks"]
    cfg = load_config()["universe"]
    out = {}
    for market, top in (("KOSPI", cfg["kospi_top"]), ("KOSDAQ", cfg["kosdaq_top"])):
        rank, page = 0, 1
        while rank < top and page <= 6:
            rows, _ = naver.ranking(market, page, 100)
            if not rows:
                break
            for s in rows:
                name = s.get("stockName") or ""
                if s.get("stockEndType") != "stock" or _is_preferred(name) \
                        or any(x in name for x in EXCLUDE_NAME):
                    continue
                if (s.get("tradeStopType") or {}).get("name") != "TRADING":
                    continue
                price = naver.num(s.get("closePrice"))
                if not price or price < cfg["min_price_krw"]:
                    continue
                rank += 1
                if rank > top:
                    break
                out[s["itemCode"]] = {
                    "name": name, "market": market, "rank": rank, "price": price,
                    "change_pct": naver.signed_pct(s.get("fluctuationsRatio"),
                                                   s.get("compareToPreviousPrice")),
                    "value_mil": naver.num(s.get("accumulatedTradingValue")) or 0,
                    "mcap_eok": naver.num(s.get("marketValue")) or 0,
                }
            page += 1
    save_json(UNIVERSE_CACHE, {"date": today, "fetched": time.time(), "stocks": out})
    return out


def trading_window(now=None):
    """(열림 여부, 사유). 평일 trade_start~trade_end 이고 네이버가 장 OPEN 이라고 할 때만."""
    lim = load_config()["limits"]
    now = now or now_kst()
    if now.weekday() >= 5:
        return False, "주말"
    hhmm = now.strftime("%H:%M")
    if not lim["trade_start"] <= hhmm <= lim["trade_end"]:
        return False, f"매매 가능 시간({lim['trade_start']}~{lim['trade_end']}) 아님"
    try:
        st = naver.index_quote("KOSPI")["market_status"]
    except naver.DataError as e:
        return False, f"시세 조회 실패: {e}"
    if st != "OPEN":
        return False, f"장 상태 {st} (휴장일 등)"
    return True, "장중"


def base_rate(bars, horizon):
    """최근 bars 에서 'horizon 거래일 뒤 종가 > 오늘 종가' 였던 비율 (Laplace 보정)."""
    closes = [b[4] for b in bars]
    n = len(closes) - horizon
    if n < 30:
        return 0.5
    ups = sum(1 for i in range(n) if closes[i + horizon] > closes[i])
    return round((ups + 1) / (n + 2), 4)


def stock_detail(code):
    u = universe()
    q = naver.quote(code)
    bars = naver.daily_bars(code, 260)
    closes = [b[4] for b in bars]

    def ret(n):
        return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None

    vols = [b[5] for b in bars[-21:-1]]
    try:
        inf = naver.info(code)
    except naver.DataError:
        inf = {}
    return {
        "code": code, "name": q["name"], "in_universe": code in u,
        "price": q["price"], "change_pct": q["change_pct"], "market_status": q["market_status"],
        "tradable": q["tradable"], "exchange": q["exchange"],
        "returns_pct": {"5d": ret(5), "20d": ret(20), "60d": ret(60), "250d": ret(250)},
        "volume_vs_20d_avg": round(bars[-1][5] / (sum(vols) / len(vols)), 2) if vols and sum(vols) else None,
        "base_rate_up": {f"{h}d": base_rate(bars, h) for h in range(1, 6)},
        "last_10_bars": [{"date": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4],
                          "volume": int(b[5])} for b in bars[-10:]],
        "info": {k: inf.get(k) for k in ("시총", "PER", "추정PER", "PBR", "배당수익률", "외인소진율",
                                        "52주 최고", "52주 최저", "대금") if k in inf},
        "url": f"https://m.stock.naver.com/domestic/stock/{code}",
    }


def scan(sort="value", query=None, limit=40, market=None):
    """유니버스에서 후보 목록. sort: value(거래대금) | up(상승률) | down(하락률) | rank(시총)."""
    rows = [dict(code=c, **v) for c, v in universe(refresh=True).items()]
    if market:
        rows = [r for r in rows if r["market"] == market.upper()]
    if query:
        rows = [r for r in rows if query in r["name"] or query == r["code"]]
    key = {"value": lambda r: -r["value_mil"], "up": lambda r: -(r["change_pct"] or 0),
           "down": lambda r: (r["change_pct"] or 0),
           "rank": lambda r: (r["market"] != "KOSPI", r["rank"])}.get(sort, lambda r: -r["value_mil"])
    rows.sort(key=key)
    return rows[:limit]


def market_overview():
    out = {}
    for s in ("KOSPI", "KOSDAQ"):
        try:
            out[s] = naver.index_quote(s)
        except naver.DataError as e:
            out[s] = {"error": str(e)}
    ok, why = trading_window()
    out["can_trade_now"] = ok
    out["reason"] = why
    out["now_kst"] = now_kst().strftime("%Y-%m-%d %H:%M")
    return out


def trading_day_after(bars, start_date, n):
    """bars(오래된 순) 에서 start_date 이후 n 번째 거래일 bar, 없으면 None."""
    after = [b for b in bars if b[0] > start_date]
    return after[n - 1] if len(after) >= n else None


def kst_date(ts_iso):
    from .config import KST, parse_ts
    return parse_ts(ts_iso).astimezone(KST).date().isoformat()


__all__ = ["universe", "trading_window", "base_rate", "stock_detail", "scan", "market_overview",
           "trading_day_after", "kst_date", "dt"]
