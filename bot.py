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

import pnl

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
MAX_CONSTITUENTS = 50   # 전체 감시 구성종목 상한
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
    (r"삼성전기", "009150.KS"), (r"SK스퀘어", "402340.KS"),
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
    """pf-dash-a3k9m이 private면 raw.githubusercontent.com이 404 — PF_DASH_LOCAL_REPO가
    가리키는 로컬 클론(맥미니는 pf-dash-runner 자동화가 이미 최신으로 유지)이 있으면 그걸 우선 읽는다."""
    local_repo = os.environ.get("PF_DASH_LOCAL_REPO")
    merged = {}
    for f in LOOKTHROUGH_FILES:
        try:
            if local_repo and (Path(local_repo) / f).exists():
                text = (Path(local_repo) / f).read_text(encoding="utf-8")
            else:
                text = http_get(RAW + f)
            obj = extract_js_obj(text)
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
            add(extra, 0.06, h["name"])  # 수동 지정 종목은 롱테일보다 항상 우선
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


# ---------- 2.5 공시 (DART) ----------

# 주가 영향 큰 공시만 (report_nm 키워드). 정기보고서·단순 IR 등은 제외.
DART_IMPORTANT_RE = re.compile(
    r"주요사항|자기주식|소각|배당|주주환원|기업가치|밸류업|유상증자|무상증자|합병|분할|"
    r"영업양수|영업양도|공급계약|단일판매|수주|잠정.*실적|손익구조|영업.*정지|회생|파산|"
    r"상장폐지|감자|전환사채|신주인수권|교환사채|소송|횡령|배임|최대주주.*변경|조회공시|풍문"
)
CORP_CACHE = HERE / "corp_codes.json"


def load_dart_key():
    """DART_API_KEY 환경변수, 없으면 DART_ENV_FILE(KEY=VALUE 파일)에서 — pf-dash-runner 키 재사용."""
    key = os.environ.get("DART_API_KEY")
    if key:
        return key.strip()
    env_file = os.environ.get("DART_ENV_FILE")
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            m = re.match(r"""\s*DART_API_KEY\s*=\s*["']?([^"'\s]+)""", line)
            if m:
                return m.group(1)
    return None


def kr_corp_map(constituents, api_key):
    """6자리 한국 종목코드 → DART corp_code. corpCode.xml(zip, ~10MB)은 1회 받아 캐시."""
    import zipfile, io as _io
    import xml.etree.ElementTree as ET
    needed = {}
    for c in constituents:
        tk = c.get("ticker") or ""
        if re.fullmatch(r"\d{6}\.(KS|KQ)", tk):
            needed[tk[:6]] = c["name"]
    if not needed:
        return {}
    cache = {}
    if CORP_CACHE.exists():
        try:
            cache = json.loads(CORP_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    if all(code in cache for code in needed):
        return {code: cache[code] for code in needed}
    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}"
    try:
        blob = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        with zipfile.ZipFile(_io.BytesIO(blob)) as z:
            xml = z.read(z.namelist()[0])
        for el in ET.fromstring(xml).iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            if sc in needed:
                cache[sc] = {"corp_code": el.findtext("corp_code").strip(),
                             "corp_name": el.findtext("corp_name").strip()}
        CORP_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[warn] corpCode 다운로드 실패: {e}")
    missing = [needed[c] for c in needed if c not in cache]
    if missing:
        print(f"[warn] corp_code 없음: {missing}")
    return {code: cache[code] for code in needed if code in cache}


