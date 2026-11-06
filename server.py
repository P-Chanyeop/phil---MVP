#!/usr/bin/env python3
"""Phil 웹앱 서버 (표준 라이브러리만). PROTECTED.

  python server.py                 # http://127.0.0.1:8787
  python server.py --host 0.0.0.0  # 같은 와이파이의 휴대폰에서 접속 (.env 의 PHIL_WEB_TOKEN 필수)
"""
import argparse
import json
import mimetypes
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import time

from phil import cycle, dart, kis, ledger, market, naver, notify, park, policy, resolve, rules, safety, score, tuning, watch
from phil.config import (CYCLE_LOGS, CYCLES_LOG, DAILY, DECISIONS, FORECASTS, INTRADAY, LEDGER, RETROS, ROOT, STRATEGY,
                         WATCH_EVENTS, STRATEGY_HISTORY, META, load_config, load_env, load_json, load_jsonl,
                         operator_state, save_operator_state)

WEB = ROOT / "web"
TOKEN = None
AUTO = None


def retro_list():
    out = []
    for f in sorted(RETROS.glob("*.md")):
        txt = f.read_text(encoding="utf-8")
        title = next((ln.lstrip("# ").strip() for ln in txt.splitlines() if ln.startswith("# ")),
                     f.stem)
        title = re.sub(r"^회고\s*[—-]\s*", "", title)
        secs = {}
        for name in ("뭐가 틀렸지?", "규칙 고치기", "다시 도전"):
            m = re.search(r"##\s*" + re.escape(name) + r"\s*\n(.*?)(?=\n##\s|\Z)", txt, re.S)
            secs[name] = m.group(1).strip()[:600] if m else ""
        out.append({"name": f.name, "title": title, "sections": secs})
    return out


_CACHE = {}


def cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:  # noqa: BLE001
        val = {"error": str(e)[:200]} if not hit else hit[1]
    _CACHE[key] = (time.time(), val)
    return val


def _quote(code):
    return cached("q:" + code, 5, lambda: naver.quote(code))


def live():
    """보유 종목 실시간 (5초 캐시) + 오늘 장중 자산 곡선."""
    cfg = load_config()
    entries = load_jsonl(LEDGER)
    pos, prices = [], {}
    for e in entries:
        if e["status"] != "open":
            continue
        q = _quote(e["code"])
        price = q.get("price") if isinstance(q, dict) else None
        price = price or e.get("last_price") or e["entry_price"]
        prices[e["code"]] = price
        proceeds = ledger.sell_proceeds(price, e["qty"], cfg["costs"])[1]
        lo, hi = e["stop_price"], e.get("target_price") or e["entry_price"] * 1.1
        pos.append({"id": e["id"], "code": e["code"], "name": e["name"], "qty": e["qty"],
                    "price": price, "day_change_pct": q.get("change_pct") if isinstance(q, dict) else None,
                    "entry": e["entry_price"], "stop": e["stop_price"], "target": e.get("target_price"),
                    "pnl_krw": proceeds - e["cost_krw"],
                    "pnl_pct": round((proceeds / e["cost_krw"] - 1) * 100, 2),
                    "progress": max(0.0, min(1.0, (price - lo) / (hi - lo))) if hi > lo else 0.5,
                    "entry_progress": max(0.0, min(1.0, (e["entry_price"] - lo) / (hi - lo))) if hi > lo else 0.5,
                    "horizon_days": e["horizon_days"], "exploration": e.get("exploration", False),
                    "edge_class": e["edge_class"], "rationale": e.get("rationale", ""), "bear_case": e.get("bear_case"),
                    "sector": (e.get("sector") or {}).get("name"), "bought": e["ts"],
                    "est_prob": e.get("est_prob"), "base_rate": e.get("base_rate"),
                    "size_notes": e.get("size_notes") or []})
    pk = cfg["park"]["code"]
    pq = cached("q:" + pk, 5, lambda: naver.quote(pk, allow_etf=True))
    if isinstance(pq, dict) and pq.get("price"):
        prices[pk] = pq["price"]
    today = __import__("phil.config", fromlist=["now_kst"]).now_kst().date().isoformat()
    intraday = [r for r in load_jsonl(INTRADAY)[-500:] if r.get("date") == today]
    psum = park.summary(prices.get(pk))
    psum["day_change_pct"] = pq.get("change_pct") if isinstance(pq, dict) else None
    return {"positions": pos, "park": psum, "cash_krw": ledger.cash(entries, cfg),
            "equity_krw": ledger.equity(entries, cfg, prices),
            "start_krw": cfg["sim_bankroll_krw"], "intraday": intraday, "ts": time.time(),
            "breakdown": score.breakdown(prices),
            "daily": [{"date": r["date"], "equity": r["equity"], "kospi": r.get("kospi")} for r in load_jsonl(DAILY)[-260:]],
            "start": {k: load_json(META, {}).get(k) for k in ("start_date", "start_kospi")}}


