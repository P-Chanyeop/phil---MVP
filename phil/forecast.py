"""예측 기록 (돈을 걸지 않는 확률 기록). PROTECTED.

질문은 항상 같다: 'N 거래일 뒤 종가가 지금 가격보다 높을까?' — 비교 기준은 그 종목의
과거 기저율. 많이 쌓일수록 어떤 근거(edge class)에서 내가 기저율보다 나은지 빨리 드러난다.
"""
import os
import uuid

from . import market, naver
from .config import FORECASTS, append_jsonl, iso, load_config, load_jsonl


class Rejected(Exception):
    pass


def record(code, horizon_days, est_prob, edge_class, note="", cycle_id=None, now=None):
    cfg = load_config()
    lim = cfg["limits"]
    cycle_id = cycle_id or os.environ.get("PHIL_CYCLE_ID") or "manual"
    rows = load_jsonl(FORECASTS)
    ok, why = market.trading_window(now)
    if not ok:
        raise Rejected(f"예측은 장중에만 기록 (기준 가격이 살아 있어야 함): {why}")
    if not 0.0 < est_prob < 1.0:
        raise Rejected("est-prob 는 (0,1) 범위")
    if not lim["horizon_days_min"] <= horizon_days <= lim["horizon_days_max"]:
        raise Rejected(f"기간은 {lim['horizon_days_min']}~{lim['horizon_days_max']} 거래일")
    if edge_class not in cfg["policy"]["edge_classes"]:
        raise Rejected(f"edge-class 는 {cfg['policy']['edge_classes']} 중 하나")
    if cycle_id != "manual" and sum(1 for r in rows if r.get("cycle_id") == cycle_id) \
            >= lim["max_forecasts_per_cycle"]:
        raise Rejected(f"사이클당 예측 한도({lim['max_forecasts_per_cycle']}) 도달")
    if any(r["code"] == code and r["status"] == "open" for r in rows):
        raise Rejected("이 종목에 이미 열린 예측이 있음 (종목당 하나)")
    if code not in market.universe():
        raise Rejected("유니버스 밖의 종목")
    q = naver.quote(code)
    bars = naver.daily_bars(code, 260)
    row = {"id": uuid.uuid4().hex[:12], "ts": iso(), "cycle_id": cycle_id, "code": code,
           "name": q["name"], "horizon_days": horizon_days, "ref_price": q["price"],
           "ref_date": market.kst_date(iso()), "est_prob": round(est_prob, 4),
           "base_rate": market.base_rate(bars, horizon_days), "edge_class": edge_class,
           "note": (note or "").strip()[:500], "status": "open"}
    append_jsonl(FORECASTS, row)
    return row