def fetch_dart_disclosures(api_key, corp_map, days_back=0):
    """회사별 list.json 조회 (종목당 1콜) → 중요 키워드 공시만."""
    out = []
    bgn = (NOW.astimezone(KST).date() - timedelta(days=days_back)).strftime("%Y%m%d")
    end = NOW.astimezone(KST).date().strftime("%Y%m%d")
    for code, corp in corp_map.items():
        q = urllib.parse.urlencode({"crtfc_key": api_key, "corp_code": corp["corp_code"],
                                    "bgn_de": bgn, "end_de": end, "page_count": 50})
        try:
            data = json.loads(http_get("https://opendart.fss.or.kr/api/list.json?" + q, timeout=20))
        except Exception as e:
            print(f"[warn] DART {corp['corp_name']}: {e}")
            continue
        if data.get("status") != "000":
            continue
        for it in data.get("list", []):
            if DART_IMPORTANT_RE.search(it.get("report_nm", "")):
                out.append({"corp_name": corp["corp_name"], "report_nm": it["report_nm"].strip(),
                            "rcept_no": it["rcept_no"], "date": it.get("rcept_dt", ""),
                            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"})
        time.sleep(0.1)
    return out


def disclosure_key(d):
    return "d:" + d["rcept_no"]


# ---------- 3. 중복 방지 ----------

SENT = HERE / "sent.json"


def load_sent():
    if SENT.exists():
        try:
            return json.loads(SENT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"keys": [], "judged": []}


def save_sent(state, new_keys, judged=()):
    state["keys"] = (state["keys"] + list(new_keys))[-2000:]
    state["judged"] = (state.get("judged", []) + list(judged))[-2000:]
    SENT.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def news_key(item):
    # Google News 리다이렉트 URL은 기사마다 고유 → 그대로 키로 사용
    return "n:" + item["url"][-80:]


def event_key(ev):
    return f"e:{ev['type']}:{ev['ticker']}:{ev['date']}"


# ---------- 4. 요약 (Codex 구독 우선, Claude API 폴백) ----------

def build_summary_prompt(payload):
    today = NOW.astimezone(KST).strftime("%-m/%-d (%a)") if os.name != "nt" else NOW.astimezone(KST).strftime("%m/%d")
    return f"""너는 포트폴리오 모닝브리프 작성자다. 아래 JSON은 내 보유 ETF들과 그 상위 구성종목에 대해
오늘 수집한 이벤트(실적 발표 예정, 배당락)와 최근 뉴스 헤드라인이다.

이 중 주가에 실제로 영향을 줄 만한 것만 골라 한국어 텔레그램 메시지로 요약하라.

규칙:
- 단순 시세 중계("~주가 상승"), SEO성 기사, 중복 기사는 버려라.
- 실적 발표 일정, 배당, M&A, 대규모 투자/수주, 가이던스 변경, 규제, 신제품, 애널리스트 목표주가 변경은 남겨라.
- 각 항목에 어느 보유 ETF와 관련되는지 짧게 표기 (payload의 held_via 참고).
- 형식: 섹션 이모지 + 제목(💰 배당 / 📈 실적 / 📰 주요 뉴스), 항목은 "· " 불릿.
- 💰 배당과 📈 실적 섹션은 항상 넣어라 — payload의 events(type: ex_dividend/earnings)에 해당 항목이 없으면
  섹션 자체를 빼지 말고 "· 없음"이라고 명시해라. (놓친 게 아니라 확인했다는 걸 보여주기 위함)
- payload에 disclosures(DART 공시)가 있으면 📢 공시 섹션으로 전부 표시하라(공시는 필터링 금지, 제목+URL).
- 📰 뉴스 섹션은 실을 게 없으면 생략해도 된다.
- 텔레그램 HTML만 사용: <b></b> 만 허용. 마크다운 금지. 전체 3500자 이내.
- 첫 줄: "📊 포트폴리오 모닝브리프 — {today}"
- payload에 pnl_block이 있으면 첫 줄 바로 다음에 그 텍스트를 <b>토씨 하나 바꾸지 말고 그대로</b> 넣어라(숫자 재계산·재포맷 금지).

JSON:
{json.dumps(payload, ensure_ascii=False)}"""


CODEX_SAFE_ENV_NAMES = ("CODEX_HOME", "HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "USER")
CODEX_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["message"],
    "properties": {"message": {"type": "string"}},
}


def codex_exec(prompt, schema):
    """맥미니 전용 자동화 ChatGPT 계정(CODEX_HOME)으로 codex exec 호출 — 구독 정액, API 과금 없음.
    schema에 맞는 dict 반환, 불가하면 None."""
    import shutil
    import subprocess
    import tempfile

    binary = shutil.which("codex")
    if not binary or not os.environ.get("CODEX_HOME"):
        return None
    environment = {n: os.environ[n] for n in CODEX_SAFE_ENV_NAMES if os.environ.get(n)}
    environment["NO_COLOR"] = "1"
    with tempfile.TemporaryDirectory(prefix="alarmbot-codex-") as tmp:
        tmp = Path(tmp)
        schema_path = tmp / "schema.json"
        result_path = tmp / "result.json"
        workdir = tmp / "workdir"
        workdir.mkdir()
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [binary, "exec", "--ignore-user-config", "--ignore-rules",
                   "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
                   "--model", "gpt-5.6-luna",
                   "--config", 'model_reasoning_effort="medium"',
                   "--output-schema", str(schema_path),
                   "--output-last-message", str(result_path),
                   "-C", str(workdir), "-"]
        try:
            completed = subprocess.run(command, input=prompt, capture_output=True, text=True,
                                        env=environment, timeout=180, check=False)
            if completed.returncode != 0:
                print(f"[warn] codex exec 실패({completed.returncode}): {completed.stderr[:500]}")
                return None
            return json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] Codex 실행 실패: {e}")
            return None


def summarize_with_codex(payload):
    data = codex_exec(build_summary_prompt(payload), CODEX_SCHEMA)
    return data["message"] if data else None


INTRADAY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["alerts"],
    "properties": {"alerts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["text", "article_indexes"],
        "properties": {"text": {"type": "string"},
                       "article_indexes": {"type": "array", "items": {"type": "integer"}}}}}},
}


