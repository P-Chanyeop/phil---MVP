"""휴대폰 알림 (텔레그램 봇). PROTECTED.

.env 의 PHIL_TELEGRAM_BOT_TOKEN 으로 보내고, 받을 채팅(chat_id)은 웹앱의 '채팅 연결' 버튼이
봇에게 온 최근 메시지에서 찾아 journal/operator.json 에 저장한다.
감시 루프가 1분마다 원장을 훑어서 새 매수·청산·예측 정산을 한 번씩만 알린다.
"""
import json
import urllib.parse
import urllib.request

from .config import (FORECASTS, LEDGER, NOTIFY_STATE, load_env, load_json, load_jsonl,
                     operator_state, save_json, save_operator_state)

API = "https://api.telegram.org/bot{token}/{method}"


class NotifyError(RuntimeError):
    pass


def token():
    return load_env().get("PHIL_TELEGRAM_BOT_TOKEN") or None


def chat_id():
    return operator_state().get("telegram_chat_id")


def configured():
    return bool(token() and chat_id())


def _call(method, params):
    t = token()
    if not t:
        raise NotifyError(".env 에 PHIL_TELEGRAM_BOT_TOKEN 이 없습니다")
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(API.format(token=t, method=method), data=data, timeout=10) as r:
            body = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise NotifyError(f"텔레그램 연결 실패: {str(e).replace(t, '***')[:200]}") from None
    if not body.get("ok"):
        raise NotifyError(f"텔레그램 오류: {body.get('description')}")
    return body.get("result")


def detect_chat():
    """봇에게 최근 메시지를 보낸 채팅을 찾아 저장. 사용자는 먼저 봇에게 아무 메시지나 보낸다."""
    updates = _call("getUpdates", {"limit": 20}) or []
    chats = [u.get("message", {}).get("chat") for u in updates if u.get("message")]
    chats = [c for c in chats if c]
    if not chats:
        raise NotifyError("봇에게 온 메시지가 없어요. 텔레그램에서 봇에게 아무 말이나 보낸 뒤 다시 눌러 주세요.")
    c = chats[-1]
    st = operator_state()
    st["telegram_chat_id"] = c["id"]
    st["telegram_chat_name"] = c.get("first_name") or c.get("title") or ""
    save_operator_state(st)
    return st["telegram_chat_name"]


def send(text):
    cid = chat_id()
    if not cid:
        raise NotifyError("채팅이 연결되지 않았어요")
    _call("sendMessage", {"chat_id": cid, "text": text[:4000], "disable_web_page_preview": "true"})


def _won(n):
    return f"{n:+,.0f}원"


def scan_and_notify():
    """원장에서 새로 생긴 매수·청산·예측 정산을 찾아 알림. 처음 실행 때는 현재 상태를 기억만 한다."""
    if not configured():
        return 0
    state = load_json(NOTIFY_STATE, None)
    entries = load_jsonl(LEDGER)
    forecasts = load_jsonl(FORECASTS)
    now_seen = {
        "bought": sorted(e["id"] for e in entries),
        "closed": sorted(e["id"] for e in entries if e["status"] == "closed"),
        "fsettled": sorted(r["id"] for r in forecasts if r["status"] in ("up", "down")),
    }
    if state is None:
        save_json(NOTIFY_STATE, now_seen)
        return 0
    msgs = []
    for e in entries:
        if e["id"] not in state.get("bought", []):
            kind = "탐색 매수" if e.get("exploration") else "매수"
            tgt = f"{e['target_price']:,.0f}" if e.get("target_price") else "없음"
            msgs.append(f"🐹 {kind} · {e['name']} {e['qty']}주 @ {e['entry_price']:,.0f}원\n"
                        f"손절 {e['stop_price']:,.0f} / 목표 {tgt} · {e['horizon_days']}일\n"
                        f"근거: {e['rationale'][:150]}")
        if e["status"] == "closed" and e["id"] not in state.get("closed", []):
            icon = "📈" if e["pnl_krw"] > 0 else "📉"
            msgs.append(f"{icon} 청산 · {e['name']} {e['ret_pct']:+.2f}% ({_won(e['pnl_krw'])})\n{e.get('exit_reason', '')}")
    newly = [r for r in forecasts if r["status"] in ("up", "down") and r["id"] not in state.get("fsettled", [])]
    if newly:
        hits = sum(1 for r in newly if (r["est_prob"] > 0.5) == (r["status"] == "up"))
        msgs.append(f"🎯 예측 정산 {len(newly)}건 · 방향 적중 {hits}/{len(newly)}")
    for m in msgs[:10]:
        try:
            send(m)
        except NotifyError:
            return 0  # 실패하면 상태를 저장하지 않아 다음에 다시 시도
    save_json(NOTIFY_STATE, now_seen)
    return len(msgs)


def alert(text):
    """실패해도 조용히 (알림이 시스템을 멈추면 안 됨)."""
    try:
        if configured():
            send(text)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def daily_summary():
    """평일 장 마감 후 하루 요약."""
    from . import ledger, naver, park, score
    from .config import INTRADAY, KST, load_config, now_kst, parse_ts
    today = now_kst().date().isoformat()
    entries = load_jsonl(LEDGER)
    intr = [r for r in load_jsonl(INTRADAY) if r.get("date") == today]
    eq = ledger.equity(entries)
    lines = [f"🐹 Phil 오늘 요약 ({today})"]
    if intr:
        d = eq - intr[0]["equity"]
        lines.append(f"오늘 손익 {d:+,.0f}원 ({d / intr[0]['equity'] * 100:+.2f}%) · 자산 {eq:,.0f}원")
    try:
        k = naver.index_quote("KOSPI")
        lines.append(f"코스피 {k['price']:,.2f} ({k['change_pct']:+.2f}%)")
    except Exception:  # noqa: BLE001
        pass
    closed = [e for e in entries if e["status"] == "closed" and e.get("exit_ts")
              and parse_ts(e["exit_ts"]).astimezone(KST).date().isoformat() == today]
    if closed:
        lines.append("청산: " + ", ".join(f"{e['name']} {e['ret_pct']:+.1f}%" for e in closed))
    opens = [e for e in entries if e["status"] == "open"]
    lines.append(f"보유 {len(opens)}종목 + 대기자금 {park.summary()['value_krw']:,.0f}원")
    fs = [r for r in load_jsonl(FORECASTS) if r["status"] in ("up", "down") and r.get("settled_ts")
          and parse_ts(r["settled_ts"]).astimezone(KST).date().isoformat() == today]
    if fs:
        hits = sum(1 for r in fs if (r["est_prob"] > 0.5) == (r["status"] == "up"))
        lines.append(f"예측 결과 {hits}/{len(fs)} 맞힘")
    sc = score.report()
    c = sc["claude_cost"]
    lines.append(f"Claude 비용 오늘 ${c['today_usd']:.2f} · 누적 ${c['total_usd']:.2f} (≈{c['total_krw']:,}원)")
    lines.append(f"누적 수익 {sc['return_pct']:+.2f}% · 비용 차감 후 {sc['return_after_cost_pct']:+.2f}%")
    alert("\n".join(lines))
    return ["📨 하루 요약 보냄"] if configured() else []