TOOL_WORDS = {"stock": "살펴보는 중", "news": "뉴스 읽는 중", "forecast": "예측 기록",
              "buy": "매수 주문", "sell": "매도 주문", "scan": "종목 훑어보는 중",
              "market": "시장 분위기 확인", "history": "지난 기록 돌아보는 중",
              "status": "계좌 확인", "score": "성적 확인", "policy": "규칙 확인"}


def _humanize(line, names):
    m = re.search(r"tools\.py (\w+)(?: (?:--code )?(\d{6}))?", line)
    if m:
        word = TOOL_WORDS.get(m.group(1), m.group(1))
        nm = names.get(m.group(2), m.group(2)) if m.group(2) else ""
        return f"{nm} {word}".strip()
    m = re.search(r"WebSearch (.+)", line)
    if m:
        return f"검색: {m.group(1)[:60]}"
    if "WebFetch" in line:
        return "기사·공시 읽는 중"
    if "Write" in line and "retros" in line:
        return "회고 쓰는 중"
    if ("Edit" in line or "Write" in line) and "strategy" in line:
        return "전략 고치는 중"
    return None


def feed(limit=40):
    items = []
    # kind: decision(매수·청산·거부·사이클 판단) / forecast(예측) / disclosure(공시) / system(대기 자금·규칙·알림)
    for e in load_jsonl(LEDGER):
        kind = "탐색 매수" if e.get("exploration") else "매수"
        size = f" · 자산의 {e['size_pct'] * 100:.0f}%" if e.get("size_pct") else ""
        items.append({"ts": e["ts"], "icon": "🐹", "kind": "decision",
                      "text": f"{kind} · {e['name']} {e['qty']}주 @ {e['entry_price']:,.0f}원 ({e['cost_krw']:,}원{size})",
                      "sub": "왜: " + e["rationale"][:140], "pos": e["id"]})
        if e["status"] == "closed" and e.get("exit_ts"):
            items.append({"ts": e["exit_ts"], "icon": "📈" if e["pnl_krw"] > 0 else "📉", "kind": "decision",
                          "text": f"청산 · {e['name']} {e['ret_pct']:+.2f}% ({e['pnl_krw']:+,}원)",
                          "sub": f"{e.get('exit_reason', '')} · 샀던 이유: {e['rationale'][:90]}", "pos": e["id"]})
    for d in load_jsonl(DECISIONS)[-80:]:
        if d.get("kind") == "buy":
            items.append({"ts": d["ts"], "icon": "🚫", "kind": "decision",
                          "text": f"매수 거부 · {d.get('name')} — 엔진이 막음",
                          "sub": d.get("reason", "")[:160]})
    for r in load_jsonl(FORECASTS):
        items.append({"ts": r["ts"], "icon": "🔮", "kind": "forecast",
                      "text": f"예측 · {r['name']} {r['horizon_days']}일 뒤 오를 확률 {round(r['est_prob'] * 100)}% (기저율 {round(r['base_rate'] * 100)}%)",
                      "sub": r.get("note", "")[:120]})
        if r["status"] in ("up", "down") and r.get("settled_ts"):
            hit = (r["est_prob"] > 0.5) == (r["status"] == "up")
            items.append({"ts": r["settled_ts"], "icon": "✅" if hit else "❌", "kind": "forecast",
                          "text": f"예측 결과 · {r['name']} {'올랐어요' if r['status'] == 'up' else '내렸어요'} — {'맞힘' if hit else '빗나감'}",
                          "sub": ""})
    icons = {"kis": "🏦", "kis_error": "⚠️", "dart": "📢", "rule": "🤖", "safety": "⛔", "job": "🗂️",
             "job_error": "⚠️", "park": "💤", "tune": "🎛️"}
    kinds = {"dart": "disclosure", "safety": "decision", "rule": "decision"}
    for w in load_jsonl(WATCH_EVENTS)[-150:]:
        if w.get("kind") in icons:
            items.append({"ts": w["ts"], "icon": icons[w["kind"]], "kind": kinds.get(w["kind"], "system"),
                          "text": w["msg"], "sub": ""})
    for c in load_jsonl(CYCLES_LOG)[-60:]:
        if c.get("mode"):
            nb = len(c.get("new_bets") or [])
            items.append({"ts": c.get("finished"), "icon": "🔁" if c.get("ok") else "⚠️", "kind": "decision",
                          "text": ("Phil 판단 · " if c.get("ok") else "사이클 오류 · ") +
                                  ((c.get("summary") or {}).get("note") or c.get("error") or ""),
                          "sub": f"{'매수 ' + str(nb) + '건' if nb else '매수 없음'} · 예측 {c.get('new_forecasts', 0)}건" +
                                 (f" · ${c['cost_usd']:.2f}" if c.get("cost_usd") else ""), "cycle": c.get("cycle_id")})
    items = [i for i in items if i.get("ts")]
    items.sort(key=lambda i: i["ts"], reverse=True)
    now_doing = None
    if cycle.CURRENT.get("running") and cycle.CURRENT.get("cycle_id"):
        f = CYCLE_LOGS / f"{cycle.CURRENT['cycle_id']}.log"
        if f.exists():
            names = {k: v["name"] for k, v in market.universe().items()}
            for ln in f.read_text(encoding="utf-8").splitlines()[::-1]:
                h = _humanize(ln, names)
                if h:
                    now_doing = h
                    break
        now_doing = now_doing or cycle.CURRENT.get("phase")
    return {"now": now_doing, "items": items[:limit]}


