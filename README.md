# 🐹 Phil — 이길 때까지 배우는 AI 트레이더 (MVP)

> 같은 날을 반복하며 이길 때까지 배운 남자, 그리고 날씨를 예측하는 그라운드호그에서 따온 이름.

Phil 은 **Polymarket 단기 예측시장**에서 베팅하고, 결과가 나오면 **스스로 회고하고 자기 전략을 고치는** AI 에이전트입니다.
오픈소스 [bennyjo/phil](https://github.com/bennyjo/phil) (Apache-2.0) 을 기반으로, 바로 써볼 수 있게
**모바일 웹앱 + 엔진 + 소액 실거래**로 다시 만들었습니다.

```
정산 → 회고("뭐가 틀렸지?") → 규칙 고치기 → 조사 → 베팅 → (다음 날) 다시 도전
```

- **모의투자가 학습 엔진**: 가상 $1,000. 체결은 실시간 호가(최우선 매도호가)로, 정산은 Polymarket 공식 결과로 해서 결과를 부풀릴 수 없습니다.
- **AI 판단**: 로컬 **Claude Code CLI** 를 헤드리스로 실행합니다 (`claude -p`). 웹 검색으로 조사하고 확률을 추정합니다.
- **정직한 지표**: 손익만이 아니라 `brier_delta` (내 확률이 시장 가격보다 더 정확했나) 로 평가합니다.
- **실거래는 증거를 따라감**: 정산된 모의 베팅 10건 이상에서 **수익 + 시장보다 정확**이 입증된 엣지 클래스만, 건당 $1·하루 $5 로 실제 주문합니다. 기본값은 OFF 입니다.

---

## 1. 준비물

| 필요 | 설치 |
|---|---|
| Python 3.10+ | <https://www.python.org/downloads/> |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` 후 터미널에서 `claude` 를 한 번 실행해 로그인 |
| (실거래 시) Polymarket 계정 + USDC | <https://polymarket.com> |

모의투자와 웹앱에는 **추가 패키지가 필요 없습니다** (표준 라이브러리만 씀).

## 2. 실행

```bash
git clone https://github.com/P-Chanyeop/phil---MVP.git
cd phil---MVP
./start.sh          # Windows: start.bat 더블클릭
```

브라우저에서 **http://127.0.0.1:8787** 이 열립니다. **▶ 사이클 실행** 을 누르면 Phil 의 하루가 시작되고,
진행 로그가 실시간으로 보입니다. 사이클 1회에 보통 5–20분, Claude 사용량은 $0.2–2 정도입니다.

`start.sh` 대신 `python server.py` 로 실행해도 됩니다.

### 화면

| 탭 | 내용 |
|---|---|
| 🏠 홈 | 모의 뱅크롤, 자산 곡선, 승률, 시장 대비 정확도, 사이클 실행/중지, 자동 반복, 보유 포지션, 최근 정산 |
| 🔍 탐색 | 7일 안에 결과가 나는 마켓 검색, 실시간 호가와 결의 조건 |
| 📄 저널 | "같은 날 · N번째" 회고 타임라인, 전략 수정 기록(diff), 엣지 클래스별 성적 |
| 👤 설정 | 실거래 ON/OFF, 지갑 점검, 실거래 주문 내역, 사이클 기록 |

### 휴대폰에서 보기 (선택)

`.env` 에 `PHIL_WEB_TOKEN=아무거나12자이상` 을 넣고 `python server.py --host 0.0.0.0` 으로 실행하세요.
같은 와이파이의 휴대폰에서 `http://<PC의 IP>:8787/?token=<토큰>` 으로 접속합니다. 토큰이 없으면 외부 접속은 열리지 않습니다.

### 터미널 명령

```bash
python run.py cycle          # 사이클 1회
python run.py loop 10 60     # 10회, 60분 간격
python run.py settle         # 정산만
python run.py score          # 성적 리포트
python run.py real-doctor    # 실거래 준비 점검
python -m unittest discover tests   # 엔진 테스트
```

## 3. 실거래 켜기 (선택, 실제 돈)

> ⚠️ 실제 손실이 날 수 있습니다. **실거래 전용 새 지갑**에 잃어도 되는 소액만 넣으세요.
> 예측시장 거래는 일부 지역에서 제한되거나 불법입니다. 본인 지역의 규정을 먼저 확인하세요.

1. `pip install -r requirements-real.txt`
2. `.env.example` 을 `.env` 로 복사하고 채웁니다:
   - `PHIL_PRIVATE_KEY`: 지갑 개인키
   - `PHIL_SIGNATURE_TYPE`: `0` 일반 지갑(EOA) / `1` Polymarket 이메일 계정 / `2` Polymarket 브라우저 지갑 계정
   - `PHIL_FUNDER_ADDRESS`: 1·2 일 때 polymarket.com 프로필의 입금(프록시) 주소
3. 웹앱 **설정** 탭에서 "준비 상태 ✅" 와 USDC 잔고를 확인합니다 (`python run.py real-doctor` 도 같음).
4. **실거래 켜기** → `실거래 시작` 입력.

켜 둔 뒤에는 사이클이 끝날 때마다 엔진이 새 모의 베팅을 검사합니다. **검증된 엣지 클래스**에 속하고,
현재 호가가 모의 체결가 +2¢ 이내이면 $1 FOK 주문을 냅니다. 처음에는 검증된 클래스가 없으므로
**모의 베팅이 쌓이기 전까지 실거래는 일어나지 않습니다.** 의도된 동작입니다.

결과가 나온 실거래 포지션의 상금은 polymarket.com 에서 **Claim(상환)** 하세요. 이 MVP 는 자동 상환을 하지 않습니다.

### 실거래 안전장치

- 한도: `config/protected.json` 의 `real` 블록 (건당 $1, 하루 $5, 동시 10개, 슬리피지 2¢).
  `phil/real.py` 에 하드 상한(건당 $10, 하루 $50, 동시 20개)이 있어서 설정값을 올려도 그 이상은 주문하지 않습니다.
- 마켓당 실거래는 한 번만 합니다. 결과가 불분명한 주문이 있으면 **주문 확인**으로 정리하기 전까지 새 주문을 막습니다.
- 개인키는 엔진 프로세스에서만 읽습니다. 에이전트(Claude) 프로세스 환경에서는 제거되고, `.env` 읽기도 금지됩니다.

## 4. 에이전트가 할 수 있는 것과 없는 것

| 에이전트(Claude Code)가 할 수 있는 것 | 할 수 없는 것 |
|---|---|
| `python tools.py` (탐색·호가·모의 베팅·성적 조회) | 그 외 모든 셸 명령 |
| 웹 검색 / 웹페이지 읽기 | `.env` 읽기, 개인키 접근 |
| `strategy/` 수정 (플레이북·리스크 정책) | `phil/` `config/` `agent/` `web/` 수정 → 사이클 후 **자동 복구** |
| `journal/retros/` 에 회고 작성 | 원장(ledger) 직접 수정, 실거래 주문 |

모의 베팅 하드캡: 건당 $10, 동시 60개, 사이클당 15개, 결과 확정 20분 미만·7일 초과 마켓 금지,
체결가 2¢–95¢, 초단기 코인 업/다운 금지.

## 5. 구조

```
phil/            보호된 엔진 (에이전트 수정 불가)
  pmapi.py         Polymarket 공개 API
  scan.py          후보 마켓 탐색
  ledger.py        모의 브로커 (실시간 ask 체결, 하드캡)
  resolve.py       공식 결과로 정산
  score.py         손익·캘리브레이션·엣지 검증
  real.py          실거래 (py-clob-client-v2, 한도·중복·불명 주문 차단)
  cycle.py         사이클 러너 (claude -p, 보호 파일 복구, 전략 diff 기록, 실거래 복제)
  guard.py         보호 파일 스냅샷/복구
agent/CYCLE.md   에이전트에게 주는 사이클 절차
strategy/        에이전트가 스스로 고치는 플레이북·리스크 정책  ← 이게 "학습"
journal/         원장·회고·사이클 로그 (로컬 데이터, git 제외)
config/protected.json  하드캡·실거래 한도
tools.py         에이전트 전용 명령줄
server.py        웹앱 서버 / web/  모바일 UI
```

## 라이선스 / 면책

[Apache-2.0](LICENSE). 원작: [bennyjo/phil](https://github.com/bennyjo/phil).
연구용 실험이며 투자·베팅 조언이 아닙니다. 과거 성과(모의든 실제든)는 미래를 보장하지 않습니다.
