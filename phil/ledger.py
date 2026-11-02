"""모의 브로커: 한국 주식 스윙 매매. PROTECTED.

- 매수: 장중에만, 실시간 현재가 x (1+슬리피지) + 수수료. 수량은 정수.
- 청산: resolve.py 가 일봉으로 손절/목표/만기를 판정 (에이전트가 조기 매도도 가능).
- journal/ledger.jsonl 의 작성자는 이 파일과 resolve.py 뿐.

포지션 row 의 status: open → closed. 별도로 'calib'(만기 종가 > 매수가 여부)를 기록해
에이전트 확률의 정확도를 손절 여부와 무관하게 잰다.
"""
import math
import os
import uuid

from . import market, naver, policy
from .config import LEDGER, append_jsonl, dump_jsonl, iso, ledger_lock, load_config, load_jsonl


class Rejected(Exception):
    pass


def read_ledger():
    return load_jsonl(LEDGER)


def buy_cost(price, qty, costs):
    fill = price * (1 + costs["slippage"])
    gross = fill * qty
    return fill, round(gross + gross * costs["commission"])


def sell_proceeds(price, qty, costs):
    fill = price * (1 - costs["slippage"])
    gross = fill * qty
    return fill, round(gross - gross * costs["commission"] - gross * costs["sell_tax"])


def cash(entries, cfg=None):
    cfg = cfg or load_config()
    c = cfg["sim_bankroll_krw"]
    for e in entries:
        c -= e["cost_krw"]
        if e["status"] == "closed":
            c += e["proceeds_krw"]
    return c


def equity(entries=None, cfg=None, prices=None):
    """현금 + 보유 종목 평가액(현재가 기준, 매도비용 차감)."""
    cfg = cfg or load_config()
    entries = entries if entries is not None else read_ledger()
    total = cash(entries, cfg)
    for e in entries:
        if e["status"] == "open":
            p = (prices or {}).get(e["code"]) or e.get("last_price") or e["entry_price"]
            total += sell_proceeds(p, e["qty"], cfg["costs"])[1]
    return total


def buy(code, est_prob, horizon_days, stop_pct, target_pct, edge_class, rationale,
        amount_krw=None, cycle_id=None, now=None):
    cfg = load_config()
    lim, costs = cfg["limits"], cfg["costs"]
    entries = read_ledger()
    open_pos = [e for e in entries if e["status"] == "open"]
    cycle_id = cycle_id or os.environ.get("PHIL_CYCLE_ID") or "manual"

    ok, why = market.trading_window(now)
    if not ok:
        raise Rejected(f"지금은 매수할 수 없음: {why}")
    if len(open_pos) >= lim["max_open_positions"]:
        raise Rejected(f"최대 보유 종목 수({lim['max_open_positions']}) 도달")
    if cycle_id != "manual" and sum(1 for e in entries if e.get("cycle_id") == cycle_id) \
            >= lim["max_new_positions_per_cycle"]:
        raise Rejected(f"사이클당 신규 매수 한도({lim['max_new_positions_per_cycle']}) 도달")
    if any(e["code"] == code for e in open_pos):
        raise Rejected("이미 보유 중인 종목")
    if not lim["horizon_days_min"] <= horizon_days <= lim["horizon_days_max"]:
        raise Rejected(f"보유 기간은 {lim['horizon_days_min']}~{lim['horizon_days_max']} 거래일")
    if not lim["stop_pct_min"] <= stop_pct <= lim["stop_pct_max"]:
        raise Rejected(f"손절폭은 {lim['stop_pct_min']:.0%}~{lim['stop_pct_max']:.0%}")
    if target_pct is not None and target_pct <= 0:
        raise Rejected("목표 수익률은 양수")
    if not 0.0 < est_prob < 1.0:
        raise Rejected("est-prob 는 (0,1) 범위")
    if not rationale or len(rationale.strip()) < 10:
        raise Rejected("rationale(근거)를 한 줄 이상 적어야 함")
    why = policy.check_edge_class(edge_class, cfg)
    if why:
        raise Rejected(why)

    uni = market.universe()
    if code not in uni:
        raise Rejected("유니버스(시총 상위 300, 우선주·스팩 제외) 밖의 종목")
    q = naver.quote(code)
    if not q["tradable"]:
        raise Rejected("거래 정지 또는 매매 불가 종목")
    if q["market_status"] != "OPEN":
        raise Rejected(f"장 상태 {q['market_status']}")
    bars = naver.daily_bars(code, 260)
    br = market.base_rate(bars, horizon_days)
    edge = round(est_prob - br, 4)
    pol = cfg["policy"]
    exp = pol.get("exploration") or {}
    exploration = False
    if edge < pol["min_edge_vs_base_rate"]:
        if not exp or edge < exp["min_edge"]:
            raise Rejected(f"엣지 부족: 내 확률 {est_prob} − 기저율 {br} = {edge:+.3f} "
                           f"< 탐색 최소 {exp.get('min_edge', pol['min_edge_vs_base_rate'])}")
        today = market.kst_date(iso())
        n_today = sum(1 for x in entries if x.get("exploration") and market.kst_date(x["ts"]) == today)
        if n_today >= exp["max_per_day"]:
            raise Rejected(f"오늘 탐색 매수 한도({exp['max_per_day']}건) 도달 — 엣지 {edge:+.3f} 는 "
                           f"정규 매수 기준 {pol['min_edge_vs_base_rate']} 미만")
        exploration = True

    eq = equity(entries, cfg)
    max_amt = eq * (exp["position_pct"] if exploration else lim["max_position_pct"])
    amount = min(amount_krw or max_amt, max_amt, cash(entries, cfg))
    price = q["price"]
    qty = math.floor(amount / (price * (1 + costs["slippage"]) * (1 + costs["commission"])))
    if qty < 1:
        raise Rejected("1주도 살 수 없는 금액")
    fill, cost = buy_cost(price, qty, costs)

    e = {
        "id": uuid.uuid4().hex[:12], "ts": iso(), "cycle_id": cycle_id,
        "code": code, "name": q["name"], "market": uni[code]["market"],
        "qty": qty, "quote_price": price, "entry_price": round(fill, 2), "cost_krw": cost,
        "horizon_days": horizon_days,
        "stop_price": round(price * (1 - stop_pct), 2),
        "target_price": round(price * (1 + target_pct), 2) if target_pct else None,
        "est_prob": round(est_prob, 4), "base_rate": br, "edge": edge,
        "edge_class": edge_class, "rationale": rationale.strip(), "exploration": exploration,
        "status": "open", "last_price": price,
    }
    # 네트워크 조회는 잠금 밖에서, 최종 확인과 기록은 잠금 안에서 (다른 프로세스와 경합 방지)
    with ledger_lock():
        entries = read_ledger()
        if any(x["code"] == code and x["status"] == "open" for x in entries):
            raise Rejected("이미 보유 중인 종목")
        if sum(1 for x in entries if x["status"] == "open") >= lim["max_open_positions"]:
            raise Rejected(f"최대 보유 종목 수({lim['max_open_positions']}) 도달")
        if cost > cash(entries, cfg):
            raise Rejected("모의 현금 부족")
        append_jsonl(LEDGER, e)
    return e


