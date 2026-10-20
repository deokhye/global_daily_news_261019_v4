"""저장(upsert)·월간/연간 파이프라인·출력 테스트 — 네트워크 없이 가짜 출처로 실행."""
import json
import shutil
from datetime import date
from pathlib import Path

from country_profile import storage
from country_profile.config import Paths, load_countries, load_settings
from country_profile.fetchers.base import FetchResult
from country_profile.pipeline_annual import resolve_min_wage, run_annual
from country_profile.pipeline_monthly import run_monthly
from country_profile.profile import build_profile
from country_profile.sources.annual import (Eurostat, Ilostat, ImfDataMapper, MinWage, WorldBank,
                                            _jsonstat_series)
from country_profile.sources.monthly import MonthlyResult

ROOT = Path(__file__).resolve().parent.parent


def make_paths(tmp_path) -> Paths:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return Paths(root=tmp_path)


# ---------------------------------------------------------------- storage
def test_upsert_overwrites_same_snapshot_only(tmp_path):
    p = tmp_path / "m.csv"
    cols, key = storage.MONTHLY_COLUMNS, storage.MONTHLY_KEY
    storage.upsert(p, [{"iso3": "KOR", "indicator": "inflation", "snapshot_date": "2026-10-01", "value": 2.9}], cols, key)
    storage.upsert(p, [{"iso3": "KOR", "indicator": "inflation", "snapshot_date": "2026-11-01", "value": 2.7}], cols, key)
    storage.upsert(p, [{"iso3": "KOR", "indicator": "inflation", "snapshot_date": "2026-11-01", "value": 2.6}], cols, key)
    df = storage.load(p, cols)
    assert len(df) == 2
    assert df[df.snapshot_date == "2026-11-01"].value.tolist() == ["2.6"]


# ---------------------------------------------------------------- monthly
class FakeMonthly:
    name = "fake"

    def __init__(self, data):
        self.data = data   # {(iso3, ind): MonthlyResult}

    def url_for(self, c, ind):
        return f"https://example.test/{c.te_slug}/{ind}"

    def fetch(self, c, ind):
        return self.data.get((c.iso3, ind), MonthlyResult(False, self.url_for(c, ind), error="HTTP 404 (주소 없음)"))


def test_monthly_statuses(tmp_path):
    paths = make_paths(tmp_path)
    countries = [c for c in load_countries(paths) if c.iso3 in ("KOR", "MAR")]
    settings = load_settings(paths)
    ok = lambda v, prev, ref: MonthlyResult(True, "u", v, prev, ref)  # noqa: E731

    s1 = run_monthly(countries, settings, paths, FakeMonthly({
        ("KOR", "inflation"): ok(2.9, 3.1, "2026-09"),
        ("KOR", "unemployment"): ok(2.5, 2.6, "2026-09"),
        ("MAR", "unemployment"): ok(9.5, 10.8, "2026-Q2"),
    }), date(2026, 10, 7))
    assert any("첫 실행 URL 점검: 3/4" in n for n in s1.notes)
    assert any("morocco (인플레이션)" in n for n in s1.notes)

    run_monthly(countries, settings, paths, FakeMonthly({
        ("KOR", "inflation"): ok(9.0, 2.9, "2026-10"),        # 6.1%p 급변
        ("MAR", "unemployment"): ok(9.5, 10.8, "2026-Q2"),     # 분기 국가: 갱신 없음
        # KOR 실업률 실패 → 직전 값 유지
    }), date(2026, 11, 1))

    df = storage.load(paths.monthly_history, storage.MONTHLY_COLUMNS)
    nov = df[df.snapshot_date == "2026-11-01"].set_index(["iso3", "indicator"])
    assert nov.loc[("KOR", "inflation"), "status"].startswith("정상 / 경고: 급변 +6.1%p")
    assert nov.loc[("MAR", "unemployment"), "status"] == "갱신 없음"
    assert nov.loc[("KOR", "unemployment"), "status"].startswith("미갱신")
    assert nov.loc[("KOR", "unemployment"), "value"] == "2.5"
    assert nov.loc[("KOR", "unemployment"), "ref_period"] == "2026-09"
    assert len(df) == 8   # 2개국 × 2지표 × 2개월, 중복 없음


# ---------------------------------------------------------------- annual
class FakeFetcher:
    def __init__(self, routes):
        self.routes = routes   # {url 부분문자열: (ok, text)}

    def get(self, url):
        for frag, (ok, text) in self.routes.items():
            if frag in url:
                return FetchResult(url, ok, 200 if ok else 503, text=text, error="" if ok else "HTTP 503")
        return FetchResult(url, False, 404, error="HTTP 404 (주소 없음)")


