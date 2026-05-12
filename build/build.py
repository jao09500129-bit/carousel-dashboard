# -*- coding: utf-8 -*-
"""
회전목마 R&D — 통합 빌드 스크립트
=================================
GitHub Actions가 매일 08:30 KST에 호출.

순서:
  1. Fetch — 한경 코리아마켓 뉴스 페이지(1-3) + 글로벌마켓 + 데이터센터 4종 fetch
  2. HTML → markdown 변환 (html2text)
  3. 파싱 — 뉴스 markdown → snapshot + signals JSON / 매크로 markdown → latest.json
  4. Morning Brief 자동 생성 — 글로벌 헤드라인 + 코리아마켓 시황 + 매크로 변동 기반 axes
  5. index.html 빌드 — dashboard_base.html 템플릿에 __DATA__, __BRIEF__ 주입

의존성: requests, beautifulsoup4, html2text + stdlib only.
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
import html2text
from bs4 import BeautifulSoup

# ───── 경로 / 상수 ─────
ROOT = Path(__file__).resolve().parent.parent   # repo root
BUILD_DIR = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"
RAW_DIR = DATA_ROOT / "raw_md"
SNAP_DIR = DATA_ROOT / "snapshots"
SIG_DIR = DATA_ROOT / "signals"
MACRO_DIR = DATA_ROOT / "macro"
for d in (RAW_DIR, SNAP_DIR, SIG_DIR, MACRO_DIR):
    d.mkdir(parents=True, exist_ok=True)

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
NOW_ISO = datetime.now(KST).isoformat()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

# ───── 1. Fetch + html2text ─────
def html_to_md(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.ignore_emphasis = False
    h.protect_links = True
    h.unicode_snob = True
    return h.handle(html)


def fetch(url: str, retries: int = 3, timeout: int = 20) -> str:
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
    print(f"  ! fetch failed {url}: {last_err}", file=sys.stderr)
    return ""


def fetch_all():
    print(f"[{NOW_ISO}] FETCH start")
    # News pages
    for tag, url in NEWS_PAGES:
        html = fetch(url)
        if not html:
            continue
        md = html_to_md(html)
        out = RAW_DIR / f"{TODAY}_{tag}.md"
        out.write_text(md, encoding="utf-8")
        print(f"  news {tag}: {len(md):,} chars -> {out.name}")
    # Global narrative
    html = fetch(GLOBAL_URL)
    if html:
        md = html_to_md(html)
        (RAW_DIR / f"{TODAY}_global.md").write_text(md, encoding="utf-8")
        print(f"  global: {len(md):,} chars")
    # Macro pages
    for tag, url in MACRO_PAGES:
        html = fetch(url)
        if not html:
            continue
        md = html_to_md(html)
        (RAW_DIR / f"macro_{tag}.md").write_text(md, encoding="utf-8")
        print(f"  macro {tag}: {len(md):,} chars")


# ───── 2. 뉴스 파싱 (parse_and_build.py inline) ─────
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
RE_DATE = re.compile(r"(20\d{2})\.(\d{2})\.(\d{2})\s+(\d{2}:\d{2})")

# html2text 출력 패턴 — `## [title](url)\n  summary lines\n  2026.05.12 09:10`
ARTICLE_RE = re.compile(
    r"##\s*\[([^\]]+)\]\((https?://(?:www\.)?hankyung\.com/article/[^)]+)\)\s*\n"
    r"([\s\S]*?)"
    r"(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2})",
    re.MULTILINE,
)


def parse_markdown(md_text: str) -> list:
    items = []
    for m in ARTICLE_RE.finditer(md_text):
        title = m.group(1).strip()
        url = m.group(2).strip()
        summary = re.sub(r"\s+", " ", m.group(3).strip())[:500]
        dt_raw = m.group(4).strip()
        title = title.replace("&quot;", '"').replace("&amp;", "&")
        summary = summary.replace("&quot;", '"').replace("&amp;", "&")
        items.append({"title": title, "url": url, "summary": summary, "datetime_raw": dt_raw})
    seen, uniq = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


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
    if RE_TARGET_UP.search(text): return "up"
    if RE_TARGET_DOWN.search(text): return "down"
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
    if not kws: return []
    kws = list(dict.fromkeys(kws))
    keep = []
    for k in sorted(kws, key=len):
        if len(k) < 2 or len(k) > 30: continue
        if any(s in k and s != k for s in keep): continue
        keep.append(k)
    return keep[:3]


def normalize_dt(raw):
    m = RE_DATE.match(raw.strip())
    if not m: return ""
    y, mo, d, hm = m.groups()
    try:
        return datetime.strptime(f"{y}-{mo}-{d} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=KST).isoformat()
    except Exception:
        return ""


def short_dt(raw):
    m = RE_DATE.match(raw.strip())
    if not m: return ""
    y, mo, d, hm = m.groups()
    return f"{y}-{mo}-{d} {hm}"


def enrich(items):
    out = []
    for it in items:
        text = it["title"] + " " + it["summary"]
        out.append({
            **it,
            "datetime": normalize_dt(it["datetime_raw"]),
            "dt": short_dt(it["datetime_raw"]),
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
        if not it["brokers"]: continue
        for kw in it["keywords"]:
            if len(kw) < 2: continue
            agg = by_kw[kw]
            agg["brokers"].update(it["brokers"])
            if it["target_action"]: agg["actions"].append(it["target_action"])
            if it["opinion"]: agg["opinions"].append(it["opinion"])
            agg["items"].append({
                "title": it["title"], "url": it["url"], "brokers": it["brokers"],
                "target_action": it["target_action"], "opinion": it["opinion"], "datetime": it["datetime"],
            })
    out = {"broker_consensus": [], "target_price_cluster_up": [],
           "target_price_cluster_down": [], "divergence": []}
    for kw, agg in by_kw.items():
        n = len(agg["brokers"])
        ups = agg["actions"].count("up")
        downs = agg["actions"].count("down")
        base = {"keyword": kw, "n_brokers": n, "brokers": sorted(agg["brokers"]),
                "ups": ups, "downs": downs, "items": agg["items"]}
        if n >= 2: out["broker_consensus"].append(base)
        if ups >= 2: out["target_price_cluster_up"].append(base)
        if downs >= 2: out["target_price_cluster_down"].append(base)
        if ups >= 1 and downs >= 1: out["divergence"].append(base)
    for k in out:
        out[k].sort(key=lambda x: -x["n_brokers"])
    return out


def build_news():
    md_files = sorted(RAW_DIR.glob(f"{TODAY}_p*.md"))
    if not md_files:
        print(f"  no news md files for {TODAY}, building empty snapshot")
        snapshot = {
            "collected_at": NOW_ISO, "date": TODAY, "source": "hankyung_koreamarket_news",
            "pages": [], "total_items": 0,
            "stats": {"with_broker_mention": 0, "target_price_up": 0, "target_price_down": 0, "top_brokers": []},
            "items": [], "consensus_signals": [],
        }
        (SNAP_DIR / f"{TODAY}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return snapshot

    all_items = []
    for f in md_files:
        items = parse_markdown(f.read_text(encoding="utf-8"))
        print(f"  parsed {f.name}: {len(items)} items")
        all_items.extend(items)
    seen, uniq = set(), []
    for it in all_items:
        if it["url"] in seen: continue
        seen.add(it["url"]); uniq.append(it)
    enriched = enrich(uniq)

    bcount = Counter()
    for x in enriched: bcount.update(x["brokers"])
    n_up = sum(1 for x in enriched if x["target_action"] == "up")
    n_down = sum(1 for x in enriched if x["target_action"] == "down")
    n_b = sum(1 for x in enriched if x["brokers"])

    signals = extract_signals(enriched)
    snapshot = {
        "collected_at": NOW_ISO, "date": TODAY,
        "source": "hankyung_koreamarket_news",
        "pages": [f.name for f in md_files],
        "total_items": len(enriched),
        "stats": {
            "with_broker_mention": n_b, "target_price_up": n_up,
            "target_price_down": n_down, "top_brokers": bcount.most_common(15),
        },
        "items": enriched,
        "consensus_signals": signals["broker_consensus"],
    }
    (SNAP_DIR / f"{TODAY}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    sig_out = {
        "computed_at": NOW_ISO, "date": TODAY,
        "summary": {k: len(v) for k, v in signals.items()},
        "signals": signals,
        "note": "★ 미검증 가설. 백테스트로 alpha 확인 전 매매 직접 사용 금지.",
    }
    (SIG_DIR / f"{TODAY}.json").write_text(json.dumps(sig_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  news total {len(enriched)} / broker-mention {n_b} / up {n_up} / down {n_down} / consensus {len(signals['broker_consensus'])}")
    return snapshot


# ───── 3. 매크로 파싱 (macro_parser.py inline) ─────
MACRO_ROW_RE = re.compile(
    r"\|\s*\[([^\]]+)\]\([^)]+\)\s*\|\s*([A-Z0-9]+)\s*\|\s*([\d,.\-]+)\s*\|\s*([\d,.\-]+)\s*\|\s*([+\-\d.]+%|0\.00%)\s*\|"
)
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


def parse_macro_table(md_text, picklist):
    rows = []
    for m in MACRO_ROW_RE.finditer(md_text):
        name = m.group(1).strip()
        if name not in picklist: continue
        rows.append({
            "name": name, "symbol": m.group(2).strip(),
            "close": m.group(3).strip(), "chg": m.group(4).strip(),
            "chg_pct": m.group(5).strip(),
        })
    return rows


def build_macro():
    macro = {"collected_at": NOW_ISO, "source": "datacenter.hankyung.com", "categories": {}}
    for key, label in MACRO_LABELS.items():
        f = RAW_DIR / f"macro_{key}.md"
        if not f.exists():
            print(f"  macro: missing {f.name}")
            macro["categories"][key] = {"label": label, "items": []}
            continue
        items = parse_macro_table(f.read_text(encoding="utf-8"), MACRO_PICK[key])
        seen, uniq = set(), []
        for it in items:
            if it["name"] in seen: continue
            seen.add(it["name"]); uniq.append(it)
        macro["categories"][key] = {"label": label, "items": uniq}
        print(f"  macro {key}: {len(uniq)} indicators")
    (MACRO_DIR / "latest.json").write_text(json.dumps(macro, ensure_ascii=False, indent=2), encoding="utf-8")
    return macro


# ───── 4. Morning Brief 자동 생성 ─────
HEADLINE_RE = re.compile(
    r"##?\s*\[([^\]]{8,80})\]\((https?://(?:www\.)?hankyung\.com/article/[^)]+)\)"
)


def extract_global_headlines(limit=5):
    f = RAW_DIR / f"{TODAY}_global.md"
    if not f.exists(): return []
    md = f.read_text(encoding="utf-8")
    out = []
    seen = set()
    for m in HEADLINE_RE.finditer(md):
        title = m.group(1).strip().replace("&quot;", '"').replace("&amp;", "&")
        url = m.group(2).strip()
        if url in seen: continue
        seen.add(url)
        if len(title) < 8: continue
        out.append({"title": title, "summary": "", "url": url})
        if len(out) >= limit: break
    return out


def extract_kr_focus(snapshot, limit=3):
    """코리아마켓 상위 헤드라인 + 합의 시그널 한 줄."""
    out = []
    items = snapshot.get("items", [])
    for it in items[:10]:
        if it.get("dt"):
            out.append(f"{it['title']} ({it['dt']})")
        else:
            out.append(it["title"])
        if len(out) >= limit: break
    cs = snapshot.get("consensus_signals", [])
    if cs and len(out) < limit + 1:
        top = cs[0]
        out.append(f"★ 합의 시그널 — {top['keyword']}: {top['n_brokers']}사 동조 ({', '.join(top['brokers'])})")
    return out[:limit + 1]


def parse_pct(s):
    """'-2.15%' -> -2.15 / fallback 0.0"""
    try:
        return float(s.replace("%", "").replace("+", ""))
    except Exception:
        return 0.0


def parse_bp_change(name, items):
    """채권 yield의 chg (절댓값 bp). chg 값은 '0.09' 같은 yield 변동분 (%포인트)."""
    for it in items:
        if it["name"] == name:
            try:
                # chg_pct sign 사용해서 방향 판단
                sign = -1 if it["chg_pct"].startswith("-") else 1
                bp = abs(float(it["chg"])) * 100  # %p → bp
                return sign * bp
            except Exception:
                return 0.0
    return 0.0


def detect_axes(macro):
    """매크로 변동에서 자동으로 회전축 함의 생성."""
    cats = macro.get("categories", {})
    indices = {x["name"]: x for x in cats.get("major-indices", {}).get("items", [])}
    fx       = {x["name"]: x for x in cats.get("currencies", {}).get("items", [])}
    comm     = {x["name"]: x for x in cats.get("commodities", {}).get("items", [])}
    rates_items = cats.get("rates-bonds", {}).get("items", [])

    axes = []

    # 금리 민감 회전축 — 美 10년 ±5bp 이상
    us10_bp = parse_bp_change("미국 국채 10년", rates_items)
    if abs(us10_bp) >= 5:
        direction = "상승" if us10_bp > 0 else "하락"
        axes.append({
            "axis": "금리 민감 회전축",
            "view": f"미국 10년 yield {us10_bp:+.1f}bp {direction}. 리츠·고배당·장기 그로스 비중에 영향. 회전 진입 시 듀레이션 노출 재점검 필요.",
            "confidence": "중 — 백테스트 미검증"
        })

    # 에너지 회전축 — 브렌트 ±2% 이상
    brent = comm.get("브렌트")
    if brent:
        pct = parse_pct(brent["chg_pct"])
        if abs(pct) >= 2:
            direction = "급등" if pct > 0 else "급락"
            axes.append({
                "axis": "에너지 회전축",
                "view": f"브렌트 {pct:+.2f}% {direction}. 정유·해운·항공 비용 구조 영향. 단기 catalyst 명확 시 회전 검토, 변동성 자체로는 alpha 검증 어려움.",
                "confidence": "낮음 — 변동성 자체가 검증 까다로움"
            })

    # 반도체 회전축 — 반도체 지수 ±1.5% 이상
    semi = indices.get("반도체")
    if semi:
        pct = parse_pct(semi["chg_pct"])
        if abs(pct) >= 1.5:
            direction = "강세" if pct > 0 else "조정"
            axes.append({
                "axis": "반도체 회전축",
                "view": f"美 반도체 지수 {pct:+.2f}% {direction}. 한국 반도체(삼성·하이닉스) 동조 가능성 큼. {'단기 과열 경계' if pct > 0 else '저점 매수 진입 검토'} — 확신은 합의 시그널과 교차검증 필요.",
                "confidence": "중 — 백테스트 미검증"
            })

    # 환율 민감 회전축 — USDKRW ±0.3% 이상
    usdkrw = fx.get("미국")
    if usdkrw:
        pct = parse_pct(usdkrw["chg_pct"])
        if abs(pct) >= 0.3:
            direction = "원화 약세" if pct > 0 else "원화 강세"
            axes.append({
                "axis": "환율 민감 회전축",
                "view": f"USDKRW {pct:+.2f}% ({direction}). {'수출주(반도체·자동차) 마진 호조' if pct > 0 else '내수주 / 외국인 수급 우호 가능'} — 단 단일 일자 변동은 노이즈 가능, 추세 확인 후 회전 검토.",
                "confidence": "낮음 — 단기 변동성 큼"
            })

    if not axes:
        axes.append({
            "axis": "관찰 대기",
            "view": "주요 매크로 지표 모두 임계치 이하. 큰 변동 없음 — 회전축 신호 약함. 기존 포지션 유지·관찰.",
            "confidence": "낮음 — 신호 부재"
        })
    return axes


def detect_regime(macro):
    """매크로 톤 한 줄."""
    cats = macro.get("categories", {})
    indices = cats.get("major-indices", {}).get("items", [])
    if not indices:
        return "데이터 부족 — 매크로 톤 판별 불가 (가설)"
    pcts = [parse_pct(x["chg_pct"]) for x in indices if x["name"] in ("S&P 500", "나스닥", "반도체")]
    if not pcts:
        return "주요 지수 데이터 부족 (가설)"
    avg = sum(pcts) / len(pcts)
    semi = next((parse_pct(x["chg_pct"]) for x in indices if x["name"] == "반도체"), 0)
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


def build_brief(snapshot, macro):
    print(f"[brief] auto-generate")
    try:
        themes = extract_global_headlines(5)
        kr_focus = extract_kr_focus(snapshot, 3)
        axes = detect_axes(macro)
        regime = detect_regime(macro)

        # headline — themes 첫 항목이 있으면 그것 사용, 없으면 합의 시그널 기반
        if themes:
            headline = themes[0]["title"]
        elif snapshot.get("consensus_signals"):
            top = snapshot["consensus_signals"][0]
            headline = f"합의 시그널 — {top['keyword']}: {top['n_brokers']}개사 동조 ({', '.join(top['brokers'])})"
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
            "themes": themes,
            "today_kr_focus": kr_focus,
            "carousel_implications": {
                "regime": regime,
                "axes": axes,
                "note": "★ 모든 함의는 미검증 가설. 백테스트 alpha 확인 전 매매 직접 사용 금지.",
            },
            "macro": macro,
        }
        # 자동 생성 검증 — themes/axes 둘 다 비어있으면 fallback
        if not themes and not kr_focus:
            print("  brief auto-gen yielded empty — using fallback")
            return load_fallback_brief(macro)

        (DATA_ROOT / "morning_brief.json").write_text(
            json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  brief: themes={len(themes)}, kr_focus={len(kr_focus)}, axes={len(axes)}")
        return brief
    except Exception as e:
        print(f"  brief auto-gen failed: {e}")
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
    return brief


# ───── 5. index.html 빌드 ─────
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
    print(f"=== Carousel R&D Daily Build :: {NOW_ISO} ===")
    try:
        fetch_all()
    except Exception as e:
        print(f"fetch error (continuing): {e}")
        traceback.print_exc()
    snapshot = build_news()
    macro = build_macro()
    brief = build_brief(snapshot, macro)
    build_index(snapshot, brief)
    print(f"=== DONE ===")


if __name__ == "__main__":
    main()
