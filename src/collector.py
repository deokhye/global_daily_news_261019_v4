"""
collector.py  (v4)
------------------
Global Daily News — 한국타이어 39개 진출국 데이터 수집기.

입력
  data/countries_metadata.json : 국가 기본 메타데이터(국가명/통화/수도/인구/GDP/물가/실업률/
                                 최저임금/거점). 리스트 형식 또는
                                 {"as_of": "2026-10-01", "countries": [...]} 형식 모두 지원.
출력
  data/countries_data.json      : 오늘자 전체 데이터 (build_site.py 입력)
  data/translation_cache.json   : 영→한 번역 캐시 (재실행 시 번역 호출 최소화)
  docs/archive/YYYY-MM-DD.json  : 날짜별 아카이브 (180일 보존) + index.json

매 실행마다 새로 수집하는 것은 환율(yfinance)과 뉴스(Google News RSS) 두 가지다.

v4 변경 사항
  1) 번역: 국가별 병렬 스레드 안에서 개별 호출하던 방식을 폐기. 수집이 끝난 뒤 한 번에
     묶음(batch) 번역 + 재시도/백오프 + MyMemory 보조 번역기 + 영구 캐시를 적용했다.
     이전 실행에서 번역에 실패해 영문으로 남은 캐시 항목도 다음 실행 때 다시 번역한다.
  2) 환율: 전체 티커를 한 번에 일괄 다운로드(yf.download)해 호출 수를 줄이고,
     - USD/PAB는 USDKRW=X를 직접 사용 (기존: USDUSD=X 교차 시도로 1개월치만 남던 버그)
     - 교차 환산은 날짜 정규화 + inner join으로 날짜 어긋남에 의한 이상 등락률 제거
     - 직접 페어가 낡았거나 교차값과 5% 이상 어긋나면 교차 환산을 채택
     - 소액 통화 100단위 표기 확대(JPY/VND/IDR/HUF/KZT/CLP/COP) + 크기별 소수 자릿수
       (decimals 필드)로 저가 통화도 변동이 보이도록 함
     - 수집·캐시 모두 실패하면 가짜(합성) 곡선 대신 "수집 실패"로 정직하게 표시
  3) 뉴스 정확도: 국가별 별칭(국명/형용사/수도)이 제목에 실제로 들어간 기사만 채택하고,
     동명이인 지명(Panama City FL, chile pepper 등)은 제외어로 걸러낸다.
     비즈니스 동향은 따옴표로 묶은 국가명 + OR 키워드 쿼리로 다시 설계했다.
  4) 요약 정리: HTML 엔티티(&nbsp; 등) 디코딩, 제목과 같은 무의미한 요약 제거,
     링크+정규화 제목 기준 중복 제거, 발행일(published) 필드 추가.
  5) RSS 요청에 타임아웃/재시도/User-Agent 적용 (feedparser 단독 호출은 타임아웃이 없음).
  6) as_of_display에 실제 수집 시각을 기록 ("YYYY-MM-DD HH:MM KST 수집").
  7) 기존 아카이브의 &nbsp; 등 깨진 텍스트를 자동 정리.

환경변수
  DISABLE_TRANSLATION=1 : 번역 호출 생략(오프라인 테스트용)
  OFFLINE=1             : 네트워크 호출 전부 생략 → 캐시/폴백 경로만 실행(테스트용)
"""

import os
import re
import copy
import html
import json
import time
import random
import logging
import calendar
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
import pandas as pd
import pytz

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("collector")

KST = pytz.timezone("Asia/Seoul")
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA_DIR = os.path.join(BASE_DIR, "data")
METADATA_PATH = os.path.join(DATA_DIR, "countries_metadata.json")
CACHE_PATH = os.path.join(DATA_DIR, "countries_data.json")
TRANSLATION_CACHE_PATH = os.path.join(DATA_DIR, "translation_cache.json")
ARCHIVE_DIR = os.path.join(BASE_DIR, "docs", "archive")
ARCHIVE_INDEX_PATH = os.path.join(ARCHIVE_DIR, "index.json")
ARCHIVE_RETENTION_DAYS = 180
TRANSLATION_CACHE_TTL_DAYS = 45

OFFLINE = os.environ.get("OFFLINE", "").strip() in {"1", "true", "yes"}
DISABLE_TRANSLATION = OFFLINE or os.environ.get("DISABLE_TRANSLATION", "").strip() in {"1", "true", "yes"}

REQUEST_TIMEOUT = 12
# 단계별 시간 예산(초). 워크플로 수집 단계 제한(20분) 안에 반드시 끝나도록 한다.
# 예산을 넘기면 남은 작업은 건너뛰고 이전 수집값/원문을 사용한다(다음 실행 때 다시 시도).
RUN_STARTED = time.monotonic()
FX_BUDGET_SEC = int(os.environ.get("FX_BUDGET_SEC", "180"))
NEWS_BUDGET_SEC = int(os.environ.get("NEWS_BUDGET_SEC", "480"))
TRANSLATION_BUDGET_SEC = int(os.environ.get("TRANSLATION_BUDGET_SEC", "240"))
NEWS_MAX_CONSECUTIVE_ERRORS = 12   # Google News 오류가 이만큼 연속되면 차단으로 보고 뉴스 수집 중단
NEWS_WORKERS = 4          # Google News 동시 요청 수 (너무 높으면 503/429 차단)
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 1단위 원화 환산값이 작아 변동이 안 보이는 통화는 100단위로 표기 (국내 은행 고시 관행)
SMALL_UNIT_CURRENCIES = {"JPY": 100, "VND": 100, "IDR": 100, "HUF": 100, "KZT": 100, "CLP": 100, "COP": 100}
# 미국 달러에 1:1 고정된 통화 → USDKRW 사용
USD_PEGGED_1to1 = {"PAB"}
MIN_RELIABLE_TRADING_DAYS = 30

FALLBACK_TEXT = "현지 산업 및 정책 모니터링 중"
HANGUL_RE = re.compile(r"[가-힣]")

REGIONS = [
    {"key": "ALL", "label": "전체"},
    {"key": "KR", "label": "한국"},
    {"key": "CN", "label": "중국"},
    {"key": "AMER", "label": "미주"},
    {"key": "EU", "label": "유럽"},
    {"key": "LATAM", "label": "중남미"},
    {"key": "APAC", "label": "아태"},
    {"key": "MEA", "label": "중아"},
]

# ---------------------------------------------------------------------------
# 국가별 뉴스 필터 사전
#   aliases  : 제목에 하나라도 (대소문자 구분, 단어 경계) 들어가야 해당 국가 기사로 인정
#   exclude  : 검색 쿼리에 붙일 제외어 (Google News 문법)
#   reject   : 제목이 이 정규식에 걸리면 버림 (동명 지명/동음이의어)
# ---------------------------------------------------------------------------
COUNTRY_NEWS_FILTERS = {
    "US": {"aliases": ["United States", "U.S.", "US", "USA", "America", "American", "Americans", "Washington", "Fed", "White House"],
           "reject": r"\bLatin America\b|\bSouth America\b|\bCentral America\b"},
    "DE": {"aliases": ["Germany", "German", "Germans", "Berlin", "Bundestag", "Bundesbank"]},
    "CN": {"aliases": ["China", "Chinese", "Beijing", "Shanghai", "Xi Jinping"]},
    "HU": {"aliases": ["Hungary", "Hungarian", "Budapest", "Orban", "Orbán"]},
    "ID": {"aliases": ["Indonesia", "Indonesian", "Jakarta", "Prabowo"]},
    "NL": {"aliases": ["Netherlands", "Dutch", "Amsterdam", "The Hague", "Rotterdam"],
           "exclude": ['-"Love is Blind"'], "reject": r"Love is Blind|Pennsylvania Dutch"},
    "TW": {"aliases": ["Taiwan", "Taiwanese", "Taipei"]},
    "RU": {"aliases": ["Russia", "Russian", "Russians", "Moscow", "Kremlin", "Putin"]},
    "RO": {"aliases": ["Romania", "Romanian", "Bucharest"]},
    "MY": {"aliases": ["Malaysia", "Malaysian", "Kuala Lumpur", "Anwar"]},
    "MX": {"aliases": ["Mexico", "Mexican", "Mexicans", "Sheinbaum"],
           "exclude": ['-"New Mexico"'], "reject": r"\bNew Mexico\b|\bGulf of Mexico\b"},
    "MA": {"aliases": ["Morocco", "Moroccan", "Rabat", "Casablanca", "Tangier"]},
    "VN": {"aliases": ["Vietnam", "Vietnamese", "Viet Nam", "Hanoi", "Ho Chi Minh"],
           "reject": r"\bVietnam War\b|\bVietnam veteran"},
    "BR": {"aliases": ["Brazil", "Brazilian", "Brasilia", "Brasília", "Sao Paulo", "São Paulo", "Lula"]},
    "SA": {"aliases": ["Saudi", "Saudi Arabia", "Riyadh", "Jeddah", "Aramco"]},
    "RS": {"aliases": ["Serbia", "Serbian", "Belgrade", "Vucic", "Vučić"]},
    "SE": {"aliases": ["Sweden", "Swedish", "Stockholm"]},
    "ES": {"aliases": ["Spain", "Spanish", "Madrid", "Barcelona", "Sánchez", "Sanchez"],
           "reject": r"\bSpanish[- ](language|speaking|speakers)\b"},
    "SG": {"aliases": ["Singapore", "Singaporean", "Singaporeans", "MAS"]},
    "AE": {"aliases": ["UAE", "United Arab Emirates", "Emirati", "Emirates", "Dubai", "Abu Dhabi"],
           "reject": r"\bEmirates (Stadium|Cup)\b"},
    "GB": {"aliases": ["UK", "U.K.", "Britain", "British", "England", "English", "London", "Scotland", "Wales", "Starmer", "Bank of England"],
           "reject": r"\bNew England\b|\bEnglish Premier League\b|\bLondon, Ontario\b"},
    "AT": {"aliases": ["Austria", "Austrian", "Vienna"]},
    "UA": {"aliases": ["Ukraine", "Ukrainian", "Ukrainians", "Kyiv", "Zelensky", "Zelenskyy"]},
    "EG": {"aliases": ["Egypt", "Egyptian", "Cairo", "Suez"]},
    "IT": {"aliases": ["Italy", "Italian", "Italians", "Rome", "Milan", "Meloni"]},
    "JP": {"aliases": ["Japan", "Japanese", "Tokyo", "BOJ", "Bank of Japan"]},
    "CZ": {"aliases": ["Czech", "Czechia", "Czech Republic", "Prague"]},
    "CL": {"aliases": ["Chile", "Chilean", "Chileans", "Santiago"],
           "exclude": ["-pepper", "-peppers", "-recipe", "-chili"],
           "reject": r"(?i)\bchile[s]? (pepper|relleno|verde|colorado)|\bgreen chile\b|\bred chile\b|\bHatch\b|\bSantiago Bernabéu\b"},
    "KZ": {"aliases": ["Kazakhstan", "Kazakh", "Astana", "Almaty", "Tokayev"]},
    "CA": {"aliases": ["Canada", "Canadian", "Canadians", "Ottawa", "Ontario", "Quebec", "Carney"]},
    "CO": {"aliases": ["Colombia", "Colombian", "Colombians", "Bogota", "Bogotá", "Medellin", "Medellín", "Petro"],
           "reject": r"\bColumbia\b"},
    "HR": {"aliases": ["Croatia", "Croatian", "Zagreb"]},
    "TH": {"aliases": ["Thailand", "Thai", "Bangkok"],
           "reject": r"(?i)\bthai (food|restaurant|curry|massage)\b"},
    "TR": {"aliases": ["Turkey", "Türkiye", "Turkiye", "Turkish", "Ankara", "Istanbul", "Erdogan", "Erdoğan"],
           "exclude": ["-Thanksgiving"], "reject": r"(?i)\bthanksgiving\b|\bturkey (recipe|dinner|sandwich)\b"},
    "PA": {"aliases": ["Panama", "Panamanian", "Panama Canal"],
           "exclude": ['-"Panama City Beach"', "-Florida", "-Fla"],
           "reject": r"Panama City(,)? (FL|Fla|Florida)|Panama City Beach|Bay County|Panama Jack"},
    "PL": {"aliases": ["Poland", "Polish", "Warsaw", "Tusk"],
           "reject": r"(?i)\bnail polish\b"},
    "FR": {"aliases": ["France", "French", "Paris", "Macron", "Élysée"],
           "reject": r"(?i)\bfrench (fries|toast|bulldog|press)\b|\bParis Hilton\b|\bParis, (Texas|Tennessee|TN|TX)\b"},
    "AU": {"aliases": ["Australia", "Australian", "Australians", "Canberra", "Sydney", "Melbourne", "Albanese", "RBA"]},
}