PNL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["comment"],
    "properties": {"comment": {"type": "string"}},
}


def explain_pnl(p, kr_only=False, news=()):
    """일간 손익 변화 요인을 1~2줄로. Codex 없으면 기여 종목 나열로 폴백."""
    if not p:
        return None
    movers = pnl.top_movers(p, kr_only, n=6)
    if not movers:
        return None
    total = p["daily_pnl_kr"] if kr_only else p["daily_pnl"]
    payload = {
        "일간손익_억원": round(total, 2),
        "기여종목": [{"종목": m["name"], "손익_억원": round(m["pnl"], 2),
                    "등락률": round(m["pct"], 2), "지역": m.get("region", ""),
                    "주력섹터": m.get("sector", ""),
                    "주요구성종목": m.get("holdings", [])} for m in movers],
        "관련뉴스": [n["title"] for n in list(news)[:15]],
    }
    prompt = f"""아래는 내 포트폴리오의 하루 손익과 종목별 기여도다.
왜 이런 손익이 났는지 <b>아주 짧은 한 문장</b>으로 요약하라.

규칙:
- 40자 안팎. 개조식으로 끝내라(예: "~기여도 컸음", "~부진").
- 금액·등락률 숫자는 쓰지 마라. 위에 이미 표시돼 있다.
- ETF·펀드 이름을 나열하지 마라. 대신 <b>시장(한국/미국 등) + 섹터·테마</b> 단위로 묶어서 말하라.
  (주력섹터·주요구성종목 필드를 참고해 무엇이 움직였는지 추론)
  좋은 예: "한국시장 내 반도체 많이 올라 기여도 컸음"
  나쁜 예: "KODEX 200 ETF가 60.76억원으로 가장 크게 기여"
- 플러스·마이너스가 뚜렷이 갈리면 둘 다 한 구절씩만.
- 관련뉴스에 명확한 원인이 있으면 짧게 덧붙여도 된다. 없으면 억지로 만들지 마라.
- 텔레그램 HTML <b></b>만 허용. 앞에 "📌 요인: "을 붙여라.

JSON:
{json.dumps(payload, ensure_ascii=False)}"""
    data = codex_exec(prompt, PNL_SCHEMA)
    if data and data.get("comment"):
        return data["comment"]
    return pnl.format_movers(p, kr_only)


def judge_urgent_news(news, held_via):
    """수시 알림용: 새 뉴스 중 즉시 알릴 가치 있는 것만 Codex가 선별. [(text, [idx...])] 반환, 실패 시 None."""
    items = [{"idx": i, "query": n["query"], "title": n["title"], "source": n["source"],
              "held_via": held_via.get(n["query"], [])} for i, n in enumerate(news)]
    prompt = f"""너는 포트폴리오 수시 알림 필터다. 아래는 내 보유 ETF의 상위 구성종목에 대해 방금 새로 수집된 뉴스 헤드라인이다.
이 중 아침 브리프까지 기다리지 않고 "지금 바로" 알려야 할 중대 이벤트만 골라라. 기준은 엄격하게:
- 남길 것: 자사주 매입/소각·주주환원·배당 정책 발표, 합병/분할/M&A, 대규모 수주·투자·증설, 실적 서프라이즈/가이던스 변경,
  규제·소송·사고 등 중대 악재, 경영진 교체, 목표주가 대폭 변경.
- 버릴 것: 단순 시세/등락 기사, 시황·전망·칼럼, 이미 알려진 내용의 재탕, 광고성/SEO 기사.
- 같은 사건을 다룬 기사 여러 건은 하나로 합쳐라.
각 알림 text는 한국어 1~2줄: 종목명 + 핵심 내용 + (관련 보유 ETF, 출처). 텔레그램 HTML <b></b>만 허용.
article_indexes에는 그 알림의 근거가 된 기사 idx를 넣어라. 알릴 게 없으면 alerts는 빈 배열.

JSON:
{json.dumps(items, ensure_ascii=False)}"""
    data = codex_exec(prompt, INTRADAY_SCHEMA)
    if not data:
        return None
    return [(a["text"], a.get("article_indexes", [])) for a in data.get("alerts", [])]


