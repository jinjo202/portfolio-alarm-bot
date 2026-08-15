# portfolio-alarm-bot

보유 ETF·펀드와 그 상위 구성종목의 배당·실적·주요 뉴스를 매일 아침 07:30 KST에 텔레그램으로 요약 발송.

- 보유 종목: `holdings.json` (로컬 파일, git에는 안 올라감 — `HOLDINGS_JSON` secret으로 CI에 전달)
- ETF 구성종목: [pf-dash-a3k9m](https://github.com/jinjo202/pf-dash-a3k9m)의 `etf-lookthrough-*.js`를 런타임에 가져옴 (그쪽 cron이 자동 갱신)
- 실적 예정일·배당락: yfinance · 뉴스: Google News RSS · 요약: Claude Haiku
- 중복 방지: `sent.json` (워크플로가 커밋백)

## 최초 설정 (1회)

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
