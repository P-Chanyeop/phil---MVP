#!/usr/bin/env bash
# Phil 코드 업데이트. 기록(journal/), 설정(.env), Phil 이 고친 전략(strategy/)은 그대로 둡니다.
#   sudo bash /home/phil/phil/deploy/update.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
APP=/home/phil/phil
BK="$(mktemp -d)"
cp -a "$APP/strategy" "$BK/strategy"
sudo -u phil git -C "$APP" fetch -q origin
BRANCH="$(sudo -u phil git -C "$APP" rev-parse --abbrev-ref HEAD)"
sudo -u phil git -C "$APP" reset -q --hard "origin/$BRANCH"
rm -rf "$APP/strategy" && cp -a "$BK/strategy" "$APP/strategy" && chown -R phil:phil "$APP/strategy"
rm -rf "$BK"
systemctl restart phil
echo "✅ 업데이트 완료 ($(sudo -u phil git -C "$APP" log --oneline -1))"
