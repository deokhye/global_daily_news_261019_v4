"""설정 파일 로드: config/countries.csv, sites.csv, country_codes.csv, settings.toml.

고정 데이터(수도·거점)와 기준값(임계치·대기시간 등)은 모두 config/ 아래 파일에서 읽는다.
코드에는 수치를 하드코딩하지 않는다.
"""
from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent


def now_kst() -> datetime:
    return datetime.now(KST)


@dataclass
class Country:
    region: str
    name_ko: str
    name_en: str
    iso3: str
    capital: str
    te_slug: str
    iso2: str = ""
    currency: str = ""
    eurostat_geo: str = ""
    sites: list[str] = field(default_factory=list)


@dataclass
class Settings:
    base_year: int
    imf_weo_vintage: str
    override_stale_days: int
    change_alert_pp: float
    user_agent: str
    scrape_min_delay: float
    scrape_max_delay: float
    api_delay: float
    max_retries: int
    timeout: float


@dataclass
class Paths:
    root: Path = ROOT

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def annual_history(self) -> Path:
        return self.data / "annual_history.csv"

    @property
    def monthly_history(self) -> Path:
        return self.data / "monthly_history.csv"

    @property
    def overrides(self) -> Path:
        return self.config / "min_wage_overrides.csv"


def _read_pipe_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(f, delimiter="|") if any((v or "").strip() for v in row.values())]


def load_countries(paths: Paths | None = None) -> list[Country]:
    paths = paths or Paths()
    rows = _read_pipe_csv(paths.config / "countries.csv")
    codes = {r["iso3"]: r for r in _read_pipe_csv(paths.config / "country_codes.csv")}
    sites: dict[str, list[str]] = {}
    for r in _read_pipe_csv(paths.config / "sites.csv"):
        sites.setdefault(r["iso3"], []).append(f"{r['site_name']} ({r['city']})")

    countries = []
    for r in rows:
        iso3 = r["iso3"].upper()
        code = codes.get(iso3, {})
        countries.append(Country(
            region=r["region"], name_ko=r["country_ko"], name_en=r["country_en"], iso3=iso3,
            capital=r["capital"], te_slug=r["te_slug"],
            iso2=code.get("iso2", ""), currency=code.get("currency", ""),
            eurostat_geo=code.get("eurostat_geo", ""),
            sites=sites.get(iso3, []),
        ))
    return countries


def load_settings(paths: Paths | None = None) -> Settings:
    paths = paths or Paths()
    with open(paths.config / "settings.toml", "rb") as f:
        s = tomllib.load(f)
    a, m, h = s.get("annual", {}), s.get("monthly", {}), s.get("http", {})
    return Settings(
        base_year=int(a.get("base_year", 0)),
        imf_weo_vintage=str(a.get("imf_weo_vintage", "")),
        override_stale_days=int(a["override_stale_days"]),
        change_alert_pp=float(m["change_alert_pp"]),
        user_agent=str(h["user_agent"]),
        scrape_min_delay=float(h["scrape_min_delay"]),
        scrape_max_delay=float(h["scrape_max_delay"]),
        api_delay=float(h["api_delay"]),
        max_retries=int(h["max_retries"]),
        timeout=float(h["timeout"]),
    )


def resolve_base_year(settings: Settings, today: date) -> int:
    return settings.base_year or today.year