def state():
    mk = cached("market", 30, market.market_overview)
    kospi = (mk.get("KOSPI") or {}).get("price") if isinstance(mk, dict) else None
    st = cached("status", 30, ledger.status)
    sc = score.report(kospi)
    cycles = load_jsonl(CYCLES_LOG)
    return {
        "status": st, "score": sc, "operator": operator_state(), "market": mk,
        "current": dict(cycle.CURRENT),
        "claude_available": bool(cycle.claude_bin()),
        "cycles_run": len(cycles),
        "last_cycle": cycles[-1] if cycles else None,
        "next_auto_run": AUTO.next_run if AUTO else None,
        "policy": policy.summary()["rules"],
        "watch": dict(watch.STATUS),
        "buy_pause": safety.paused()[1] or None,
        "dart": {"key_set": bool(dart.key()), **dart.STATUS},
        "rules_active": sum(1 for r in rules.all_rules() if r["status"] in ("shadow", "live")),
        "kis": {"configured": kis.configured(), "enabled": operator_state().get("kis_enabled", False)},
        "auth": "api_key" if (load_env().get("ANTHROPIC_API_KEY") or __import__("os").environ.get("ANTHROPIC_API_KEY")) else "login",
        "model": load_config()["cycle"].get("model") or "Claude Code 기본값",
        "post_close_time": load_config()["cycle"].get("post_close_time", "16:30"),
        "breakdown": cached("breakdown", 30, score.breakdown),
        "park_gate": dict(park.GATE),
        "risk": cached("risk", 60, lambda: __import__("phil.riskbook", fromlist=["summary"]).summary()),
        "tuning_brief": cached("tuning_brief", 60, _tuning_brief),
    }


