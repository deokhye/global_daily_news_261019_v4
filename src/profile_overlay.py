"""
profile_overlay.py
------------------
국가 기본 정보와 '국가 기본 프로필' 값을 한곳에서 조립한다. collector.py 와 build_site.py 가 함께 쓴다.

설정 파일 (모두 config/ 아래, 사람이 직접 관리)
  countries.csv        국가·권역·영문명·ISO3·수도·Trading Economics slug        (| 구분)
  country_codes.csv    ISO3 → ISO2·통화·Eurostat 코드                          (| 구분)
  sites.csv            한국타이어 거점 (파일 순서대로 표시)                     (| 구분)
  official_sources.csv 공식 기관·보고서 바로가기 (ALL=전체 국가, EU=EU 회원국) (| 구분)
  manual_profile.json  자동 수집 값이 없을 때 쓰는 보조 지표 (키: ISO3)

자동 수집 값
  output/country_profile.json  — python -m country_profile annual / monthly 결과

값 우선순위: 자동 수집 → 보조 지표(manual_profile.json) → 직전 수집값
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re

log = logging.getLogger("profile_overlay")

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
PROFILE_JSON_PATH = os.path.join(BASE_DIR, "output", "country_profile.json")
MANUAL_PROFILE_PATH = os.path.join(CONFIG_DIR, "manual_profile.json")

PROFILE_FIELDS = ("population", "gdp", "inflation", "unemployment", "min_wage")
AUTO_DEFAULT_SOURCE = {"population": "IMF", "gdp": "IMF", "inflation": "Trading Economics",
                       "unemployment": "Trading Economics", "min_wage": "정부 공식 발표"}
MANUAL_SOURCE = "Global HR 자체 조사"
NO_STATUTORY_TEXT = "법정 최저임금 없음"

# countries.csv 의 권역 이름 → 화면/데이터에서 쓰는 권역 키 (표시 순서 그대로)
REGION_KEYS = {"한국": "KR", "중국": "CN", "미주": "AMER", "유럽": "EU", "중남미": "LATAM", "아태": "APAC", "중아": "MEA"}
REGIONS = [{"key": "ALL", "label": "전체"}] + [{"key": v, "label": k} for k, v in REGION_KEYS.items()]
EU_MEMBERS_ISO3 = {"DEU", "HUN", "FRA", "ITA", "ESP", "NLD", "POL", "SWE", "CZE", "AUT", "ROU", "HRV"}


def is_bad_value(value) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in {"error", "err", "undefined", "null", "none", "nan", "n/a", ""}


def read_pipe_csv(path: str) -> list:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(f, delimiter="|") if any((v or "").strip() for v in row.values())]


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # noqa: BLE001
        log.warning(f"{path} 읽기 실패(무시): {e}")
        return default


# ---------------------------------------------------------------------------
# 국가 목록 (config/*.csv 단일 출처)
# ---------------------------------------------------------------------------
def _official_sources(iso3_list) -> dict:
    path = os.path.join(CONFIG_DIR, "official_sources.csv")
    if not os.path.exists(path):
        return {}
    own, common = {}, {}
    for r in read_pipe_csv(path):
        key, url = r.get("iso3", "").upper(), r.get("url", "")
        if not key or not r.get("name") or not re.match(r"^https?://", url):
            continue
        entry = {"name": r["name"], "url": url, "desc": r.get("desc", "")}
        (common if key in ("ALL", "EU") else own).setdefault(key, []).append(entry)
    out = {}
    for iso3 in iso3_list:
        lst = list(own.get(iso3, []))
        if iso3 in EU_MEMBERS_ISO3:
            lst += common.get("EU", [])
        lst += common.get("ALL", [])
        out[iso3] = lst
    return out


def load_countries() -> list:
    """config 의 csv 들을 합쳐 국가 목록을 만든다. 순서는 countries.csv 순서."""
    codes = {r["iso3"].upper(): r for r in read_pipe_csv(os.path.join(CONFIG_DIR, "country_codes.csv"))}
    sites: dict = {}
    for r in read_pipe_csv(os.path.join(CONFIG_DIR, "sites.csv")):
        if r.get("site_name"):
            sites.setdefault(r["iso3"].upper(), []).append(
                f"{r['site_name']} ({r['city']})" if r.get("city") else r["site_name"])
    rows = read_pipe_csv(os.path.join(CONFIG_DIR, "countries.csv"))
    official = _official_sources([r["iso3"].upper() for r in rows])
    countries = []
    for r in rows:
        iso3 = r["iso3"].upper()
        code_row = codes.get(iso3)
        if not code_row or not code_row.get("iso2"):
            log.warning(f"[config] {iso3}: country_codes.csv 에 ISO2 가 없어 건너뜁니다")
            continue
        iso2 = code_row["iso2"].upper()
        countries.append({
            "code": iso2, "iso3": iso3,
            "region": REGION_KEYS.get(r.get("region", ""), "ALL"),
            "name_kr": r.get("country_ko", iso2), "name_en": r.get("country_en", iso2),
            "currency": code_row.get("currency", ""), "flag": iso2.lower(),
            "capital": r.get("capital", "-"),
            "hubs": sites.get(iso3, []),
            "official_sources": official.get(iso3, []),
        })
    if not countries:
        raise ValueError("config/countries.csv 에서 국가를 하나도 읽지 못했습니다.")
    return countries


def load_manual() -> dict:
    return (_read_json(MANUAL_PROFILE_PATH, {}) or {}).get("countries", {})


def load_auto_profiles() -> tuple:
    """output/country_profile.json → ({ISO2: 항목}, 생성일 YYYY-MM-DD). 없으면 ({}, '')."""
    payload = _read_json(PROFILE_JSON_PATH, {}) or {}
    by_code = {str(c.get("iso2", "")).upper(): c for c in payload.get("countries", []) if isinstance(c, dict) and c.get("iso2")}
    return by_code, str(payload.get("generated_at", ""))[:10]


# ---------------------------------------------------------------------------
# 프로필 값 조립
# ---------------------------------------------------------------------------
def _mw_text(mw) -> str:
    return str(mw.get("display", "")) if isinstance(mw, dict) else str(mw or "")


def official_min_wage_source(mw, official: list) -> str:
    """보조 지표의 최저임금은 각국 정부 발표를 옮긴 값이므로 공식 출처로 표기한다."""
    if NO_STATUTORY_TEXT in _mw_text(mw):
        return "각국 노동법 기준"
    note = str(mw.get("note", "")) if isinstance(mw, dict) else ""
    if "Eurostat" in note:
        return "Eurostat"
    for o in official:
        if "최저임금" in (o.get("desc") or "") or "minimum" in (o.get("name") or "").lower():
            return f"{o['name']} (정부 공식 발표)"
    return "정부 공식 발표"


def build_profile(country: dict, manual_row: dict, auto_row: dict, auto_asof: str, cached_profile: dict = None) -> dict:
    cached_profile = cached_profile or {}
    values, sources, used_auto = {}, {}, False
    for k in PROFILE_FIELDS:
        v = (auto_row or {}).get(k)
        if not is_bad_value(v) and v != "-":
            used_auto = True
            src = str((((auto_row or {}).get("details") or {}).get(k) or {}).get("source") or AUTO_DEFAULT_SOURCE[k])
            src = src.split(" (")[0].replace("IMF WEO", "IMF")
            if k == "min_wage" and ("보정" in src or "HR 확인" in src):
                src = "정부 공식 발표"
        else:
            v = (manual_row or {}).get(k)
            if is_bad_value(v):
                v = cached_profile.get(k, "-")
            src = MANUAL_SOURCE
        values[k], sources[k] = v, src
    if NO_STATUTORY_TEXT in _mw_text(values["min_wage"]):
        sources["min_wage"] = "각국 노동법 기준"
    elif sources["min_wage"] == MANUAL_SOURCE:
        sources["min_wage"] = official_min_wage_source(values["min_wage"], country.get("official_sources") or [])
    return {
        "capital": country.get("capital") or cached_profile.get("capital", "-"),
        **values,
        "sources": sources,
        "stats_asof": auto_asof if used_auto else "",
    }


class ProfileContext:
    """한 번 읽은 설정·자동 수집 값을 재사용하기 위한 묶음."""

    def __init__(self):
        self.countries = load_countries()
        self.manual = load_manual()
        self.auto, self.auto_asof = load_auto_profiles()
        self.by_code = {c["code"]: c for c in self.countries}

    def profile_for(self, code: str, cached_profile: dict = None) -> dict:
        c = self.by_code.get(code)
        if not c:
            return cached_profile or {}
        return build_profile(c, self.manual.get(c["iso3"], {}), self.auto.get(code, {}), self.auto_asof, cached_profile)

    def apply(self, countries: list) -> int:
        """수집된 국가 데이터에 '현재' 설정·프로필 값을 덮어쓴다 (build_site 에서 사용).
        수도·거점·공식 기관·프로필 지표만 바꾸고 뉴스·환율은 건드리지 않는다."""
        n = 0
        for item in countries:
            c = self.by_code.get(str(item.get("code", "")).upper())
            if not c:
                continue
            item["profile"] = self.profile_for(c["code"], item.get("profile"))
            item["hubs"] = c["hubs"]
            item["official_sources"] = c["official_sources"]
            item.pop("offices", None)
            n += 1
        return n
