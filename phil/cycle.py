"""한 번의 트레이딩 사이클 실행 (운영자 전용). PROTECTED.

0) 사전 점검: 네이버 증권 접속이 안 되면 Claude 를 부르지 않고 끝 (비용 0)
1) 정산: 손절/목표/만기 청산, 예측 정산
2) 모드 결정: 장중 → FULL (회고·전략·조사·예측·매매)
             장 밖 + 새 정산 있음 → RETRO (회고·전략 수정만)
             장 밖 + 새 정산 없음 → 에이전트 생략 (비용 0)
3) Claude Code 헤드리스 실행 → 보호 파일 복구 → 전략 diff 기록
"""
import difflib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

from . import guard, ledger, market, naver, resolve, score
from .config import (CYCLE_LOGS, CYCLES_LOG, FORECASTS, LEDGER, ROOT, STRATEGY,
                     STRATEGY_HISTORY, append_jsonl, iso, load_config, load_jsonl, now_kst,
                     now_utc, operator_state)

_lock = threading.Lock()
CURRENT = {"running": False, "cycle_id": None, "started": None, "phase": None}


def python_cmd():
    return "python3" if shutil.which("python3") and os.name != "nt" else "python"


def claude_bin():
    return shutil.which("claude") or shutil.which("claude.cmd")


def _strategy_snapshot():
    out = {}
    for f in STRATEGY.rglob("*"):
        if f.is_file():
            try:
                out[str(f.relative_to(ROOT))] = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                pass
    return out


def _diff(before, after):
    diffs = []
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path, ""), after.get(path, "")
        if a != b:
            d = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                             f"a/{path}", f"b/{path}", n=1))
            diffs.append({"path": path, "diff": d[:20000]})
    return diffs


def build_prompt(cycle_id, settled, mode, why_mode):
    py = python_cmd()
    stamp = now_kst().strftime("%Y%m%d-%H%M")
    body = (ROOT / "agent" / "CYCLE.md").read_text(encoding="utf-8")
    body = body.replace("{PY}", py).replace("{STAMP}", stamp)
    try:
        kospi = naver.index_quote("KOSPI")["price"]
    except naver.DataError:
        kospi = None
    sc = score.report(kospi)
    st = ledger.status(refresh_prices=False)
    ctx = {
        "cycle_id": cycle_id,
        "mode": mode,
        "mode_reason": why_mode,
        "now_kst": now_kst().strftime("%Y-%m-%d %H:%M (%a)"),
        "newly_closed_positions": settled["closed"],
        "newly_settled_forecasts": settled["forecasts_settled"][:40],
        "equity_krw": sc["equity_krw"], "cash_krw": sc["cash_krw"],
        "return_pct": sc["return_pct"], "kospi_return_pct": sc["kospi_return_pct"],
        "open_positions": st["open_positions"],
        "trades": sc["trades"], "calibration": sc["calibration"],
        "banned_edge_classes": sc["banned_edge_classes"],
    }
    return body + "\n\n## 이번 사이클 컨텍스트\n\n```json\n" + \
        json.dumps(ctx, ensure_ascii=False, indent=1) + "\n```\n"


def agent_cmd(prompt):
    cfg = load_config()["cycle"]
    py = python_cmd()
    allowed = [f"Bash({py} tools.py:*)", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
               "Edit(strategy/**)", "Write(strategy/**)", "MultiEdit(strategy/**)",
               "Write(journal/retros/**)", "Edit(journal/retros/**)", "TodoWrite"]
    disallowed = ["Read(.env)", "Read(./.env)", "Read(**/.env)"]
    cmd = [claude_bin(), "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--max-turns", str(cfg.get("max_turns", 80)),
           "--allowedTools", *allowed, "--disallowedTools", *disallowed]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    return cmd


def _summarize_event(ev):
    t = ev.get("type")
    if t == "assistant":
        parts = []
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                parts.append(c["text"].strip())
            elif c.get("type") == "tool_use":
                inp = c.get("input", {})
                arg = inp.get("command") or inp.get("query") or inp.get("url") or \
                    inp.get("file_path") or inp.get("pattern") or ""
                parts.append(f"🔧 {c.get('name')} {str(arg)[:160]}")
        return "\n".join(parts) or None
    if t == "result":
        return f"✅ 종료 ({ev.get('subtype')}, {ev.get('num_turns')}턴, ${ev.get('total_cost_usd', 0):.2f})"
    return None


