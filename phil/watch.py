"""장중 감시 루프 (Claude 를 부르지 않음, 비용 0). PROTECTED.

웹앱 서버 안에서 1분마다:
 1) 보유 포지션의 현재가를 보고 손절가·목표가에 닿으면 즉시 청산, 만기일 15:15 에 청산.
    (서버가 꺼져 있던 동안의 청산은 resolve.py 가 일봉으로 보정한다.)
 2) 한투 모의계좌 연동이 켜져 있으면, 계좌를 모의 장부에 맞춘다:
    장부에 새로 산 종목 → 시장가 매수, 장부에서 청산된 종목 → 시장가 매도.
"""
import threading
import time
import traceback

from . import dart, kis, ledger, naver, notify, research, rules, safety
from .config import (INTRADAY, KIS_LEDGER, WATCH_EVENTS, append_jsonl, iso, load_config,
                     load_jsonl, now_kst, operator_state)
from .market import kst_date

STATUS = {"last_tick": None, "last_error": None, "watching": 0}
EXPIRY_TIME = "15:15"


def event(kind, msg, **kw):
    append_jsonl(WATCH_EVENTS, {"ts": iso(), "kind": kind, "msg": msg, **kw})


class _DayCache:
    date, dates = None, []


def trading_dates(today):
    """최근 거래일 목록 (코스피 일봉 기준, 하루 한 번 갱신). 오늘이 장중이면 오늘 포함."""
    if _DayCache.date != today:
        _DayCache.dates = [b[0] for b in naver.daily_bars("KOSPI", 30)]
        _DayCache.date = today
    d = list(_DayCache.dates)
    if today not in d:
        d.append(today)
    return d


def day_index(entry_date, today):
    return sum(1 for d in trading_dates(today) if d > entry_date)


def _market_open(now):
    if now.weekday() >= 5 or not "09:00" <= now.strftime("%H:%M") <= "15:20":
        return False
    try:
        return naver.index_quote("KOSPI")["market_status"] == "OPEN"
    except naver.DataError:
        return False


def check_positions(now):
    today = now.date().isoformat()
    hhmm = now.strftime("%H:%M")
    opens = [e for e in ledger.read_ledger() if e["status"] == "open"]
    STATUS["watching"] = len(opens)
    prices = {}
    for e in opens:
        try:
            price = naver.quote(e["code"])["price"]
        except naver.DataError:
            continue
        prices[e["code"]] = price
        reason = None
        if price <= e["stop_price"]:
            reason = "손절 (실시간)"
        elif e.get("target_price") and price >= e["target_price"]:
            reason = "목표가 도달 (실시간)"
        elif day_index(kst_date(e["ts"]), today) >= e["horizon_days"] and hhmm >= EXPIRY_TIME:
            reason = "만기 (15:15)"
        if reason:
            c = ledger.close_position(e["id"], price, reason)
            if c:
                event("close", f"{c['name']} {reason} {c['ret_pct']:+.2f}% ({c['pnl_krw']:+,}원)",
                      position_id=c["id"])
    return prices


def snapshot_equity(now, prices):
    """장중 1분마다 평가자산을 기록 (홈 화면의 오늘 그래프용)."""
    try:
        kospi = naver.index_quote("KOSPI")["price"]
    except naver.DataError:
        kospi = None
    eq = ledger.equity(prices=prices)
    append_jsonl(INTRADAY, {"date": now.date().isoformat(), "t": now.strftime("%H:%M"),
                            "equity": eq, "kospi": kospi})
    msg = safety.check(eq, now)
    if msg:
        event("safety", msg)
        _say(msg)


def _say(msg):
    try:
        if notify.configured():
            notify.send(msg)
    except Exception:  # noqa: BLE001
        pass


class Jobs:
    """하루 한 번짜리 작업과 공시 감시 (각자 별도 스레드, 감시 루프를 막지 않음)."""
    done = {}
    running = set()
    last_dart = 0.0
    on_event_cycle = None   # 서버가 사이클 실행 함수를 넣어 준다

    @classmethod
    def once(cls, name, day, fn):
        if cls.done.get(name) == day or name in cls.running:
            return
        cls.done[name] = day
        cls.running.add(name)

        def run():
            try:
                for m in fn() or []:
                    event("rule" if "규칙" in str(m) else "job", str(m))
                    _say(str(m))
            except Exception as e:  # noqa: BLE001
                event("job_error", f"{name}: {e}")
            finally:
                cls.running.discard(name)
        threading.Thread(target=run, daemon=True).start()


def _prefetch_and_evaluate():
    research.prefetch()
    return rules.evaluate()


