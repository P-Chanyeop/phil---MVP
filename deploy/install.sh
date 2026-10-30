#!/usr/bin/env bash
# Phil 서버 설치 (Ubuntu / Debian / Amazon Linux). 다른 서비스가 돌고 있는 서버에 같이 올려도 되도록
# 전용 사용자 'phil' 로 격리해서 설치합니다.
#
#   git clone -b claude/friendly-goldberg-sqe2wj https://github.com/P-Chanyeop/phil---MVP.git
#   cd phil---MVP && sudo bash deploy/install.sh            # 포트 바꾸려면: sudo PHIL_PORT=8790 bash deploy/install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요: sudo bash deploy/install.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PHIL_PORT:-8787}"
APP=/home/phil/phil
REPO_URL="$(git -C "$SRC" remote get-url origin 2>/dev/null || echo https://github.com/P-Chanyeop/phil---MVP.git)"
BRANCH="$(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo claude/friendly-goldberg-sqe2wj)"

echo "▶ 1/6 패키지 확인 (python3, git, node)"
if command -v apt-get >/dev/null; then PM="apt-get"; apt-get update -qq; apt-get install -y -qq python3 git curl ca-certificates >/dev/null
elif command -v dnf >/dev/null; then PM="dnf"; dnf install -y -q python3 git curl >/dev/null
else echo "apt-get / dnf 가 없는 배포판입니다. python3, git, node 18+ 를 직접 설치한 뒤 다시 실행하세요."; exit 1; fi
NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/' || echo 0)"
if [ "${NODE_MAJOR:-0}" -lt 18 ]; then
  echo "  Node.js 20 설치"
  if [ "$PM" = apt-get ]; then curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null; apt-get install -y -qq nodejs >/dev/null
  else curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - >/dev/null; dnf install -y -q nodejs >/dev/null; fi
fi

echo "▶ 2/6 Claude Code 설치"
command -v claude >/dev/null || npm install -g @anthropic-ai/claude-code >/dev/null

echo "▶ 3/6 전용 사용자 'phil' (다른 서비스 파일에 접근 못 하게 격리)"
id phil >/dev/null 2>&1 || useradd --create-home --shell /bin/bash phil
chmod 750 /home/phil

echo "▶ 4/6 코드 받기 → $APP"
if [ ! -d "$APP/.git" ]; then sudo -u phil git clone -q -b "$BRANCH" "$REPO_URL" "$APP"; else echo "  이미 있음 (업데이트는 deploy/update.sh)"; fi
[ -f "$APP/.env" ] || { sudo -u phil cp "$APP/.env.example" "$APP/.env"; chmod 600 "$APP/.env"; }

echo "▶ 5/6 포트 $PORT 확인"
if command -v ss >/dev/null && ss -ltn | grep -q ":$PORT "; then
  echo "  ⚠️ 포트 $PORT 를 이미 다른 프로그램이 쓰고 있습니다. PHIL_PORT=다른번호 로 다시 실행하세요."; exit 1; fi

echo "▶ 6/6 서비스 등록 (재부팅해도 자동 시작)"
sed "s/__PORT__/$PORT/" "$APP/deploy/phil.service" > /etc/systemd/system/phil.service
systemctl daemon-reload
systemctl enable --now phil >/dev/null
sleep 2
systemctl --no-pager --lines=3 status phil || true

cat <<MSG

✅ 설치 완료. 남은 일 (deploy/SERVER.md 참고):

 1) Claude 로그인 — 둘 중 하나
    a. API 키: sudo -u phil nano $APP/.env  →  ANTHROPIC_API_KEY=sk-ant-... 추가
    b. 구독 로그인: sudo -u phil -i  →  cd phil && claude  →  /login (나오는 주소를 내 PC 브라우저에서 열기)
 2) (선택) 한투 모의계좌: 같은 .env 에 PHIL_KIS_APP_KEY / PHIL_KIS_APP_SECRET / PHIL_KIS_ACCOUNT
 3) .env 를 고쳤으면: sudo systemctl restart phil
 4) 화면 보기 (내 PC 에서): ssh -L $PORT:127.0.0.1:$PORT <서버주소>  → 브라우저 http://127.0.0.1:$PORT
MSG
