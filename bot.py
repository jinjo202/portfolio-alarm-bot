# -*- coding: utf-8 -*-
"""
포트폴리오 홀딩스 알람 봇 — 매일 아침 텔레그램 다이제스트.

1. holdings.json 의 보유 종목 + pf-dash-a3k9m 공개 룩스루 파일에서 ETF 상위 구성종목 수집
2. yfinance 로 실적 발표 예정일 / 배당락일, Google News RSS 로 최근 뉴스 수집
3. Claude 로 "주가에 영향 줄 이벤트만" 필터·한국어 요약 (키 없으면 원본 목록 발송)
4. 텔레그램 발송. sent.json 으로 뉴스 중복 방지 (워크플로가 커밋백)

secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY(선택)
"""
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).parent
KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

RAW = "https://raw.githubusercontent.com/jinjo202/pf-dash-a3k9m/main/"
LOOKTHROUGH_FILES = [
    "etf-lookthrough-us.js", "etf-lookthrough-kr.js",
    "etf-lookthrough-global.js", "etf-lookthrough-partial.js",
]

TOP_N_PER_ETF = 10      # ETF당 감시할 상위 구성종목 수
MAX_CONSTITUENTS = 30   # 전체 감시 구성종목 상한
NEWS_PER_NAME = 3
NEWS_WINDOW_H = 36      # 최근 N시간 뉴스만
EVENT_WINDOW_D = 7      # 실적/배당락 D-7 이내만

# 구성종목 이름 → yfinance 티커 (정규식, 대소문자 무시). 없으면 뉴스만 감시.
TICKER_PATTERNS = [
    (r"NVIDIA", "NVDA"), (r"APPLE", "AAPL"), (r"MICROSOFT", "MSFT"),
    (r"BROADCOM", "AVGO"), (r"ADVANCED MICRO", "AMD"), (r"MICRON", "MU"),
    (r"TAIWAN SEMI|TSMC", "TSM"), (r"SAMSUNG ELEC|삼성전자", "005930.KS"),
    (r"SK HYNIX|SK하이닉스", "000660.KS"), (r"TENCENT", "TCEHY"),
    (r"ALIBABA", "BABA"), (r"AMAZON", "AMZN"), (r"META PLATFORMS", "META"),
    (r"ALPHABET|GOOGLE", "GOOGL"), (r"TESLA", "TSLA"), (r"NETFLIX", "NFLX"),
    (r"PALANTIR", "PLTR"), (r"CISCO", "CSCO"), (r"INTEL CORP", "INTC"),
    (r"APPLIED MATERIALS", "AMAT"), (r"LAM RESEARCH", "LRCX"), (r"KLA CORP", "KLAC"),
    (r"TEXAS INSTR", "TXN"), (r"ORACLE", "ORCL"), (r"QUALCOMM", "QCOM"),
    (r"BERKSHIRE", "BRK-B"), (r"JPMORGAN", "JPM"), (r"EXXON", "XOM"),
    (r"UNITEDHEALTH", "UNH"), (r"JOHNSON & JOHNSON", "JNJ"), (r"WALMART", "WMT"),
    (r"VISA INC", "V"), (r"MASTERCARD", "MA"), (r"ELI LILLY", "LLY"),
    (r"COSTCO", "COST"), (r"HOME DEPOT", "HD"), (r"CATERPILLAR", "CAT"),
    (r"GE AEROSPACE|GENERAL ELECTRIC", "GE"), (r"HONEYWELL", "HON"),
    (r"UNION PACIFIC", "UNP"), (r"RTX CORP", "RTX"), (r"UBER", "UBER"),
    (r"LINDE", "LIN"), (r"SHERWIN", "SHW"), (r"ECOLAB", "ECL"),
    (r"FREEPORT", "FCX"), (r"NEWMONT", "NEM"), (r"AIR PRODUCTS", "APD"),
    (r"WALT DISNEY", "DIS"), (r"COMCAST", "CMCSA"), (r"T-MOBILE", "TMUS"),
    (r"NOVO NORDISK", "NVO"), (r"^ASML", "ASML"), (r"^SAP", "SAP"),
    (r"NESTLE", "NSRGY"), (r"ROCHE", "RHHBY"), (r"LVMH", "LVMUY"),
    (r"SIEMENS(?! ENERGY)", "SIEGY"), (r"ALLIANZ", "ALIZY"),
    (r"UNICREDIT", "UNCRY"), (r"INTESA", "ISNPY"), (r"^ENEL", "ENLAY"),
    (r"TOYOTA", "TM"), (r"현대차", "005380.KS"), (r"기아", "000270.KS"),
    (r"현대모비스", "012330.KS"), (r"한화에어로스페이스", "012450.KS"),
    (r"현대로템", "064350.KS"), (r"LIG넥스원", "079550.KS"),
    (r"두산에너빌리티", "034020.KS"), (r"한전기술", "052690.KS"),
    (r"한전KPS", "051600.KS"), (r"삼성바이오로직스", "207940.KS"),
    (r"셀트리온", "068270.KS"), (r"알테오젠", "196170.KQ"),
    (r"HD현대일렉트릭", "267260.KS"), (r"LS ELECTRIC", "010120.KS"),
    (r"효성중공업", "298040.KS"), (r"NAVER", "035420.KS"), (r"카카오", "035720.KS"),
]


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


