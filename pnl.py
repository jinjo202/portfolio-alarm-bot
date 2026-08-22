# -*- coding: utf-8 -*-
"""포트폴리오 손익 요약 — pf-dash 암호화 데이터(portfolio-data.js)를 복호화해 집계.

맥미니 전용: PORTFOLIO_PASSWORD(또는 PF_DASH_ENV_FILE의 같은 키)와
PF_DASH_LOCAL_REPO(pf-dash-a3k9m 클론)가 있어야 동작한다. 없으면 None을 돌려주고
호출부는 손익 섹션 없이 진행한다.

집계 정의는 대시보드(portfolio.html)와 맞춘다:
  총평가금액 = Σ mkt
  YTD 평가손익(미실현) = Σ (mkt − book_basis)   # 한국식 평균매입가 회계의 잔여 장부가 기준
  YTD 매각이익(실현)   = Σ (ytd_sell − cost_sold)
  YTD 배당            = Σ dividend
  YTD 총손익           = 평가손익 + 매각이익 + 배당
  YTD 수익률           = 총손익 / 평균잔액(historical.portfolio_total의 YTD 평균)
  금일 평가손익        = Σ daily_pnl            # 국내/해외 분리

주의: buy 필드는 종목마다 단위(주당가/억원)가 불일치해 쓰지 않는다 — portfolio.html
cumBase() 주석과 동일한 이유. 반드시 book_basis를 원가 기준으로 쓸 것.
"""
import base64
import json
import os
import re
from pathlib import Path


def _password():
    pw = os.environ.get("PORTFOLIO_PASSWORD")
    if not pw:
        env_file = os.environ.get("PF_DASH_ENV_FILE") or os.environ.get("DART_ENV_FILE")
        if env_file and Path(env_file).exists():
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                m = re.match(r"""\s*PORTFOLIO_PASSWORD\s*=\s*["']?([^"'\s]+)""", line)
                if m:
                    pw = m.group(1)
                    break
    return pw.strip().lstrip("﻿") if pw else None


def load_portfolio():
    """복호화된 portfolio-data dict. 키·레포·의존성 없으면 None."""
    repo = os.environ.get("PF_DASH_LOCAL_REPO")
    pw = _password()
    if not repo or not pw:
        return None
    enc = Path(repo) / "portfolio-data.js"
    if not enc.exists():
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        print("[warn] cryptography 미설치 — 손익 섹션 생략")
        return None
    try:
        blob = base64.b64decode(
            re.search(r'ENCRYPTED\s*=\s*"([^"]+)"', enc.read_text(encoding="utf-8")).group(1))
        salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=salt, iterations=200_000).derive(pw.encode())
        return json.loads(AESGCM(key).decrypt(nonce, ct, None).decode("utf-8"))
    except Exception as e:
        print(f"[warn] 포트폴리오 복호화 실패: {e}")
        return None


def summarize_pnl():
    """손익 요약 dict. 데이터 없으면 None."""
    obj = load_portfolio()
    if not obj or not obj.get("holdings"):
        return None
    H = obj["holdings"]
    tot = lambda k, rows=H: sum(r.get(k) or 0 for r in rows)
    kr = [h for h in H if (h.get("region") or "") == "한국"]
    ov = [h for h in H if (h.get("region") or "") != "한국"]
    mkt = tot("mkt")
    basis = sum(h["book_basis"] if h.get("book_basis") is not None else (h.get("buy") or 0)
                for h in H)
    unrealized = mkt - basis
    realized = tot("ytd_sell") - tot("cost_sold")
    dividend = tot("dividend")
    # 기준일: 종목별 daily_close_date 중 가장 최근 (국내/해외 마감 시차 존재)
    dates = sorted({h.get("daily_close_date") for h in H if h.get("daily_close_date")})
    # 수익률 분모 = 시간가중 평균잔액. 대시보드 computeAvgBalanceFromHistorical()와 같은 정의.
    series = [v for v in ((obj.get("historical") or {}).get("portfolio_total") or [])
              if v is not None]
    avg_invested = sum(series) / len(series) if series else basis
    # 지역별 평가금액 비중 (region 필드 그대로: 한국/미국/유럽/글로벌/이머징)
    by_region = {}
    for h in H:
        r = h.get("region") or "기타"
        by_region[r] = by_region.get(r, 0) + (h.get("mkt") or 0)
    region_mix = sorted(by_region.items(), key=lambda kv: -kv[1])
    # 지역 안의 국가 분해 (lookthrough.country 가중). 유럽·글로벌은 국가가 섞여 있어
    # 지역 이름만으론 실제 노출이 안 보인다.
    region_countries = {}
    for reg in ("유럽", "글로벌"):
        agg = {}
        for h in H:
            if (h.get("region") or "") != reg:
                continue
            mkt = h.get("mkt") or 0
            for code, w in ((h.get("lookthrough") or {}).get("country") or {}).items():
                agg[code] = agg.get(code, 0) + mkt * w
        total = sum(agg.values())
        if total:
            region_countries[reg] = [(COUNTRY_KO.get(c, c), v / total * 100)
                                     for c, v in sorted(agg.items(), key=lambda kv: -kv[1])
                                     if c != "Other"]
    return {
        "as_of": obj.get("last_updated", ""),
        "region_mix": region_mix,
        "region_countries": region_countries,
        "close_date": dates[-1] if dates else "",
        "total_mkt": mkt,
        "total_pnl": unrealized + realized + dividend,
        "avg_invested": avg_invested,
        "total_pnl_pct": (unrealized + realized + dividend) / avg_invested * 100
        if avg_invested else 0,
        "unrealized": unrealized,
        "realized": realized,
        "dividend": dividend,
        "daily_pnl": tot("daily_pnl"),
        "daily_pnl_kr": tot("daily_pnl", kr),
        "daily_pnl_ov": tot("daily_pnl", ov),
        "daily_pct_kr": tot("daily_pnl", kr) / (tot("mkt", kr) - tot("daily_pnl", kr)) * 100
        if tot("mkt", kr) - tot("daily_pnl", kr) else 0,
        "daily_pct_ov": tot("daily_pnl", ov) / (tot("mkt", ov) - tot("daily_pnl", ov)) * 100
        if tot("mkt", ov) - tot("daily_pnl", ov) else 0,
    }