def _tuning_brief():
    t = tuning.summary()
    return {"defense_level": (t.get("defense") or {}).get("level", 0), "cost_saver": (t.get("cost_saver") or {}).get("active"),
            "window_10d": t.get("window_10d"), "trial": t.get("trial")}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, qs):
        if TOKEN is None:
            return True
        if qs.get("token", [None])[0] == TOKEN:
            return "set"
        return f"phil_token={TOKEN}" in (self.headers.get("Cookie") or "")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        auth = self._authed(qs)
        if not auth:
            return self._send(401, "토큰 필요: ?token=<PHIL_WEB_TOKEN>".encode(), "text/plain; charset=utf-8")
        extra = {"Set-Cookie": f"phil_token={TOKEN}; Path=/; HttpOnly; SameSite=Strict"} \
            if auth == "set" else None
        p = u.path
        q = lambda k, d=None: qs.get(k, [d])[0]  # noqa: E731
        try:
            if p == "/api/state":
                return self._send(200, state(), extra=extra)
            if p == "/api/positions":
                return self._send(200, load_jsonl(LEDGER)[::-1][: int(q("n", 200))])
            if p == "/api/forecasts":
                return self._send(200, load_jsonl(FORECASTS)[::-1][: int(q("n", 200))])
            if p == "/api/cycles":
                return self._send(200, load_jsonl(CYCLES_LOG)[::-1][:50])
            if p == "/api/cycle-log":
                cid = q("id") or cycle.CURRENT.get("cycle_id")
                if not cid or not re.fullmatch(r"[\w-]+", cid):
                    return self._send(200, {"id": None, "text": ""})
                f = CYCLE_LOGS / f"{cid}.log"
                return self._send(200, {"id": cid, "text": f.read_text(encoding="utf-8")[-60000:]
                                        if f.exists() else ""})
            if p == "/api/retros":
                return self._send(200, retro_list()[::-1])
            if p == "/api/retro":
                name = q("name", "")
                f = RETROS / name
                if not re.fullmatch(r"[\w.-]+\.md", name) or not f.exists():
                    return self._send(404, {"error": "없음"})
                return self._send(200, {"name": name, "text": f.read_text(encoding="utf-8")})
            if p == "/api/strategy":
                hist = [{"cycle_id": h["cycle_id"], "ts": h["ts"],
                         "paths": [d["path"] for d in h["diffs"]]}
                        for h in load_jsonl(STRATEGY_HISTORY)][::-1]
                return self._send(200, {
                    "playbook": (STRATEGY / "playbook.md").read_text(encoding="utf-8"),
                    "risk": (STRATEGY / "risk.json").read_text(encoding="utf-8"),
                    "history": hist})
            if p == "/api/strategy-diff":
                for h in load_jsonl(STRATEGY_HISTORY):
                    if h["cycle_id"] == q("cycle_id"):
                        return self._send(200, h)
                return self._send(404, {"error": "없음"})
            if p == "/api/scan":
                return self._send(200, market.scan(q("sort", "value"), q("query") or None,
                                                   int(q("limit", 40)), q("market") or None))
            if p == "/api/rules":
                from phil.config import RULE_SIGNALS
                from phil import discover
                rep = discover.last_report()
                return self._send(200, {"rules": rules.summary()[::-1],
                                        "signals": load_jsonl(RULE_SIGNALS)[::-1][:40],
                                        "discovery": {k: rep.get(k) for k in ("ts", "tested", "passed", "added_to_shadow", "seconds")}})
            if p == "/api/tuning":
                return self._send(200, tuning.summary())
            if p == "/api/live":
                return self._send(200, live())
            if p == "/api/feed":
                return self._send(200, feed(min(int(qs.get("limit", ["40"])[0]), 300)))
            if p == "/api/notify":
                return self._send(200, {"token_set": bool(notify.token()), "chat": operator_state().get("telegram_chat_name"),
                                        "configured": notify.configured(), "error": watch.STATUS.get("notify_error")})
            if p == "/api/watch":
                return self._send(200, load_jsonl(WATCH_EVENTS)[::-1][:50])
            if p == "/api/kis":
                out = {"configured": kis.configured(), "enabled": operator_state().get("kis_enabled", False),
                       "rows": list(watch.kis_rows().values())[::-1][:50]}
                if out["configured"]:
                    out["balance"] = cached("kis_balance", 30, kis.balance)
                return self._send(200, out)
            if p == "/api/stock":
                code = q("code", "")
                if not re.fullmatch(r"\d{6}", code):
                    return self._send(400, {"error": "종목코드 6자리"})
                d = market.stock_detail(code)
                try:
                    d["news"] = naver.news(code, 8)
                except naver.DataError:
                    d["news"] = []
                return self._send(200, d)
            return self._static(p, extra)
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)[:500]})

    def _static(self, p, extra):
        rel = "index.html" if p in ("", "/") else p.lstrip("/")
        f = (WEB / rel).resolve()
        if not str(f).startswith(str(WEB.resolve())) or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        return self._send(200, f.read_bytes(), ctype, extra)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self._authed(urllib.parse.parse_qs(u.query)):
            return self._send(401, {"error": "토큰 필요"})
        # 브라우저 CSRF 방지: 같은 출처 요청만
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, {"error": "다른 출처 요청 거부"})
        body = self._body()
        p = u.path
        try:
            if p == "/api/cycle/run":
                if cycle.CURRENT["running"]:
                    return self._send(409, {"error": "이미 실행 중"})
                threading.Thread(target=cycle.run_cycle, daemon=True).start()
                return self._send(200, {"ok": True})
            if p == "/api/cycle/stop":
                return self._send(200, {"ok": cycle.stop_current()})
            if p == "/api/tuning/revert":
                return self._send(200, {"msg": tuning.revert_trial_by_operator()})
            if p == "/api/tuning/defense_reset":
                tuning.reset_defense_by_operator()
                return self._send(200, {"ok": True})
            if p == "/api/notify/detect":
                try:
                    return self._send(200, {"chat": notify.detect_chat()})
                except notify.NotifyError as e:
                    return self._send(400, {"error": str(e)})
            if p == "/api/notify/test":
                try:
                    notify.send("🐹 Phil 알림 테스트 — 연결됐어요! 매수·청산·예측 결과를 여기로 보내드릴게요.")
                    return self._send(200, {"ok": True})
                except notify.NotifyError as e:
                    return self._send(400, {"error": str(e)})
            if p == "/api/settle":
                r = resolve.settle_all()
                _CACHE.pop("status", None)
                return self._send(200, r)
            if p == "/api/operator":
                st = operator_state()
                if "auto_loop" in body:
                    st["auto_loop"] = bool(body["auto_loop"])
                if "park_enabled" in body:
                    st["park_enabled"] = bool(body["park_enabled"])
                if body.get("resume_buys"):
                    safety.resume()
                if "kis_enabled" in body:
                    want = bool(body["kis_enabled"])
                    if want and not kis.configured():
                        return self._send(400, {"error": ".env 에 한투 앱키·시크릿·계좌번호를 먼저 넣어 주세요"})
                    if want and not st.get("kis_enabled"):
                        st["kis_enabled_at"] = __import__("phil.config", fromlist=["iso"]).iso()
                    st["kis_enabled"] = want
                if "loop_minutes" in body:
                    st["loop_minutes"] = max(15, min(390, int(body["loop_minutes"])))
                save_operator_state(st)
                return self._send(200, st)
            return self._send(404, {"error": "없음"})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)[:500]})


def main():
    global TOKEN, AUTO
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    load_config()
    if a.host not in ("127.0.0.1", "localhost"):
        TOKEN = load_env().get("PHIL_WEB_TOKEN")
        if not TOKEN or len(TOKEN) < 12:
            raise SystemExit("외부 접속을 열려면 .env 에 PHIL_WEB_TOKEN(12자 이상)을 설정하세요.")
    for d in (RETROS, CYCLE_LOGS):
        d.mkdir(parents=True, exist_ok=True)
    AUTO = cycle.AutoLoop()
    watch.Jobs.on_event_cycle = AUTO.event_cycle
    watch.Watcher(60)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "localhost") else a.host
    print(f"🐹 Phil 실행 중 → http://{shown}:{a.port}" + (f"/?token={TOKEN}" if TOKEN else ""))
    print("   종료: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
