# -*- coding: utf-8 -*-
"""
회전목마 R&D — 통합 빌드 스크립트 (v5: 친근 라이트 모드 + 회전목마 paper 패널)
====================================================================
GitHub Actions가 매일 08:30 KST + paper_status.json push 시에도 호출.

변경 사항 (v4 대비):
- repo 루트의 paper_status.json 파일을 읽어 __PAPER_STATUS__ placeholder로 주입.
- 회전목마 m21/m26 paper trading 패널 6개를 대시보드 *상단*에 추가 (HTML 측).
- paper_status.json이 없거나 비어 있으면 회전목마 섹션은 자동 숨김(graceful fallback).
- 데이터 로직 / 분석 7종 동일 — 정확도 변경 없음.

변경 사항 (v3 대비):
- 데이터 로직 / 분석 7종 동일 — 정확도 변경 없음.
- v3 Bloomberg 다크 모드 → v4 라이트 모드 친근 UI로 통째 재디자인 (HTML 측).
- TODAY_CALL 페이로드에 친근화 필드 추가:
    * friendly_action: "지금이 기회예요" / "신중하게 진입" / "관망하세요"
    * friendly_emoji: 🟢 / 🟡 / 🔴
    * friendly_reason: 한 줄 한국어 이유
    * stars (1~5): confidence를 별점으로
- 영어 약어(STRONG CONFIRM / CONFIRM / DIVERGENT)는 유지하되,
  HTML 측에서 한국어 ("강한 일치" / "일치" / "의견 충돌")로 표시.
- placeholder 이름 (v3/v4 + v5 신규):
  __DATA__, __BRIEF__, __KOSPI__, __CAROUSEL__, __ALIGNMENT__,
  __TODAY_CALL__, __HEATMAP__, __MOOD__, __PAPER_STATUS__ (신규).

분석 단계 (v3와 동일):
    1. KOSPI Range Forecast — 기사 본문에서 예상 수치 정규식 추출
    2. Conviction Engine — 시그널별 multi-factor 점수 (0~100)
    3. Active Carousel State — 현재 활성 축 + 다음 후보 확률
    4. Strategy-Consensus Alignment — 회전목마 추천 vs 컨센서스
    5. Today's Call — 전체 합성 헤드라인 + 종합 confidence + friendly_*
    6. Sector Heat Map — 12개 GICS 섹터 합의 강도
    7. Market Mood Index — 0~100 Fear&Greed 영감

순서:
  1. Fetch — 한경 코리아마켓 뉴스(1-3) + 글로벌마켓 + 데이터센터 4종
  2. BeautifulSoup으로 직접 파싱 → snapshot/signals/macro JSON
  3. Morning Brief 자동 생성
  4. 신규 분석 7종
  5. index.html 빌드 — dashboard_base.html 템플릿에 placeholders 주입

의존성: requests, beautifulsoup4, lxml + stdlib only.
모든 출력은 UTF-8, JSON은 ensure_ascii=False.
모든 점수·예측값은 "추정 — 백테스트 미검증" 마커 부착.
"""

import os
import re
import sys
import json
import time
import hashlib
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

import requests
from bs4 import BeautifulSoup

# ───── 경로 / 상수 ─────
ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"
RAW_DIR = DATA_ROOT / "raw_html"
SNAP_DIR = DATA_ROOT / "snapshots"
SIG_DIR = DATA_ROOT / "signals"
MACRO_DIR = DATA_ROOT / "macro"
for d in (RAW_DIR, SNAP_DIR, SIG_DIR, MACRO_DIR):
    d.mkdir(parents=True, exist_ok=True)

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
NOW_ISO = datetime.now(KST).isoformat()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}

NEWS_PAGES = [
    ("p1", "https://www.hankyung.com/koreamarket/news/all-news?page=1"),
    ("p2", "https://www.hankyung.com/koreamarket/news/all-news?page=2"),
    ("p3", "https://www.hankyung.com/koreamarket/news/all-news?page=3"),
]
GLOBAL_URL = "https://www.hankyung.com/globalmarket"
MACRO_PAGES = [
    ("major-indices", "https://datacenter.hankyung.com/major-indices"),
    ("currencies",    "https://datacenter.hankyung.com/currencies"),
    ("commodities",   "https://datacenter.hankyung.com/commodities"),
    ("rates-bonds",   "https://datacenter.hankyung.com/rates-bonds"),
]

MAJOR_BROKERS = {"미래에셋", "KB", "NH", "한국투자", "삼성"}

SECTOR_KEYWORDS = [
    # 정보기술 (반도체·디스플레이·IT 종목)
    (["반도체", "메모리", "HBM", "D램", "낸드", "파운드리", "장비", "디스플레이", "OLED",
      "삼성전자", "SK하이닉스", "하이닉스", "삼성SDI", "LG디스플레이", "DB하이텍",
      "한미반도체", "원익IPS", "리노공업"], "정보기술"),
    # 자동차
    (["자동차", "로봇", "전기차", "EV", "타이어", "차부품",
      "현대차", "기아", "현대모비스", "한온시스템", "한국타이어"], "자동차"),
    # 에너지·화학
    (["정유", "석유", "화학", "에너지", "원유", "가스", "배터리소재", "2차전지",
      "SK이노베이션", "S-Oil", "에쓰오일", "GS칼텍스", "LG화학", "롯데케미칼",
      "한화솔루션", "OCI", "에코프로", "포스코퓨처엠"], "에너지"),
    # 금융
    (["은행", "증권", "보험", "금융지주", "카드", "캐피탈",
      "KB금융", "신한지주", "하나금융", "우리금융", "메리츠금융", "삼성생명",
      "삼성화재", "DB손해보험"], "금융"),
    # 헬스케어
    (["제약", "바이오", "헬스케어", "의료", "임상", "신약", "백신",
      "셀트리온", "삼성바이오로직스", "유한양행", "한미약품", "녹십자", "SK바이오팜"], "헬스케어"),
    # 산업재
    (["항공", "해운", "물류", "조선", "기계", "철강", "건설기계",
      "대한항공", "아시아나", "HMM", "팬오션", "HD현대중공업", "한화에어로스페이스",
      "포스코홀딩스", "현대제철"], "산업재"),
    # 필수소비재
    (["백화점", "유통", "소비재", "식품", "음료", "화장품", "패션", "주류",
      "이마트", "롯데쇼핑", "신세계", "오리온", "농심", "CJ제일제당",
      "아모레퍼시픽", "LG생활건강"], "필수소비재"),
    # 커뮤니케이션
    (["통신", "인터넷", "포털", "게임", "미디어", "엔터", "콘텐츠", "플랫폼",
      "SK텔레콤", "KT", "LG유플러스", "네이버", "NAVER", "카카오",
      "엔씨소프트", "크래프톤", "넷마블", "JYP", "하이브", "SM"], "커뮤니케이션"),
    # 부동산·건설
    (["부동산", "건설", "리츠", "건자재",
      "현대건설", "GS건설", "대우건설", "DL이앤씨"], "부동산"),
    # 유틸리티
    (["유틸리티", "전력", "가스공사", "한전",
      "한국전력", "한국가스공사"], "유틸리티"),
    # 소재
    (["소재", "금속", "광물", "비철", "리튬", "니켈", "구리", "철광석", "시멘트",
      "고려아연", "POSCO", "포스코"], "소재"),
]
ALL_SECTORS = ["정보기술", "자동차", "에너지", "금융", "헬스케어", "산업재",
               "필수소비재", "커뮤니케이션", "부동산", "유틸리티", "소재", "기타"]