def scheduled(now):
    """매일·매주 정해진 일 (장 시간과 무관하게 tick 마다 확인)."""
    day = now.date().isoformat()
    hhmm = now.strftime("%H:%M")
    weekday = now.weekday() < 5
    cfg = load_config()
    if weekday and cfg["rules"]["signal_time"] <= hhmm <= "15:18" and _market_open(now):
        Jobs.once("rule_signals", day, rules.run_signals)
    if weekday and hhmm >= "16:10":
        Jobs.once("post_close", day, _prefetch_and_evaluate)
    if not research._cache_path("KOSPI").exists():
        Jobs.once("first_prefetch", day, lambda: [f"과거 데이터 준비: {research.prefetch()['ok']}종목"])
    # 공시 감시
    if dart.key() and weekday and "07:30" <= hhmm <= "15:20" and \
            time.time() - Jobs.last_dart >= cfg["dart"]["poll_minutes"] * 60:
        Jobs.last_dart = time.time()
        try:
            new = dart.poll(now)
        except dart.DartError as e:
            dart.STATUS["error"] = str(e)
            new = []
        for r in new:
            event("dart", f"📢 공시 · {r['name']} — {r['title']}")
        if new and Jobs.on_event_cycle and _market_open(now):
            Jobs.on_event_cycle(new)


def kis_rows():
    last = {}
    for r in load_jsonl(KIS_LEDGER):
        last[r["paper_id"]] = r
    return last


def _kis_write(row, **kw):
    row = dict(row, **kw)
    row["updated"] = iso()
    append_jsonl(KIS_LEDGER, row)
    return row


def sync_kis(now):
    st = operator_state()
    if not st.get("kis_enabled") or not kis.configured():
        return
    since = st.get("kis_enabled_at") or iso()
    rows = kis_rows()
    entries = ledger.read_ledger()
    today = now.date().isoformat()
    for e in entries:
        r = rows.get(e["id"])
        # 1) 연동을 켠 뒤 오늘 새로 산 모의 포지션 → 한투 모의계좌 매수
        if r is None and e["status"] == "open" and e["ts"] >= since and kst_date(e["ts"]) == today:
            base = {"paper_id": e["id"], "code": e["code"], "name": e["name"], "qty": e["qty"],
                    "ts": iso()}
            try:
                odno = kis.order(e["code"], e["qty"], "buy")
                _kis_write(base, status="bought", buy_order=odno)
                event("kis", f"한투 모의 매수 {e['name']} {e['qty']}주 (주문 {odno})")
            except kis.KisError as err:
                _kis_write(base, status="buy_failed", error=str(err)[:300])
                event("kis_error", f"한투 모의 매수 실패 {e['name']}: {err}")
        # 2) 장부에서 청산된 포지션인데 계좌엔 남아 있음 → 매도 (최대 3번 시도)
        elif r and r["status"] in ("bought", "sell_failed") and e["status"] == "closed" \
                and r.get("sell_tries", 0) < 3:
            try:
                odno = kis.order(e["code"], r["qty"], "sell")
                _kis_write(r, status="sold", sell_order=odno, exit_reason=e.get("exit_reason"))
                event("kis", f"한투 모의 매도 {e['name']} {r['qty']}주 — {e.get('exit_reason')}")
            except kis.KisError as err:
                _kis_write(r, status="sell_failed", sell_tries=r.get("sell_tries", 0) + 1,
                           error=str(err)[:300])
                event("kis_error", f"한투 모의 매도 실패 {e['name']}: {err}")


def tick():
    now = now_kst()
    STATUS["last_tick"] = iso()
    try:
        scheduled(now)
    except Exception as e:  # noqa: BLE001
        STATUS["last_error"] = f"scheduled: {e}"
    if _market_open(now):
        prices = check_positions(now)
        snapshot_equity(now, prices)
        sync_kis(now)
    else:
        STATUS["watching"] = sum(1 for e in ledger.read_ledger() if e["status"] == "open")
    try:  # 장 밖에서도 (마감 후 정산·청산 알림)
        notify.scan_and_notify()
    except Exception as e:  # noqa: BLE001
        STATUS["notify_error"] = str(e)[:200]


class Watcher:
    def __init__(self, interval=60):
        self.interval = interval
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                tick()
                STATUS["last_error"] = None
            except Exception as e:  # noqa: BLE001 — 감시는 죽지 않는다
                STATUS["last_error"] = f"{e}"
                traceback.print_exc()
            time.sleep(self.interval)


__all__ = ["Watcher", "tick", "STATUS", "load_config"]
