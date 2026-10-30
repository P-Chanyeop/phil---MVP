# Phil 서버(AWS) 설치 가이드

이미 다른 서비스가 돌고 있는 서버에 같이 올려도 됩니다. Phil 은 **전용 사용자 `phil`** 로 격리되어 돌고,
AI 는 **Phil 폴더 안의 파일만** 읽을 수 있습니다. 메모리 약 200MB, CPU 는 거의 쓰지 않습니다.

## 1. 설치 (5분)

서버에 SSH 로 접속해서:

```bash
git clone -b claude/friendly-goldberg-sqe2wj https://github.com/P-Chanyeop/phil---MVP.git
cd phil---MVP
sudo bash deploy/install.sh              # 8787 포트가 이미 쓰이면: sudo PHIL_PORT=8790 bash deploy/install.sh
```

스크립트가 하는 일: python3·git·Node.js 20·Claude Code 설치 → `phil` 사용자 생성 → `/home/phil/phil` 에 코드 →
systemd 서비스 등록(재부팅해도 자동 시작, 죽으면 10초 뒤 재시작).

## 2. Claude 로그인 (둘 중 하나)

**A. API 키 (만료 없음, 사용한 만큼 결제 — 서버 무인 운영에 추천)**
```bash
sudo -u phil nano /home/phil/phil/.env      # ANTHROPIC_API_KEY=sk-ant-... 입력
sudo systemctl restart phil
```

**B. Claude 구독 로그인 (추가 결제 없음, 가끔 만료)**
```bash
sudo -u phil -i
cd phil && claude          # 처음 실행 시 로그인 안내, 아니면 /login 입력
# 화면에 나오는 주소를 내 PC 브라우저에서 열어 로그인 → 받은 코드를 붙여넣기
exit
```
만료되면 Phil 화면에 "🔑 로그인이 만료됐어요" 안내가 뜹니다. 위 과정을 다시 하면 됩니다.

## 3. 화면 보기

Phil 은 보안을 위해 서버 내부(127.0.0.1)에서만 열립니다. **내 PC** 에서:

```bash
ssh -L 8787:127.0.0.1:8787 ubuntu@<서버주소>     # (pem 키를 쓰면 -i 키.pem 추가)
```
접속해 둔 채로 브라우저에서 **http://127.0.0.1:8787**. 설정 탭에서 자동 반복을 켜 두면,
SSH 를 끊어도 서버에서 계속 돕니다.

## 4. 한투 모의투자 계좌 (선택)

1. [KIS Developers](https://apiportal.koreainvestment.com) 에서 **모의투자** 신청 후 앱키·시크릿 발급
2. `sudo -u phil nano /home/phil/phil/.env` 에 `PHIL_KIS_APP_KEY`, `PHIL_KIS_APP_SECRET`, `PHIL_KIS_ACCOUNT`(모의계좌 10자리)
3. `sudo systemctl restart phil` → 화면 설정 탭 → **한투 모의투자 계좌 → 연동 켜기**

## 5. 관리 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 상태 보기 | `sudo systemctl status phil` |
| 로그 보기 | `sudo journalctl -u phil -f` |
| 재시작 | `sudo systemctl restart phil` |
| 끄기 / 켜기 | `sudo systemctl stop phil` / `start phil` |
| 코드 업데이트 (기록·전략·.env 유지) | `sudo bash /home/phil/phil/deploy/update.sh` |
| 완전 삭제 | `sudo systemctl disable --now phil && sudo rm /etc/systemd/system/phil.service && sudo userdel -r phil` |

## 참고

- **리전**: 네이버 증권 시세를 쓰므로 서울(ap-northeast-2) 리전이 가장 안정적입니다.
- **시간대**: Phil 은 한국 시간으로 계산하므로 서버 시간대는 상관없습니다.
- **비용**: 기본 모델 Sonnet 5.5 기준 장중 1회 약 $0.6. 자동 반복 2시간 간격이면 한 달 약 $50.
  모델은 `config/protected.json` 의 `cycle.model` 에서 바꿉니다.
