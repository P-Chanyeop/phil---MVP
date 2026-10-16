"""한 번의 트레이딩 사이클 실행 (운영자 전용). PROTECTED.

정산 → Claude Code 헤드리스 에이전트(CYCLE.md) → 보호 파일 복구 → 전략 diff 기록
→ (켜져 있으면) 검증된 엣지의 새 베팅을 실거래로 복제.
"""
import difflib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

from . import ledger, real, resolve, score
from .config import (CYCLE_LOGS, CYCLES_LOG, LEDGER, ROOT, STRATEGY, STRATEGY_HISTORY,
                     append_jsonl, iso, load_config, load_jsonl, now_utc, operator_state)
from . import guard

_lock = threading.Lock()
CURRENT = {"running": False, "cycle_id": None, "started": None, "phase": None}
SECRET_ENV = ("PHIL_PRIVATE_KEY", "PHIL_FUNDER_ADDRESS", "PHIL_SIGNATURE_TYPE")


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


def build_prompt(cycle_id, settled):
    py = python_cmd()
    stamp = now_utc().strftime("%Y%m%d-%H%M")
    body = (ROOT / "agent" / "CYCLE.md").read_text(encoding="utf-8")
    body = body.replace("{PY}", py).replace("{STAMP}", stamp)
    st = ledger.status()
    sc = score.report()
    ctx = {
        "cycle_id": cycle_id,
        "now_utc": iso(),
        "newly_settled_this_cycle": settled["settled"],
        "cash": st["cash"], "open_positions": st["open_positions"],
        "overall": sc["overall"],
        "qualified_edge_classes_for_real": sc["qualified_edge_classes"],
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
    """stream-json 이벤트를 UI 용 한 줄로."""
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


def run_cycle(dry=False):
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "이미 사이클이 실행 중입니다"}
    cycle_id = now_utc().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    CYCLE_LOGS.mkdir(parents=True, exist_ok=True)
    log_path = CYCLE_LOGS / f"{cycle_id}.log"
    CURRENT.update(running=True, cycle_id=cycle_id, started=iso(), phase="정산")
    result = {"cycle_id": cycle_id, "started": iso(), "ok": True}

    def log(line):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    try:
        settled = resolve.settle_all()
        result["settled"] = len(settled["settled"])
        log(f"📒 정산: {len(settled['settled'])}건 (오픈 {settled['still_open']})")
        if operator_state()["real_enabled"]:
            try:
                rs = real.settle()
                if rs:
                    log(f"💵 실거래 정산 {len(rs)}건")
            except real.RealError as e:
                log(f"⚠️ 실거래 정산 실패: {e}")

        if dry or not claude_bin():
            if not claude_bin():
                log("⚠️ `claude` CLI 를 찾을 수 없습니다. Claude Code 를 설치하세요: "
                    "npm install -g @anthropic-ai/claude-code")
                result.update(ok=False, error="claude CLI 없음")
            return result

        CURRENT["phase"] = "에이전트 실행"
        snap = guard.snapshot()
        strat_before = _strategy_snapshot()
        before_ids = {e["id"] for e in load_jsonl(LEDGER)}
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env["PHIL_CYCLE_ID"] = cycle_id
        timeout = load_config()["cycle"].get("timeout_minutes", 40) * 60
        summary_line = None
        log("🐹 에이전트 시작")
        proc = subprocess.Popen(agent_cmd(build_prompt(cycle_id, settled)), cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
                    txt = ev.get("result") or ""
                    for ln in txt.splitlines()[::-1]:
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
        new_bets = [e["id"] for e in load_jsonl(LEDGER) if e["id"] not in before_ids]
        result["new_bets"] = new_bets
        log(f"🎯 새 모의 베팅 {len(new_bets)}건")
        if summary_line:
            try:
                result["summary"] = json.loads(summary_line)
            except ValueError:
                result["summary"] = {"note": summary_line[:300]}

        if new_bets and operator_state()["real_enabled"]:
            CURRENT["phase"] = "실거래 복제"
            mres = real.mirror(new_bets)
            result["real"] = mres
            for r in mres:
                log(f"💵 실거래 {r['paper_id']}: {r['status']} {r.get('reason', '')}")
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
    """웹앱의 '자동 반복' 토글. operator.json 의 auto_loop / loop_minutes 를 따른다."""

    def __init__(self):
        self.next_run = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            st = operator_state()
            if st.get("auto_loop"):
                if self.next_run is None:
                    self.next_run = time.time()
                if time.time() >= self.next_run and not CURRENT["running"]:
                    self.next_run = time.time() + max(10, int(st.get("loop_minutes", 60))) * 60
                    threading.Thread(target=run_cycle, daemon=True).start()
            else:
                self.next_run = None
            time.sleep(5)
