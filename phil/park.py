"""대기 자금 — 놀고 있는 현금을 코스피200 ETF 에 넣어 두기. PROTECTED.

- 엔진(감시 루프)만 사고판다. 한 시간에 한 번, 장중 09:10~15:10 에 현금이 넘치면 ETF 를 산다
  (자산의 5% 는 현금으로 남김, 자산의 3% 미만 차이는 거래하지 않음 — 수수료 낭비 방지).
- 시장 조건(gate): 하락장(코스피 60일선 아래)·코스피 급락일·안전장치 작동 중에는 새로 넣지 않는다.
  하락장에서는 대기 자금을 자산의 bear_max_pct 까지만 두고 넘치는 만큼 하루 한 번 판다.
- 한 번에 넣는 양은 자산의 max_tranche_pct 까지 (한 번에 몰아 넣어 급락일에 잡히는 일 방지).
- 종목 매수에 현금이 모자라면 그 자리에서 필요한 만큼 ETF 를 판다.
- ETF 는 증권거래세가 없어서 수수료·슬리피지만 든다.
- journal/park.json: {"qty", "cost_krw"(남은 수량의 매입원가), "net_out"(누적 순투입 현금), "last_price", "trades": [...]}
"""
import math

from . import naver
from .config import PARK, iso, ledger_lock, load_config, load_json, operator_state, save_json

STATE0 = {"qty": 0, "cost_krw": 0, "net_out": 0, "last_price": None, "trades": []}
GATE = {"reason": None, "ts": None}   # 마지막 판정 (화면용)


def state():
    return dict(STATE0, **load_json(PARK, {}))


def enabled():
    return operator_state().get("park_enabled", True)


def value(price=None):
    """지금 팔면 받을 돈 (수수료·슬리피지 차감, 거래세 없음)."""
    s = state()
    p = price or s["last_price"]
    if not s["qty"] or not p:
        return 0
    c = load_config()["costs"]
    return round(s["qty"] * p * (1 - c["slippage"]) * (1 - c["commission"]))


def net_out():
    return state()["net_out"]


def _price():
    q = naver.quote(load_config()["park"]["code"], allow_etf=True)
    if not q["tradable"]:
        raise naver.DataError(f"대기 자금 ETF 일시 거래 불가 (네이버 상태 {q.get('status')})")
    return q["price"]


def _trade(side, qty, price, why):
    c = load_config()["costs"]
    with ledger_lock():
        s = state()
        if side == "buy":
            cost = round(qty * price * (1 + c["slippage"]) * (1 + c["commission"]))
            s["qty"] += qty
            s["cost_krw"] += cost
            s["net_out"] += cost
            amt = cost
        else:
            qty = min(qty, s["qty"])
            if qty <= 0:
                return None
            proceeds = round(qty * price * (1 - c["slippage"]) * (1 - c["commission"]))
            s["cost_krw"] = round(s["cost_krw"] * (s["qty"] - qty) / s["qty"]) if s["qty"] else 0
            s["qty"] -= qty
            s["net_out"] -= proceeds
            amt = proceeds
        s["last_price"] = price
        s["trades"] = (s["trades"] + [{"ts": iso(), "side": side, "qty": qty, "price": price,
                                       "amount": amt, "why": why}])[-200:]
        save_json(PARK, s)
    return {"side": side, "qty": qty, "amount": amt}


def raise_cash(need_krw, why="종목 매수 자금"):
    """현금이 need 만큼 더 필요하면 ETF 를 팔아 마련. 판 금액 반환 (없으면 0)."""
    s = state()
    if need_krw <= 0 or not s["qty"]:
        return 0
    price = _price()
    c = load_config()["costs"]
    per = price * (1 - c["slippage"]) * (1 - c["commission"])
    qty = min(s["qty"], math.ceil(need_krw * 1.002 / per))
    r = _trade("sell", qty, price, why)
    return r["amount"] if r else 0