def summarize_with_claude(payload):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = build_summary_prompt(payload)
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


def format_disclosures(disclosures):
    lines = ["📢 <b>공시</b>"]
    for d in disclosures:
        lines.append(f"· {d['corp_name']}: {d['report_nm']}\n  {d['url']}")
    return "\n".join(lines)


def fallback_format(events, news, disclosures=()):
    today = NOW.astimezone(KST).strftime("%m/%d")
    dividends = [e for e in events if e["type"] == "ex_dividend"]
    earnings = [e for e in events if e["type"] == "earnings"]
    lines = [f"📊 포트폴리오 모닝브리프 — {today} (요약 없이 원본)"]
    if disclosures:
        lines.append("\n" + format_disclosures(disclosures))
    lines.append("\n💰 <b>배당</b>")
    if dividends:
        for ev in dividends:
            lines.append(f"· {ev['name']} ({ev['ticker']}): {ev['date']} 배당락 (D-{ev['dday']})")
    else:
        lines.append("· 없음")
    lines.append("\n📈 <b>실적</b>")
    if earnings:
        for ev in earnings:
            lines.append(f"· {ev['name']} ({ev['ticker']}): {ev['date']} 실적발표 (D-{ev['dday']})")
    else:
        lines.append("· 없음")
    if news:
        lines.append(f"\n📰 <b>뉴스</b> ({len(news)}건 — 필터링 안 됨, ANTHROPIC_API_KEY 확인 필요)")
        for n in news:
            lines.append(f"· [{n['query']}] {n['title']} ({n['source']})")
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

def load_universe():
    raw = os.environ.get("HOLDINGS_JSON") or (HERE / "holdings.json").read_text(encoding="utf-8")
    holdings = json.loads(raw.lstrip("\ufeff"))["holdings"]  # PowerShell 파이프가 BOM을 붙이는 경우 대응
    lookthrough = fetch_lookthrough()
    constituents = build_universe(holdings, lookthrough)
    print(f"직접보유 {len(holdings)} / 감시 구성종목 {len(constituents)}")
    return holdings, lookthrough, constituents


def collect_news(holdings, lookthrough, constituents):
    """구성종목 + 룩스루 없는 직접보유(펀드/테마ETF는 자체 뉴스 감시)."""
    news = []
    targets = [c["name"] for c in constituents]
    targets += [h["name"] for h in holdings if h["name"] not in lookthrough]
    for name in targets:
        news.extend(fetch_news(name, is_korean(name)))
        time.sleep(0.4)
    return news


def collect_disclosures(constituents, days_back=0):
    key = load_dart_key()
    if not key:
        print("[info] DART 키 없음 — 공시 감시 생략")
        return []
    return fetch_dart_disclosures(key, kr_corp_map(constituents, key), days_back)


