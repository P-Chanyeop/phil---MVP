#!/usr/bin/env python3
"""운영자 명령줄.

  python run.py                 # 웹앱 실행 (http://127.0.0.1:8787)  == python server.py
  python run.py cycle           # 사이클 1회 (터미널에서)
  python run.py loop 10 60      # 10회, 60분 간격
  python run.py settle          # 정산만
  python run.py score           # 성적 리포트
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import time


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "serve"
    if cmd == "serve":
        import server
        sys.argv = [sys.argv[0]] + args[1:]
        return server.main()
    from phil import cycle, naver, resolve, score
    if cmd == "cycle":
        print(json.dumps(cycle.run_cycle(), ensure_ascii=False, indent=1))
    elif cmd == "loop":
        n = int(args[1]) if len(args) > 1 else 1
        gap = float(args[2]) if len(args) > 2 else 60
        for i in range(n):
            print(f"=== 사이클 {i + 1}/{n} ===")
            print(json.dumps(cycle.run_cycle(), ensure_ascii=False, indent=1))
            if i < n - 1:
                time.sleep(gap * 60)
    elif cmd == "settle":
        print(json.dumps(resolve.settle_all(), ensure_ascii=False, indent=1))
    elif cmd == "score":
        r = score.report(naver.index_quote("KOSPI")["price"])
        r.pop("equity_curve")
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