# ---------- 1. 감시 유니버스 ----------

def extract_js_obj(text):
    """window.X = {...}; 에서 {...} JSON 추출 (중괄호 균형 스캔)."""
    start = text.index("{", text.index("="))
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("닫는 } 못 찾음")


def fetch_lookthrough():
    merged = {}
    for f in LOOKTHROUGH_FILES:
        try:
            obj = extract_js_obj(http_get(RAW + f))
        except Exception as e:
            print(f"[warn] {f}: {e}")
            continue
        for etf, rows in obj.items():
            if isinstance(rows, dict):   # partial 형식: {coverage, rows}
                rows = rows.get("rows", [])
            merged[etf] = [
                {"name": r["name"], "w": r.get("weight", r.get("w", 0))}
                for r in rows if r.get("name")
            ]
    return merged


def clean_name(name):
    """검색용 이름 정리: 'NVIDIA CORP' → 'NVIDIA', 'ALPHABET INC CL A' → 'ALPHABET'."""
    n = re.sub(r"\s+(INC|CORP|CO|LTD|PLC|SA|NV|AG|SE|HOLDINGS?|GROUP|COMPANY)\.?(\s|$)", " ", name, flags=re.I)
    n = re.sub(r"\s+(CLASS|CL)\s+[ABC]$", "", n, flags=re.I)
    n = re.sub(r"\s+[ABC]$", "", n)
    return re.sub(r"\s+", " ", n).strip()


def to_ticker(name):
    for pat, tk in TICKER_PATTERNS:
        if re.search(pat, name, re.I):
            return tk
    return None


def build_universe(holdings, lookthrough):
    """감시 대상: ETF별 상위 구성종목, 티커(없으면 대문자 이름) 기준 병합 후 비중 합산 상위 MAX개."""
    agg = {}   # 병합키 → {"name", "ticker", "score", "held_via"}
    def add(raw_name, w, etf):
        name = clean_name(raw_name)
        if re.search(r"섹터|익스포저|기타|현금|CASH", name, re.I):  # 종목이 아닌 집계 행 제외
            return
        ticker = to_ticker(name)
        key = ticker or name.upper()
        if key in agg:
            agg[key]["score"] += w
            if etf not in agg[key]["held_via"]:
                agg[key]["held_via"].append(etf)
        else:
            agg[key] = {"name": name, "ticker": ticker, "score": w, "held_via": [etf]}
    for h in holdings:
        rows = sorted(lookthrough.get(h["name"], []), key=lambda r: -r["w"])[:TOP_N_PER_ETF]
        for r in rows:
            add(r["name"], r["w"], h["name"])
        for extra in h.get("extra_constituents", []):
            add(extra, 0.05, h["name"])
    top = sorted(agg.values(), key=lambda c: -c["score"])[:MAX_CONSTITUENTS]
    return [{**c, "score": round(c["score"], 4)} for c in top]