# 비즈니스 동향 2대 카테고리
#   kr_* : 한국어 검색(1순위)   en_* : 영문 검색(2순위, 번역 대상)
#   KR(한국) 자체는 국내 전용 쿼리(home_query)를 사용
INDUSTRY_TOPICS = [
    {
        "category": "AUTO MARKET",
        "tag": "완성차·타이어 시장",
        "tag_class": "bg-rose-100 text-rose-700",
        "kr_keywords": "(타이어 OR 완성차 OR 자동차 판매 OR 자동차 시장 OR 전기차)",
        "en_keywords": "(tire OR tyre OR automotive OR \"car sales\" OR automaker OR \"auto industry\" OR \"EV sales\")",
        "home_query": "(타이어 OR 완성차 OR 자동차 판매 OR 자동차 수출 OR 전기차 시장)",
        "max_items": 4,
    },
    {
        "category": "HR & LABOR",
        "tag": "노동법·HR·임금",
        "tag_class": "bg-indigo-100 text-indigo-700",
        "kr_keywords": "(노동 OR 임금 OR 고용 OR 노조 OR 파업 OR 최저임금)",
        "en_keywords": "(\"labor law\" OR \"labour law\" OR \"minimum wage\" OR \"employment law\" OR union OR strike OR wages OR layoffs)",
        "home_query": "(노동법 OR 최저임금 OR 노조 OR 파업 OR 고용노동부 OR 임금협상 OR 근로시간)",
        "max_items": 4,
    },
]


# ---------------------------------------------------------------------------
# 0. 메타데이터 로드
# ---------------------------------------------------------------------------
def _is_bad_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in {"error", "err", "undefined", "null", "none", "nan", "n/a", ""}:
        return True
    return False


def _read_metadata_payload():
    if not os.path.exists(METADATA_PATH):
        raise FileNotFoundError(f"{METADATA_PATH} 파일이 없습니다. 39개국 메타데이터 파일을 먼저 준비하세요.")
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        raise ValueError(f"{METADATA_PATH} 파일이 비어 있습니다.")
    return json.loads(raw)


def load_countries_metadata(payload) -> list:
    rows = payload.get("countries") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{METADATA_PATH} 안에 유효한 countries 리스트가 없습니다.")

    countries = []
    for row in rows:
        if not isinstance(row, dict) or _is_bad_value(row.get("code")):
            log.warning(f"[metadata] 잘못된 국가 항목을 건너뜁니다: {row}")
            continue
        code = str(row["code"]).strip().upper()
        offices = row.get("offices") if isinstance(row.get("offices"), list) else []
        countries.append({
            "code": code,
            "region": row.get("region", "ALL"),
            "name_kr": row.get("name_ko", code),
            "name_en": row.get("name_en", code),
            "currency": row.get("currency", "USD"),
            "flag": code.lower(),
            "offices": offices,
            "hubs": offices,
            "meta_profile": {
                "capital": row.get("capital", "-"),
                "population": row.get("population", "-"),
                "gdp": row.get("gdp", "-"),
                "inflation": row.get("inflation", "-"),
                "unemployment": row.get("unemployment", "-"),
                "min_wage": row.get("min_wage", "-"),
                "curr_unit": row.get("curr_unit", ""),
                "as_of": row.get("as_of", ""),
            },
        })
    if not countries:
        raise ValueError(f"{METADATA_PATH} 에서 유효한 국가를 하나도 읽지 못했습니다.")
    return countries


def _metadata_asof(payload) -> str:
    """메타데이터 기준일. dict 형식일 때 as_of / generated_at 키를 읽고, 없으면 빈 문자열
    (프론트엔드는 빈 값이면 '(… 기준)' 문구 자체를 숨긴다)."""
    if isinstance(payload, dict):
        for k in ("as_of", "generated_at", "updated_at"):
            v = payload.get(k)
            if not _is_bad_value(v):
                return str(v)
    return ""


_METADATA_PAYLOAD = _read_metadata_payload()
COUNTRIES = load_countries_metadata(_METADATA_PAYLOAD)
METADATA_ASOF = _metadata_asof(_METADATA_PAYLOAD)


# ---------------------------------------------------------------------------
# 공용 텍스트 유틸
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text) -> str:
    """HTML 태그 제거 + 엔티티(&nbsp; &amp; &#39; 등) 디코딩 + 공백 정리."""
    if not isinstance(text, str) or not text:
        return ""
    t = html.unescape(text)
    t = _TAG_RE.sub(" ", t)
    t = html.unescape(t)  # 이중 인코딩(&amp;nbsp;) 대비
    t = t.replace("\xa0", " ")
    return " ".join(t.split())