def close_position(position_id, price, reason):
    """보유 포지션을 주어진 가격(슬리피지·수수료·세금 차감)으로 청산. 이미 청산됐으면 None."""
    cfg = load_config()
    with ledger_lock():
        rows = read_ledger()
        for e in rows:
            if e["id"] == position_id and e["status"] == "open":
                fill, proceeds = sell_proceeds(price, e["qty"], cfg["costs"])
                e.update(status="closed", exit_price=round(fill, 2), proceeds_krw=proceeds,
                         exit_reason=reason, exit_ts=iso(),
                         exit_date=market.kst_date(iso()),
                         pnl_krw=proceeds - e["cost_krw"],
                         ret_pct=round((proceeds / e["cost_krw"] - 1) * 100, 2))
                dump_jsonl(LEDGER, rows)
                return e
    return None


def sell(position_id, reason, now=None):
    """에이전트의 조기 매도 (장중, 현재가)."""
    ok, why = market.trading_window(now)
    if not ok:
        raise Rejected(f"지금은 매도할 수 없음: {why}")
    e = next((x for x in read_ledger() if x["id"] == position_id and x["status"] == "open"), None)
    if not e:
        raise Rejected("보유 중인 포지션 id 가 아님")
    if market.kst_date(e["ts"]) == market.kst_date(iso()):
        raise Rejected("당일 매수한 종목은 당일 매도 불가 (스윙 규칙)")
    q = naver.quote(e["code"])
    closed = close_position(position_id, q["price"], f"조기매도: {reason.strip()[:200]}")
    if not closed:
        raise Rejected("이미 청산된 포지션")
    return closed


def status(refresh_prices=True):
    cfg = load_config()
    entries = read_ledger()
    open_pos = [e for e in entries if e["status"] == "open"]
    prices = {}
    if refresh_prices and open_pos:
        uni = market.universe(refresh=True)
        prices = {e["code"]: (uni.get(e["code"]) or {}).get("price") for e in open_pos}
    closed = [e for e in entries if e["status"] == "closed"]
    return {
        "cash_krw": cash(entries, cfg),
        "equity_krw": equity(entries, cfg, prices),
        "bankroll_start_krw": cfg["sim_bankroll_krw"],
        "open_positions": len(open_pos),
        "closed": len(closed),
        "wins": sum(1 for e in closed if e["pnl_krw"] > 0),
        "realized_pnl_krw": sum(e["pnl_krw"] for e in closed),
        "open": [{
            "id": e["id"], "code": e["code"], "name": e["name"], "qty": e["qty"],
            "entry": e["entry_price"], "now": prices.get(e["code"]) or e.get("last_price"),
            "unrealized_pct": round(((prices.get(e["code"]) or e["entry_price"]) / e["entry_price"] - 1) * 100, 2),
            "stop": e["stop_price"], "target": e["target_price"], "horizon_days": e["horizon_days"],
            "bought": e["ts"], "est_prob": e["est_prob"], "base_rate": e["base_rate"],
            "edge_class": e["edge_class"], "exploration": e.get("exploration", False)}
            for e in open_pos],
    }
