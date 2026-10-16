@echo off
REM Phil 실행 (Windows)
cd /d "%~dp0"
chcp 65001 >nul
where claude >nul 2>nul || echo [!] claude CLI 가 없습니다: npm install -g @anthropic-ai/claude-code  (설치 후 claude 로 한 번 로그인)
start "" http://127.0.0.1:8787
python server.py %*