COUNTRY_KO = {
    "JP": "일본", "CA": "캐나다", "UK": "영국", "GB": "영국", "FR": "프랑스",
    "DE": "독일", "CH": "스위스", "NL": "네덜란드", "IT": "이탈리아", "ES": "스페인",
    "SE": "스웨덴", "DK": "덴마크", "AU": "호주", "HK": "홍콩", "SG": "싱가포르",
    "US": "미국", "KR": "한국", "CN": "중국", "TW": "대만", "IN": "인도",
    "BR": "브라질", "NO": "노르웨이", "FI": "핀란드", "BE": "벨기에", "IE": "아일랜드",
}


def _won(v):
    """억원 단위 값을 조/억 표기로. 1,000억 이상은 조원."""
    sign = "+" if v > 0 else ""
    if abs(v) >= 10000:
        return f"{sign}{v / 10000:,.2f}조원"
    return f"{sign}{v:,.2f}억원"


def format_pnl(p, daily_label="금일 평가손익", kr_only=False):
    """텔레그램 HTML 손익 섹션.

    daily_label로 '금일'/'전일 대비' 구분.
    kr_only=True면 일간 손익을 국내만 표시 — 한국장 마감 직후엔 해외가 아직
    전일 종가라 국내와 합산하면 오해를 부른다.
    """
    if not p:
        return None
    lines = [f"💵 <b>손익</b> ({p['close_date'] or p['as_of']} 종가 기준)"]
    lines.append(f"· 총 평가금액: <b>{_won(p['total_mkt']).lstrip('+')}</b>")
    if p.get("region_mix") and p["total_mkt"]:
        parts = []
        for r, v in p["region_mix"]:
            pct = v / p["total_mkt"] * 100
            detail = p.get("region_countries", {}).get(r)
            if detail:   # 유럽·글로벌은 룩스루 상위 국가를 괄호로
                inner = ", ".join(f"{c} {cp:.0f}%" for c, cp in detail[:3])
                parts.append(f"{r} {pct:.1f}%({inner})")
            else:
                parts.append(f"{r} {pct:.1f}%")
        lines.append("   " + " · ".join(parts))
    lines.append(f"· YTD 총손익: <b>{_won(p['total_pnl'])}</b> "
                 f"({p['total_pnl_pct']:+.2f}% · 평균잔액 {_won(p['avg_invested']).lstrip('+')})")
    lines.append(f"   평가 {_won(p['unrealized'])} · 매각 {_won(p['realized'])} · 배당 {_won(p['dividend'])}")
    if kr_only:
        lines.append(f"· {daily_label}(국내): <b>{_won(p['daily_pnl_kr'])}</b> "
                     f"({p['daily_pct_kr']:+.2f}%)")
        lines.append("   해외는 아직 당일 시세 미반영 — 내일 아침 브리프에서 확인")
    else:
        lines.append(f"· {daily_label}: <b>{_won(p['daily_pnl'])}</b>")
        lines.append(f"   국내 {_won(p['daily_pnl_kr'])} ({p['daily_pct_kr']:+.2f}%) · "
                     f"해외 {_won(p['daily_pnl_ov'])} ({p['daily_pct_ov']:+.2f}%)")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_pnl(summarize_pnl()) or "손익 데이터 없음")
