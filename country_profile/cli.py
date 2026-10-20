"""실행 진입점.

  python -m country_profile annual  [--year 2026]        총인구·GDP·최저임금 (연 1회)
  python -m country_profile monthly [--date 2026-11-01]  인플레이션·실업률 (월 1회)
  python -m country_profile check-urls                   Trading Economics 78개 URL 점검
  python -m country_profile save-samples --slug south-korea   파서 테스트용 HTML 샘플 저장
  python -m country_profile build                        이력 CSV로 output/ 프로필만 다시 생성
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from .config import Paths, load_countries, load_settings, now_kst, resolve_base_year
from .fetchers.http import PoliteHttpFetcher
from .parsers.tradingeconomics import ParseError, parse_indicator_page
from .pipeline_annual import run_annual
from .pipeline_monthly import RunSummary, run_monthly
from .profile import build_profile
from .sources.annual import Eurostat, Ilostat, ImfDataMapper, WorldBank
from .sources.monthly import INDICATORS, TradingEconomicsWebSource

log = logging.getLogger("country_profile")


def _scraper(settings) -> PoliteHttpFetcher:
    return PoliteHttpFetcher(settings.user_agent, settings.scrape_min_delay, settings.scrape_max_delay,
                             settings.max_retries, settings.timeout)


def _api(settings) -> PoliteHttpFetcher:
    # 공개 API는 robots.txt 대상이 아니므로(데이터 API 엔드포인트) 확인을 생략하고 짧게 대기
    return PoliteHttpFetcher(settings.user_agent, settings.api_delay, settings.api_delay,
                             settings.max_retries, settings.timeout, respect_robots=False)


def report(summary: RunSummary) -> str:
    lines = [f"## {summary.title}", "",
             f"- 성공: {len(summary.ok)}건", f"- 실패(직전 값 유지·미갱신): {len(summary.failed)}건",
             f"- 경고/검토: {len(summary.warnings)}건", ""]
    country_of = lambda s: " ".join(s.split()[:2])  # "KOR 한국 …" → "KOR 한국"  # noqa: E731
    failed_countries = sorted({country_of(x) for x in summary.failed})
    ok_countries = sorted({country_of(x) for x in summary.ok} - set(failed_countries))
    lines += ["### 모든 항목 성공 국가", ", ".join(ok_countries) or "(없음)", ""]
    if summary.failed:
        lines += ["### 실패 항목", *[f"- {x}" for x in summary.failed], ""]
    if summary.warnings:
        lines += ["### 경고 / 검토 필요", *[f"- {x}" for x in summary.warnings], ""]
    if summary.notes:
        lines += ["### 참고", *[f"- {x}" for x in summary.notes], ""]
    return "\n".join(lines)


def _emit(summary: RunSummary) -> None:
    text = report(summary)
    print(text)
    gh = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    if summary.blocked_count:
        print(f"::warning::수집 차단 {summary.blocked_count}건 — 실행 요약을 확인하세요.")
    if summary.failed and not summary.ok:
        print("::error::모든 항목 수집에 실패했습니다 (직전 값 유지).")


def cmd_annual(args) -> int:
    paths, settings, countries = Paths(), load_settings(), load_countries()
    today = now_kst().date()
    year = args.year or resolve_base_year(settings, today)
    api = _api(settings)
    summary = run_annual(countries, settings, paths, ImfDataMapper(api, settings.imf_weo_vintage),
                         WorldBank(api), Eurostat(api), Ilostat(api), today, year)
    build_profile(countries, paths)
    _emit(summary)
    return 0


def cmd_monthly(args) -> int:
    paths, settings, countries = Paths(), load_settings(), load_countries()
    today = date.fromisoformat(args.date) if args.date else now_kst().date()
    summary = run_monthly(countries, settings, paths, TradingEconomicsWebSource(_scraper(settings)), today)
    build_profile(countries, paths)
    _emit(summary)
    return 0


def cmd_check_urls(args) -> int:
    settings, countries = load_settings(), load_countries()
    source = TradingEconomicsWebSource(_scraper(settings))
    bad = []
    total = 0
    for c in countries:
        for ind in INDICATORS:
            total += 1
            r = source.fetch(c, ind)
            mark = "OK " if r.ok else "NG "
            print(f"{mark} {c.te_slug:<24} {ind:<12} {r.ref_period or r.error}")
            if not r.ok:
                bad.append(f"{c.te_slug} ({ind}): {r.error}")
    print(f"\n정상 {total - len(bad)}/{total}")
    if bad:
        print("열리지 않거나 해석되지 않은 slug:")
        print("\n".join(f"  - {b}" for b in bad))
    return 0 if not bad else 1


def cmd_save_samples(args) -> int:
    settings = load_settings()
    fetcher = _scraper(settings)
    out = Paths().root / "tests" / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    for ind, meta in INDICATORS.items():
        url = f"{TradingEconomicsWebSource.base}/{args.slug}/{meta['te_path']}"
        r = fetcher.get(url)
        if not r.ok:
            print(f"실패: {url} — {r.error}")
            continue
        path = out / f"te_{args.slug}_{ind}.html"
        path.write_text(r.text, encoding="utf-8")
        try:
            p = parse_indicator_page(r.text)
            print(f"저장: {path.name} → {p.value} / 이전 {p.previous} / {p.ref_period} ({p.ref_source})")
        except ParseError as e:
            print(f"저장: {path.name} → 파싱 실패: {e}")
    return 0


def cmd_build(args) -> int:
    build_profile(load_countries(), Paths())
    print("output/country_profile.json, output/country_profile.xlsx 생성 완료")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(prog="python -m country_profile")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("annual", help="총인구·GDP·최저임금 수집")
    a.add_argument("--year", type=int, help="기준연도 (기본: settings.toml 또는 실행 연도)")
    m = sub.add_parser("monthly", help="인플레이션·실업률 수집")
    m.add_argument("--date", help="실행일 지정(테스트용, YYYY-MM-DD). 스냅샷은 그달 1일로 기록")
    sub.add_parser("check-urls", help="Trading Economics URL 78개 점검")
    s = sub.add_parser("save-samples", help="파서 테스트용 HTML 샘플 저장")
    s.add_argument("--slug", required=True)
    sub.add_parser("build", help="이력으로 프로필 출력만 재생성")
    args = ap.parse_args(argv)
    return {"annual": cmd_annual, "monthly": cmd_monthly, "check-urls": cmd_check_urls,
            "save-samples": cmd_save_samples, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
