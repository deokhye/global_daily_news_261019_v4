"""연간 지표 출처: IMF DataMapper, World Bank, Eurostat, ILOSTAT, 최저임금 보정 파일.

확인된 응답 형식 (2026-10 확인)
  IMF DataMapper  GET /external/datamapper/api/v1/{지표}
                  → {"values": {"LP": {"KOR": {"2026": 51.6, ...}, ...}}}
                  LP = 인구(백만 명), NGDPD = 명목 GDP(십억 USD). 대만은 TWN 코드로 제공.
  World Bank      GET /v2/country/{ISO3}/indicator/{지표}?format=json&date={연도}
                  → [메타, [{"countryiso3code": "KOR", "date": "2025", "value": 51684564}, ...]]
  Eurostat        earn_mw_cur (JSON-stat 2.0), id = [freq, currency, geo, time], time = "2026-S1"
                  currency=NAC(자국통화) 월 단위 법정 최저임금.

검증하지 못한 가정 (README에도 기재)
  ILOSTAT         rplumber 서비스의 CSV 다운로드
                  GET https://rplumber.ilo.org/data/indicator/?id=EAR_4MMN_CUR_NB_A&ref_area={ISO3}&format=.csv
                  열: ref_area, source, indicator, classif1, time, obs_value …
                  classif1 = CUR_TYPE_LCU(자국통화) 행만 사용. 지표명: Statutory nominal gross monthly minimum wage.
                  → 첫 실행 로그에서 'ILOSTAT' 실패가 많으면 이 주소/열 이름을 먼저 확인할 것.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import Country
from ..fetchers.base import Fetcher

log = logging.getLogger(__name__)

IMF_URL = "https://www.imf.org/external/datamapper/api/v1/{indicator}"
IMF_PAGE = "https://www.imf.org/external/datamapper/{indicator}@WEO"
WB_URL = "https://api.worldbank.org/v2/country/{iso3}/indicator/{indicator}?format=json&date={year}"
WB_PAGE = "https://data.worldbank.org/indicator/{indicator}?locations={iso2}"
EUROSTAT_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/earn_mw_cur"
                "?format=JSON&lang=EN&currency=NAC&sinceTimePeriod={since}-S1")
EUROSTAT_PAGE = "https://ec.europa.eu/eurostat/databrowser/view/earn_mw_cur/default/table"
ILOSTAT_URL = "https://rplumber.ilo.org/data/indicator/?id=EAR_4MMN_CUR_NB_A&ref_area={iso3}&format=.csv"
ILOSTAT_PAGE = "https://ilostat.ilo.org/data/"

# 지표 정의: IMF 코드, World Bank 대체 코드, World Bank 값을 IMF 단위로 바꾸는 나눗수
# (나눗수는 단위 정의 그 자체 — 인구: 명→백만 명, GDP: USD→십억 USD)
ANNUAL_INDICATORS = {
    "population": {"label": "총인구", "imf": "LP", "wb": "SP.POP.TOTL", "wb_divisor": 1e6, "unit": "백만 명", "currency": ""},
    "gdp": {"label": "연간 GDP 규모(명목)", "imf": "NGDPD", "wb": "NY.GDP.MKTP.CD", "wb_divisor": 1e9, "unit": "십억", "currency": "USD"},
}


@dataclass
class AnnualValue:
    value: float | None
    source: str
    source_url: str
    year: int | None = None
    fallback: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# IMF / World Bank
# ---------------------------------------------------------------------------
class ImfDataMapper:
    def __init__(self, fetcher: Fetcher, vintage: str = ""):
        self.fetcher = fetcher
        self.vintage = vintage
        self._cache: dict[str, dict] = {}
        self._errors: dict[str, str] = {}

    def _load(self, code: str) -> dict:
        if code not in self._cache:
            res = self.fetcher.get(IMF_URL.format(indicator=code))
            data = {}
            if res.ok:
                try:
                    data = res.json().get("values", {}).get(code, {}) or {}
                except Exception as e:  # noqa: BLE001
                    self._errors[code] = f"IMF {code} 응답 해석 실패: {e}"
            else:
                self._errors[code] = f"IMF {code} 요청 실패: {res.error}"
            if code in self._errors:
                log.warning(self._errors[code])
            self._cache[code] = data
        return self._cache[code]

    def get(self, code: str, iso3: str, year: int) -> AnnualValue:
        label = f"IMF WEO ({self.vintage})" if self.vintage else "IMF WEO"
        series = self._load(code).get(iso3) or {}
        v = series.get(str(year))
        page = IMF_PAGE.format(indicator=code)
        if v is None:
            err = self._errors.get(code) or f"IMF {code} {year}년 값 없음"
            return AnnualValue(None, label, page, year, error=err)
        return AnnualValue(float(v), label, page, year)


class WorldBank:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def get(self, code: str, country: Country, year: int, divisor: float) -> AnnualValue:
        url = WB_URL.format(iso3=country.iso3, indicator=code, year=year)
        page = WB_PAGE.format(indicator=code, iso2=country.iso2)
        res = self.fetcher.get(url)
        if not res.ok:
            return AnnualValue(None, "World Bank", page, year, True, f"World Bank 요청 실패: {res.error}")
        try:
            payload = res.json()
            rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and payload[1] else []
            for r in rows:
                if str(r.get("date")) == str(year) and r.get("value") is not None:
                    return AnnualValue(float(r["value"]) / divisor, "World Bank", page, year, True)
        except Exception as e:  # noqa: BLE001
            return AnnualValue(None, "World Bank", page, year, True, f"World Bank 응답 해석 실패: {e}")
        return AnnualValue(None, "World Bank", page, year, True, f"World Bank {year}년 값 없음")


# ---------------------------------------------------------------------------
# 최저임금
# ---------------------------------------------------------------------------
@dataclass
class MinWage:
    source: str
    source_url: str
    amount: float | None = None
    currency: str = ""
    unit: str = ""
    effective_date: str = ""      # YYYY-MM-DD 또는 YYYY(연도만 아는 경우)
    region: str = ""
    note: str = ""
    checked_date: str = ""
    no_statutory: bool = False
    ref_year: int | None = None
    extra: dict = field(default_factory=dict)


def _sortable(d: str) -> str:
    """'2026' → '2026-01-01' 처럼 비교 가능한 날짜 문자열."""
    d = (d or "").strip()
    if len(d) == 4:
        return f"{d}-01-01"
    if len(d) == 7:
        return f"{d}-01"
    return d


def load_overrides(path) -> dict[str, list[MinWage]]:
    """config/min_wage_overrides.csv (쉼표 구분, UTF-8). 금액이 비어 있으면 '법정 최저임금 없음'."""
    out: dict[str, list[MinWage]] = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                iso3 = row.get("iso3", "").upper()
                if not iso3:
                    continue
                amount_raw = row.get("amount", "").replace(",", "")
                no_stat = amount_raw == "" or amount_raw.upper() == "NONE"
                out.setdefault(iso3, []).append(MinWage(
                    source="각국 노동법 기준" if no_stat else "정부 공식 발표", source_url=row.get("source_url", ""),
                    amount=None if no_stat else float(amount_raw),
                    currency=row.get("currency", ""), unit=row.get("unit", ""),
                    effective_date=row.get("effective_date", ""), region=row.get("region", ""),
                    note=row.get("note", ""), checked_date=row.get("checked_date", ""),
                    no_statutory=no_stat,
                    ref_year=int(row["effective_date"][:4]) if row.get("effective_date", "")[:4].isdigit() else None,
                ))
    except FileNotFoundError:
        log.warning("보정 파일이 없습니다: %s", path)
    return out


def pick_override(rows: list[MinWage], today: date) -> MinWage | None:
    """시행일이 오늘 이전인 항목 중 가장 최근 것. (시행일 없는 '법정 최저임금 없음' 행 포함)"""
    valid = [r for r in rows if not r.effective_date or _sortable(r.effective_date) <= today.isoformat()]
    if not valid:
        return None
    return max(valid, key=lambda r: _sortable(r.effective_date) or "0000")


def _jsonstat_series(payload: dict) -> dict[str, list[tuple[str, float]]]:
    """Eurostat JSON-stat → {geo: [(time, value), ...]} (값 있는 것만, 시간순)."""
    ids, sizes, dims = payload["id"], payload["size"], payload["dimension"]
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    pos = {d: {code: idx for code, idx in dims[d]["category"]["index"].items()} for d in ids}
    out: dict[str, list[tuple[str, float]]] = {}
    values = payload.get("value", {})
    for geo, gi in pos["geo"].items():
        for t, ti in sorted(pos["time"].items(), key=lambda kv: kv[1]):
            flat = 0
            for d, size_stride in zip(ids, strides):
                idx = gi if d == "geo" else ti if d == "time" else 0
                flat += idx * size_stride
            v = values.get(str(flat)) if isinstance(values, dict) else (values[flat] if flat < len(values) else None)
            if v is not None:
                out.setdefault(geo, []).append((t, float(v)))
    return out


def _semester_start(period: str) -> str:
    # "2026-S1" → 2026-01-01, "2026-S2" → 2026-07-01
    y, s = period.split("-S")
    return f"{y}-01-01" if s == "1" else f"{y}-07-01"


class Eurostat:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self._data: dict | None = None
        self.error = ""

    def _load(self, since_year: int) -> dict:
        if self._data is None:
            res = self.fetcher.get(EUROSTAT_URL.format(since=since_year))
            self._data = {}
            if res.ok:
                try:
                    self._data = _jsonstat_series(res.json())
                except Exception as e:  # noqa: BLE001
                    self.error = f"Eurostat 응답 해석 실패: {e}"
            else:
                self.error = f"Eurostat 요청 실패: {res.error}"
            if self.error:
                log.warning(self.error)
        return self._data

    def get(self, country: Country, today: date) -> MinWage | None:
        if not country.eurostat_geo:
            return None
        series = self._load(today.year - 2).get(country.eurostat_geo, [])
        current = [(t, v) for t, v in series if _semester_start(t) <= today.isoformat()]
        if not current:
            return None
        t, v = current[-1]
        return MinWage(source="Eurostat (earn_mw_cur)", source_url=EUROSTAT_PAGE, amount=v,
                       currency=country.currency, unit="월급", effective_date=_semester_start(t),
                       ref_year=int(t[:4]), extra={"period": t})


class Ilostat:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def get(self, country: Country) -> tuple[MinWage | None, str]:
        url = ILOSTAT_URL.format(iso3=country.iso3)
        res = self.fetcher.get(url)
        if not res.ok:
            return None, f"ILOSTAT 요청 실패: {res.error}"
        try:
            rows = list(csv.DictReader(io.StringIO(res.text)))
        except Exception as e:  # noqa: BLE001
            return None, f"ILOSTAT 응답 해석 실패: {e}"
        lcu = [r for r in rows if "LCU" in (r.get("classif1") or "") and (r.get("obs_value") or "").strip()]
        if not lcu:
            return None, "ILOSTAT 값 없음"
        best = max(lcu, key=lambda r: str(r.get("time")))
        year = str(best.get("time"))[:4]
        return MinWage(source="ILOSTAT (Statutory nominal gross monthly minimum wage)", source_url=ILOSTAT_PAGE,
                       amount=float(best["obs_value"]), currency=country.currency, unit="월급",
                       effective_date=year, ref_year=int(year) if year.isdigit() else None), ""


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(_sortable(s), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
