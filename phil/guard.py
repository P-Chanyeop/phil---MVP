"""보호 경로 스냅샷/복구. PROTECTED.

사이클 시작 전 보호 파일을 메모리에 저장하고, 끝난 뒤 바뀐 파일은 되돌린다.
에이전트 권한 규칙이 1차 방어선, 이것이 2차 방어선.
"""
from .config import PROTECTED_PATHS, ROOT


def _files():
    for rel in PROTECTED_PATHS:
        p = ROOT / rel
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts:
                    yield f


def snapshot():
    return {f: f.read_bytes() for f in _files()}


def restore(snap):
    changed = []
    now = set(_files())
    for f, data in snap.items():
        if not f.exists() or f.read_bytes() != data:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            changed.append(str(f.relative_to(ROOT)))
    for f in now - set(snap):
        f.unlink()
        changed.append(str(f.relative_to(ROOT)) + " (삭제)")
    return changed