def warn(msg: str):
    print(f"  ! {msg}", file=sys.stderr)


# ───── v4 친근화 유틸 ─────
def stars_from_score(score_0_100):
    """0~100 점수를 별점 5개로. \"⭐⭐⭐⭐○\" 형식."""
    try:
        n = max(0, min(5, round(float(score_0_100) / 20.0)))
    except Exception:
        n = 0
    return "⭐" * n + "○" * (5 - n)


def stars_from_n(n_1_to_5):
    try:
        n = max(0, min(5, int(n_1_to_5)))
    except Exception:
        n = 0
    return "⭐" * n + "○" * (5 - n)


def signal_emoji(score_0_100):
    """80↑=🟢 매수/강세, 50↑=🟡 중립, 그 외=🔴"""
    try:
        s = float(score_0_100)
    except Exception:
        s = 0
    if s >= 80:
        return "🟢"
    if s >= 50:
        return "🟡"
    return "🔴"


def mood_label_emoji(score_0_100):
    """시장 분위기 라벨 + 이모지. (label_kr, emoji)"""
    try:
        s = float(score_0_100)
    except Exception:
        s = 0
    if s >= 85:
        return ("과열", "🤩")
    if s >= 70:
        return ("낙관적", "😊")
    if s >= 50:
        return ("보통", "😐")
    if s >= 30:
        return ("약간 조심", "😟")
    return ("불안", "🥶")


def friendly_today_action(confidence_1_5, alignment_score_0_100):
    """오늘의 액션 한 줄. confidence + alignment 기반."""
    try:
        c = int(confidence_1_5)
    except Exception:
        c = 0
    try:
        a = float(alignment_score_0_100)
    except Exception:
        a = 0.0
    if c >= 4 and a >= 80:
        return ("🟢", "지금이 기회예요",
                "여러 지표가 같은 방향을 가리키고 있어요. 계획대로 진입을 검토하세요.")
    if c >= 3 and a >= 60:
        return ("🟡", "신중하게 진입",
                "방향성은 있지만 확신이 아주 강하진 않아요. 분할 매수나 작은 비중으로.")
    if c >= 2 and a >= 40:
        return ("🟡", "관망하세요",
                "신호가 엇갈리고 있어요. 1~2일 더 지켜본 후 행동하세요.")
    return ("🔴", "관망하세요",
            "오늘은 시장 의견이 갈리거나 신호가 약해요. 무리해서 진입하지 마세요.")



# ───── 1. Fetch ─────
def fetch(url: str, retries: int = 2, timeout: int = 20) -> str:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.encoding = r.apparent_encoding or "utf-8"
            if r.status_code == 200 and r.text:
                return r.text
            last_err = f"status={r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.5 * (i + 1))
    warn(f"fetch failed {url}: {last_err}")
    return ""


def fetch_all() -> dict:
    print(f"[{NOW_ISO}] FETCH start")
    out = {"news_pages": {}, "global": "", "macro": {}}
    for tag, url in NEWS_PAGES:
        html = fetch(url)
        out["news_pages"][tag] = html
        if html:
            (RAW_DIR / f"{TODAY}_{tag}.html").write_text(html, encoding="utf-8")
            print(f"  news {tag}: {len(html):,} chars")
        else:
            warn(f"news {tag} empty")
    g = fetch(GLOBAL_URL)
    out["global"] = g
    if g:
        (RAW_DIR / f"{TODAY}_global.html").write_text(g, encoding="utf-8")
        print(f"  global: {len(g):,} chars")
    else:
        warn("global empty")
    for tag, url in MACRO_PAGES:
        html = fetch(url)
        out["macro"][tag] = html
        if html:
            (RAW_DIR / f"macro_{tag}.html").write_text(html, encoding="utf-8")
            print(f"  macro {tag}: {len(html):,} chars")
        else:
            warn(f"macro {tag} empty")
    return out


# ───── 2. News parsing (BeautifulSoup) ─────
BROKERS = {
    "미래에셋": ["미래에셋증권"], "삼성": ["삼성증권"], "NH": ["NH투자증권"],
    "KB": ["KB증권"], "한국투자": ["한국투자증권", "한국투자신탁운용"],
    "신한": ["신한투자증권", "신한금융투자"], "하나": ["하나증권", "하나금융투자"],
    "키움": ["키움증권"], "대신": ["대신증권"], "메리츠": ["메리츠증권"],
    "유안타": ["유안타증권"], "유진": ["유진투자증권"], "IBK": ["IBK투자증권"],
    "SK": ["SK증권"], "다올": ["다올투자증권"], "하이": ["하이투자증권"],
    "교보": ["교보증권"], "현대차": ["현대차증권"],
    "DB": ["DB금융투자", "DB증권"], "BNK": ["BNK투자증권"],
    "이베스트": ["이베스트투자증권"], "한화": ["한화투자증권"], "LS": ["LS증권"],
}
TITLE_TAG_RE = re.compile(
    r"-(미래에셋|삼성|NH|KB|한국투자|신한|하나|키움|대신|메리츠|유안타|유진|IBK|SK|다올|하이|교보|현대차|DB|BNK|이베스트|한화|LS)\s*$"
)
RE_TARGET_UP = re.compile(
    r"(목표주?가?\s*↑"
    r"|목표주가.{0,40}(?:상향|올[리려][다고]?|올림|상승)"
    r"|목표가.{0,40}(?:상향|올[리려][다고]?|올림))"
)
RE_TARGET_DOWN = re.compile(
    r"(목표주?가?\s*↓"
    r"|목표주가.{0,40}(?:하향|내[리려][다고]?|낮[추춤]|하락)"
    r"|목표가.{0,40}(?:하향|내[리려][다고]?|낮[추춤]))"
)
RE_OPINION = re.compile(r"(강력매수|단기매수|매수|중립|보유|매도|비중확대|비중축소)")
RE_TICKER = re.compile(r"[\"'“‘]([^\"'”’]{2,40})[\"'”’]")
RE_DATE = re.compile(r"(20\d{2})[.\-/](\d{2})[.\-/](\d{2})\s+(\d{2}:\d{2})")
RE_TARGET_PRICE = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)\s*(?:만|만원|원)?\s*(?:→|->|에서)\s*"
    r"(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)\s*(?:만|만원|원)?"
)


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("&quot;", '"').replace("&amp;", "&").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.hankyung.com" + href
    return href


def _is_article_url(href: str) -> bool:
    if not href:
        return False
    return bool(re.search(r"/article/[A-Za-z0-9]+(?:[?#]|$)", href))


