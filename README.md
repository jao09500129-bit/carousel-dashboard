# 회전목마 R&D Dashboard

매일 한국 증시 시황 + 글로벌 매크로 + 증권사 합의 시그널을 자동 수집·분석하는 R&D 대시보드.

## Live

https://jao09500129-bit.github.io/carousel-dashboard/

## 자동 빌드

- 스케줄: **매일 08:30 KST** (Mon-Fri, GitHub Actions cron)
- 워크플로: `.github/workflows/daily.yml`
- 빌드 스크립트: `build/build.py` (단일 파일, stdlib + requests + bs4 + html2text)
- 수동 실행: Actions 탭 → "Daily Carousel Dashboard Build" → Run workflow

## 데이터 소스

- **뉴스**: [한경 코리아마켓 — 전체 뉴스](https://www.hankyung.com/koreamarket/news/all-news) (page 1-3)
- **글로벌**: [한경 글로벌마켓](https://www.hankyung.com/globalmarket)
- **매크로**: 한경 데이터센터 — [해외지수](https://datacenter.hankyung.com/major-indices) / [외환](https://datacenter.hankyung.com/currencies) / [원자재](https://datacenter.hankyung.com/commodities) / [채권·금리](https://datacenter.hankyung.com/rates-bonds)

## 빌드 산출물

```
data/
  raw_md/         — 원본 markdown 변환본 (YYYY-MM-DD_p1.md, macro_*.md)
  snapshots/      — 일별 뉴스 스냅샷 + 합의 시그널 (YYYY-MM-DD.json)
  signals/        — 합의·클러스터 시그널 상세 (YYYY-MM-DD.json)
  macro/latest.json — 매크로 numeric 최신값
  morning_brief.json — Brief 종합
index.html        — Pages가 서빙하는 통합 대시보드
```

## 주의

★ 모든 "합의 시그널" 과 "회전축 함의" 는 **미검증 가설**.
백테스트로 alpha 검증 전에는 실제 매매에 직접 사용하지 말 것 — R&D / 환경 인식 보조 자료로만 활용.

## 로컬 실행

```bash
pip install requests beautifulsoup4 html2text
python build/build.py
```
