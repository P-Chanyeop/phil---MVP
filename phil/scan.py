"""단기 Polymarket 후보 마켓 탐색. PROTECTED.

결과 확정이 min_minutes ~ max_days 사이인 활성 마켓을 거래량 순으로 가져와,
금지 유형과 이미 결론이 난(가격 극단) 마켓을 걸러낸다.
"""
import datetime as dt
import re

from . import pmapi
from .config import iso, load_config, now_utc, parse_ts


def _category(m):
    ev = (m.get("events") or [{}])[0]
    return m.get("sportsMarketType") and "sports" or (ev.get("category") or ev.get("seriesSlug") or "")


def scan(limit=40, query=None, max_days=None, pages=4):
    cfg = load_config()
    n = now_utc()
    max_days = max_days or cfg["max_days_to_resolution"]
    params = dict(active="true", closed="false", limit=100, order="volume24hr",
                  ascending="false",
                  end_date_min=iso(n + dt.timedelta(minutes=cfg["min_minutes_to_resolution"])),
                  end_date_max=iso(n + dt.timedelta(days=max_days)))
    out, seen = [], set()
    q = (query or "").lower().strip()
    for page in range(pages):
        rows = pmapi.gamma_markets(offset=page * 100, **params)
        if not rows:
            break
        for m in rows:
            if m["id"] in seen or not m.get("enableOrderBook", True):
                continue
            seen.add(m["id"])
            question = m.get("question") or ""
            if any(re.search(p, question) for p in cfg["banned_question_patterns"]):
                continue
            if q and q not in question.lower() and q not in (m.get("description") or "").lower()[:400]:
                continue
            prices = pmapi.outcome_prices(m)
            if not prices or pmapi.market_tokens(m) is None:
                continue
            if max(prices.values()) > 0.97 or min(prices.values()) < 0.03:
                continue  # 이미 사실상 결론난 마켓
            end = parse_ts(m.get("endDate"))
            out.append({
                "id": m["id"],
                "question": question,
                "prices": {k: round(v, 3) for k, v in prices.items()},
                "best_ask": m.get("bestAsk"),
                "spread": m.get("spread"),
                "hours_left": round((end - n).total_seconds() / 3600, 1) if end else None,
                "end_date": m.get("endDate"),
                "volume24h": round(float(m.get("volume24hr") or 0)),
                "liquidity": round(float(m.get("liquidityNum") or 0)),
                "event": (m.get("events") or [{}])[0].get("title"),
                "slug": m.get("slug"),
            })
            if len(out) >= limit:
                return out
    return out


def market_detail(market_id):
    m = pmapi.gamma_market(market_id)
    tokens = pmapi.market_tokens(m) or {}
    books = {}
    for outcome, tok in tokens.items():
        try:
            bid, ask, size = pmapi.best_prices(tok)
            books[outcome] = {"bid": bid, "ask": ask, "ask_size": size}
        except RuntimeError as e:
            books[outcome] = {"error": str(e)}
    return {
        "id": m["id"],
        "question": m.get("question"),
        "description": (m.get("description") or "")[:3000],
        "resolution_source": m.get("resolutionSource"),
        "end_date": m.get("endDate"),
        "closed": m.get("closed"),
        "prices": pmapi.outcome_prices(m),
        "books": books,
        "volume24h": m.get("volume24hr"),
        "event": (m.get("events") or [{}])[0].get("title"),
        "url": f"https://polymarket.com/market/{m.get('slug')}",
    }
