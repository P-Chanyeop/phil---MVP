"""예측 기록 (돈을 걸지 않는 확률 기록). PROTECTED.

조사한 후보는 베팅하든 안 하든 확률을 남긴다. 정산되면 카테고리별 정확도가 쌓이고,
시장보다 못하는 카테고리는 엔진이 자동으로 베팅을 금지한다 (policy.auto_ban).
journal/forecasts.jsonl 의 작성자는 이 파일과 resolve.py 뿐.
"""
import os
import uuid

from . import pmapi
from .config import FORECASTS, append_jsonl, iso, load_config, load_jsonl, now_utc, parse_ts


class Rejected(Exception):
    pass


def record(market_id, outcome, est_prob, category, note="", cycle_id=None):
    cfg = load_config()
    cycle_id = cycle_id or os.environ.get("PHIL_CYCLE_ID") or "manual"
    rows = load_jsonl(FORECASTS)
    if not 0.0 < est_prob < 1.0:
        raise Rejected("est-prob 는 (0,1) 범위")
    if category not in cfg["policy"]["categories"]:
        raise Rejected(f"category 는 {cfg['policy']['categories']} 중 하나")
    if cycle_id != "manual" and sum(1 for r in rows if r.get("cycle_id") == cycle_id) \
            >= cfg["max_forecasts_per_cycle"]:
        raise Rejected(f"사이클당 예측 기록 한도({cfg['max_forecasts_per_cycle']}) 도달")
    if any(r["market_id"] == str(market_id) and r["status"] == "open" for r in rows):
        raise Rejected("이 마켓에 이미 열린 예측이 있음 (마켓당 하나)")
    m = pmapi.gamma_market(market_id)
    if m.get("closed"):
        raise Rejected("마감된 마켓")
    end = parse_ts(m.get("endDate"))
    if not end or end <= now_utc():
        raise Rejected("마감 시각이 지났거나 알 수 없음")
    tokens = pmapi.market_tokens(m)
    if not tokens or outcome not in tokens:
        raise Rejected(f"outcome 은 {list(tokens or {})} 중 하나")
    bid, ask, _ = pmapi.best_prices(tokens[outcome])
    if bid is None or ask is None:
        raise Rejected("호가가 비어 있어 시장 확률을 알 수 없음")
    row = {"id": uuid.uuid4().hex[:12], "ts": iso(), "cycle_id": cycle_id,
           "market_id": str(market_id), "question": m.get("question"), "slug": m.get("slug"),
           "end_date": m.get("endDate"), "outcome": outcome, "token_id": tokens[outcome],
           "est_prob": round(est_prob, 4), "best_bid_at_record": bid, "best_ask_at_record": ask,
           "market_prob_at_record": round((bid + ask) / 2, 4), "category": category,
           "note": (note or "").strip()[:500], "status": "open"}
    append_jsonl(FORECASTS, row)
    return row