def gate(now=None):
    """대기 자금을 새로 넣으면 안 되는 이유. 없으면 None. (시세 조회 실패도 '넣지 않음')"""
    from . import market, safety
    cfg = load_config()["park"]
    reason = None
    if cfg.get("regime_gate", True):
        paused, why = safety.paused(now)
        if paused:
            reason = f"안전장치 작동 중 ({why})"
        else:
            rg = market.regime_now()
            if rg is None:
                reason = "장세 판정 불가 (코스피 일봉 조회 실패)"
            elif rg["label"] == "하락장":
                reason = f"하락장 (코스피 60일선 대비 {rg['kospi_vs_ma60']:+.1f}%)"
            else:
                try:
                    chg = naver.index_quote("KOSPI")["change_pct"]
                except naver.DataError:
                    chg = None
                if chg is not None and chg <= cfg.get("kospi_day_drop_pct", -2.0):
                    reason = f"코스피 급락일 ({chg:+.1f}%)"
    GATE.update(reason=reason, ts=iso())
    return reason


def _bear_reduce(equity_krw, now):
    """하락장: 대기 자금이 자산의 bear_max_pct 를 넘으면 넘치는 만큼 판다 (하루 한 번)."""
    cfg = load_config()["park"]
    s = state()
    if not s["qty"]:
        return None
    today = now.strftime("%Y-%m-%d")
    if any(t["side"] == "sell" and "하락장" in t["why"] and t["ts"][:10] == today for t in s["trades"]):
        return None
    cap = equity_krw * cfg.get("bear_max_pct", 0.3)
    price = _price()
    excess = s["qty"] * price - cap
    if excess < equity_krw * cfg["min_trade_pct"]:
        return None
    qty = min(s["qty"], math.floor(excess / price))
    if qty < 1:
        return None
    return _trade("sell", qty, price, f"자동: 하락장 축소 (대기 자금을 자산의 {cfg.get('bear_max_pct', 0.3):.0%} 까지로)")


def rebalance(cash_krw, equity_krw, now):
    """감시 루프가 부른다. 남는 현금 → ETF 매수 (시장 조건 통과 시, 자산의 max_tranche_pct 씩).
    하락장이면 넣지 않고, 넘치는 대기 자금은 줄인다. (모자란 현금은 raise_cash 가 매수 시점에 해결)"""
    cfg = load_config()["park"]
    if not enabled() or not cfg["window"][0] <= now.strftime("%H:%M") <= cfg["window"][1]:
        return None
    s = state()
    trades = [t for t in s["trades"] if "자동" in t["why"]]
    if trades and _minutes_since(trades[-1]["ts"]) < cfg["rebalance_minutes"]:
        return None
    why = gate(now)
    if why:
        if why.startswith("하락장"):
            return _bear_reduce(equity_krw, now)
        return None
    spare = cash_krw - equity_krw * cfg["cash_reserve_pct"]
    if spare < equity_krw * cfg["min_trade_pct"]:
        return None
    spare = min(spare, equity_krw * cfg.get("max_tranche_pct", 1.0))
    price = _price()
    c = load_config()["costs"]
    qty = math.floor(spare / (price * (1 + c["slippage"]) * (1 + c["commission"])))
    if qty < 1:
        return None
    return _trade("buy", qty, price, "자동: 남는 현금 투입")


def withdraw_all(why="대기 자금 끄기"):
    s = state()
    if not s["qty"]:
        return None
    return _trade("sell", s["qty"], _price(), why)


def mark(price):
    s = state()
    if s["qty"]:
        s["last_price"] = price
        save_json(PARK, s)


def _minutes_since(ts):
    from .config import now_utc, parse_ts
    return (now_utc() - parse_ts(ts)).total_seconds() / 60


def summary(price=None):
    s = state()
    p = price or s["last_price"]
    v = value(p)
    return {"enabled": enabled(), "name": load_config()["park"]["name"], "qty": s["qty"],
            "price": p, "value_krw": v, "cost_krw": s["cost_krw"],
            "pnl_krw": v - s["cost_krw"] if s["qty"] else 0,
            "total_pnl_krw": (v - s["net_out"]) if (s["qty"] or s["net_out"]) else 0,   # 실현분 포함
            "gate": dict(GATE),
            "recent": s["trades"][-5:][::-1]}