def parse_news_html(html: str) -> list:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen_urls = set()
    anchors = soup.select('a[href*="/article/"]')
    for a in anchors:
        href = a.get("href", "").strip()
        if not _is_article_url(href):
            continue
        url = _normalize_url(href)
        url_canon = re.split(r"[?#]", url)[0]
        if url_canon in seen_urls:
            continue
        card = a.find_parent(["li", "article"])
        if card is None:
            card = a.find_parent("div")
        if card is None:
            card = a.parent
        title = _clean(a.get_text(" ", strip=True))
        if not title or len(title) < 4:
            h = card.find(["h1", "h2", "h3", "h4", "h5"]) if card else None
            if h:
                title = _clean(h.get_text(" ", strip=True))
            if not title or len(title) < 4:
                cands = [_clean(x.get_text(" ", strip=True))
                         for x in (card.find_all("a") if card else [])]
                cands = [c for c in cands if c and len(c) >= 8]
                if cands:
                    title = max(cands, key=len)
        if not title or len(title) < 4:
            continue
        summary = ""
        if card:
            for sel in ["p.lead", "p.summary", "div.summary", "div.lead", "p", "div.txt"]:
                el = card.select_one(sel)
                if el:
                    txt = _clean(el.get_text(" ", strip=True))
                    if txt and txt != title and len(txt) >= 10:
                        summary = txt[:500]
                        break
        dt_raw = ""
        if card:
            card_text = card.get_text(" ", strip=True)
            m = RE_DATE.search(card_text)
            if m:
                dt_raw = m.group(0)
            else:
                time_el = card.find("time")
                if time_el:
                    dt_attr = time_el.get("datetime") or time_el.get_text(" ", strip=True)
                    if dt_attr:
                        dt_raw = dt_attr
        seen_urls.add(url_canon)
        items.append({
            "title": title, "url": url_canon, "summary": summary, "datetime_raw": dt_raw,
        })
    return items


def extract_brokers(text, title=""):
    found = []
    for short, aliases in BROKERS.items():
        for a in aliases:
            if a in text:
                found.append(short)
                break
    m = TITLE_TAG_RE.search(title)
    if m and m.group(1) not in found:
        found.append(m.group(1))
    return list(dict.fromkeys(found))


def extract_target_action(text):
    if RE_TARGET_UP.search(text):
        return "up"
    if RE_TARGET_DOWN.search(text):
        return "down"
    return None


def extract_target_change_pct(text):
    m = RE_TARGET_PRICE.search(text)
    if not m:
        return None
    try:
        a = float(m.group(1).replace(",", ""))
        b = float(m.group(2).replace(",", ""))
        if a <= 0:
            return None
        return (b - a) / a * 100
    except Exception:
        return None


def extract_opinion(text):
    m = RE_OPINION.search(text)
    return m.group(1) if m else None


def extract_keywords(title):
    kws = []
    for m in RE_TICKER.finditer(title):
        c = m.group(1).strip()
        if len(c) < 2 or c in {"매수", "매도", "보유"}:
            continue
        kws.append(c)
    head = re.split(r"[,，·\-…]", title)[0].strip().strip("\"'“‘”’")
    if 2 <= len(head) <= 25 and head not in kws:
        kws.append(head)
    return kws[:3]


def dedupe_keywords(kws):
    if not kws:
        return []
    kws = list(dict.fromkeys(kws))
    keep = []
    for k in sorted(kws, key=len):
        if len(k) < 2 or len(k) > 30:
            continue
        if any(s in k and s != k for s in keep):
            continue
        keep.append(k)
    return keep[:3]


def normalize_dt(raw):
    if not raw:
        return ""
    try:
        if "T" in raw and len(raw) >= 16:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return d.astimezone(KST).isoformat()
    except Exception:
        pass
    m = RE_DATE.search(raw.strip())
    if not m:
        return ""
    y, mo, d, hm = m.groups()
    try:
        return datetime.strptime(f"{y}-{mo}-{d} {hm}", "%Y-%m-%d %H:%M") \
            .replace(tzinfo=KST).isoformat()
    except Exception:
        return ""


def short_dt(raw):
    if not raw:
        return ""
    m = RE_DATE.search(raw.strip())
    if m:
        y, mo, d, hm = m.groups()
        return f"{y}-{mo}-{d} {hm}"
    try:
        if "T" in raw and len(raw) >= 16:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(KST)
            return d.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return ""


def enrich(items):
    out = []
    for it in items:
        text = it["title"] + " " + it.get("summary", "")
        out.append({
            **it,
            "datetime": normalize_dt(it.get("datetime_raw", "")),
            "dt": short_dt(it.get("datetime_raw", "")),
            "brokers": extract_brokers(text, it["title"]),
            "opinion": extract_opinion(text),
            "target_action": extract_target_action(text),
            "target_change_pct": extract_target_change_pct(text),
            "keywords": dedupe_keywords(extract_keywords(it["title"])),
            "hash": hashlib.md5(it["url"].encode()).hexdigest()[:12],
        })
    return out


def extract_signals(items):
    by_kw = defaultdict(lambda: {"brokers": set(), "actions": [], "items": [], "opinions": [], "tchanges": []})
    for it in items:
        if not it["brokers"]:
            continue
        for kw in it["keywords"]:
            if len(kw) < 2:
                continue
            agg = by_kw[kw]
            agg["brokers"].update(it["brokers"])
            if it["target_action"]:
                agg["actions"].append(it["target_action"])
            if it["opinion"]:
                agg["opinions"].append(it["opinion"])
            if it.get("target_change_pct") is not None:
                agg["tchanges"].append(it["target_change_pct"])
            agg["items"].append({
                "title": it["title"], "url": it["url"], "brokers": it["brokers"],
                "target_action": it["target_action"], "opinion": it["opinion"],
                "datetime": it["datetime"],
            })
    out = {"broker_consensus": [], "target_price_cluster_up": [],
           "target_price_cluster_down": [], "divergence": []}
    for kw, agg in by_kw.items():
        n = len(agg["brokers"])
        ups = agg["actions"].count("up")
        downs = agg["actions"].count("down")
        avg_change = sum(agg["tchanges"]) / len(agg["tchanges"]) if agg["tchanges"] else 0.0
        base = {"keyword": kw, "n_brokers": n, "brokers": sorted(agg["brokers"]),
                "ups": ups, "downs": downs, "items": agg["items"],
                "avg_target_change_pct": round(avg_change, 2)}
        if n >= 2:
            out["broker_consensus"].append(base)
        if ups >= 2:
            out["target_price_cluster_up"].append(base)
        if downs >= 2:
            out["target_price_cluster_down"].append(base)
        if ups >= 1 and downs >= 1:
            out["divergence"].append(base)
    for k in out:
        out[k].sort(key=lambda x: -x["n_brokers"])
    return out


