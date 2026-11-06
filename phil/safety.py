"""안전장치: 종목 포지션 기준 자산이 최근 고점 대비 일정 비율 이상 빠지면 며칠간 신규 매수 중단. PROTECTED.

기준 자산 = 전체 자산 − 대기 자금(ETF)의 누적 손익. 즉 Phil 의 종목 선택이 잃고 있을 때만 멈춘다.
(지수가 빠져서 대기 자금이 줄어드는 것은 park.gate 가 따로 막는다 — 그때 종목 매수까지 멈출 이유는 없다.)
"""
import datetime as dt

from .config import META, load_config, load_json, now_kst, save_json


def _add_trading_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def stock_equity(equity, park_total_pnl=0):
    """안전장치 기준 자산: 전체 자산에서 대기 자금(ETF) 손익을 뺀 것."""
    return equity - (park_total_pnl or 0)


def check(equity, now=None, park_total_pnl=0):
    """감시 루프가 1분마다 호출. equity 는 전체 자산, park_total_pnl 은 대기 자금 누적 손익.
    새로 멈췄으면 메시지 반환."""
    now = now or now_kst()
    cfg = load_config()["safety"]
    meta = load_json(META, {})
    equity = stock_equity(equity, park_total_pnl)
    peak = max(meta.get("equity_peak") or equity, equity)
    msg = None
    if equity < peak * (1 - cfg["max_drawdown_pct"] / 100) and not paused(now)[0]:
        until = _add_trading_days(now.date(), cfg["pause_days"]).isoformat()
        meta["buy_pause_until"] = until
        meta["buy_pause_reason"] = f"종목 기준 자산이 고점 {peak:,.0f}원 대비 {(equity / peak - 1) * 100:.1f}% 하락"
        peak = equity  # 다시 멈추지 않도록 기준을 현재로
        msg = f"⛔ 신규 매수 중단 ({until} 까지): {meta['buy_pause_reason']}"
    meta["equity_peak"] = peak
    save_json(META, meta)
    return msg


def paused(now=None):
    now = now or now_kst()
    meta = load_json(META, {})
    until = meta.get("buy_pause_until")
    if until and now.date().isoformat() <= until:
        return True, f"{until} 까지 신규 매수 중단 — {meta.get('buy_pause_reason', '')}"
    return False, ""


def resume():
    meta = load_json(META, {})
    meta.pop("buy_pause_until", None)
    meta["equity_peak"] = None
    save_json(META, meta)
