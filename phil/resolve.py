"""보유 포지션 청산 판정과 예측 정산 — 일봉 기준. PROTECTED.

포지션 (매수 다음 거래일부터, 하루씩):
  1) 시가가 손절가 이하 → 시가에 청산 (갭하락)
  2) 저가가 손절가 이하 → 손절가에 청산
  3) 시가가 목표가 이상 → 시가에 청산 (갭상승)
  4) 고가가 목표가 이상 → 목표가에 청산
  같은 날 손절·목표가 둘 다 닿으면 손절로 본다 (보수적).
  5) 만기 거래일 장 마감 후 → 종가에 청산
정확도(calib): 손절 여부와 무관하게 '만기 거래일 종가 > 매수 시점 가격' 이면 up.
"""
import datetime as dt

from . import market, naver
from .config import (FORECASTS, KST, LEDGER, dump_jsonl, iso, ledger_lock, load_config,
                     load_jsonl, now_kst)
from .ledger import sell_proceeds

CLOSE_DONE = "15:40"


def _day_complete(date_str, now):
    today = now.date().isoformat()
    return date_str < today or (date_str == today and now.strftime("%H:%M") >= CLOSE_DONE)


def _exit(e, price, reason, date_str, costs):
    fill, proceeds = sell_proceeds(price, e["qty"], costs)
    e.update(status="closed", exit_price=round(fill, 2), proceeds_krw=proceeds,
             exit_reason=reason, exit_date=date_str, exit_ts=iso(),
             pnl_krw=proceeds - e["cost_krw"],
             ret_pct=round((proceeds / e["cost_krw"] - 1) * 100, 2))


def settle_position(e, bars, now, costs):
    """True 면 이번에 상태가 바뀜 (청산 또는 calib 기록)."""
    changed = False
    entry_date = market.kst_date(e["ts"])
    if bars:
        e["last_price"] = bars[-1][4]
    if e["status"] == "open":
        for i in range(1, e["horizon_days"] + 1):
            b = market.trading_day_after(bars, entry_date, i)
            if b is None:
                break
            d, o, h, lo, c, _ = b
            stop, tgt = e["stop_price"], e.get("target_price")
            if o <= stop:
                _exit(e, o, "손절 (갭하락, 시가)", d, costs)
            elif lo <= stop:
                _exit(e, stop, "손절", d, costs)
            elif tgt and o >= tgt:
                _exit(e, o, "목표가 도달 (갭상승, 시가)", d, costs)
            elif tgt and h >= tgt:
                _exit(e, tgt, "목표가 도달", d, costs)
            elif i == e["horizon_days"] and _day_complete(d, now):
                _exit(e, c, "만기 (종가)", d, costs)
            if e["status"] == "closed":
                changed = True
                break
    if "calib" not in e:
        hb = market.trading_day_after(bars, entry_date, e["horizon_days"])
        if hb and _day_complete(hb[0], now):
            e["calib"] = "up" if hb[4] > e["quote_price"] else "down"
            e["calib_close"] = hb[4]
            changed = True
    return changed


def settle_forecast(r, bars, now):
    if r["status"] != "open":
        return False
    hb = market.trading_day_after(bars, r["ref_date"], r["horizon_days"])
    if hb and _day_complete(hb[0], now):
        r["status"] = "up" if hb[4] > r["ref_price"] else "down"
        r["settle_close"] = hb[4]
        r["settle_date"] = hb[0]
        return True
    return False


def settle_all(now=None):
    now = now or now_kst()
    cfg = load_config()
    need = {e["code"] for e in load_jsonl(LEDGER) if e["status"] == "open" or "calib" not in e}
    need |= {r["code"] for r in load_jsonl(FORECASTS) if r["status"] == "open"}
    bars, errors = {}, []
    for code in need:  # 네트워크는 잠금 밖에서
        try:
            bars[code] = naver.daily_bars(code, 40)
        except naver.DataError as err:
            errors.append(f"{code}: {err}")
    with ledger_lock():
        return _apply(bars, errors, now, cfg)


def _apply(bars, errors, now, cfg):
    entries = load_jsonl(LEDGER)
    forecasts = load_jsonl(FORECASTS)
    closed, calib, fsettled = [], 0, []
    for e in entries:
        if e["code"] in bars:
            was_open = e["status"] == "open"
            had_calib = "calib" in e
            if settle_position(e, bars[e["code"]], now, cfg["costs"]):
                if was_open and e["status"] == "closed":
                    closed.append(e)
                if not had_calib and "calib" in e:
                    calib += 1
    for r in forecasts:
        if r["code"] in bars and settle_forecast(r, bars[r["code"]], now):
            fsettled.append(r)
    if entries:
        dump_jsonl(LEDGER, entries)
    if fsettled:
        dump_jsonl(FORECASTS, forecasts)
    return {
        "closed": [{"id": e["id"], "name": e["name"], "reason": e["exit_reason"],
                    "ret_pct": e["ret_pct"], "pnl_krw": e["pnl_krw"], "est": e["est_prob"],
                    "edge_class": e["edge_class"]} for e in closed],
        "calib_settled": calib,
        "still_open": sum(1 for e in entries if e["status"] == "open"),
        "forecasts_settled": [{"id": r["id"], "name": r["name"], "result": r["status"],
                               "est": r["est_prob"], "base": r["base_rate"],
                               "edge_class": r["edge_class"]} for r in fsettled],
        "forecasts_open": sum(1 for r in forecasts if r["status"] == "open"),
        "errors": errors,
    }


__all__ = ["settle_all", "settle_position", "settle_forecast", "dt", "KST"]
