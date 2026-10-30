"""경로·설정·작은 유틸. PROTECTED."""
import datetime as dt
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "protected.json"
JOURNAL = ROOT / "journal"
LEDGER = JOURNAL / "ledger.jsonl"
FORECASTS = JOURNAL / "forecasts.jsonl"
CYCLES_LOG = JOURNAL / "cycles.jsonl"
OPERATOR = JOURNAL / "operator.json"
META = JOURNAL / "meta.json"
UNIVERSE_CACHE = JOURNAL / "universe.json"
KIS_LEDGER = JOURNAL / "kis-ledger.jsonl"
KIS_TOKEN = JOURNAL / "kis-token.json"
WATCH_EVENTS = JOURNAL / "watch-events.jsonl"
LOCK_FILE = JOURNAL / ".ledger.lock"
RETROS = JOURNAL / "retros"
CYCLE_LOGS = JOURNAL / "cycle-logs"
STRATEGY = ROOT / "strategy"
STRATEGY_HISTORY = JOURNAL / "strategy-history.jsonl"
ENV_FILE = ROOT / ".env"

# 에이전트가 바꾸면 안 되는 경로 (guard.py 가 사이클 전후로 검사·복구).
# 원장·operator.json 은 에이전트 권한 규칙으로 막고 엔진/웹앱만 쓴다
# (사이클 중 웹앱에서 바꾼 설정이 복구로 되돌려지면 안 되므로 여기엔 넣지 않음).
PROTECTED_PATHS = ["phil", "config", "agent", "web", "server.py", "tools.py", "run.py"]

KST = dt.timezone(dt.timedelta(hours=9))


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def now_kst():
    return dt.datetime.now(KST)


def iso(t=None):
    return (t or now_utc()).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


class ledger_lock:
    """프로세스 간 원장 잠금 (웹앱 감시 루프 · 사이클 · 에이전트의 tools.py 가 동시에 쓰므로).
    O_EXCL 로 잠금 파일을 만드는 방식이라 Windows·리눅스 모두 동작. 30초 넘은 잠금은 죽은 것으로 본다."""

    def __enter__(self):
        import time
        JOURNAL.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 20
        while True:
            try:
                self.fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK_FILE.stat().st_mtime > 30:
                        LOCK_FILE.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError("원장 잠금 대기 시간 초과")
                time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def load_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def dump_jsonl(path, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(path)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_env():
    """.env 를 읽어 dict 로 반환 (os.environ 이 우선)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith("PHIL_"):
            env[k] = v
    return env


def operator_state():
    st = {"auto_loop": False, "loop_minutes": 120, "kis_enabled": False, "kis_enabled_at": None}
    st.update(load_json(OPERATOR, {}))
    return st


def save_operator_state(state):
    save_json(OPERATOR, state)
