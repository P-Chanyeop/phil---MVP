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

from phil import cycle, ledger, market, naver, policy, resolve, score
from phil.config import (CYCLE_LOGS, CYCLES_LOG, FORECASTS, LEDGER, RETROS, ROOT, STRATEGY,
                         STRATEGY_HISTORY, load_config, load_env, load_jsonl,
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
    }


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
            if p == "/api/settle":
                r = resolve.settle_all()
                _CACHE.pop("status", None)
                return self._send(200, r)
            if p == "/api/operator":
                st = operator_state()
                if "auto_loop" in body:
                    st["auto_loop"] = bool(body["auto_loop"])
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