EUROSTAT_JSON = {
    "id": ["freq", "currency", "geo", "time"], "size": [1, 1, 2, 3],
    "dimension": {
        "freq": {"category": {"index": {"S": 0}}}, "currency": {"category": {"index": {"NAC": 0}}},
        "geo": {"category": {"index": {"DE": 0, "HU": 1}}},
        "time": {"category": {"index": {"2025-S2": 0, "2026-S1": 1, "2026-S2": 2}}},
    },
    "value": {"0": 2161, "1": 2343, "2": 2343, "3": 290800, "4": 322800},
}


def test_jsonstat_decoding():
    s = _jsonstat_series(EUROSTAT_JSON)
    assert s["DE"] == [("2025-S2", 2161.0), ("2026-S1", 2343.0), ("2026-S2", 2343.0)]
    assert s["HU"] == [("2025-S2", 290800.0), ("2026-S1", 322800.0)]


def test_annual_run_with_fallbacks(tmp_path):
    paths = make_paths(tmp_path)
    (paths.config / "min_wage_overrides.csv").write_text(
        "iso3,region,amount,currency,unit,effective_date,checked_date,source_url,note\n"
        "KOR,전국,10320,KRW,시급,2026-01-01,2026-09-01,https://www.minimumwage.go.kr,\n"
        "SWE,,,,,,2026-09-01,https://example.test,법정 최저임금 없음 — 단체협약으로 결정\n",
        encoding="utf-8")
    countries = [c for c in load_countries(paths) if c.iso3 in ("KOR", "TWN", "DEU", "SWE")]
    imf_lp = {"values": {"LP": {"KOR": {"2026": 51.6}, "DEU": {"2026": 83.5}, "SWE": {"2026": 10.6}}}}
    imf_gdp = {"values": {"NGDPD": {"KOR": {"2026": 1872.3}, "DEU": {"2026": 4900.1}, "SWE": {"2026": 620.0}}}}
    fetcher = FakeFetcher({
        "datamapper/api/v1/LP": (True, json.dumps(imf_lp)),
        "datamapper/api/v1/NGDPD": (True, json.dumps(imf_gdp)),
        "worldbank.org/v2/country/TWN": (True, json.dumps([{}, None])),   # 대만은 WB에 없음
        "earn_mw_cur": (True, json.dumps(EUROSTAT_JSON)),
    })
    summary = run_annual(countries, load_settings(paths), paths, ImfDataMapper(fetcher, "2026년 10월"),
                         WorldBank(fetcher), Eurostat(fetcher), Ilostat(fetcher), date(2026, 10, 19), 2026)

    df = storage.load(paths.annual_history, storage.ANNUAL_COLUMNS).set_index(["iso3", "indicator"])
    assert df.loc[("KOR", "population"), "value"] == "51.6"
    assert df.loc[("KOR", "population"), "source"] == "IMF WEO (2026년 10월)"
    assert df.loc[("TWN", "population"), "status"].startswith("미갱신")
    assert df.loc[("KOR", "min_wage"), "status"] == "정상"
    assert df.loc[("KOR", "min_wage"), "unit"] == "시급"
    assert df.loc[("DEU", "min_wage"), "value"] == "2343.0"
    assert df.loc[("DEU", "min_wage"), "effective_date"] == "2026-07-01"
    assert df.loc[("SWE", "min_wage"), "status"] == "법정 최저임금 없음"
    assert any(f.startswith("TWN 대만 총인구") for f in summary.failed)

    prof = build_profile(countries, paths)
    kor = next(c for c in prof["countries"] if c["iso3"] == "KOR")
    assert kor["population"] == "51.6백만 명"
    assert kor["gdp"] == "1,872.3십억 USD"
    assert kor["min_wage"] == "KRW 10,320 / 시급 (전국, 2026-01-01 시행)"
    assert kor["sites"][0] == "Hankook Tire Global Headquarters (Seongnam)"
    swe = next(c for c in prof["countries"] if c["iso3"] == "SWE")
    assert swe["min_wage"] == "법정 최저임금 없음 (단체협약으로 결정)"
    assert (paths.output / "country_profile.xlsx").exists()
    assert any(r["iso3"] == "TWN" for r in prof["review"])


def test_min_wage_review_rules(tmp_path):
    countries = {c.iso3: c for c in load_countries(make_paths(tmp_path))}
    today = date(2026, 10, 19)
    old = MinWage("보정 파일(HR 확인)", "u", 2161, "EUR", "월급", "2025-07-01", checked_date="2025-06-01", ref_year=2025)
    eu = MinWage("Eurostat (earn_mw_cur)", "u", 2343, "EUR", "월급", "2026-07-01", ref_year=2026)
    chosen, reasons = resolve_min_wage(countries["DEU"], [old], eu, None, today, 365)
    assert chosen is old
    assert any("1년 경과" in r for r in reasons)
    assert any("Eurostat에 더 최근 값" in r for r in reasons)

    ilo_old = MinWage("ILOSTAT (x)", "u", 100, "MAD", "월급", "2024", ref_year=2024)
    chosen, reasons = resolve_min_wage(countries["MAR"], [], None, ilo_old, today, 365)
    assert chosen is ilo_old and any("2024년 기준" in r for r in reasons)
