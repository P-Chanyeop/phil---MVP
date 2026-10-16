"""Polymarket 공개 API (읽기 전용, 표준 라이브러리만). PROTECTED.

원본: bennyjo/phil core/pmapi.py (Apache-2.0) 를 기반으로 단순화.
"""
import json
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "phil-mvp/0.1"}


def get_json(url, params=None, retries=3, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET {url} 실패: {last}")


def gamma_markets(**params):
    return get_json(f"{GAMMA}/markets", params)


def gamma_market(market_id):
    return get_json(f"{GAMMA}/markets/{market_id}")


def clob_book(token_id):
    return get_json(f"{CLOB}/book", {"token_id": token_id})


def best_prices(token_id):
    """(best_bid, best_ask, ask_size) — 비어 있으면 None."""
    book = clob_book(token_id)
    bids = [float(b["price"]) for b in book.get("bids", [])]
    asks = sorted(((float(a["price"]), float(a.get("size", 0))) for a in book.get("asks", [])))
    return (max(bids) if bids else None,
            asks[0][0] if asks else None,
            asks[0][1] if asks else 0.0)


def _loads(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "null")
    except (ValueError, TypeError):
        return None


def market_tokens(m):
    """outcome 이름 -> CLOB token id. 읽을 수 없으면 None."""
    if (m.get("version") or "v1") != "v1":
        return None
    ids, outs = _loads(m.get("clobTokenIds")), _loads(m.get("outcomes"))
    if not (isinstance(ids, list) and isinstance(outs, list) and ids and len(ids) == len(outs)):
        return None
    return dict(zip(outs, ids))


def outcome_prices(m):
    outs, prices = _loads(m.get("outcomes")) or [], _loads(m.get("outcomePrices")) or []
    try:
        return {o: float(p) for o, p in zip(outs, prices)}
    except (TypeError, ValueError):
        return {}


def taker_fee_per_share(m, price, default=None):
    """주당 테이커 수수료 = rate * (p*(1-p))**exponent (py-clob-client-v2 fees.py 와 동일).
    feesEnabled 가 False 면 0. 스케줄이 없으면 default."""
    if m.get("feesEnabled") is False:
        return 0.0
    fs = m.get("feeSchedule") or default
    if not fs:
        return 0.0
    rate, exp = float(fs.get("rate", 0)), float(fs.get("exponent", 1))
    return round(rate * (price * (1 - price)) ** exp, 6)
