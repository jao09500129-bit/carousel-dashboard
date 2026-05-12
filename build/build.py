# -*- coding: utf-8 -*-
"""
회전목마 R&D — 통합 빌드 스크립트 (v2: BeautifulSoup 기반)
==========================================================
GitHub Actions가 매일 08:30 KST에 호출.

변경 사항 (v1 대비):
- html2text + markdown regex 파이프라인 제거
- requests → BeautifulSoup(lxml) → DOM 직접 파싱
- 한경 페이지 구조 변경 시 셀렉터 한 군데만 수정하면 됨

순서:
  1. Fetch — 한경 코리아마켓 뉴스(1-3) + 글로벌마켓 + 데이터센터 4종
  2. BeautifulSoup으로 직접 파싱 → snapshot/signals/macro JSON
  3. Morning Brief 자동 생성 — 글로벌 헤드라인 + 매크로 변동 기반 axes
  4. index.html 빌드 — dashboard_base.html 템플릿에 __DATA__, __BRIEF__ 주입

의존성: requests, beautifulsoup4, lxml + stdlib only.
모든 출력은 UTF-8, JSON은 ensure_ascii=False.
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
ROOT = Path(__file__).resolve().parent.parent   # repo root
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


def warn(msg: str):
    print(f"  ! {msg}", file=sys.stderr)


# ───── 1. Fetch ─────
def fetch(url: str, retries: int = 2, timeout: int = 20) -> str:
    """간단 retry. 실패 시 빈 문자열."""
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
    """모든 페이지의 raw HTML을 반환. raw_html 디렉토리에도 백업 저장."""
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


def _clean(text: str) -> str:
    """줄바꿈/연속 공백/HTML entity 정리."""
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
    """기사 URL 인지 — /article/<id> 패턴."""
    if not href:
        return False
    return bool(re.search(r"/article/[A-Za-z0-9]+(?:[?#]|$)", href))


def parse_news_html(html: str) -> list:
    """
    한경 코리아마켓 뉴스 목록 페이지에서 기사 카드 추출.

    구조 가정:
      - 기사 카드의 메인 링크는 <a href="/article/XXX"> 또는 풀URL
      - 같은 카드 컨테이너 안에 제목, 요약, 일시가 함께 있음
      - 같은 카드에서 같은 URL을 가진 <a>가 중복 출현(이미지+제목+더보기 등)
        → 카드 컨테이너로 올라가서 dedupe
    """
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
            "title": title,
            "url": url_canon,
            "summary": summary,
            "datetime_raw": dt_raw,
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
            "keywords": dedupe_keywords(extract_keywords(it["title"])),
            "hash": hashlib.md5(it["url"].encode()).hexdigest()[:12],
        })
    return out


def extract_signals(items):
    by_kw = defaultdict(lambda: {"brokers": set(), "actions": [], "items": [], "opinions": []})
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
        base = {"keyword": kw, "n_brokers": n, "brokers": sorted(agg["brokers"]),
                "ups": ups, "downs": downs, "items": agg["items"]}
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
        "pages": pages_used,
        "total_items": len(enriched),
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


# ───── 3. Macro parsing (BeautifulSoup) ─────
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
    """
    한경 데이터센터 표 파싱.

    구조 가정:
      - 페이지에 여러 <table>이 있을 수 있음 (일일 데이터 + 기간 데이터)
      - 일일 표 행은 보통 9개 셀: [이름 a] | 심볼 | 종가 | 전일비 | 전일비(%) | 시가 | 고가 | 저가 | 거래일
      - 첫 컬럼 anchor 텍스트가 picklist에 포함되면 채택
      - 같은 이름이 여러 table에 등장하면 첫 등장(=일일 표)만 채택
    """
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


# ───── 4. Global headlines (글로벌마켓 페이지) ─────
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


# ───── 5. Morning Brief 자동 생성 ─────
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
            })

    if not axes:
        axes.append({
            "axis": "관찰 대기",
            "view": "주요 매크로 지표 모두 임계치 이하. 큰 변동 없음 — 회전축 신호 약함. 기존 포지션 유지·관찰.",
            "confidence": "낮음 — 신호 부재",
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
                "regime": regime,
                "axes": axes,
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
                "note": "★ 모든 함의는 미검증 가설. 백테스트 alpha 확인 전 매매 직접 사용 금지.",
            },
            "macro": macro,
        }
    brief = json.loads(fb.read_text(encoding="utf-8"))
    brief["macro"] = macro
    brief["as_of"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST") + " (fallback)"
    (DATA_ROOT / "morning_brief.json").write_text(
        json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return brief


# ───── 6. index.html 빌드 ─────
def build_index(snapshot, brief):
    tpl_path = BUILD_DIR / "dashboard_base.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    data_js = json.dumps(snapshot, ensure_ascii=False)
    brief_js = json.dumps(brief, ensure_ascii=False)
    html = tpl.replace("__DATA__", data_js).replace("__BRIEF__", brief_js)
    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[html] index.html {len(html):,} bytes")


# ───── main ─────
def main():
    print(f"=== Carousel R&D Daily Build (v2 / bs4) :: {NOW_ISO} ===")
    fetched = {"news_pages": {}, "global": "", "macro": {}}
    try:
        fetched = fetch_all()
    except Exception as e:
        warn(f"fetch error (continuing): {e}")
        traceback.print_exc()
    snapshot = build_news(fetched)
    macro = build_macro(fetched)
    brief = build_brief(snapshot, macro, fetched)
    build_index(snapshot, brief)
    print("=== DONE ===")


if __name__ == "__main__":
    main()