# ---------- 2. 이벤트 수집 ----------

def fetch_calendar_events(tickers):
    """yfinance calendar → D-7 이내 실적 발표일·배당락일."""
    import yfinance as yf
    events = []
    horizon = NOW.date() + timedelta(days=EVENT_WINDOW_D)
    for label, tk in tickers:
        try:
            cal = yf.Ticker(tk).calendar or {}
        except Exception as e:
            print(f"[warn] calendar {tk}: {e}")
            continue
        for d in (cal.get("Earnings Date") or []):
            if NOW.date() <= d <= horizon:
                events.append({"type": "earnings", "name": label, "ticker": tk,
                               "date": d.isoformat(), "dday": (d - NOW.date()).days})
                break
        exd = cal.get("Ex-Dividend Date")
        if exd and NOW.date() <= exd <= horizon:
            events.append({"type": "ex_dividend", "name": label, "ticker": tk,
                           "date": exd.isoformat(), "dday": (exd - NOW.date()).days,
                           "amount": cal.get("Dividend Rate") or ""})
        time.sleep(0.2)
    return events


def fetch_news(query, korean):
    if korean:
        params = {"q": f"{query} 주가", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    else:
        params = {"q": f"{query} stock", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    try:
        xml = http_get(url, timeout=15)
    except Exception as e:
        print(f"[warn] news {query}: {e}")
        return []
    out = []
    cutoff = NOW - timedelta(hours=NEWS_WINDOW_H)
    for it in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL):
        tm = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
        lm = re.search(r"<link>(.*?)</link>", it, re.DOTALL)
        pm = re.search(r"<pubDate>(.*?)</pubDate>", it, re.DOTALL)
        sm = re.search(r"<source[^>]*>(.*?)</source>", it, re.DOTALL)
        if not (tm and lm and pm):
            continue
        try:
            pub = datetime.strptime(pm.group(1).strip()[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if pub < cutoff:
            continue
        title = html.unescape(re.sub(r"<.*?>", "", tm.group(1))).strip()
        src = html.unescape(sm.group(1)).strip() if sm else ""
        if not src and " - " in title:
            title, src = title.rsplit(" - ", 1)
        out.append({"query": query, "title": title[:120], "url": lm.group(1).strip(),
                    "source": src[:30], "pub": pub.isoformat()})
        if len(out) >= NEWS_PER_NAME:
            break
    return out


def is_korean(s):
    return bool(re.search(r"[가-힣]", s))


# ---------- 3. 중복 방지 ----------

SENT = HERE / "sent.json"


def load_sent():
    if SENT.exists():
        try:
            return json.loads(SENT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"keys": []}


def save_sent(state, new_keys):
    state["keys"] = (state["keys"] + new_keys)[-2000:]
    SENT.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def news_key(item):
    # Google News 리다이렉트 URL은 기사마다 고유 → 그대로 키로 사용
    return "n:" + item["url"][-80:]


def event_key(ev):
    return f"e:{ev['type']}:{ev['ticker']}:{ev['date']}"


# ---------- 4. 요약 (Claude) ----------

def summarize_with_claude(payload):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    today = NOW.astimezone(KST).strftime("%-m/%-d (%a)") if os.name != "nt" else NOW.astimezone(KST).strftime("%m/%d")
    prompt = f"""너는 포트폴리오 모닝브리프 작성자다. 아래 JSON은 내 보유 ETF들과 그 상위 구성종목에 대해
오늘 수집한 이벤트(실적 발표 예정, 배당락)와 최근 뉴스 헤드라인이다.

이 중 주가에 실제로 영향을 줄 만한 것만 골라 한국어 텔레그램 메시지로 요약하라.

규칙:
- 단순 시세 중계("~주가 상승"), SEO성 기사, 중복 기사는 버려라.
- 실적 발표 일정, 배당, M&A, 대규모 투자/수주, 가이던스 변경, 규제, 신제품, 애널리스트 목표주가 변경은 남겨라.
- 각 항목에 어느 보유 ETF와 관련되는지 짧게 표기 (payload의 held_via 참고).
- 형식: 섹션 이모지 + 제목(💰 배당 / 📈 실적 / 📰 주요 뉴스), 항목은 "· " 불릿.
- 텔레그램 HTML만 사용: <b></b> 만 허용. 마크다운 금지. 전체 3500자 이내.
- 첫 줄: "📊 포트폴리오 모닝브리프 — {today}"
- 남길 게 하나도 없으면 그 섹션 생략. 전부 없으면 "오늘은 특이 이벤트 없음" 한 줄.

JSON:
{json.dumps(payload, ensure_ascii=False)}"""
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return "".join(b.get("text", "") for b in resp.get("content", []))
    except Exception as e:
        print(f"[warn] Claude 요약 실패, 원본 발송: {e}")
        return None


def fallback_format(events, news):
    today = NOW.astimezone(KST).strftime("%m/%d")
    lines = [f"📊 포트폴리오 모닝브리프 — {today} (요약 없이 원본)"]
    if events:
        lines.append("\n📅 <b>실적/배당 일정</b>")
        for ev in events:
            kind = "실적발표" if ev["type"] == "earnings" else "배당락"
            lines.append(f"· {ev['name']} ({ev['ticker']}): {ev['date']} {kind} (D-{ev['dday']})")
    if news:
        lines.append("\n📰 <b>뉴스</b>")
        for n in news[:25]:
            lines.append(f"· [{n['query']}] {n['title']} ({n['source']})")
    if not events and not news:
        lines.append("오늘은 특이 이벤트 없음")
    return "\n".join(lines)


# ---------- 5. 텔레그램 ----------

def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # 4096자 제한 — 줄 단위로 분할
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    chunks.append(cur)
    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk,
                                       "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[telegram] {e.code}: {body}")
            if e.code == 400 and "can't parse entities" in body.lower():  # HTML 파싱 실패 → 평문 재시도
                data = urllib.parse.urlencode({"chat_id": chat_id, "text": re.sub(r"</?b>", "", chunk),
                                               "disable_web_page_preview": "true"}).encode()
                urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30)
            else:
                raise


# ---------- main ----------

def main():
    raw = os.environ.get("HOLDINGS_JSON") or (HERE / "holdings.json").read_text(encoding="utf-8")
    holdings = json.loads(raw.lstrip("﻿"))["holdings"]  # PowerShell 파이프가 BOM을 붙이는 경우 대응
    lookthrough = fetch_lookthrough()
    constituents = build_universe(holdings, lookthrough)
    print(f"직접보유 {len(holdings)} / 감시 구성종목 {len(constituents)}")
    for c in constituents:
        print(f"  {c['name']:40s} {c['ticker'] or '-':12s} {c['score']}")

    # 이벤트: 티커 있는 구성종목만 (ETF 자체는 yfinance calendar 미지원)
    watch_tickers = [(c["name"], c["ticker"]) for c in constituents if c["ticker"]]
    events = fetch_calendar_events(watch_tickers)

    # 뉴스: 구성종목 + 룩스루 없는 직접보유(펀드/테마ETF는 자체 뉴스 감시)
    news = []
    news_targets = [c["name"] for c in constituents]
    news_targets += [h["name"] for h in holdings if h["name"] not in lookthrough]
    for name in news_targets:
        news.extend(fetch_news(name, is_korean(name)))
        time.sleep(0.4)

    # 중복 제거
    state = load_sent()
    seen = set(state["keys"])
    events = [e for e in events if event_key(e) not in seen]
    news = [n for n in news if news_key(n) not in seen]
    print(f"신규 이벤트 {len(events)} / 신규 뉴스 {len(news)}")

    payload = {
        "date_kst": NOW.astimezone(KST).isoformat(),
        "events": events,
        "news": news,
        "held_via": {c["name"]: c["held_via"] for c in constituents},
    }
    text = summarize_with_claude(payload) or fallback_format(events, news)
    send_telegram(text)
    save_sent(state, [event_key(e) for e in events] + [news_key(n) for n in news])
    print("발송 완료")


if __name__ == "__main__":
    main()