def build_news(fetched: dict):
    all_items = []
    pages_used = []
    for tag, _url in NEWS_PAGES:
        html = fetched["news_pages"].get(tag, "")
        if not html:
            continue
        try:
            page_items = parse_news_html(html)
        except Exception as e:
            warn(f"news parse {tag} failed: {e}")
            traceback.print_exc()
            continue
        print(f"  parsed {tag}: {len(page_items)} items")
        if not page_items:
            warn(f"news {tag} parse returned 0 items — DOM 셀렉터 점검 필요")
        all_items.extend(page_items)
        pages_used.append(tag)
    seen, uniq = set(), []
    for it in all_items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    enriched = enrich(uniq)
    bcount = Counter()
    for x in enriched:
        bcount.update(x["brokers"])
    n_up = sum(1 for x in enriched if x["target_action"] == "up")
    n_down = sum(1 for x in enriched if x["target_action"] == "down")
    n_b = sum(1 for x in enriched if x["brokers"])
    signals = extract_signals(enriched)
    snapshot = {
        "collected_at": NOW_ISO, "date": TODAY,
        "source": "hankyung_koreamarket_news",
        "pages": pages_used, "total_items": len(enriched),
        "stats": {
            "with_broker_mention": n_b,
            "target_price_up": n_up,
            "target_price_down": n_down,
            "top_brokers": bcount.most_common(15),
        },
        "items": enriched,
        "consensus_signals": signals["broker_consensus"],
    }
    (SNAP_DIR / f"{TODAY}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    sig_out = {
        "computed_at": NOW_ISO, "date": TODAY,
        "summary": {k: len(v) for k, v in signals.items()},
        "signals": signals,
        "note": "★ 미검증 가설. 백테스트로 alpha 확인 전 매매 직접 사용 금지.",
    }
    (SIG_DIR / f"{TODAY}.json").write_text(
        json.dumps(sig_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  news total {len(enriched)} / broker-mention {n_b} / up {n_up} / "
          f"down {n_down} / consensus {len(signals['broker_consensus'])}")
    if len(enriched) == 0:
        warn("뉴스 0건 — 페이지 fetch 또는 파싱 모두 실패")
    return snapshot


# ───── 3. Macro parsing ─────
MACRO_PICK = {
    "major-indices": ["S&P 500", "나스닥", "다우존스", "반도체", "NIKKEI 225", "홍콩", "독일", "대만"],
    "currencies":    ["미국", "유로", "일본", "중국"],
    "commodities":   ["브렌트", "두바이유 현물", "전기동", "전기동 3M", "니켈", "알루미늄 H/G 캐시"],
    "rates-bonds":   ["미국 국채 10년", "미국 국채 2년", "미국 국채 30년", "국고10년", "국고3년", "일본 국채 10년"],
}
MACRO_LABELS = {
    "major-indices": "한경 데이터센터 해외지수",
    "currencies":    "한경 데이터센터 외환",
    "commodities":   "한경 데이터센터 원자재",
    "rates-bonds":   "한경 데이터센터 채권금리",
}
RE_PCT_CELL = re.compile(r"^[+\-]?\d+(?:\.\d+)?%$")


def parse_macro_html(html: str, picklist: list) -> list:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        first = tds[0]
        a = first.find("a")
        name = _clean((a.get_text(" ", strip=True) if a else first.get_text(" ", strip=True)))
        if not name or name not in picklist:
            continue
        cells = [_clean(td.get_text(" ", strip=True)) for td in tds]
        symbol = cells[1] if len(cells) > 1 else ""
        close = cells[2] if len(cells) > 2 else ""
        chg = cells[3] if len(cells) > 3 else ""
        chg_pct = ""
        for c in cells[3:7]:
            if RE_PCT_CELL.match(c):
                chg_pct = c
                break
        rows.append({
            "name": name, "symbol": symbol,
            "close": close, "chg": chg, "chg_pct": chg_pct,
        })
    seen, uniq = set(), []
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        uniq.append(r)
    return uniq


def build_macro(fetched: dict) -> dict:
    macro = {"collected_at": NOW_ISO, "source": "datacenter.hankyung.com", "categories": {}}
    for key, label in MACRO_LABELS.items():
        html = fetched["macro"].get(key, "")
        if not html:
            warn(f"macro {key}: empty html")
            macro["categories"][key] = {"label": label, "items": []}
            continue
        try:
            items = parse_macro_html(html, MACRO_PICK[key])
        except Exception as e:
            warn(f"macro {key} parse failed: {e}")
            traceback.print_exc()
            items = []
        macro["categories"][key] = {"label": label, "items": items}
        if not items:
            warn(f"macro {key}: 0 items — DOM 셀렉터 점검 필요")
        print(f"  macro {key}: {len(items)} indicators")
    (MACRO_DIR / "latest.json").write_text(
        json.dumps(macro, ensure_ascii=False, indent=2), encoding="utf-8")
    return macro


# ───── 4. Global headlines ─────
def parse_global_headlines(html: str, limit: int = 5) -> list:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out = []
    seen = set()
    for a in soup.select('a[href*="/article/"]'):
        href = a.get("href", "")
        if not _is_article_url(href):
            continue
        url = re.split(r"[?#]", _normalize_url(href))[0]
        if url in seen:
            continue
        title = _clean(a.get_text(" ", strip=True))
        if not title or len(title) < 8:
            continue
        seen.add(url)
        out.append({"title": title, "summary": "", "url": url})
        if len(out) >= limit:
            break
    return out


# ───── 5. Morning Brief ─────
def extract_kr_focus(snapshot, limit=3):
    out = []
    for it in snapshot.get("items", [])[:10]:
        if it.get("dt"):
            out.append(f"{it['title']} ({it['dt']})")
        else:
            out.append(it["title"])
        if len(out) >= limit:
            break
    cs = snapshot.get("consensus_signals", [])
    if cs and len(out) < limit + 1:
        top = cs[0]
        out.append(f"★ 합의 시그널 — {top['keyword']}: {top['n_brokers']}사 동조 "
                   f"({', '.join(top['brokers'])})")
    return out[:limit + 1]


def parse_pct(s):
    try:
        return float(str(s).replace("%", "").replace("+", "").replace(",", ""))
    except Exception:
        return 0.0


def parse_bp_change(name, items):
    for it in items:
        if it["name"] == name:
            try:
                sign = -1 if str(it.get("chg_pct", "")).startswith("-") else 1
                bp = abs(float(str(it.get("chg", "0")).replace(",", ""))) * 100
                return sign * bp
            except Exception:
                return 0.0
    return 0.0


def detect_axes(macro):
    cats = macro.get("categories", {})
    indices = {x["name"]: x for x in cats.get("major-indices", {}).get("items", [])}
    fx = {x["name"]: x for x in cats.get("currencies", {}).get("items", [])}
    comm = {x["name"]: x for x in cats.get("commodities", {}).get("items", [])}
    rates_items = cats.get("rates-bonds", {}).get("items", [])
    axes = []
    us10_bp = parse_bp_change("미국 국채 10년", rates_items)
    if abs(us10_bp) >= 5:
        direction = "상승" if us10_bp > 0 else "하락"
        axes.append({
            "axis": "금리 민감 회전축",
            "view": f"미국 10년 yield {us10_bp:+.1f}bp {direction}. "
                    "리츠·고배당·장기 그로스 비중에 영향. 회전 진입 시 듀레이션 노출 재점검 필요.",
            "confidence": "중 — 백테스트 미검증",
            "trigger_metric": "미국 10년 yield",
            "trigger_value": f"{us10_bp:+.1f}bp",
        })
    brent = comm.get("브렌트")
    if brent:
        pct = parse_pct(brent.get("chg_pct"))
        if abs(pct) >= 2:
            direction = "급등" if pct > 0 else "급락"
            axes.append({
                "axis": "에너지 회전축",
                "view": f"브렌트 {pct:+.2f}% {direction}. 정유·해운·항공 비용 구조 영향. "
                        "단기 catalyst 명확 시 회전 검토, 변동성 자체로는 alpha 검증 어려움.",
                "confidence": "낮음 — 변동성 자체가 검증 까다로움",
                "trigger_metric": "브렌트",
                "trigger_value": f"{pct:+.2f}%",
            })
    semi = indices.get("반도체")
    if semi:
        pct = parse_pct(semi.get("chg_pct"))
        if abs(pct) >= 1.5:
            direction = "강세" if pct > 0 else "조정"
            tail = "단기 과열 경계" if pct > 0 else "저점 매수 진입 검토"
            axes.append({
                "axis": "반도체 회전축",
                "view": f"美 반도체 지수 {pct:+.2f}% {direction}. "
                        f"한국 반도체(삼성·하이닉스) 동조 가능성 큼. {tail} — "
                        "확신은 합의 시그널과 교차검증 필요.",
                "confidence": "중 — 백테스트 미검증",
                "trigger_metric": "美 반도체 지수",
                "trigger_value": f"{pct:+.2f}%",
            })
    usdkrw = fx.get("미국")
    if usdkrw:
        pct = parse_pct(usdkrw.get("chg_pct"))
        if abs(pct) >= 0.3:
            direction = "원화 약세" if pct > 0 else "원화 강세"
            tail = ("수출주(반도체·자동차) 마진 호조"
                    if pct > 0 else "내수주 / 외국인 수급 우호 가능")
            axes.append({
                "axis": "환율 민감 회전축",
                "view": f"USDKRW {pct:+.2f}% ({direction}). {tail} — "
                        "단 단일 일자 변동은 노이즈 가능, 추세 확인 후 회전 검토.",
                "confidence": "낮음 — 단기 변동성 큼",
                "trigger_metric": "USDKRW",
                "trigger_value": f"{pct:+.2f}%",
            })
    if not axes:
        axes.append({
            "axis": "관찰 대기",
            "view": "주요 매크로 지표 모두 임계치 이하. 큰 변동 없음 — 회전축 신호 약함. 기존 포지션 유지·관찰.",
            "confidence": "낮음 — 신호 부재",
            "trigger_metric": "",
            "trigger_value": "",
        })
    return axes


def detect_regime(macro):
    cats = macro.get("categories", {})
    indices = cats.get("major-indices", {}).get("items", [])
    if not indices:
        return "데이터 부족 — 매크로 톤 판별 불가 (가설)"
    pcts = [parse_pct(x.get("chg_pct")) for x in indices
            if x["name"] in ("S&P 500", "나스닥", "반도체")]
    if not pcts:
        return "주요 지수 데이터 부족 (가설)"
    avg = sum(pcts) / len(pcts)
    semi = next((parse_pct(x.get("chg_pct")) for x in indices if x["name"] == "반도체"), 0)
    rates = cats.get("rates-bonds", {}).get("items", [])
    us10_bp = parse_bp_change("미국 국채 10년", rates)
    if avg > 0.8 and semi > 1:
        tone = "美 메가캡·반도체 강세 주도"
    elif avg < -0.8:
        tone = "美 증시 조정 — 위험회피 분위기"
    elif abs(us10_bp) >= 5:
        tone = f"금리 변동 주도 ({us10_bp:+.1f}bp)"
    else:
        tone = "혼조 — 명확한 방향성 부족"
    return f"{tone} (가설)"


def build_brief(snapshot, macro, fetched):
    print("[brief] auto-generate")
    try:
        themes = []
        try:
            themes = parse_global_headlines(fetched.get("global", ""), limit=5)
        except Exception as e:
            warn(f"global headlines parse failed: {e}")
            traceback.print_exc()
        kr_focus = extract_kr_focus(snapshot, 3)
        axes = detect_axes(macro)
        regime = detect_regime(macro)
        if themes:
            headline = themes[0]["title"]
        elif snapshot.get("consensus_signals"):
            top = snapshot["consensus_signals"][0]
            headline = (f"합의 시그널 — {top['keyword']}: "
                        f"{top['n_brokers']}개사 동조 ({', '.join(top['brokers'])})")
        else:
            headline = f"{TODAY} 자동 빌드 — 신규 합의 시그널 없음"
        brief = {
            "as_of": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
            "sources": [
                "https://www.hankyung.com/koreamarket/news/all-news",
                "https://www.hankyung.com/globalmarket",
                "https://datacenter.hankyung.com/",
            ],
            "headline": headline,
            "us_market_snapshot": [],
            "themes": themes[:4],
            "today_kr_focus": kr_focus,
            "carousel_implications": {
                "regime": regime, "axes": axes,
                "note": "★ 모든 함의는 미검증 가설. 백테스트 alpha 확인 전 매매 직접 사용 금지.",
            },
            "macro": macro,
        }
        no_news = (snapshot.get("total_items", 0) == 0)
        no_macro_items = all(
            not v.get("items") for v in macro.get("categories", {}).values()
        )
        if no_news and no_macro_items and not themes:
            print("  brief auto-gen yielded empty across all sources — using fallback")
            return load_fallback_brief(macro)
        (DATA_ROOT / "morning_brief.json").write_text(
            json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  brief: themes={len(themes)}, kr_focus={len(kr_focus)}, axes={len(axes)}")
        return brief
    except Exception as e:
        warn(f"brief auto-gen failed: {e}")
        traceback.print_exc()
        return load_fallback_brief(macro)


def load_fallback_brief(macro):
    fb = BUILD_DIR / "morning_brief_fallback.json"
    if not fb.exists():
        return {
            "as_of": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
            "sources": [], "headline": "(빌드 실패 — 기본값)",
            "us_market_snapshot": [], "themes": [], "today_kr_focus": [],
            "carousel_implications": {
                "regime": "데이터 없음 (가설)", "axes": [],
                "note": "★ 모든 함의는 미검증 가설.",
            },
            "macro": macro,
        }
    brief = json.loads(fb.read_text(encoding="utf-8"))
    brief["macro"] = macro
    brief["as_of"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST") + " (fallback)"
    (DATA_ROOT / "morning_brief.json").write_text(
        json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return brief


# ═══════════════════════════════════════════════════════════════════════════
#  Phase 1: 신규 분석 함수 (v3)
# ═══════════════════════════════════════════════════════════════════════════

# ───── (1) KOSPI Range Forecast ─────
RE_KOSPI_RANGE = re.compile(r"KOSPI\s*([\d,]{4,6})\s*[~∼–-]\s*([\d,]{4,6})")
RE_KOSPI_TARGET = re.compile(r"(?:KOSPI|코스피).{0,15}?목표.{0,10}?([\d,]{4,6})")
RE_KOSPI_EXPECT = re.compile(r"(?:KOSPI|코스피).{0,20}?(?:예상|전망|기대|도달).{0,15}?([\d,]{4,6})")
RE_KOSPI_UNTIL = re.compile(r"(?:KOSPI|코스피).{0,20}?([\d,]{4,6})\s*(?:포인트|p|P)?\s*까지")
RE_KOSPI_POINT = re.compile(r"(?:예상|전망).{0,10}?([\d,]{4,6})\s*포인트")


def _to_int(s):
    try:
        return int(str(s).replace(",", "").strip())
    except Exception:
        return None


def _in_kospi_range(v):
    return v is not None and 7000 <= v <= 9000


def build_kospi_forecast(snapshot):
    """기사 본문에서 KOSPI 예상치 추출 → low/high/mid 계산."""
    sources = []
    all_lows = []
    all_highs = []
    all_points = []
    for it in snapshot.get("items", []):
        text = (it.get("title", "") + " " + it.get("summary", ""))
        if not text:
            continue
        title = it.get("title", "")
        url = it.get("url", "")
        found = {"low": None, "high": None, "points": []}
        for m in RE_KOSPI_RANGE.finditer(text):
            lo = _to_int(m.group(1))
            hi = _to_int(m.group(2))
            if _in_kospi_range(lo) and _in_kospi_range(hi) and lo < hi:
                found["low"] = lo if found["low"] is None else min(found["low"], lo)
                found["high"] = hi if found["high"] is None else max(found["high"], hi)
        for pat in (RE_KOSPI_TARGET, RE_KOSPI_EXPECT, RE_KOSPI_UNTIL, RE_KOSPI_POINT):
            for m in pat.finditer(text):
                v = _to_int(m.group(1))
                if _in_kospi_range(v):
                    found["points"].append(v)
        if found["low"] or found["high"] or found["points"]:
            sources.append({
                "url": url, "title": title[:80],
                "low": found["low"], "high": found["high"],
                "points": found["points"],
            })
            if found["low"]:
                all_lows.append(found["low"])
            if found["high"]:
                all_highs.append(found["high"])
            all_points.extend(found["points"])
    pool_low = all_lows + all_points
    pool_high = all_highs + all_points
    overall_low = min(pool_low) if pool_low else None
    overall_high = max(pool_high) if pool_high else None
    all_vals = all_lows + all_highs + all_points
    mid = round(sum(all_vals) / len(all_vals)) if all_vals else None
    result = {
        "computed_at": NOW_ISO, "date": TODAY,
        "low": overall_low, "high": overall_high, "mid": mid,
        "sample_size": len(sources), "sources": sources[:20],
        "note": "★ 추정 — 기사 본문 정규식 추출, 백테스트 미검증. 7000~9000 범위 내 수치만 채택.",
    }
    (DATA_ROOT / "kospi_forecast.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  kospi forecast: low={overall_low} high={overall_high} mid={mid} (n={len(sources)})")
    return result


# ───── (2) Conviction Engine ─────
def _keyword_to_sector(kw):
    for kws, sector in SECTOR_KEYWORDS:
        for k in kws:
            if k in kw:
                return sector
    return "기타"


def _active_macro_sectors(macro):
    active = set()
    cats = macro.get("categories", {})
    indices = {x["name"]: x for x in cats.get("major-indices", {}).get("items", [])}
    fx = {x["name"]: x for x in cats.get("currencies", {}).get("items", [])}
    comm = {x["name"]: x for x in cats.get("commodities", {}).get("items", [])}
    semi = indices.get("반도체")
    if semi and abs(parse_pct(semi.get("chg_pct"))) >= 1.5:
        active.add("정보기술")
    brent = comm.get("브렌트")
    if brent and abs(parse_pct(brent.get("chg_pct"))) >= 2:
        active.add("에너지")
    usdkrw = fx.get("미국")
    if usdkrw and abs(parse_pct(usdkrw.get("chg_pct"))) >= 0.3:
        active.add("자동차")
        active.add("정보기술")
    return active


def _persistence_days(keyword, max_days=5):
    if not SNAP_DIR.exists():
        return 1
    today = datetime.now(KST).date()
    days = 0
    for offset in range(max_days):
        d = today - timedelta(days=offset)
        f = SNAP_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if not f.exists():
            continue
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
            for sig in snap.get("consensus_signals", []):
                if sig.get("keyword") == keyword:
                    days += 1
                    break
        except Exception:
            continue
    return max(days, 1)


def compute_conviction_scores(snapshot, macro):
    signals = snapshot.get("consensus_signals", [])
    active_sectors = _active_macro_sectors(macro)
    enriched = []
    for s in signals:
        brokers = s.get("brokers", [])
        n = len(brokers)
        if n == 0:
            continue
        f_brokers = min(n / 7.0, 1.0) * 30
        major_count = sum(1 for b in brokers if b in MAJOR_BROKERS)
        f_major = (major_count / n) * 20
        avg_chg = abs(s.get("avg_target_change_pct", 0))
        f_target = min(avg_chg / 10.0, 1.0) * 20
        days = _persistence_days(s["keyword"])
        f_persist = min(days / 5.0, 1.0) * 15
        sector = _keyword_to_sector(s["keyword"])
        align = 1.0 if sector in active_sectors else 0.3
        f_align = align * 15
        score = round(f_brokers + f_major + f_target + f_persist + f_align, 1)
        enriched.append({
            **s,
            "conviction_score": score,
            "score_breakdown": {
                "brokers": round(f_brokers, 1),
                "major_broker": round(f_major, 1),
                "target_change": round(f_target, 1),
                "persistence": round(f_persist, 1),
                "macro_align": round(f_align, 1),
            },
            "persistence_days": days,
            "sector": sector,
            "macro_aligned": align >= 1.0,
        })
    enriched.sort(key=lambda x: -x["conviction_score"])
    return enriched


# ───── (3) Active Carousel State ─────
def build_carousel_state(brief, scored_signals, macro):
    axes = brief.get("carousel_implications", {}).get("axes", [])
    current = None
    for a in axes:
        conf = a.get("confidence", "")
        if conf.startswith("중") or conf.startswith("높") or "중 —" in conf:
            current = a
            break
    if current is None and axes:
        current = axes[0]
    current_pivot_name = ""
    if current:
        current_pivot_name = current["axis"].replace("회전축", "").replace("축", "").strip()
    candidates = []
    seen_sectors = set([current_pivot_name])
    for a in axes:
        name = a["axis"].replace("회전축", "").replace("축", "").strip()
        if name == current_pivot_name or name in seen_sectors:
            continue
        conf = a.get("confidence", "")
        if conf.startswith("중") or "중 —" in conf:
            prob = 0.45
        elif conf.startswith("높"):
            prob = 0.65
        else:
            prob = 0.20
        candidates.append({
            "name": name, "probability": prob,
            "trigger": f"{a.get('trigger_metric', '')} {a.get('trigger_value', '')}".strip(),
        })
        seen_sectors.add(name)
    sector_scores = defaultdict(float)
    for s in scored_signals[:5]:
        sec = s.get("sector", "기타")
        sector_scores[sec] += s.get("conviction_score", 0)
    max_sec_score = max(sector_scores.values()) if sector_scores else 1
    for sec, score in sorted(sector_scores.items(), key=lambda x: -x[1]):
        if sec in seen_sectors or sec == "기타":
            continue
        candidates.append({
            "name": sec,
            "probability": round(0.15 + (score / max_sec_score) * 0.45, 2),
            "trigger": f"시그널 누적 score {score:.0f}",
        })
        seen_sectors.add(sec)
    candidates = candidates[:4]
    vol_sum = 0.0
    cats = macro.get("categories", {})
    for cat_key in ["major-indices", "currencies", "commodities"]:
        for x in cats.get(cat_key, {}).get("items", []):
            vol_sum += abs(parse_pct(x.get("chg_pct")))
    rates_items = cats.get("rates-bonds", {}).get("items", [])
    for r_name in ["미국 국채 10년", "미국 국채 2년"]:
        vol_sum += abs(parse_bp_change(r_name, rates_items)) / 10.0
    if vol_sum >= 12:
        velocity = "빠름"
    elif vol_sum >= 6:
        velocity = "중간"
    else:
        velocity = "느림"
    state = {
        "computed_at": NOW_ISO,
        "current_pivot": current_pivot_name or "관찰 대기",
        "current_view": current.get("view", "") if current else "",
        "current_confidence": current.get("confidence", "") if current else "",
        "current_days": 1,
        "rotation_velocity": velocity,
        "volatility_index": round(vol_sum, 1),
        "candidates": candidates,
        "note": "★ 추정 — 매크로 변동성·conviction 휴리스틱 기반, 백테스트 미검증.",
    }
    (DATA_ROOT / "carousel_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  carousel state: pivot={state['current_pivot']} velocity={velocity} candidates={len(candidates)}")
    return state


# ───── (4) Strategy-Consensus Alignment ─────
def build_alignment(brief, scored_signals):
    axes = brief.get("carousel_implications", {}).get("axes", [])
    carousel_rec = ""
    carousel_keywords = set()
    if axes:
        first = axes[0]
        carousel_rec = first["axis"].replace("회전축", "").replace("축", "").strip()
        text = first["axis"] + " " + first.get("view", "")
        for kws, sector in SECTOR_KEYWORDS:
            for k in kws:
                if k in text:
                    carousel_keywords.add(k)
                    carousel_keywords.add(sector)
                    break
    sec_weight = defaultdict(float)
    total_score = 0
    for s in scored_signals[:10]:
        score = s.get("conviction_score", 0)
        sec_weight[s.get("sector", "기타")] += score
        total_score += score
    consensus_top = []
    for sec, w in sorted(sec_weight.items(), key=lambda x: -x[1])[:5]:
        consensus_top.append({
            "sector": sec,
            "weight": round(w / total_score, 2) if total_score > 0 else 0,
        })
    consensus_keywords = set(c["sector"] for c in consensus_top[:3])
    carousel_sec = "기타"
    if axes:
        rec_text = axes[0]["axis"] + " " + axes[0].get("view", "") + " " + carousel_rec
        for kws, sector in SECTOR_KEYWORDS:
            for k in kws:
                if k in rec_text:
                    carousel_sec = sector
                    break
            if carousel_sec != "기타":
                break
    if carousel_keywords and consensus_keywords:
        inter = carousel_keywords & consensus_keywords
        top3_secs = set(c["sector"] for c in consensus_top[:3])
        top5_secs = set(c["sector"] for c in consensus_top[:5])
        if carousel_sec in top3_secs:
            alignment_score = 80 + min(len(inter) * 5, 15)
        elif carousel_sec in top5_secs:
            alignment_score = 60
        else:
            alignment_score = max(30, len(inter) * 10)
    else:
        alignment_score = 0
    alignment_score = min(alignment_score, 100)
    if alignment_score >= 80:
        action = "STRONG CONFIRM"
    elif alignment_score >= 60:
        action = "CONFIRM"
    else:
        action = "DIVERGENT"
    action_kr_map = {
        "STRONG CONFIRM": "강한 일치",
        "CONFIRM": "일치",
        "DIVERGENT": "의견 충돌",
    }
    result = {
        "computed_at": NOW_ISO,
        "carousel_recommendation": carousel_rec or "관찰 대기",
        "carousel_sector": carousel_sec,
        "consensus_top": consensus_top,
        "alignment_score": alignment_score,
        "action": action,
        "action_kr": action_kr_map.get(action, action),
        "note": "★ 추정 — 섹터 매핑 휴리스틱, 백테스트 미검증.",
    }
    (DATA_ROOT / "alignment.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  alignment: rec={carousel_rec}({carousel_sec}) score={alignment_score} -> {action}")
    return result


# ───── (5) Today's Call ─────
def build_today_call(brief, scored_signals, alignment, carousel_state, mood, kospi):
    axes = brief.get("carousel_implications", {}).get("axes", [])
    primary = carousel_state.get("current_pivot", "관찰 대기")
    new_candidates = [c for c in carousel_state.get("candidates", []) if c["probability"] >= 0.4]
    if new_candidates:
        cand_names = ", ".join(c["name"] for c in new_candidates[:2])
        action_headline = f"{primary} 축 가속 + {cand_names} 신규 진입 검토"
    elif primary and primary != "관찰 대기":
        action_headline = f"{primary} 축 유지 — 신규 후보 신호 약함"
    else:
        action_headline = "회전축 신호 약함 — 기존 포지션 유지·관찰"
    conv_avg = (sum(s["conviction_score"] for s in scored_signals[:3]) / 3
                if len(scored_signals) >= 3 else
                (scored_signals[0]["conviction_score"] if scored_signals else 0))
    align_score = alignment.get("alignment_score", 0)
    composite = (conv_avg * 0.5 + align_score * 0.5)
    if composite >= 80:
        confidence = 5
    elif composite >= 65:
        confidence = 4
    elif composite >= 50:
        confidence = 3
    elif composite >= 30:
        confidence = 2
    else:
        confidence = 1
    rationale = []
    if axes and axes[0].get("trigger_metric"):
        a0 = axes[0]
        rationale.append(f"{a0.get('trigger_metric')} {a0.get('trigger_value')} — {a0['axis']}")
    elif axes:
        rationale.append(axes[0].get("view", "")[:80])
    if scored_signals:
        s0 = scored_signals[0]
        rationale.append(
            f"합의 시그널 '{s0['keyword']}' — Conviction {s0['conviction_score']:.0f}/100 "
            f"({s0['n_brokers']}사 동조)")
    if kospi.get("mid"):
        rationale.append(
            f"KOSPI 컨센서스 mid {kospi['mid']:,} "
            f"({kospi.get('low', '-')}~{kospi.get('high', '-')}, n={kospi.get('sample_size', 0)})")
    elif mood.get("score"):
        rationale.append(f"Market Mood {mood['score']}/100 ({mood.get('label', '')})")
    if not rationale:
        rationale = ["데이터 수집 부족 — 분석 임계치 미달", "회전 신호 약함"]
    rationale = rationale[:3]
    align_score_for_friendly = alignment.get("alignment_score", 0)
    f_emoji, f_action, f_reason = friendly_today_action(confidence, align_score_for_friendly)
    action_map_kr = {
        "STRONG CONFIRM": "강한 일치",
        "CONFIRM": "일치",
        "DIVERGENT": "의견 충돌",
    }
    action_raw = alignment.get("action", "DIVERGENT")
    result = {
        "computed_at": NOW_ISO,
        "action_headline": action_headline,
        "confidence": confidence,
        "confidence_pct": round(composite, 1),
        "rationale": rationale,
        "action": action_raw,
        "action_kr": action_map_kr.get(action_raw, action_raw),
        "stars": stars_from_n(confidence),
        "friendly_emoji": f_emoji,
        "friendly_action": f_action,
        "friendly_reason": f_reason,
        "note": "★ 추정 — multi-factor 합성, 백테스트 미검증. 직접 매매 사용 금지.",
    }
    (DATA_ROOT / "today_call.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  today's call: confidence={confidence}/5 ({composite:.0f}%) -> {f_action}")
    return result


# ───── (6) Sector Heat Map ─────
def build_sector_heatmap(snapshot, scored_signals):
    sec_data = {s: {"intensity": 0.0, "count": 0, "top_keywords": [], "scores": []}
                for s in ALL_SECTORS}
    for s in scored_signals:
        sec = s.get("sector", "기타")
        delta = (s.get("ups", 0) - s.get("downs", 0))
        sec_data[sec]["intensity"] += delta
        sec_data[sec]["scores"].append(s["conviction_score"])
        sec_data[sec]["top_keywords"].append({
            "kw": s["keyword"], "n": s["n_brokers"], "score": s["conviction_score"],
        })
    for it in snapshot.get("items", []):
        for kw in it.get("keywords", []):
            sec = _keyword_to_sector(kw)
            sec_data[sec]["count"] += 1
            break
    sectors = []
    for sec in ALL_SECTORS:
        d = sec_data[sec]
        intensity_raw = d["intensity"]
        if d["scores"]:
            avg_score = sum(d["scores"]) / len(d["scores"])
            intensity_norm = max(-3, min(3, intensity_raw * 1.0 + (avg_score - 50) / 25))
        else:
            intensity_norm = max(-3, min(3, intensity_raw * 1.0))
        top = sorted(d["top_keywords"], key=lambda x: -x["score"])[:2]
        sectors.append({
            "name": sec,
            "intensity": round(intensity_norm, 1),
            "count": d["count"],
            "top": top,
        })
    result = {
        "computed_at": NOW_ISO,
        "sectors": sectors,
        "note": "★ 추정 — GICS 섹터 매핑 휴리스틱. 합의 강도는 ups-downs + conviction 합성.",
    }
    (DATA_ROOT / "sector_heatmap.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    nonzero = sum(1 for s in sectors if s["count"] > 0 or abs(s["intensity"]) > 0)
    print(f"  sector heatmap: {nonzero}/12 active")
    return result


# ───── (7) Market Mood Index ─────
def build_mood_index(snapshot, macro, scored_signals, carousel_state):
    cats = macro.get("categories", {})
    vol_sum = 0.0
    for cat_key in ["major-indices", "currencies", "commodities"]:
        for x in cats.get(cat_key, {}).get("items", []):
            vol_sum += abs(parse_pct(x.get("chg_pct")))
    vol_score = max(0, 100 - vol_sum * 5)
    n_divergence = sum(1 for s in scored_signals if s.get("ups", 0) > 0 and s.get("downs", 0) > 0)
    n_signals = len(scored_signals)
    if n_signals > 0:
        div_score = max(0, 100 - (n_divergence / n_signals) * 200)
    else:
        div_score = 50
    stats = snapshot.get("stats", {})
    ups = stats.get("target_price_up", 0)
    downs = stats.get("target_price_down", 0)
    if ups + downs > 0:
        target_score = (ups / (ups + downs)) * 100
    else:
        target_score = 50
    total = snapshot.get("total_items", 0)
    pub_score = min(100, (total / 50) * 100)
    vel = carousel_state.get("rotation_velocity", "중간")
    vel_score = {"느림": 80, "중간": 60, "빠름": 30}.get(vel, 50)
    score = round(
        vol_score * 0.25 + div_score * 0.20 + target_score * 0.20 +
        pub_score * 0.15 + vel_score * 0.20
    )
    score = max(0, min(100, score))
    if score >= 70:
        label = "GREED"
    elif score >= 50:
        label = "NEUTRAL"
    else:
        label = "FEAR"
    mood_kr, mood_emoji = mood_label_emoji(score)
    result = {
        "computed_at": NOW_ISO,
        "score": int(score), "label": label,
        "label_kr": mood_kr,
        "label_emoji": mood_emoji,
        "components": {
            "volatility": round(vol_score, 1),
            "divergence": round(div_score, 1),
            "target_up_ratio": round(target_score, 1),
            "publish_freq": round(pub_score, 1),
            "rotation_velocity": round(vel_score, 1),
        },
        "raw": {
            "vol_sum": round(vol_sum, 2),
            "n_divergence": n_divergence, "n_signals": n_signals,
            "target_up": ups, "target_down": downs,
            "total_items": total, "velocity": vel,
        },
        "note": "★ 추정 — Bloomberg Fear&Greed 영감, 5-factor 가중합. 백테스트 미검증.",
    }
    (DATA_ROOT / "mood_index.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  mood index: {score}/100 ({label})")
    return result


# ───── 5b. paper_status.json 로드 (v5 신규) ─────
def load_paper_status():
    """repo 루트의 paper_status.json 읽기. 없거나 망가졌으면 None.

    이 파일은 사용자의 별도 자동화 시스템(paper_export.bat)이 매일
    회전목마 m21/m26 paper trading 결과를 마스킹한 형태로 push해줌.
    이 빌드 스크립트는 *읽기 전용* — 절대 paper_status.json을 수정하지 않음.
    """
    p = ROOT / "paper_status.json"
    if not p.exists():
        print("[paper] paper_status.json not found — 회전목마 패널은 hidden 으로 렌더")
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            warn("paper_status.json is empty")
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            warn("paper_status.json root is not an object")
            return None
        print(f"[paper] loaded paper_status.json "
              f"(date={data.get('date','?')}, "
              f"m21={'yes' if data.get('m21') else 'no'}, "
              f"m26={'yes' if data.get('m26') else 'no'})")
        return data
    except Exception as e:
        warn(f"paper_status.json parse failed: {e}")
        return None


# ───── 6. index.html 빌드 ─────
def build_index(snapshot, brief, kospi, carousel_state, alignment, today_call,
                heatmap, mood, scored_signals, paper_status):
    tpl_path = BUILD_DIR / "dashboard_base.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    snapshot_out = dict(snapshot)
    snapshot_out["consensus_signals_scored"] = scored_signals
    data_js = json.dumps(snapshot_out, ensure_ascii=False)
    brief_js = json.dumps(brief, ensure_ascii=False)
    kospi_js = json.dumps(kospi, ensure_ascii=False)
    carousel_js = json.dumps(carousel_state, ensure_ascii=False)
    align_js = json.dumps(alignment, ensure_ascii=False)
    today_js = json.dumps(today_call, ensure_ascii=False)
    heat_js = json.dumps(heatmap, ensure_ascii=False)
    mood_js = json.dumps(mood, ensure_ascii=False)
    # v5: paper_status — None이면 빈 객체 ({}) 로 주입해 JS 측에서 hidden 처리
    paper_js = json.dumps(paper_status or {}, ensure_ascii=False)
    html = (tpl
            .replace("__DATA__", data_js)
            .replace("__BRIEF__", brief_js)
            .replace("__KOSPI__", kospi_js)
            .replace("__CAROUSEL__", carousel_js)
            .replace("__ALIGNMENT__", align_js)
            .replace("__TODAY_CALL__", today_js)
            .replace("__HEATMAP__", heat_js)
            .replace("__MOOD__", mood_js)
            .replace("__PAPER_STATUS__", paper_js))
    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[html] index.html {len(html):,} bytes "
          f"(paper_status={'embedded' if paper_status else 'empty'})")


# ───── main ─────
def main():
    print(f"=== Carousel R&D Daily Build (v5 / Friendly Light + Paper Panels) :: {NOW_ISO} ===")
    fetched = {"news_pages": {}, "global": "", "macro": {}}
    try:
        fetched = fetch_all()
    except Exception as e:
        warn(f"fetch error (continuing): {e}")
        traceback.print_exc()
    snapshot = build_news(fetched)
    macro = build_macro(fetched)
    brief = build_brief(snapshot, macro, fetched)
    print("[analysis] computing v3 multi-factor signals")
    kospi = build_kospi_forecast(snapshot)
    scored_signals = compute_conviction_scores(snapshot, macro)
    carousel_state = build_carousel_state(brief, scored_signals, macro)
    alignment = build_alignment(brief, scored_signals)
    heatmap = build_sector_heatmap(snapshot, scored_signals)
    mood = build_mood_index(snapshot, macro, scored_signals, carousel_state)
    today_call = build_today_call(brief, scored_signals, alignment, carousel_state, mood, kospi)
    # v5: 사용자의 별도 자동화가 push한 paper_status.json 을 읽어 회전목마 패널에 주입
    paper_status = load_paper_status()
    build_index(snapshot, brief, kospi, carousel_state, alignment, today_call,
                heatmap, mood, scored_signals, paper_status)
    print("=== DONE ===")


if __name__ == "__main__":
    main()
# v5 build script EOF