def _shorten(text: str, max_len: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[:max_len].rstrip() + "…"


def _norm_title(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower())


def _yahoo_finance_url(currency: str) -> str:
    if not currency or currency == "KRW":
        return ""
    if currency in USD_PEGGED_1to1:
        return "https://finance.yahoo.com/quote/USDKRW=X"
    return f"https://finance.yahoo.com/quote/{currency}KRW=X"


# ---------------------------------------------------------------------------
# 1. 국가 프로필
# ---------------------------------------------------------------------------
PROFILE_JSON_PATH = os.path.join(BASE_DIR, "output", "country_profile.json")
PROFILE_FIELDS = ("population", "gdp", "inflation", "unemployment", "min_wage")


def load_auto_profiles() -> tuple[dict, str]:
    """country_profile 파이프라인(python -m country_profile)이 만든 output/country_profile.json 을
    읽어 {ISO2: 항목} 으로 돌려준다. 파일이 없으면 빈 dict — 기존 메타데이터만 사용한다."""
    try:
        with open(PROFILE_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        by_code = {str(c.get("iso2", "")).upper(): c for c in payload.get("countries", []) if c.get("iso2")}
        return by_code, str(payload.get("generated_at", ""))[:10]
    except FileNotFoundError:
        return {}, ""
    except Exception as e:  # noqa: BLE001
        log.warning(f"자동 수집 프로필 로드 실패(메타데이터만 사용): {e}")
        return {}, ""


AUTO_PROFILES, AUTO_PROFILES_ASOF = load_auto_profiles()

CONFIG_DIR = os.path.join(BASE_DIR, "config")


def _read_pipe_csv(path: str) -> list:
    import csv
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(f, delimiter="|")]


def load_fixed_config() -> dict:
    """config/countries.csv(수도)·sites.csv(거점)·country_codes.csv(ISO3→ISO2) 를 읽어
    {ISO2: {"capital": str, "sites": [..]}} 로 돌려준다. 수도·거점은 수집 대상이 아닌 고정값이라
    국가 프로필 파이프라인 실행 여부와 관계없이 이 파일 값을 바로 화면에 쓴다."""
    try:
        iso2_of = {r["iso3"].upper(): r["iso2"].upper()
                   for r in _read_pipe_csv(os.path.join(CONFIG_DIR, "country_codes.csv")) if r.get("iso2")}
        out = {}
        for r in _read_pipe_csv(os.path.join(CONFIG_DIR, "countries.csv")):
            iso2 = iso2_of.get(r.get("iso3", "").upper())
            if iso2:
                out[iso2] = {"capital": r.get("capital", ""), "sites": []}
        for r in _read_pipe_csv(os.path.join(CONFIG_DIR, "sites.csv")):
            iso2 = iso2_of.get(r.get("iso3", "").upper())
            if iso2 in out and r.get("site_name"):
                out[iso2]["sites"].append(f"{r['site_name']} ({r['city']})" if r.get("city") else r["site_name"])
        _attach_official_sources(out, iso2_of)
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning(f"config/ 고정 데이터(수도·거점) 로드 실패 — 메타데이터 값 사용: {e}")
        return {}


EU_MEMBERS_ISO3 = {"DEU", "HUN", "FRA", "ITA", "ESP", "NLD", "POL", "SWE", "CZE", "AUT", "ROU", "HRV"}


def _attach_official_sources(out: dict, iso2_of: dict) -> None:
    """config/official_sources.csv (iso3|name|url|desc). iso3 칸에 ALL 이면 전체 국가, EU 면 EU 회원국에 붙는다.
    국가별 행이 먼저, 공통 행(EU → ALL)이 뒤에 오도록 정렬한다. http(s) 주소만 허용."""
    path = os.path.join(CONFIG_DIR, "official_sources.csv")
    if not os.path.exists(path):
        return
    own, common = {}, {}
    for r in _read_pipe_csv(path):
        key, url = r.get("iso3", "").upper(), r.get("url", "")
        if not key or not r.get("name") or not re.match(r"^https?://", url):
            continue
        entry = {"name": r["name"], "url": url, "desc": r.get("desc", "")}
        if key in ("ALL", "EU"):
            common.setdefault(key, []).append(entry)
        else:
            own.setdefault(key, []).append(entry)
    for iso3, iso2 in iso2_of.items():
        if iso2 not in out:
            continue
        lst = list(own.get(iso3, []))
        if iso3 in EU_MEMBERS_ISO3:
            lst += common.get("EU", [])
        lst += common.get("ALL", [])
        out[iso2]["official"] = lst


FIXED_CONFIG = load_fixed_config()


def _official_min_wage_source(mw, official: list) -> str:
    """메타데이터 최저임금은 각국 정부 공식 발표를 옮겨 적은 값이므로 '자체 조사'가 아니라 공식 출처로 표기한다.
    비고에 Eurostat 이 적혀 있으면 Eurostat, 아니면 config/official_sources.csv 에서 최저임금 담당 기관
    (desc 에 '최저임금' 포함)을 찾아 쓰고, 없으면 '정부 공식 발표'."""
    note = mw.get("note", "") if isinstance(mw, dict) else str(mw or "")
    if "Eurostat" in note:
        return "Eurostat"
    for o in official:
        if "최저임금" in (o.get("desc") or "") or "minimum" in (o.get("name") or "").lower():
            return f"{o['name']} (정부 공식 발표)"
    return "정부 공식 발표"


def build_profile(country: dict, cached: dict) -> dict:
    """프로필 카드 값. 총인구·GDP·인플레이션·실업률·최저임금은 자동 수집 결과
    (output/country_profile.json)를 우선 쓰고, 값이 없으면('-') 기존 메타데이터로 채운다.
    수도·거점은 config/countries.csv·sites.csv 값을 우선 쓴다(없으면 메타데이터)."""
    meta = country.get("meta_profile", {})
    cached_profile = (cached or {}).get("profile", {})
    auto = AUTO_PROFILES.get(country.get("code", "").upper(), {})
    fixed = FIXED_CONFIG.get(country.get("code", "").upper(), {})

    def pick(key):
        if key == "capital" and fixed.get("capital"):
            return fixed["capital"]
        v = auto.get(key) if key in PROFILE_FIELDS else None
        if not _is_bad_value(v) and v != "-":
            return v
        v = meta.get(key)
        if not _is_bad_value(v):
            return v
        return cached_profile.get(key, "-")

    used_auto = any(not _is_bad_value(auto.get(k)) and auto.get(k) != "-" for k in PROFILE_FIELDS)
    default_src = {"population": "IMF", "gdp": "IMF", "inflation": "Trading Economics",
                   "unemployment": "Trading Economics", "min_wage": "정부 공식 발표"}
    sources = {}
    for k in PROFILE_FIELDS:
        v = auto.get(k)
        if not _is_bad_value(v) and v != "-":
            src = str(((auto.get("details") or {}).get(k) or {}).get("source") or default_src[k])
            sources[k] = src.split(" (")[0].replace("IMF WEO", "IMF")
        else:
            sources[k] = "Global HR 자체 조사"
    if sources["min_wage"] == "Global HR 자체 조사":
        sources["min_wage"] = _official_min_wage_source(meta.get("min_wage"), fixed.get("official") or [])
    # 법정 최저임금이 없는 나라는 '각국 노동법 기준', 보정 파일 값은 '정부 공식 발표'로 표기 (내부 용어 노출 금지)
    _mw = pick("min_wage")
    _mw_text = _mw.get("display", "") if isinstance(_mw, dict) else str(_mw or "")
    if "법정 최저임금 없음" in _mw_text:
        sources["min_wage"] = "각국 노동법 기준"
    elif "보정" in sources["min_wage"] or "HR 확인" in sources["min_wage"]:
        sources["min_wage"] = "정부 공식 발표"
    return {
        "capital": pick("capital"),
        "population": pick("population"),
        "gdp": pick("gdp"),
        "inflation": pick("inflation"),
        "unemployment": pick("unemployment"),
        "min_wage": pick("min_wage"),
        "min_wage_note": "",
        "sites": auto.get("sites") or [],
        "stats_source": ("IMF WEO · Trading Economics · Eurostat/ILOSTAT · 정부 공식 발표" if used_auto
                         else "Hankook Tire Global HR 자체 조사"),
        "stats_asof": (AUTO_PROFILES_ASOF if used_auto else (meta.get("as_of") or METADATA_ASOF or "")),
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# 2. 환율
# ---------------------------------------------------------------------------
def decimals_for(value) -> int:
    """표시값 크기에 따른 소수 자릿수: ≥100 → 1자리, ≥10 → 2자리, 그 미만 → 3자리."""
    try:
        v = abs(float(value))
    except Exception:
        return 1
    if v >= 100:
        return 1
    if v >= 10:
        return 2
    return 3


def _round_to(value, decimals: int):
    try:
        return round(float(value), decimals)
    except Exception:
        return None


def _normalize_series(s) -> pd.Series:
    """yfinance 결과를 tz-naive '날짜' 인덱스, 양수 값, 중복 제거, 오름차순으로 정규화."""
    try:
        if s is None:
            return pd.Series(dtype="float64")
        s = pd.Series(s).dropna()
        if s.empty:
            return pd.Series(dtype="float64")
        idx = pd.to_datetime(s.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        s.index = idx.normalize()
        s = s[s > 0]
        s = s[~s.index.duplicated(keep="last")].sort_index()
        return s.astype("float64")
    except Exception as e:
        log.warning(f"시계열 정규화 실패: {e}")
        return pd.Series(dtype="float64")


def _yf_daily_close(ticker: str, days: int = 400) -> pd.Series:
    if OFFLINE:
        return pd.Series(dtype="float64")
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=f"{days}d", interval="1d", timeout=REQUEST_TIMEOUT)
        if hist is not None and not hist.empty:
            return _normalize_series(hist["Close"])
    except Exception as e:
        log.warning(f"[yfinance] {ticker} 조회 실패: {e}")
    return pd.Series(dtype="float64")


def _needed_tickers(countries: list) -> list:
    tickers = {"USDKRW=X"}
    for c in countries:
        cur = c.get("currency")
        if not cur or cur in ("KRW", "USD") or cur in USD_PEGGED_1to1:
            continue
        tickers.add(f"{cur}KRW=X")
        tickers.add(f"{cur}USD=X")
    return sorted(tickers)


def prefetch_fx_series(countries: list) -> dict:
    """필요한 모든 FX 티커를 yf.download 한 번으로 일괄 수집하고, 빠진 것만 개별 재시도."""
    tickers = _needed_tickers(countries)
    out = {}
    if OFFLINE:
        return out
    try:
        import yfinance as yf
        df = yf.download(tickers, period="400d", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
        if df is not None and not df.empty:
            for t in tickers:
                try:
                    if isinstance(df.columns, pd.MultiIndex):
                        if t in df.columns.get_level_values(0):
                            s = df[t]["Close"]
                        else:
                            s = df["Close"][t]
                    else:
                        s = df["Close"]
                    out[t] = _normalize_series(s)
                except Exception:
                    continue
    except Exception as e:
        log.warning(f"[yfinance] 일괄 다운로드 실패 → 개별 조회로 전환: {e}")

    missing = [t for t in tickers if len(out.get(t, [])) < MIN_RELIABLE_TRADING_DAYS]
    if missing:
        log.info(f"[yfinance] 일괄 수집 부족 티커 {len(missing)}개 개별 재시도: {missing}")
    for t in missing:
        if time.monotonic() - RUN_STARTED > FX_BUDGET_SEC:
            log.warning(f"[yfinance] 환율 시간 예산({FX_BUDGET_SEC}초) 초과 — 남은 개별 재시도 생략")
            break
        s = _yf_daily_close(t)
        if len(s) > len(out.get(t, [])):
            out[t] = s
        time.sleep(0.3)
    return out


def _cross_series(cur_usd: pd.Series, usd_krw: pd.Series) -> pd.Series:
    """CUR/KRW = CUR/USD × USD/KRW. 같은 거래일끼리만 곱한다(inner join)."""
    if cur_usd is None or usd_krw is None or cur_usd.empty or usd_krw.empty:
        return pd.Series(dtype="float64")
    try:
        df = pd.concat([cur_usd.rename("a"), usd_krw.rename("b")], axis=1, join="inner").dropna()
        return (df["a"] * df["b"]).astype("float64")
    except Exception as e:
        log.warning(f"교차 환산 실패: {e}")
        return pd.Series(dtype="float64")


def select_fx_series(currency: str, fx_series: dict):
    """(daily_series, source_label) 반환. 적절한 데이터가 없으면 빈 시리즈."""
    empty = pd.Series(dtype="float64")
    usd_krw = fx_series.get("USDKRW=X", empty)

    if currency == "USD" or currency in USD_PEGGED_1to1:
        label = "Yahoo Finance" if currency == "USD" else "Yahoo Finance (USD 1:1 고정환율)"
        return usd_krw, label

    direct = fx_series.get(f"{currency}KRW=X", empty)
    cross = _cross_series(fx_series.get(f"{currency}USD=X", empty), usd_krw)
    d_ok = len(direct) >= MIN_RELIABLE_TRADING_DAYS
    x_ok = len(cross) >= MIN_RELIABLE_TRADING_DAYS

    if d_ok and x_ok:
        deviation = abs(float(direct.iloc[-1]) / float(cross.iloc[-1]) - 1)
        stale_days = (cross.index[-1] - direct.index[-1]).days
        if deviation > 0.05 or stale_days > 5:
            log.info(f"[{currency}] 직접 페어 이상(편차 {deviation:.1%}, 지연 {stale_days}일) → 교차 환산 채택")
            return cross, "Yahoo Finance (USD 교차 환산)"
        return direct, "Yahoo Finance"
    if d_ok:
        return direct, "Yahoo Finance"
    if x_ok:
        return cross, "Yahoo Finance (USD 교차 환산)"
    best = direct if len(direct) >= len(cross) else cross
    label = "Yahoo Finance" if best is direct else "Yahoo Finance (USD 교차 환산)"
    return best, label


def _monthly_from_daily(daily: pd.Series, months: int = 12):
    """월말(해당 월 마지막 거래일) 종가로 월별 시리즈 생성. 반올림하지 않는다."""
    if daily is None or daily.empty:
        return [], []
    try:
        grouped = daily.groupby(daily.index.to_period("M")).last().tail(months)
        labels = [f"{p.year}년 {p.month}월" for p in grouped.index]
        values = [float(v) for v in grouped]
        return labels, values
    except Exception as e:
        log.warning(f"월별 리샘플 실패: {e}")
        return [], []


def _failed_fx(currency: str, unit_base: int) -> dict:
    return {
        "is_base": False, "unit_base": unit_base, "decimals": 1,
        "current_rate": None, "change_pct": None,
        "history_labels": [], "history_values": [],
        "source": "수집 실패", "url": _yahoo_finance_url(currency), "stale": True, "asof": "",
    }


def _rescale_cached_fx(cached_fx: dict, unit_base: int) -> dict:
    """캐시의 unit_base가 현재 설정과 다르면 값들을 새 단위로 환산."""
    fx = copy.deepcopy(cached_fx)
    old = fx.get("unit_base") or 1
    try:
        old = int(old)
    except Exception:
        old = 1
    if old != unit_base and old > 0:
        factor = unit_base / old
        if isinstance(fx.get("current_rate"), (int, float)):
            fx["current_rate"] = fx["current_rate"] * factor
        fx["history_values"] = [v * factor for v in fx.get("history_values", []) if isinstance(v, (int, float))]
        fx["unit_base"] = unit_base
    dec = decimals_for(fx.get("current_rate") or 0)
    fx["decimals"] = dec
    if isinstance(fx.get("current_rate"), (int, float)):
        fx["current_rate"] = _round_to(fx["current_rate"], dec)
    fx["history_values"] = [_round_to(v, dec) for v in fx.get("history_values", [])]
    return fx


def get_exchange_rate(country: dict, cached: dict, fx_series: dict) -> dict:
    currency = country["currency"]
    unit_base = SMALL_UNIT_CURRENCIES.get(currency, 1)
    yahoo_url = _yahoo_finance_url(currency)

    if currency == "KRW":
        return {"is_base": True, "unit_base": 1, "decimals": 1, "current_rate": 1.0, "change_pct": 0.0,
                "history_labels": [], "history_values": [], "source": "기준통화", "url": ""}

    try:
        daily, source = select_fx_series(currency, fx_series)
        if daily.empty or len(daily) < 2:
            raise ValueError(f"일별 환율 데이터 부족({len(daily)}일)")

        current_raw = float(daily.iloc[-1]) * unit_base
        prev_raw = float(daily.iloc[-2]) * unit_base
        change_pct = round((current_raw - prev_raw) / prev_raw * 100, 2) if prev_raw else 0.0
        if abs(change_pct) > 10:
            log.warning(f"[{country['code']}] 전일 대비 {change_pct}% — 이상치 가능성, 원천 데이터 확인 필요")

        labels, values_raw = _monthly_from_daily(daily, months=12)
        if not labels:
            raise ValueError("월별 히스토리 생성 실패")

        dec = decimals_for(current_raw)
        return {
            "is_base": False,
            "unit_base": unit_base,
            "decimals": dec,
            "current_rate": _round_to(current_raw, dec),
            "change_pct": change_pct,
            "history_labels": labels,
            "history_values": [_round_to(v * unit_base, dec) for v in values_raw],
            "source": source,
            "url": yahoo_url,
            "asof": daily.index[-1].strftime("%Y-%m-%d"),
            "stale": False,
        }
    except Exception as e:
        log.warning(f"[{country['code']}] 환율 수집 실패 → 이전 수집값 사용: {e}")
        cached_fx = (cached or {}).get("exchange_rate", {})
        if isinstance(cached_fx, dict) and cached_fx.get("history_values") and not cached_fx.get("is_base"):
            fx = _rescale_cached_fx(cached_fx, unit_base)
            fx["url"] = yahoo_url
            fx["is_base"] = False
            fx["stale"] = True
            src = str(fx.get("source") or "Yahoo Finance")
            if "이전 수집값" not in src:
                fx["source"] = f"{src} · 이전 수집값"
            return fx
        return _failed_fx(currency, unit_base)


# ---------------------------------------------------------------------------
# 3. Google News RSS
# ---------------------------------------------------------------------------
def _q(s: str) -> str:
    return requests.utils.quote(s, safe="")


def _google_news_url_ko(query: str) -> str:
    return f"https://news.google.com/rss/search?q={_q(query)}&hl=ko&gl=KR&ceid=KR:ko"


def _google_news_top_url_ko() -> str:
    return "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"


def _google_news_url_en(query: str) -> str:
    return f"https://news.google.com/rss/search?q={_q(query)}&hl=en-US&gl=US&ceid=US:en"


def _google_news_search_link(query: str, lang: str = "en") -> str:
    if lang == "ko":
        return f"https://news.google.com/search?q={_q(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/search?q={_q(query)}&hl=en-US&gl=US&ceid=US:en"


def _published_date(entry) -> str:
    try:
        tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if not tm:
            return ""
        dt = datetime.fromtimestamp(calendar.timegm(tm), tz=pytz.utc).astimezone(KST)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def strip_source_suffix(title: str, source: str = ""):
    """Google News 제목 끝의 ' - 언론사' 를 제거. 언론사명이 두 번 붙거나(… - 머니투데이 - 머니투데이)
    표기가 조금 다른 경우도 정리한다. 반환: (정리된 제목, 언론사)."""
    title = (title or "").strip()
    source = (source or "").strip()
    for _ in range(3):
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].rstrip()
            continue
        if " - " in title:
            head, tail = title.rsplit(" - ", 1)
            norm = lambda x: re.sub(r"[\W_]+", "", x.lower())
            if not source and len(tail) <= 30:
                title, source = head.rstrip(), tail.strip()
                continue
            if source and tail and (norm(tail) in norm(source) or norm(source) in norm(tail)):
                title = head.rstrip()
                continue
        break
    return title, source


# 연예·가십 기사 제외 (모든 뉴스 분류 공통). 제목에 걸리면 수집하지 않는다.
GOSSIP_RE = re.compile(
    r"연예|아이돌|걸그룹|보이그룹|가수|(?<![가-힣])배우(?![다고며는기])|드라마|예능|열애|결혼식|이혼|컴백|앨범|뮤직비디오|"
    r"데뷔|소속사|방송인|유튜버|인플루언서|셀럽|스캔들|팬미팅|콘서트|오디션|"
    r"(?i:\bcelebrit|\bsinger\b|\bactor\b|\bactress\b|\bk-?pop\b|\bidol\b|girl group|boy band|\balbum\b|"
    r"\bconcert\b|reality (show|tv)|\bdating\b|\bdivorce|\bwedding\b|box office|\brapper\b|\binfluencer|"
    r"\bred carpet|\bpaparazzi|\bgossip|\bLove Is Blind\b|\bBachelor\b|\bKardashian)"
)


def is_gossip(title: str) -> bool:
    return bool(GOSSIP_RE.search(title or ""))


def _parse_entry(entry) -> dict:
    title_raw = clean_text(getattr(entry, "title", "") or "")
    link = (getattr(entry, "link", "") or "").strip()
    if not title_raw or not link:
        raise ValueError("title 또는 link 누락")

    source = ""
    try:
        src = getattr(entry, "source", None)
        if src is not None:
            source = clean_text(getattr(src, "title", "") or (src.get("title") if isinstance(src, dict) else ""))
    except Exception:
        source = ""

    title = title_raw
    # Google News 제목은 "기사 제목 - 언론사" 형식. 언론사 접미사만 제거한다.
    title, source = strip_source_suffix(title, source)

    summary = clean_text(getattr(entry, "summary", "") or "")
    # Google News RSS 요약은 대부분 "제목 + 언론사" 반복 → 의미 없으면 버린다.
    if summary:
        ns, nt = _norm_title(summary), _norm_title(title)
        if not ns or ns.startswith(nt[: max(10, len(nt) // 2)]) or nt.startswith(ns) or ns == _norm_title(title + source):
            summary = ""

    return {
        "title": title.strip(),
        "source": (source or "Google News").strip(),
        "link": link,
        "summary": _shorten(summary, 140) if summary else "",
        "published": _published_date(entry),
    }


_NEWS_ERRORS = 0
_NEWS_STOPPED = False


def _fetch_feed(url: str, limit: int) -> list:
    """RSS 요청(타임아웃+재시도) → 파싱 → 유효 기사 최대 limit개."""
    global _NEWS_ERRORS, _NEWS_STOPPED
    if OFFLINE or _NEWS_STOPPED:
        return []
    if time.monotonic() - RUN_STARTED > FX_BUDGET_SEC + NEWS_BUDGET_SEC:
        if not _NEWS_STOPPED:
            log.warning(f"뉴스 시간 예산({NEWS_BUDGET_SEC}초) 초과 — 남은 국가는 이전 수집 기사를 사용합니다")
        _NEWS_STOPPED = True
        return []
    content = None
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": HTTP_USER_AGENT})
            if r.status_code == 200:
                content = r.content
                _NEWS_ERRORS = 0
                break
            log.warning(f"RSS HTTP {r.status_code} ({attempt + 1}/2): {url[:120]}")
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            log.warning(f"RSS 요청 실패({attempt + 1}/2): {e}")
        if attempt == 0:
            time.sleep(1.5 + random.random())
    if content is None:
        _NEWS_ERRORS += 1
        if _NEWS_ERRORS >= NEWS_MAX_CONSECUTIVE_ERRORS and not _NEWS_STOPPED:
            _NEWS_STOPPED = True
            log.warning(f"Google News 오류 {_NEWS_ERRORS}회 연속 — 차단으로 보고 뉴스 수집 중단(이전 기사 사용)")
        return []

    try:
        feed = feedparser.parse(content)
        entries = getattr(feed, "entries", []) or []
    except Exception as e:
        log.warning(f"피드 파싱 실패: {e}")
        return []

    items = []
    for entry in entries:
        try:
            items.append(_parse_entry(entry))
        except Exception:
            continue
        if len(items) >= limit:
            break
    return items


def _dedup_keys(item: dict) -> list:
    keys = []
    link = (item.get("link") or "").strip().lower()
    if link:
        keys.append("L:" + link)
    nt = _norm_title(item.get("original_title") or item.get("title"))
    if nt:
        keys.append("T:" + nt)
    return keys


def _is_seen(item: dict, seen: set) -> bool:
    return any(k in seen for k in _dedup_keys(item))


def _mark_seen(item: dict, seen: set) -> None:
    for k in _dedup_keys(item):
        seen.add(k)


# ---------------------------------------------------------------------------
# 4. 국가 관련성 필터
# ---------------------------------------------------------------------------
def _alias_regex(code: str, name_en: str):
    spec = COUNTRY_NEWS_FILTERS.get(code, {})
    aliases = list(dict.fromkeys(spec.get("aliases", []) + [name_en]))
    parts = []
    for a in aliases:
        esc = re.escape(a)
        # 단어 경계: 앞뒤가 영숫자가 아니어야 함 (U.S. 처럼 마침표로 끝나는 별칭 대응)
        parts.append(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])")
    return re.compile("|".join(parts)) if parts else None


def _reject_regex(code: str):
    pat = COUNTRY_NEWS_FILTERS.get(code, {}).get("reject")
    return re.compile(pat) if pat else None


def make_relevance_checker(country: dict):
    """(title, lang) → bool. 영문 기사는 별칭이 제목에, 한국어 기사는 한글 국가명이 제목에 있어야 통과."""
    code = country.get("code", "")
    name_kr = country.get("name_kr", "")
    alias_re = _alias_regex(code, country.get("name_en", ""))
    reject_re = _reject_regex(code)

    def check(title: str, lang: str) -> bool:
        if not title:
            return False
        if reject_re and reject_re.search(title):
            return False
        if lang == "ko":
            return bool(name_kr) and name_kr in title
        return bool(alias_re and alias_re.search(title))

    return check


def _exclusions(code: str) -> str:
    ex = COUNTRY_NEWS_FILTERS.get(code, {}).get("exclude", [])
    return (" " + " ".join(ex)) if ex else ""


# ---------------------------------------------------------------------------
# 5. 현지 주요 뉴스
# ---------------------------------------------------------------------------
def _fallback_headline_item(country: dict) -> dict:
    name_kr = country.get("name_kr", "")
    return {
        "title": FALLBACK_TEXT,
        "source": "Google News 검색",
        "link": _google_news_search_link(country.get("name_en", "")),
        "summary": f"{name_kr} 관련 최신 기사를 찾지 못해 Google 뉴스 검색 결과로 연결됩니다." if name_kr
                   else "관련 최신 기사를 찾지 못해 Google 뉴스 검색 결과로 연결됩니다.",
        "lang": "ko",
        "translated": False,
        "original_title": "",
        "published": "",
        "is_fallback": True,
    }


def sanitize_item(item: dict) -> dict:
    """캐시/아카이브에서 온 항목의 텍스트를 정리(&nbsp; 등)하고 필수 키를 보강."""
    if not isinstance(item, dict):
        return item
    it = dict(item)
    for k in ("title", "summary", "desc", "source", "original_title"):
        if isinstance(it.get(k), str):
            it[k] = clean_text(it[k])
    if it.get("title") and it.get("source"):
        it["title"], _ = strip_source_suffix(it["title"], it["source"])
    heads = [_norm_title(it.get("title"))[:20], _norm_title(it.get("original_title"))[:20]]
    heads = [h for h in heads if len(h) >= 6]
    for k in ("summary", "desc"):
        body = _norm_title(it.get(k))
        if body and any(body.startswith(h) for h in heads):
            it[k] = ""
    it.setdefault("lang", "ko")
    it.setdefault("translated", False)
    it.setdefault("original_title", "")
    it.setdefault("published", "")
    return it


def _cached_list(cached_list, check=None) -> list:
    """이전 실행에서 수집한 기사 목록을 정리해 재사용. check가 있으면 관련성 필터를 다시 적용해
    과거 버전이 수집한 무관 기사(예: Panama City Beach)를 걸러낸다."""
    if not isinstance(cached_list, list):
        return []
    out = []
    for x in cached_list:
        if not isinstance(x, dict) or x.get("is_fallback") or x.get("title") == FALLBACK_TEXT:
            continue
        it = sanitize_item(x)
        if is_gossip(it.get("original_title") or it.get("title", "")) or is_gossip(it.get("title", "")):
            continue
        if check:
            lang = it.get("lang", "ko")
            title = (it.get("original_title") or it.get("title", "")) if lang == "en" else it.get("title", "")
            if not check(title, lang):
                continue
        out.append(it)
    return out


def _pick(candidates: list, lang: str, limit: int, seen: set, check=None, already: list = None) -> list:
    picked = list(already or [])
    for it in candidates:
        if len(picked) >= limit:
            break
        if check and not check(it.get("title", ""), lang):
            continue
        if is_gossip(it.get("title", "")):
            continue
        if _is_seen(it, seen):
            continue
        it["lang"] = lang
        it["translated"] = False
        it["original_title"] = it["title"] if lang == "en" else ""
        picked.append(it)
        _mark_seen(it, seen)
    return picked


def get_headlines(country: dict, cached: dict, seen: set) -> list:
    limit = 3
    code = country.get("code")
    try:
        if code == "KR":
            picked = _pick(_fetch_feed(_google_news_top_url_ko(), limit * 6), "ko", limit, seen)
        else:
            check = make_relevance_checker(country)
            name_en = country["name_en"]
            ex = _exclusions(code)
            picked = []
            queries = [
                f'"{name_en}" (economy OR government OR business OR policy OR trade OR market){ex} when:3d',
                f'"{name_en}"{ex} when:3d',
                f'"{name_en}"{ex} when:7d',
            ]
            for q in queries:
                if len(picked) >= limit:
                    break
                picked = _pick(_fetch_feed(_google_news_url_en(q), 40), "en", limit, seen, check, picked)
        if picked:
            return picked
    except Exception as e:
        log.warning(f"[{code}] 헤드라인 수집 예외: {e}")

    cached_items = _cached_list((cached or {}).get("headlines"),
                                None if code == "KR" else make_relevance_checker(country))
    if cached_items:
        log.info(f"[{code}] 헤드라인: 새 기사 없음 → 이전 수집 기사 유지")
        return cached_items
    return [_fallback_headline_item(country)]


# ---------------------------------------------------------------------------
# 6. 비즈니스 동향
# ---------------------------------------------------------------------------
def _fallback_trend_item(spec: dict, country: dict) -> dict:
    if country.get("code") == "KR":
        link = _google_news_search_link(spec["home_query"], "ko")
    else:
        link = _google_news_search_link(f'"{country.get("name_en", "")}" {spec["en_keywords"]}')
    return {
        "title": FALLBACK_TEXT,
        "desc": f"{spec.get('tag', '')} 관련 최신 기사를 찾지 못해 Google 뉴스 검색 결과로 연결됩니다. 다음 갱신 시 자동으로 업데이트됩니다.",
        "source": "Google News 검색",
        "link": link,
        "lang": "ko",
        "translated": False,
        "original_title": "",
        "published": "",
        "is_fallback": True,
    }


def _to_trend_item(p: dict) -> dict:
    return {
        "title": p.get("title", ""),
        "desc": p.get("summary", ""),
        "source": p.get("source", "Google News"),
        "link": p.get("link", ""),
        "lang": p.get("lang", "ko"),
        "translated": p.get("translated", False),
        "original_title": p.get("original_title", ""),
        "published": p.get("published", ""),
    }


def get_industry_trends(country: dict, cached: dict, seen: set) -> list:
    code = country.get("code")
    name_kr = country.get("name_kr", "")
    name_en = country.get("name_en", "")
    check = make_relevance_checker(country)
    ex = _exclusions(code)

    cached_by_cat = {}
    for t in (cached or {}).get("hr_trends", []) or []:
        if isinstance(t, dict) and t.get("category"):
            cached_by_cat[t["category"]] = _cached_list(t.get("items"), None if code == "KR" else check)

    trends = []
    for spec in INDUSTRY_TOPICS:
        limit = spec.get("max_items", 4)
        picked = []
        try:
            if code == "KR":
                picked = _pick(_fetch_feed(_google_news_url_ko(f"{spec['home_query']} when:14d"), 30),
                               "ko", limit, seen)
            else:
                q_ko = f'"{name_kr}" {spec["kr_keywords"]} when:14d'
                picked = _pick(_fetch_feed(_google_news_url_ko(q_ko), 30), "ko", limit, seen, check)
                if len(picked) < limit:
                    q_en = f'"{name_en}" {spec["en_keywords"]}{ex} when:14d'
                    picked = _pick(_fetch_feed(_google_news_url_en(q_en), 40), "en", limit, seen, check, picked)
        except Exception as e:
            log.warning(f"[{code}] '{spec['category']}' 수집 예외: {e}")

        items = [_to_trend_item(p) for p in picked]
        if not items:
            items = cached_by_cat.get(spec["category"]) or [_fallback_trend_item(spec, country)]

        trends.append({
            "category": spec["category"],
            "tag": spec["tag"],
            "tag_class": spec["tag_class"],
            "items": items[:limit],
        })
    return trends


# ---------------------------------------------------------------------------
# 7. 번역 (수집 완료 후 일괄 처리)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 6-2. 현지 뉴스 3분류 (안전·재난 / 정치·경제 / 사회·문화) + 법·제도 표시 + 글로벌 산업 동향
#   - 분류마다 별도 검색어로 수집하고, 국가 관련성 필터(별칭이 제목에 있어야 함)를 그대로 적용한다.
#   - 안전·재난은 제목이 아래 SAFETY_TAGS 중 하나에 걸려야만 채택(오탐 방지)하고 태그를 붙인다.
#     '공식 경보'가 아니라 '확인이 필요한 보도'로 화면에 표시한다.
# ---------------------------------------------------------------------------
NEWS_CATEGORIES = [
    # 안전·재난을 먼저 수집해야 같은 기사가 정치·경제로 먼저 분류되지 않는다
    {"id": "safe", "label": "안전·재난", "limit": 3, "when": "when:3d",
     "en": '"{name}" (earthquake OR flood OR flooding OR storm OR typhoon OR hurricane OR wildfire OR explosion '
           'OR blast OR collapse OR missile OR drone OR airstrike OR terror OR riot OR protest OR outbreak OR evacuation)',
     "home": "(지진 OR 태풍 OR 폭우 OR 홍수 OR 산불 OR 대형화재 OR 폭발 OR 붕괴 OR 테러 OR 감염병)"},
    {"id": "pol", "label": "정치·경제", "limit": 4, "when": "when:2d",
     "en": '"{name}" (government OR election OR parliament OR minister OR economy OR inflation '
           'OR "central bank" OR trade OR tariff OR budget)',
     "home": "(정부 OR 국회 OR 대통령 OR 경제 OR 금리 OR 물가 OR 수출)"},
    {"id": "soc", "label": "사회·문화", "limit": 3, "when": "when:3d",
     "en": '"{name}" (society OR culture OR education OR health OR immigration OR population OR tourism OR festival)',
     "home": "(사회 OR 문화 OR 교육 OR 저출생 OR 의료 OR 복지)"},
]

SAFETY_TAGS = [
    # 순서가 곧 판정 우선순위: 테러성 폭발은 '무력충돌·테러', 그 밖의 폭발·화재는 '대형사고'
    ("무력충돌·테러", re.compile(r"(?i)\bmissile|\bairstrike|\bair strike|\bdrone (attack|strike)|\bshelling|"
                              r"strikes? on|\binvasion|\battacks? (on|by)|\bwar\b|\bterror|\bbomb|\bsuicide attack|"
                              r"\bgunm[ae]n|\bmass shooting|\bshooting\b|\bhostage|미사일|공습|교전|포격|테러|폭탄|총격|인질")),
    ("자연재해", re.compile(r"(?i)\bearthquakes?\b|\bquake\b|\btremor|\btsunami|\bflood|\bstorm\b|\btyphoon|\bhurricane|"
                         r"\bcyclone|\bwildfire|\blandslide|heavy rain|\bheatwave|\bheat wave|\bvolcan|\bblizzard|\bdrought|"
                         r"지진|쓰나미|홍수|태풍|폭우|산불|폭염|산사태|화산|폭설|가뭄")),
    ("대형사고", re.compile(r"(?i)\bexplosion|\bblast\b|\bfactory fire|\bmajor fire|\bbuilding collapse|\bcollapse[sd]?\b|"
                         r"\bderail|\btrain crash|\bplane crash|\bcrash kills|\bchemical (leak|spill)|\bgas leak|"
                         r"폭발|대형화재|붕괴|탈선|추락|유출 사고")),
    ("사회불안", re.compile(r"(?i)\bprotest|\briot|\bunrest|\bgeneral strike|\bcurfew|\bcoup\b|시위|폭동|소요|총파업|통행금지|쿠데타")),
    ("보건·환경", re.compile(r"(?i)\bhaze\b|\bsmog|air pollution|\boutbreak|\bepidemic|\bpandemic|\bcholera|"
                          r"\bdengue|\bmpox|연무|미세먼지|감염병|전염병")),
]
# 안전 태그를 붙이지 않을 문맥(훈련·과거사·문화 콘텐츠 등)
SAFETY_EXCLUDE = re.compile(r"(?i)\bdrill\b|\banniversary\b|\bmuseum\b|\bfilm\b|\bmovie\b|\bgame\b|\bnovel\b|"
                            r"\bWorld War\b|\bwar (crimes? )?memorial|훈련|기념|영화|게임")

LAW_RE = re.compile(r"(?i)\blaws?\b|\bbill\b|\bact\b|\bamend|\bregulation|\bdecree|\blegislat|\blabou?r code|"
                    r"\bminimum wage|\bordinance|\bruling\b|\bcourt\b|법|개정|시행령|입법|최저임금|판결")


def safety_tag(title: str) -> str:
    if not title or SAFETY_EXCLUDE.search(title):
        return ""
    for tag, rx in SAFETY_TAGS:
        if rx.search(title):
            return tag
    return ""


def is_law_item(item: dict) -> bool:
    return bool(LAW_RE.search(item.get("original_title") or item.get("title") or ""))


def get_local_news(country: dict, cached: dict, seen: set) -> dict:
    """현지 뉴스를 safe/pol/soc 세 분류로 수집. 분류별로 새 기사가 없으면 이전 수집 기사(관련성 재검사)를
    쓰고, 그것도 없으면 빈 목록(화면에 '보도 없음')으로 둔다 — 대체 문구를 만들지 않는다."""
    code = country.get("code")
    is_home = code == "KR"
    check = None if is_home else make_relevance_checker(country)
    ex = "" if is_home else _exclusions(code)
    name = country.get("name_en", "")
    cached_news = (cached or {}).get("news") or {}
    out = {}
    for cat in NEWS_CATEGORIES:
        cid, limit = cat["id"], cat["limit"]
        picked = []
        try:
            if is_home:
                url = _google_news_url_ko(f'{cat["home"]} {cat["when"]}')
                cands = _fetch_feed(url, 40)
                lang = "ko"
            else:
                url = _google_news_url_en(cat["en"].format(name=name) + f'{ex} {cat["when"]}')
                cands = _fetch_feed(url, 50)
                lang = "en"
            if cid == "safe":
                cands = [c for c in cands if safety_tag(c.get("title", ""))]
            picked = _pick(cands, lang, limit, seen, check)
            if cid == "pol" and len(picked) < 2 and not is_home:
                more = _fetch_feed(_google_news_url_en(f'"{name}"{ex} when:2d'), 50)
                picked = _pick(more, "en", limit, seen, check, picked)
        except Exception as e:
            log.warning(f"[{code}] 뉴스({cat['label']}) 수집 예외: {e}")
        if not picked:
            prev = _cached_list(cached_news.get(cid), check)
            if cid == "safe":
                prev = [p for p in prev if safety_tag(p.get("original_title") or p.get("title", ""))]
            picked = prev[:limit]
        for it in picked:
            if cid == "safe":
                it["safety_tag"] = safety_tag(it.get("original_title") or it.get("title", ""))
        out[cid] = picked
    return out


GLOBAL_INDUSTRY_QUERIES = [
    {"lang": "ko", "q": "(타이어 OR 완성차 OR 전기차 판매 OR 자동차 수출) when:2d", "limit": 4},
    {"lang": "en", "q": "(Michelin OR Bridgestone OR Continental OR Goodyear OR Pirelli OR \"Sumitomo Rubber\") tire when:3d", "limit": 3},
    {"lang": "en", "q": "(\"tire market\" OR \"tyre market\" OR \"global auto sales\" OR automaker) when:2d", "limit": 3},
]
INDUSTRY_TAG_RULES = [
    ("경쟁사", re.compile(r"(?i)michelin|bridgestone|continental|goodyear|pirelli|sumitomo|yokohama|금호|넥센|미쉐린|브리지스톤|굿이어|피렐리")),
    ("타이어", re.compile(r"(?i)\btires?\b|\btyres?\b|타이어")),
    ("EV", re.compile(r"(?i)\bEVs?\b|electric vehicle|전기차|배터리")),
    ("시장전망", re.compile(r"(?i)forecast|outlook|market size|전망|시장 규모")),
    ("완성차", re.compile(r"(?i)automaker|carmaker|auto sales|car sales|vehicle|완성차|자동차")),
]


def industry_tag(title: str) -> str:
    for tag, rx in INDUSTRY_TAG_RULES:
        if rx.search(title or ""):
            return tag
    return "완성차"


def get_global_industry(previous: list) -> list:
    """국가와 무관한 완성차·타이어·경쟁사 이슈. 수집이 비면 직전 값을 유지한다."""
    seen: set = set()
    items = []
    for spec in GLOBAL_INDUSTRY_QUERIES:
        try:
            url = _google_news_url_ko(spec["q"]) if spec["lang"] == "ko" else _google_news_url_en(spec["q"])
            items = _pick(_fetch_feed(url, 30), spec["lang"], len(items) + spec["limit"], seen, None, items)
        except Exception as e:
            log.warning(f"글로벌 산업 동향 수집 예외: {e}")
    if not items:
        items = _cached_list(previous)
    for it in items:
        it["tag"] = industry_tag(it.get("original_title") or it.get("title", ""))
    return items[:10]


def load_translation_cache() -> dict:
    try:
        if os.path.exists(TRANSLATION_CACHE_PATH):
            with open(TRANSLATION_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("ko")}
    except Exception as e:
        log.warning(f"번역 캐시 로드 실패(무시): {e}")
    return {}


def save_translation_cache(cache: dict) -> None:
    cutoff = (datetime.now(KST).date() - timedelta(days=TRANSLATION_CACHE_TTL_DAYS)).isoformat()
    pruned = {k: v for k, v in cache.items() if str(v.get("last", "")) >= cutoff}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TRANSLATION_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=0, sort_keys=True)
    except Exception as e:
        log.warning(f"번역 캐시 저장 실패(무시): {e}")


GTX_URL = "https://translate.googleapis.com/translate_a/single"
GTX_ALT_URL = "https://clients5.google.com/translate_a/t"       # Google 번역 보조 주소 (1번이 막힐 때)
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
# MyMemory 는 익명이면 하루 약 5천 자 한도. 저장소 Secrets 에 MYMEMORY_EMAIL 을 넣으면 하루 5만 자로 늘어난다.
MYMEMORY_EMAIL = os.environ.get("MYMEMORY_EMAIL", "").strip()
TRANSLATE_TIMEOUT = 10
TRANSLATE_STATS: dict = {}


def _json_with_retry(method: str, url: str, **kw):
    """429(요청 과다)·503 이면 3초, 6초 쉬었다가 다시 시도한다."""
    for attempt in range(3):
        r = requests.request(method, url, timeout=TRANSLATE_TIMEOUT, headers={"User-Agent": HTTP_USER_AGENT}, **kw)
        if r.status_code in (429, 503) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()


def _translate_google(text: str) -> str:
    """Google 번역 공개 엔드포인트(client=gtx). 줄바꿈으로 묶은 여러 문장도 한 번에 번역된다."""
    data = _json_with_retry("POST", GTX_URL, params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t"},
                            data={"q": text})
    return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])


def _translate_google_alt(text: str) -> str:
    """Google 번역 보조 주소. 응답 형태가 여러 가지라 첫 번째 문자열을 꺼낸다."""
    data = _json_with_retry("GET", GTX_ALT_URL, params={"client": "dict-chrome-ex", "sl": "auto", "tl": "ko", "q": text})
    if isinstance(data, dict):
        return "".join(s.get("trans", "") for s in data.get("sentences", []) if isinstance(s, dict))
    while isinstance(data, list) and data:
        data = data[0]
    return data if isinstance(data, str) else ""


def _translate_mymemory(text: str) -> str:
    params = {"q": text[:480], "langpair": "en|ko"}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL
    data = _json_with_retry("GET", MYMEMORY_URL, params=params)
    out = (data.get("responseData") or {}).get("translatedText") or ""
    return "" if "MYMEMORY WARNING" in out.upper() else out


TRANSLATORS = [_translate_google, _translate_google_alt, _translate_mymemory]


def _translate_one(text: str) -> str:
    """단건 번역: Google → Google 보조 → MyMemory 순서. 계속 실패한 번역기는 이번 실행에서 건너뛴다."""
    for fn in TRANSLATORS:
        st = TRANSLATE_STATS.setdefault(fn.__name__, {"ok": 0, "fail": 0, "streak": 0})
        if st["streak"] >= 4:
            continue
        try:
            out = fn(text)
            if out and HANGUL_RE.search(out):
                st["ok"] += 1
                st["streak"] = 0
                return out.strip()
            st["fail"] += 1
            st["streak"] += 1
        except Exception as e:
            st["fail"] += 1
            st["streak"] += 1
            log.warning(f"{fn.__name__} 실패: {e.__class__.__name__}: {str(e)[:120]}")
    return ""


def _translate_batch(texts: list):
    """여러 문장을 줄바꿈으로 묶어 한 번에 번역. 줄 수가 어긋나거나 실패하면 None."""
    st = TRANSLATE_STATS.setdefault("batch", {"ok": 0, "fail": 0, "streak": 0})
    if st["streak"] >= 2:                     # 묶음 번역이 연달아 막히면 단건 번역으로만 진행
        return None
    joined = "\n".join(t.replace("\n", " ") for t in texts)
    try:
        out = _translate_google(joined)
        lines = [ln.strip() for ln in (out or "").split("\n") if ln.strip()]
        if len(lines) == len(texts):
            st["ok"] += 1
            st["streak"] = 0
            return lines
        log.info(f"묶음 번역 줄 수 불일치({len(lines)}≠{len(texts)}) → 단건 번역으로 전환")
    except Exception as e:
        log.warning(f"묶음 번역 실패: {e.__class__.__name__}: {str(e)[:120]}")
    st["fail"] += 1
    st["streak"] += 1
    return None


def translate_texts(texts: list, cache: dict) -> dict:
    """영문 문장 리스트 → {원문: 번역문}. texts 순서가 곧 우선순위(앞쪽부터 번역).
    캐시 우선, 미번역분만 호출하며 시간 예산(TRANSLATION_BUDGET_SEC)으로 전체 실행 시간을 제한한다."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    result = {}
    pending = []
    for t in dict.fromkeys(x for x in texts if x and x.strip()):
        hit = cache.get(t)
        if hit and hit.get("ko"):
            hit["last"] = today
            result[t] = hit["ko"]
        else:
            pending.append(t)

    if DISABLE_TRANSLATION or not pending:
        if pending:
            log.info(f"번역 비활성화 — 미번역 {len(pending)}건은 원문 유지")
        return result

    log.info(f"번역 대상 {len(pending)}건 (캐시 적중 {len(result)}건)")
    chunks, chunk, size = [], [], 0
    for t in pending:
        if chunk and (size + len(t) > 1500 or len(chunk) >= 20):
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(t)
        size += len(t) + 1
    if chunk:
        chunks.append(chunk)

    started = time.monotonic()
    stop_reason = ""
    for ch in chunks:
        if time.monotonic() - started > TRANSLATION_BUDGET_SEC:
            stop_reason = f"시간 예산({TRANSLATION_BUDGET_SEC}초) 초과"
            break
        translated = _translate_batch(ch)
        if translated is None:
            translated = []
            for t in ch:
                if time.monotonic() - started > TRANSLATION_BUDGET_SEC:
                    translated.append("")
                    continue
                translated.append(_translate_one(t))
                time.sleep(0.3)
        for src, ko in zip(ch, translated):
            if ko and HANGUL_RE.search(ko):
                result[src] = ko
                cache[src] = {"ko": ko, "last": today}
        if all(TRANSLATE_STATS.get(fn.__name__, {}).get("streak", 0) >= 4 for fn in TRANSLATORS) \
                and TRANSLATE_STATS.get("batch", {}).get("streak", 0) >= 2:
            stop_reason = "모든 번역 서비스 연속 실패"
            break
        time.sleep(0.5)

    done = len([t for t in pending if t in result])
    stats = ", ".join(f"{k} 성공 {v['ok']}·실패 {v['fail']}" for k, v in TRANSLATE_STATS.items())
    log.info(f"번역 완료: {done}/{len(pending)}건 성공 ({time.monotonic() - started:.0f}초) [{stats}]")
    if stop_reason:
        log.warning(f"번역 중단: {stop_reason} — 나머지는 원문 유지, 다음 실행 때 재시도")
    return result

def _iter_news_items(countries: list):
    for c in countries:
        for h in c.get("headlines", []) or []:
            if isinstance(h, dict):
                yield h, "summary"
        for lst in (c.get("news") or {}).values():
            for h in lst or []:
                if isinstance(h, dict):
                    yield h, "summary"
        for t in c.get("hr_trends", []) or []:
            for it in (t.get("items") or []) if isinstance(t, dict) else []:
                if isinstance(it, dict):
                    yield it, "desc"


def apply_translations(countries: list) -> None:
    """lang=en 이면서 아직 번역되지 않은 기사 제목을 일괄 번역해 반영.
    - 화면에는 제목만 나오므로 본문 요약은 번역하지 않는다(요청량 절반 감소).
    - 글로벌 동향 → 최신 기사 순으로 번역해, 시간이 모자라도 브리핑에 나오는 기사부터 번역되게 한다."""
    cache = load_translation_cache()
    targets = []
    for item, _body_key in _iter_news_items(countries):
        if item.get("lang") != "en" or item.get("translated"):
            continue
        src_title = item.get("original_title") or item.get("title") or ""
        item["original_title"] = src_title
        targets.append((item, src_title))

    if not targets:
        save_translation_cache(cache)
        return

    # 호출부에서 글로벌 동향을 마지막 묶음으로 넘긴다 → 그 항목들을 맨 앞으로
    global_ids = {id(i) for i, _ in _iter_news_items(countries[-1:])}
    ordered = sorted(targets, key=lambda t: (0 if id(t[0]) in global_ids else 1, -_date_num(t[0].get("published"))))
    mapping = translate_texts([t[1] for t in ordered], cache)
    for item, src_title in targets:
        ko_title = mapping.get(src_title)
        if ko_title:
            item["title"] = ko_title
            item["translated"] = True
        else:
            item["title"] = src_title
            item["translated"] = False
    save_translation_cache(cache)


def _date_num(d) -> int:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    return int(m.group(1) + m.group(2) + m.group(3)) if m else 0


# ---------------------------------------------------------------------------
# 8. 캐시 / 아카이브 저장 + 180일 Retention
# ---------------------------------------------------------------------------
def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return {c["code"]: c for c in payload.get("countries", []) if isinstance(c, dict) and c.get("code")}
        except Exception as e:
            log.warning(f"캐시 로드 실패: {e}")
    return {}


def save_cache(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def clean_old_archives(archive_dir: str, max_days: int = ARCHIVE_RETENTION_DAYS) -> None:
    if not os.path.isdir(archive_dir):
        return
    cutoff = datetime.now(KST).date() - timedelta(days=max_days)
    valid_dates = []
    try:
        filenames = os.listdir(archive_dir)
    except Exception as e:
        log.warning(f"[clean_old_archives] {archive_dir} 조회 실패: {e}")
        return

    for fname in filenames:
        if fname == "index.json" or not fname.endswith(".json"):
            continue
        date_part = fname[:-5]
        try:
            d = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.remove(os.path.join(archive_dir, fname))
                log.info(f"[clean_old_archives] {fname} 삭제 ({max_days}일 경과)")
            except Exception as e:
                log.warning(f"[clean_old_archives] {fname} 삭제 실패: {e}")
            continue
        valid_dates.append(date_part)

    valid_dates.sort()
    index_payload = {
        "dates": valid_dates,
        "available_dates": valid_dates,
        "min_date": valid_dates[0] if valid_dates else None,
        "latest": valid_dates[-1] if valid_dates else None,
    }
    try:
        with open(os.path.join(archive_dir, "index.json"), "w", encoding="utf-8") as f:
            json.dump(index_payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"[clean_old_archives] index.json 저장 실패: {e}")


def save_archive(data: dict) -> None:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    date_str = data["generated_at"][:10]
    with open(os.path.join(ARCHIVE_DIR, f"{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    clean_old_archives(ARCHIVE_DIR, max_days=ARCHIVE_RETENTION_DAYS)


# ---------------------------------------------------------------------------
# 9. 구버전 아카이브 마이그레이션 + 텍스트 정리
# ---------------------------------------------------------------------------
def _is_legacy_archive(data: dict) -> bool:
    if not isinstance(data, dict):
        return True
    countries = data.get("countries")
    if not isinstance(countries, list) or len(countries) != len(COUNTRIES):
        return True
    for c in countries:
        if not isinstance(c, dict):
            return True
        profile = c.get("profile")
        if not isinstance(profile, dict) or "min_wage" not in profile:
            return True
        fx = c.get("exchange_rate")
        if not isinstance(fx, dict) or "unit_base" not in fx or "url" not in fx:
            return True
        hr_trends = c.get("hr_trends")
        if not isinstance(hr_trends, list) or len(hr_trends) != len(INDUSTRY_TOPICS):
            return True
        for t in hr_trends:
            if not isinstance(t, dict) or not isinstance(t.get("items"), list):
                return True
        if "offices" not in c and "hubs" not in c:
            return True
    return False


def _migrate_exchange_rate_legacy(old_fx: dict, meta: dict) -> dict:
    currency = meta["currency"]
    if currency == "KRW":
        return {"is_base": True, "unit_base": 1, "decimals": 1, "current_rate": 1.0, "change_pct": 0.0,
                "history_labels": [], "history_values": [], "source": "기준통화", "url": ""}
    old_fx = old_fx if isinstance(old_fx, dict) else {}
    labels = old_fx.get("history_labels") if isinstance(old_fx.get("history_labels"), list) else []
    values = old_fx.get("history_values") if isinstance(old_fx.get("history_values"), list) else []
    if len(labels) != len(values):
        labels, values = [], []
    try:
        current_rate = float(old_fx.get("current_rate"))
    except Exception:
        current_rate = None
    try:
        change_pct = round(float(old_fx.get("change_pct")), 2)
    except Exception:
        change_pct = None
    unit_base = old_fx.get("unit_base") if isinstance(old_fx.get("unit_base"), int) and old_fx.get("unit_base") > 0 \
        else SMALL_UNIT_CURRENCIES.get(currency, 1)
    dec = decimals_for(current_rate or 0)
    return {
        "is_base": False,
        "unit_base": unit_base,
        "decimals": dec,
        "current_rate": _round_to(current_rate, dec) if current_rate else None,
        "change_pct": change_pct,
        "history_labels": [str(x) for x in labels],
        "history_values": [_round_to(v, dec) for v in values],
        "source": old_fx.get("source") if not _is_bad_value(old_fx.get("source")) else "이전 데이터(마이그레이션)",
        "url": _yahoo_finance_url(currency),
    }


def _migrate_profile_legacy(old_profile: dict, meta: dict) -> dict:
    old_profile = old_profile if isinstance(old_profile, dict) else {}
    meta_profile = meta.get("meta_profile", {})

    def _clean(key):
        v = old_profile.get(key)
        return meta_profile.get(key, "-") if _is_bad_value(v) else v

    return {
        "capital": _clean("capital"),
        "population": _clean("population"),
        "gdp": _clean("gdp"),
        "inflation": _clean("inflation"),
        "unemployment": _clean("unemployment"),
        "min_wage": meta_profile.get("min_wage", _clean("min_wage")),
        "min_wage_note": "",
        "stats_source": "Hankook Tire Global HR 자체 조사",
        "stats_asof": "",
    }


def _migrate_items(old_list, fallback_item: dict, limit: int) -> list:
    items = []
    if isinstance(old_list, list):
        for h in old_list:
            if isinstance(h, dict) and not _is_bad_value(h.get("title")) and not _is_bad_value(h.get("link")):
                items.append(sanitize_item({
                    "title": str(h.get("title")),
                    "source": str(h.get("source") or "Google News"),
                    "link": str(h.get("link")),
                    "summary": str(h.get("summary") or ""),
                    "desc": str(h.get("desc") or ""),
                    "lang": str(h.get("lang") or "ko"),
                    "translated": bool(h.get("translated")) if isinstance(h.get("translated"), bool) else False,
                    "original_title": str(h.get("original_title") or ""),
                }))
    return items[:limit] if items else [fallback_item]


def migrate_legacy_archive_file(path: str, date_str: str) -> None:
    old_data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        if raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                old_data = parsed
    except Exception as e:
        log.warning(f"[migrate] {path} 로드 실패, 전체를 Fallback으로 재구성: {e}")

    old_countries = old_data.get("countries", []) if isinstance(old_data.get("countries"), list) else []
    old_by_code = {c.get("code"): c for c in old_countries if isinstance(c, dict) and c.get("code")}

    new_countries = []
    for meta in COUNTRIES:
        meta = copy.deepcopy(meta)
        old = old_by_code.get(meta["code"]) or {}
        old_trends = {t.get("category"): t for t in (old.get("hr_trends") or []) if isinstance(t, dict)}
        trends = []
        for spec in INDUSTRY_TOPICS:
            ot = old_trends.get(spec["category"]) or {}
            src_list = ot.get("items") if isinstance(ot.get("items"), list) else ([ot] if ot.get("title") else [])
            trends.append({
                "category": spec["category"], "tag": spec["tag"], "tag_class": spec["tag_class"],
                "items": _migrate_items(src_list, _fallback_trend_item(spec, meta), spec["max_items"]),
            })
        new_countries.append({
            "code": meta["code"], "region": meta["region"], "name_kr": meta["name_kr"],
            "name_en": meta["name_en"], "currency": meta["currency"], "flag": meta["flag"],
            "offices": meta.get("offices", []), "hubs": meta.get("offices", []),
            "profile": _migrate_profile_legacy(old.get("profile", {}), meta),
            "exchange_rate": _migrate_exchange_rate_legacy(old.get("exchange_rate", {}), meta),
            "headlines": _migrate_items(old.get("headlines"), _fallback_headline_item(meta), 3),
            "hr_trends": trends,
        })

    new_data = {
        "generated_at": old_data.get("generated_at") or f"{date_str}T06:00:00+09:00",
        "generated_at_display": old_data.get("generated_at_display") or f"{date_str} 06:00 KST",
        "as_of_display": f"{date_str} 아카이브",
        "regions": REGIONS,
        "countries": new_countries,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, default=str)
    log.info(f"[migrate] {os.path.basename(path)} 최신 스키마로 마이그레이션 ({len(new_countries)}개국)")


def _sanitize_archive_data(data: dict) -> bool:
    """아카이브 내 기사 텍스트의 HTML 엔티티(&nbsp; 등)를 정리. 변경 시 True."""
    changed = False
    for c in data.get("countries", []) or []:
        if not isinstance(c, dict):
            continue
        heads = c.get("headlines")
        if isinstance(heads, list):
            new = [sanitize_item(h) if isinstance(h, dict) else h for h in heads]
            if new != heads:
                c["headlines"] = new
                changed = True
        for t in c.get("hr_trends", []) or []:
            if isinstance(t, dict) and isinstance(t.get("items"), list):
                new = [sanitize_item(i) if isinstance(i, dict) else i for i in t["items"]]
                if new != t["items"]:
                    t["items"] = new
                    changed = True
        prof = c.get("profile")
        if isinstance(prof, dict) and prof.get("stats_asof") == "-":
            prof["stats_asof"] = ""
            changed = True
    return changed


def migrate_all_legacy_archives() -> None:
    if not os.path.isdir(ARCHIVE_DIR):
        return
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    touched = 0
    for fname in sorted(os.listdir(ARCHIVE_DIR)):
        if fname == "index.json" or not fname.endswith(".json") or not date_pattern.match(fname[:-5]):
            continue
        date_str = fname[:-5]
        path = os.path.join(ARCHIVE_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            log.warning(f"[migrate] {fname} 로드 실패(재구성): {e}")
            data = {}
        try:
            if _is_legacy_archive(data):
                migrate_legacy_archive_file(path, date_str)
                touched += 1
            elif _sanitize_archive_data(data):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, default=str)
                touched += 1
        except Exception as e:
            log.error(f"[migrate] {fname} 처리 실패, 다음 파일로 계속: {e}")
    if touched:
        log.info(f"[migrate] 아카이브 {touched}건 정리/마이그레이션 완료")
        clean_old_archives(ARCHIVE_DIR, max_days=ARCHIVE_RETENTION_DAYS)


# ---------------------------------------------------------------------------
# 10. 오케스트레이션
# ---------------------------------------------------------------------------
def collect_country(country: dict, cache_by_code: dict, fx_series: dict) -> dict:
    code = country["code"]
    cached = cache_by_code.get(code, {})
    seen: set = set()
    fixed_sites = FIXED_CONFIG.get(code.upper(), {}).get("sites") or []
    auto_sites = AUTO_PROFILES.get(code.upper(), {}).get("sites") or []
    offices = copy.deepcopy(fixed_sites or auto_sites or country.get("offices", []))
    result = {
        "code": code, "region": country["region"], "name_kr": country["name_kr"],
        "name_en": country["name_en"], "currency": country["currency"], "flag": country["flag"],
        "offices": offices, "hubs": offices,
    }

    try:
        result["profile"] = build_profile(country, cached)
    except Exception as e:
        log.error(f"[{code}] 프로필 구성 실패: {e}")
        result["profile"] = (cached or {}).get("profile", {})

    try:
        result["exchange_rate"] = get_exchange_rate(country, cached, fx_series)
    except Exception as e:
        log.error(f"[{code}] 환율 처리 실패: {e}")
        result["exchange_rate"] = _failed_fx(country["currency"], SMALL_UNIT_CURRENCIES.get(country["currency"], 1))

    try:
        result["news"] = get_local_news(country, cached, seen)
    except Exception as e:
        log.error(f"[{code}] 현지 뉴스 실패: {e}")
        result["news"] = {c["id"]: [] for c in NEWS_CATEGORIES}
    # 구버전 화면·도구 호환용: 현지 주요 뉴스 상위 3건
    result["headlines"] = (result["news"].get("pol") or result["news"].get("soc") or [])[:3] \
        or [_fallback_headline_item(country)]
    result["official_sources"] = FIXED_CONFIG.get(code.upper(), {}).get("official", [])

    try:
        result["hr_trends"] = get_industry_trends(country, cached, seen)
    except Exception as e:
        log.error(f"[{code}] 산업 동향 실패: {e}")
        result["hr_trends"] = [
            {"category": s["category"], "tag": s["tag"], "tag_class": s["tag_class"],
             "items": [_fallback_trend_item(s, country)]}
            for s in INDUSTRY_TOPICS
        ]
    for t in result.get("hr_trends", []):
        if t.get("category") == "HR & LABOR":
            for it in t.get("items", []):
                it["law"] = (not it.get("is_fallback")) and is_law_item(it)
    return result


def _build_country_fallback(code: str) -> dict:
    meta = next((copy.deepcopy(c) for c in COUNTRIES if c["code"] == code), None)
    if meta is None:
        meta = {"code": code, "region": "ALL", "name_kr": code, "name_en": code, "currency": "USD",
                "flag": "un", "offices": [], "meta_profile": {}}
    offices = meta.get("offices", [])
    return {
        "code": meta["code"], "region": meta["region"], "name_kr": meta["name_kr"],
        "name_en": meta["name_en"], "currency": meta["currency"], "flag": meta["flag"],
        "offices": offices, "hubs": offices,
        "profile": build_profile(meta, {}),
        "exchange_rate": _failed_fx(meta["currency"], SMALL_UNIT_CURRENCIES.get(meta["currency"], 1)),
        "headlines": [_fallback_headline_item(meta)],
        "news": {c["id"]: [] for c in NEWS_CATEGORIES},
        "official_sources": FIXED_CONFIG.get(code.upper(), {}).get("official", []),
        "hr_trends": [
            {"category": s["category"], "tag": s["tag"], "tag_class": s["tag_class"],
             "items": [_fallback_trend_item(s, meta)]}
            for s in INDUSTRY_TOPICS
        ],
    }


def main() -> dict:
    started = time.time()
    cache_by_code = load_cache()

    log.info("환율 일괄 수집 시작")
    try:
        fx_series = prefetch_fx_series(COUNTRIES)
    except Exception as e:
        log.error(f"환율 일괄 수집 실패: {e}")
        fx_series = {}
    log.info(f"환율 티커 {sum(1 for s in fx_series.values() if len(s) >= MIN_RELIABLE_TRADING_DAYS)}/"
             f"{len(_needed_tickers(COUNTRIES))}개 정상 수집")

    collected = {}
    with ThreadPoolExecutor(max_workers=NEWS_WORKERS) as pool:
        futures = {pool.submit(collect_country, copy.deepcopy(c), cache_by_code, fx_series): c["code"]
                   for c in COUNTRIES}
        for future in as_completed(futures):
            code = futures[future]
            try:
                collected[code] = future.result()
                log.info(f"[{code}] 수집 완료")
            except Exception as e:
                log.error(f"[{code}] 국가 단위 수집 실패 → 안전망 사용: {e}")
                collected[code] = cache_by_code.get(code) or _build_country_fallback(code)

    countries_result = [collected.get(c["code"]) or _build_country_fallback(c["code"]) for c in COUNTRIES]

    try:
        global_industry = get_global_industry(_load_previous_global_industry())
    except Exception as e:
        log.error(f"글로벌 산업 동향 실패: {e}")
        global_industry = _load_previous_global_industry()

    try:
        apply_translations(countries_result + [{"news": {"global": global_industry}}])
    except Exception as e:
        log.error(f"일괄 번역 단계 실패(원문 유지): {e}")

    now_kst = datetime.now(KST)
    data = {
        "generated_at": now_kst.isoformat(),
        "generated_at_display": now_kst.strftime("%Y-%m-%d %H:%M KST"),
        "as_of_display": f"{now_kst.strftime('%Y-%m-%d %H:%M')} KST 수집",
        "regions": REGIONS,
        "countries": countries_result,
        "global_industry": global_industry,
    }

    save_cache(data)  # 실패 시 예외 → 워크플로 실패로 즉시 드러나게 함
    log.info(f"data/countries_data.json 저장 완료 ({len(countries_result)}개국)")

    try:
        save_archive(data)
    except Exception as e:
        log.error(f"아카이브 저장 실패(사이트 생성은 계속): {e}")
    try:
        migrate_all_legacy_archives()
    except Exception as e:
        log.error(f"아카이브 마이그레이션 오류(무시): {e}")

    _log_quality_report(countries_result)
    log.info(f"전체 {len(countries_result)}개국 수집 완료 ({time.time() - started:.0f}초)")
    return data


def _load_previous_global_industry() -> list:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return [sanitize_item(x) for x in (json.load(f).get("global_industry") or []) if isinstance(x, dict)]
    except Exception:
        return []


def _log_quality_report(countries: list) -> None:
    """실행 로그에 품질 요약을 남겨 GitHub Actions에서 바로 확인할 수 있게 한다."""
    fx_fail = [c["code"] for c in countries if not c["exchange_rate"].get("is_base")
               and (c["exchange_rate"].get("stale") or not c["exchange_rate"].get("history_values"))]
    fallback_news = [c["code"] for c in countries if any(h.get("is_fallback") for h in c.get("headlines", []))]
    en_items = [i for i, _ in _iter_news_items(countries) if i.get("lang") == "en"]
    untranslated = sum(1 for i in en_items if not i.get("translated"))
    log.info(f"[품질] 환율 최신값 미확보: {len(fx_fail)}개국 {fx_fail}")
    log.info(f"[품질] 헤드라인 폴백: {len(fallback_news)}개국 {fallback_news}")
    safe = [(c["code"], i.get("safety_tag")) for c in countries for i in (c.get("news") or {}).get("safe", [])]
    log.info(f"[품질] 안전·재난 보도 {len(safe)}건 {safe}")
    log.info(f"[품질] 영문 기사 {len(en_items)}건 중 미번역 {untranslated}건")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("collector.py 실행 중 처리되지 않은 예외")
        raise
