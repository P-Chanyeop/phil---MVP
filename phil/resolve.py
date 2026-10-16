"""오픈 모의 포지션을 Polymarket 공식 결과로 정산. PROTECTED.

마감 + 한쪽 가격 >= 0.99 → won/lost. 마감 후 48시간 넘게 결정이 안 나면 void(원금 반환).
원본: bennyjo/phil core/resolve.py (Apache-2.0) 기반.
"""
import datetime as dt
import sys

from . import pmapi
from .config import FORECASTS, LEDGER, dump_jsonl, iso, load_jsonl, now_utc, parse_ts

VOID_GRACE_HOURS = 48


def settle_row(e, m, now):
    if not m.get("closed"):
        return False
    prices = pmapi.outcome_prices(m)
    decisive = {o for o, p in prices.items() if p >= 0.99}
    if decisive:
        won = e["outcome"] in decisive
        e["status"] = "won" if won else "lost"
        if "shares" in e:
            e["pnl_usd"] = round(e["shares"] - e["stake_usd"], 4) if won else -e["stake_usd"]
        e["outcome_won"] = max(prices.items(), key=lambda kv: kv[1])[0]
        e["settled_ts"] = iso(parse_ts(m.get("closedTime")) or now)
        return True
    end = parse_ts(e["end_date"])
    if end and now - end > dt.timedelta(hours=VOID_GRACE_HOURS):
        e["status"] = "void"
        if "shares" in e:
            e["pnl_usd"] = 0.0
        e["settled_ts"] = iso(now)
        return True
    return False


def _settle_file(path, now, cache):
    rows = load_jsonl(path)
    settled = []
    for e in rows:
        if e["status"] != "open":
            continue
        end = parse_ts(e["end_date"])
        if end and end > now:
            continue  # 아직 마감 전
        mid = e["market_id"]
        if mid not in cache:
            try:
                cache[mid] = pmapi.gamma_market(mid)
            except RuntimeError as err:
                print(f"resolve: {mid} 조회 실패: {err}", file=sys.stderr)
                cache[mid] = None
        if cache[mid] is not None and settle_row(e, cache[mid], now):
            settled.append(e)
    if settled:
        dump_jsonl(path, rows)
    return rows, settled


def settle_all():
    now = now_utc()
    cache = {}
    entries, settled = _settle_file(LEDGER, now, cache)
    forecasts, fsettled = _settle_file(FORECASTS, now, cache)
    return {"settled": [{"id": e["id"], "status": e["status"], "pnl": e.get("pnl_usd"),
                         "est": e["est_prob"], "entry": e["entry_price"],
                         "edge_class": e["edge_class"], "q": e["question"][:90]}
                        for e in settled],
            "still_open": sum(1 for e in entries if e["status"] == "open"),
            "forecasts_settled": [{"id": r["id"], "status": r["status"], "est": r["est_prob"],
                                   "mkt": r["market_prob_at_record"], "category": r["category"],
                                   "q": r["question"][:90]} for r in fsettled],
            "forecasts_open": sum(1 for r in forecasts if r["status"] == "open")}
