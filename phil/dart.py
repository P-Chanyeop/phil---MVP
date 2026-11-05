"""DART 전자공시 감시 — 유니버스 종목의 주요 공시가 뜨면 사이클을 앞당겨 깨운다. PROTECTED.

.env 의 PHIL_DART_API_KEY (opendart.fss.or.kr 무료 발급)가 있을 때만 동작.
"""
import html
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile

from . import market
from .config import DISCLOSURES, append_jsonl, iso, load_config, load_env, load_jsonl, now_kst

API = "https://opendart.fss.or.kr/api/list.json"
DOC_API = "https://opendart.fss.or.kr/api/document.xml"
STATUS = {"last_poll": None, "error": None}


class DartError(RuntimeError):
    pass


def key():
    return load_env().get("PHIL_DART_API_KEY") or None


def fetch_today(now=None):
    k = key()
    if not k:
        raise DartError(".env 에 PHIL_DART_API_KEY 가 없습니다")
    d = (now or now_kst()).strftime("%Y%m%d")
    out, page = [], 1
    while page <= 5:
        url = API + "?" + urllib.parse.urlencode({"crtfc_key": k, "bgn_de": d, "end_de": d,
                                                  "page_no": page, "page_count": 100})
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                body = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            raise DartError(f"DART 연결 실패: {str(e).replace(k, '***')[:150]}") from None
        st = body.get("status")
        if st == "013":   # 조회된 데이터 없음
            break
        if st != "000":
            raise DartError(f"DART 오류 {st}: {body.get('message')}")
        out += body.get("list", [])
        if page >= int(body.get("total_page") or 1):
            break
        page += 1
    return out


def poll(now=None):
    """새 주요 공시 목록 (유니버스 종목 + 키워드). 기록도 남긴다."""
    now = now or now_kst()
    cfg = load_config()["dart"]
    uni = market.universe()
    seen = {r["rcept_no"] for r in load_jsonl(DISCLOSURES)}
    new = []
    for it in fetch_today(now):
        code = (it.get("stock_code") or "").strip()
        title = it.get("report_nm", "").strip()
        if code not in uni or it.get("rcept_no") in seen:
            continue
        if not any(k in title for k in cfg["keywords"]):
            continue
        row = {"ts": iso(), "rcept_no": it["rcept_no"], "code": code, "name": uni[code]["name"],
               "title": title, "filer": it.get("flr_nm"), "date": it.get("rcept_dt"),
               "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"}
        append_jsonl(DISCLOSURES, row)
        new.append(row)
    STATUS.update(last_poll=iso(), error=None)
    return new


def recent(code=None, n=30):
    rows = load_jsonl(DISCLOSURES)
    if code:
        rows = [r for r in rows if r["code"] == code]
    return rows[-n:][::-1]


def _xml_to_text(raw):
    """DART 원문 XML → 읽기 쉬운 글. 표는 한 행을 한 줄로 ('매출액 | 당해실적 | 25,152 | 24,268 | ...')."""
    t = raw.decode("utf-8", errors="replace").replace("\r", "")
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?i)</?(tr)[^>]*>", "\n", t)                       # 표의 행 = 줄
    t = re.sub(r"(?i)<(td|th|te|tu)[^>]*>", " | ", t)                # 칸 = ' | '
    t = re.sub(r"(?i)<(br|p|title|li|div|table|section-\d+|cover)[^>]*>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    lines = []
    for ln in t.split("\n"):
        ln = re.sub(r"(\s*\|\s*)+", " | ", ln).strip(" |")
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def document_text(rcept_no, max_chars=6000):
    """공시 원문 (DART 공식 API: 원본 XML 이 든 ZIP). 웹페이지는 자바스크립트라 직접 못 읽으므로 이걸 쓴다."""
    from .config import JOURNAL
    if not re.fullmatch(r"\d{14}", str(rcept_no)):
        raise DartError("접수번호(rcept_no)는 숫자 14자리")
    cache = JOURNAL / "dart-docs" / f"{rcept_no}.v2.txt"   # v2: 표의 한 행을 한 줄로
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        k = key()
        if not k:
            raise DartError(".env 에 PHIL_DART_API_KEY 가 없습니다")
        url = DOC_API + "?" + urllib.parse.urlencode({"crtfc_key": k, "rcept_no": rcept_no})
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read()
        except Exception as e:  # noqa: BLE001
            raise DartError(f"DART 연결 실패: {str(e).replace(k, '***')[:150]}") from None
        if body[:2] != b"PK":   # 오류는 XML 로 온다
            m = re.search(rb"<message>(.*?)</message>", body)
            raise DartError("DART 원문 오류: " + (m.group(1).decode("utf-8", "replace") if m else body[:150].decode("utf-8", "replace")))
        parts = []
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            for name in sorted(z.namelist(), key=lambda n: z.getinfo(n).file_size, reverse=True)[:3]:
                parts.append(_xml_to_text(z.read(name)))
        text = "\n\n".join(p for p in parts if p)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return {"rcept_no": rcept_no, "chars": len(text), "truncated": len(text) > max_chars,
            "text": text[:max_chars],
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"}