def main():
    holdings, lookthrough, constituents = load_universe()
    for c in constituents:
        print(f"  {c['name']:40s} {c['ticker'] or '-':12s} {c['score']}")

    # 이벤트: 티커 있는 구성종목만 (ETF 자체는 yfinance calendar 미지원)
    watch_tickers = [(c["name"], c["ticker"]) for c in constituents if c["ticker"]]
    events = fetch_calendar_events(watch_tickers)
    news = collect_news(holdings, lookthrough, constituents)
    disclosures = collect_disclosures(constituents, days_back=1)  # 전날 밤~새벽 공시까지

    # 중복 제거 (아침은 수시 알림이 이미 보낸 것만 제외 — judged는 무시)
    state = load_sent()
    seen = set(state["keys"])
    events = [e for e in events if event_key(e) not in seen]
    news = [n for n in news if news_key(n) not in seen]
    disclosures = [d for d in disclosures if disclosure_key(d) not in seen]
    print(f"신규 이벤트 {len(events)} / 신규 뉴스 {len(news)} / 신규 공시 {len(disclosures)}")

    pnl_summary = pnl.summarize_pnl()
    pnl_block = pnl.format_pnl(pnl_summary, daily_label="전일 대비 평가손익")
    if pnl_block:
        why = explain_pnl(pnl_summary, news=news)
        if why:
            pnl_block += "\n" + why
    payload = {
        "date_kst": NOW.astimezone(KST).isoformat(),
        "pnl_block": pnl_block,
        "events": events,
        "disclosures": disclosures,
        "news": news,
        "held_via": {c["name"]: c["held_via"] for c in constituents},
    }
    text = summarize_with_codex(payload)
    source = "codex"
    if text is None:
        text = summarize_with_claude(payload)
        source = "claude"
    if text is None:
        text = fallback_format(events, news, disclosures)
        source = "fallback(raw)"
    if pnl_block and pnl_block not in text:   # 요약이 손익을 빠뜨렸으면 직접 삽입
        head, _, rest = text.partition("\n")
        text = f"{head}\n\n{pnl_block}\n{rest}"
    print(f"[info] 요약 소스: {source}")
    send_telegram(text)
    save_sent(state, [event_key(e) for e in events] + [news_key(n) for n in news]
              + [disclosure_key(d) for d in disclosures])
    print("발송 완료")


def intraday():
    """30분마다: 새 공시는 즉시, 새 뉴스는 Codex가 중대하다고 판정한 것만 즉시 발송."""
    hour = NOW.astimezone(KST).hour
    if not (8 <= hour <= 22):
        print(f"[info] KST {hour}시 — 수시 알림 시간대(08~22시) 아님, 종료")
        return
    holdings, lookthrough, constituents = load_universe()
    state = load_sent()
    seen = set(state["keys"])
    judged = set(state.get("judged", []))

    # 전날까지 조회: 22시 이후 늦은 공시·주말 전 공시도 놓치지 않게 (중복은 sent.json이 막음)
    disclosures = [d for d in collect_disclosures(constituents, days_back=1) if disclosure_key(d) not in seen]
    news = [n for n in collect_news(holdings, lookthrough, constituents)
            if news_key(n) not in seen and news_key(n) not in judged]
    print(f"신규 공시 {len(disclosures)} / 미판정 뉴스 {len(news)}")

    sections, sent_keys = [], [disclosure_key(d) for d in disclosures]
    if disclosures:
        sections.append(format_disclosures(disclosures))
    if news:
        held_via = {c["name"]: c["held_via"] for c in constituents}
        alerts = judge_urgent_news(news, held_via)
        if alerts is None:
            print("[warn] Codex 판정 실패 — 이번 회차 뉴스는 다음 회차에 재시도")
            news = []   # judged에 넣지 않아 다음 회차에 다시 판정
        elif alerts:
            sections.append("🔔 <b>주요 뉴스</b>\n" + "\n".join(f"· {t}" for t, _ in alerts))
            for _, idxs in alerts:
                sent_keys += [news_key(news[i]) for i in idxs if 0 <= i < len(news)]
    if sections:
        stamp = NOW.astimezone(KST).strftime("%m/%d %H:%M")
        send_telegram(f"⚡ <b>수시 알림</b> — {stamp}\n\n" + "\n\n".join(sections))
        print("발송 완료")
    else:
        print("알릴 것 없음")
    save_sent(state, sent_keys, judged=[news_key(n) for n in news])


def market_close():
    """한국장 마감 후: 손익만 간단히. 뉴스·공시는 수시 알림이 이미 담당."""
    p = pnl.summarize_pnl()
    block = pnl.format_pnl(p, kr_only=True)
    if not block:
        print("[warn] 손익 데이터 없음 — 발송 생략")
        return
    why = explain_pnl(p, kr_only=True)
    if why:
        block += "\n" + why
    stamp = NOW.astimezone(KST).strftime("%m/%d %H:%M")
    send_telegram(f"🔔 <b>국내 장 마감 손익</b> — {stamp}\n\n{block}")
    print("발송 완료")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    {"intraday": intraday, "close": market_close}.get(mode, main)()
