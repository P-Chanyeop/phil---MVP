"""안전장치: 자산이 최근 고점 대비 일정 비율 이상 빠지면 며칠간 신규 매수 중단. PROTECTED."""
import datetime as dt

from .config import META, load_config, load_json, now_kst, save_json


def _add_trading_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def check(equity, now=None):
    """감시 루프가 1분마다 호출. 새로 멈췄으면 메시지 반환."""
    now = now or now_kst()
    cfg = load_config()["safety"]
    meta = load_json(META, {})
    peak = max(meta.get("equity_peak") or equity, equity)
    msg = None
    if equity < peak * (1 - cfg["max_drawdown_pct"] / 100) and not paused(now)[0]:
        until = _add_trading_days(now.date(), cfg["pause_days"]).isoformat()
        meta["buy_pause_until"] = until
        meta["buy_pause_reason"] = f"고점 {peak:,.0f}원 대비 {(equity / peak - 1) * 100:.1f}% 하락"
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
