"""네이버 증권 시세 (읽기 전용, 표준 라이브러리만). PROTECTED.

비공식 공개 API 라서 네이버가 형식을 바꾸면 이 파일만 고치면 된다.
"""
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request

M = "https://m.stock.naver.com/api"
FCHART = "https://fchart.stock.naver.com/sise.nhn"
UA = {"User-Agent": "Mozilla/5.0 (phil-mvp)", "Referer": "https://m.stock.naver.com/"}


class DataError(RuntimeError):
    pass


def _get(url, params=None, retries=3, timeout=15, raw=False):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return body if raw else json.loads(body)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (attempt + 1))
    raise DataError(f"네이버 증권 조회 실패 ({url[:80]}): {last}")


def num(s):
    """'217,000' / '3.33' / '-1,200' → float. 실패 시 None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?[\d,]*\.?\d+", str(s))
    return float(m.group(0).replace(",", "")) if m else None


def quote(code):
    """현재가(장중) 또는 종가."""
    d = _get(f"{M}/stock/{code}/basic")
    if d.get("stockEndType") != "stock":
        raise DataError(f"{code} 는 주식이 아님 ({d.get('stockEndType')})")
    return {
        "code": code,
        "name": d.get("stockName"),
        "price": num(d.get("closePrice")),
        "change_pct": num(d.get("fluctuationsRatio")) * (-1 if (d.get("compareToPreviousPrice") or {}).get("name") in ("FALLING", "LOWER_LIMIT") else 1),
        "market_status": d.get("marketStatus"),
        "tradable": d.get("tradableStatusCode") == "ok"
                    and (d.get("tradeStopType") or {}).get("name") == "TRADING",
        "exchange": (d.get("stockExchangeType") or {}).get("name"),
        "traded_at": d.get("localTradedAt"),
    }


def info(code):
    """시가·고가·저가·거래대금·시총·PER·PBR·52주 등."""
    d = _get(f"{M}/stock/{code}/integration")
    out = {i.get("key"): i.get("value") for i in d.get("totalInfos", [])}
    out["업종"] = (d.get("industryCode") or "")
    return out


def index_quote(symbol="KOSPI"):
    d = _get(f"{M}/index/{symbol}/basic")
    return {"symbol": symbol, "name": d.get("stockName"), "price": num(d.get("closePrice")),
            "change_pct": num(d.get("fluctuationsRatio")) * (-1 if (d.get("compareToPreviousPrice") or {}).get("name") == "FALLING" else 1),
            "market_status": d.get("marketStatus")}


def daily_bars(symbol, count=260):
    """[(date, open, high, low, close, volume)] 오래된 순. symbol: 종목코드 또는 KOSPI/KOSDAQ."""
    body = _get(FCHART, {"symbol": symbol, "timeframe": "day", "count": count,
                         "requestType": 0}, raw=True)
    text = body.decode("euc-kr", errors="replace")
    bars = []
    for m in re.finditer(r'data="([^"]+)"', text):
        p = m.group(1).split("|")
        if len(p) < 6:
            continue
        try:
            d = dt.date(int(p[0][:4]), int(p[0][4:6]), int(p[0][6:8]))
            o, h, lo, c = (float(x) for x in p[1:5])
            v = float(p[5]) if p[5] not in ("", "null") else 0.0
        except ValueError:
            continue
        if c > 0:
            bars.append((d.isoformat(), o, h, lo, c, v))
    if not bars:
        raise DataError(f"{symbol} 일봉 없음")
    return bars


def ranking(market="KOSPI", page=1, size=100):
    d = _get(f"{M}/stocks/marketValue/{market}", {"page": page, "pageSize": size})
    return d.get("stocks", []), d.get("marketStatus")


def news(code, n=10):
    d = _get(f"{M}/news/stock/{code}", {"pageSize": n, "page": 1})
    out = []
    for grp in d if isinstance(d, list) else []:
        for it in grp.get("items", []):
            out.append({"datetime": it.get("datetime"), "office": it.get("officeName"),
                        "title": it.get("titleFull") or it.get("title"),
                        "summary": (it.get("body") or "")[:200],
                        "url": it.get("mobileNewsUrl")})
    return out[:n]


def ping():
    """데이터 접속 확인. (ok, 메시지)"""
    try:
        q = index_quote("KOSPI")
        return True, f"네이버 증권 연결 OK (코스피 {q['price']:,.2f}, {q['market_status']})"
    except DataError as e:
        return False, str(e)
