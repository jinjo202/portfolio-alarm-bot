# portfolio-alarm-bot

보유 ETF·펀드와 그 상위 구성종목의 배당·실적·주요 뉴스를 매일 아침 07:30 KST에 텔레그램으로 요약 발송.

- 보유 종목: `holdings.json` (로컬 파일, git에는 안 올라감 — `HOLDINGS_JSON` secret으로 CI에 전달)
- ETF 구성종목: [pf-dash-a3k9m](https://github.com/jinjo202/pf-dash-a3k9m)의 `etf-lookthrough-*.js`를 런타임에 가져옴 (그쪽 cron이 자동 갱신)
- 실적 예정일·배당락: yfinance · 뉴스: Google News RSS · 공시: DART Open API(한국 구성종목 주요 공시 — 주주환원·자사주·배당·합병·수주 등 키워드)
- 요약: Codex CLI 구독 계정(맥미니, `CODEX_HOME` 있으면 우선) → 없으면 Claude API(`ANTHROPIC_API_KEY`, 선택) → 둘 다 없으면 원본 목록 그대로 발송
- 중복 방지: `sent.json`

## 발송 2종

| 잡 | 주기 | 내용 |
|---|---|---|
| `bot.py` (daily) | 매일 07:30 KST | 모닝브리프: 배당·실적(없으면 "없음" 명시)·공시·주요 뉴스 요약 |
| `bot.py intraday` | 30분마다, 08~22시 KST만 | 새 공시는 즉시, 새 뉴스는 Codex가 "지금 알릴 중대 이벤트"로 판정한 것만 즉시 |

수시 알림이 판정만 하고 안 보낸 뉴스는 `sent.json`의 `judged`에 기록돼 재판정은 안 하지만 아침 브리프에는 포함된다. 보낸 건 `keys`에 들어가 아침에 중복되지 않는다.

## 실행 위치: 맥미니 (launchd)

메인 배포는 맥미니 cron이다 — Codex CLI 구독으로 요약해서 API 과금이 없다. `~/Developer/portfolio-alarm-bot`에 클론, `~/Library/LaunchAgents/com.portfolio-alarm-bot.{daily,intraday}.plist` 두 개가 실행한다. DART 키는 `DART_ENV_FILE`(pf-dash-runner의 `.env`)에서 읽어 재사용한다. `CODEX_HOME=/Users/jk/.codex-pfdash`(pf-dash-runner와 공유하는 자동화 전용 ChatGPT 계정, 이미 로그인됨)을 그대로 재사용한다.

holdings.json은 git에 올리지 않고 로컬에만 둔다. TELEGRAM_BOT_TOKEN/CHAT_ID는 plist의 EnvironmentVariables에 넣는다 (파일 권한 600).

## GitHub Actions (보조, 수동 테스트용)

맥미니가 메인이라 스케줄 트리거는 꺼뒀다. `workflow_dispatch`로 수동 실행만 가능 — Codex를 못 쓰는 환경이라 `ANTHROPIC_API_KEY`가 없으면 원본 목록으로 발송된다.

1. [@BotFather](https://t.me/BotFather)에서 봇 생성 → 토큰 복사
2. 생성된 봇에게 아무 메시지 전송 후 `https://api.telegram.org/bot<토큰>/getUpdates`에서 `chat.id` 확인
3. secrets 등록:

```bash
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set ANTHROPIC_API_KEY   # 선택 — 없으면 요약 없이 원본 목록 발송
gh secret set HOLDINGS_JSON < holdings.json
```

4. 수동 테스트: Actions → Daily Digest → Run workflow

## 로컬 실행

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py
```

## 안 하는 것

- 한국 배당공시 실시간 감시 — pf-dash의 `kind_dividend_watch`(이메일·카톡)가 이미 커버
- 증권사 리포트 발췌, 장중 속보 — 필요해지면 추가 (PLAN.md 2·3단계)