def run_cycle(force_agent=False):
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "이미 사이클이 실행 중입니다"}
    cycle_id = now_utc().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    CYCLE_LOGS.mkdir(parents=True, exist_ok=True)
    log_path = CYCLE_LOGS / f"{cycle_id}.log"
    CURRENT.update(running=True, cycle_id=cycle_id, started=iso(), phase="점검")
    result = {"cycle_id": cycle_id, "started": iso(), "ok": True}

    def log(line):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    try:
        ok, msg = naver.ping()
        log(("🔌 " if ok else "⛔ ") + msg)
        if not ok:
            result.update(ok=False, error="시세 데이터 접속 실패 — Claude 를 부르지 않았습니다 (비용 0)")
            return result
        score.ensure_meta()

        CURRENT["phase"] = "정산"
        settled = resolve.settle_all()
        result["settled"] = len(settled["closed"])
        result["forecasts_settled"] = len(settled["forecasts_settled"])
        log(f"📒 정산: 청산 {len(settled['closed'])}건 (보유 {settled['still_open']}), "
            f"예측 {len(settled['forecasts_settled'])}건 (대기 {settled['forecasts_open']})")
        for c in settled["closed"]:
            log(f"   · {c['name']} {c['reason']} {c['ret_pct']:+.2f}% ({c['pnl_krw']:+,}원)")
        for e in settled["errors"]:
            log(f"⚠️ {e}")

        is_open, why = market.trading_window()
        has_new = bool(settled["closed"] or settled["forecasts_settled"] or settled["calib_settled"])
        if is_open:
            mode = "FULL"
        elif has_new or force_agent:
            mode = "RETRO"
        else:
            mode = None
        result["mode"] = mode
        if mode is None:
            log(f"😴 {why} · 새로 정산된 것도 없어 에이전트를 생략합니다 (비용 0)")
            result["summary"] = {"note": f"{why} — 정산만 하고 쉬었다"}
            return result
        if not claude_bin():
            log("⚠️ `claude` CLI 를 찾을 수 없습니다. npm install -g @anthropic-ai/claude-code")
            result.update(ok=False, error="claude CLI 없음")
            return result

        log(f"🐹 에이전트 시작 — {'장중: 회고·조사·예측·매매' if mode == 'FULL' else f'{why}: 회고·전략 수정만'}")
        CURRENT["phase"] = "에이전트 실행"
        snap = guard.snapshot()
        strat_before = _strategy_snapshot()
        before_ids = {e["id"] for e in load_jsonl(LEDGER)}
        before_fc = len(load_jsonl(FORECASTS))
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHIL_")}
        env["PHIL_CYCLE_ID"] = cycle_id
        env["PYTHONIOENCODING"] = "utf-8"
        timeout = load_config()["cycle"].get("timeout_minutes", 40) * 60
        summary_line = None
        proc = subprocess.Popen(agent_cmd(build_prompt(cycle_id, settled, mode, why)), cwd=ROOT,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        CURRENT["pid"] = proc.pid
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    log(raw[:500])
                    continue
                line = _summarize_event(ev)
                if line:
                    log(line)
                if ev.get("type") == "result":
                    result["cost_usd"] = ev.get("total_cost_usd")
                    for ln in (ev.get("result") or "").splitlines()[::-1]:
                        if ln.strip().startswith("PHIL_SUMMARY"):
                            summary_line = ln.strip()[len("PHIL_SUMMARY"):].strip()
                            break
            proc.wait()
        finally:
            timer.cancel()
        result["exit_code"] = proc.returncode

        CURRENT["phase"] = "검증"
        reverted = guard.restore(snap)
        if reverted:
            log(f"🛡️ 보호 파일 변경 감지 → 복구: {', '.join(reverted)}")
            result["reverted"] = reverted
        diffs = _diff(strat_before, _strategy_snapshot())
        if diffs:
            append_jsonl(STRATEGY_HISTORY, {"cycle_id": cycle_id, "ts": iso(), "diffs": diffs})
            log(f"✏️ 전략 수정: {', '.join(d['path'] for d in diffs)}")
        result["strategy_changed"] = [d["path"] for d in diffs]
        result["new_bets"] = [e["id"] for e in load_jsonl(LEDGER) if e["id"] not in before_ids]
        result["new_forecasts"] = len(load_jsonl(FORECASTS)) - before_fc
        log(f"🎯 새 매수 {len(result['new_bets'])}건 · 예측 기록 {result['new_forecasts']}건")
        if summary_line:
            try:
                result["summary"] = json.loads(summary_line)
            except ValueError:
                result["summary"] = {"note": summary_line[:300]}
    except Exception as e:  # noqa: BLE001
        result.update(ok=False, error=str(e)[:500])
        log(f"❌ 오류: {e}")
    finally:
        result["finished"] = iso()
        append_jsonl(CYCLES_LOG, result)
        CURRENT.update(running=False, phase=None, pid=None)
        _lock.release()
    return result


def stop_current():
    pid = CURRENT.get("pid")
    if CURRENT.get("running") and pid:
        try:
            os.kill(pid, 15)
            return True
        except OSError:
            return False
    return False


class AutoLoop:
    """웹앱 '자동 반복': 장중에는 loop_minutes 마다, 평일 장 마감 후 한 번 (정산·회고)."""

    def __init__(self):
        self.next_run = None
        self.post_close_done = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — 루프는 죽지 않는다
                pass
            time.sleep(20)

    def _tick(self):
        st = operator_state()
        if not st.get("auto_loop") or CURRENT["running"]:
            if not st.get("auto_loop"):
                self.next_run = None
            return
        now = now_kst()
        lim = load_config()["limits"]
        hhmm = now.strftime("%H:%M")
        weekday = now.weekday() < 5
        in_hours = weekday and lim["trade_start"] <= hhmm <= lim["trade_end"]
        if in_hours:
            if self.next_run is None or time.time() >= self.next_run:
                self.next_run = time.time() + max(15, int(st.get("loop_minutes", 60))) * 60
                threading.Thread(target=run_cycle, daemon=True).start()
            return
        self.next_run = None
        post = load_config()["cycle"].get("post_close_time", "15:45")
        today = now.date().isoformat()
        if weekday and hhmm >= post and self.post_close_done != today:
            self.post_close_done = today
            threading.Thread(target=run_cycle, daemon=True).start()
