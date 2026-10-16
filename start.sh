#!/usr/bin/env bash
# Phil 실행 (macOS / Linux)
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null || PY=python
command -v claude >/dev/null || echo "⚠️  claude CLI 가 없습니다: npm install -g @anthropic-ai/claude-code  (설치 후 'claude' 로 한 번 로그인)"
( sleep 1.5; (command -v open >/dev/null && open http://127.0.0.1:8787) || (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8787) ) >/dev/null 2>&1 &
exec $PY server.py "$@"
