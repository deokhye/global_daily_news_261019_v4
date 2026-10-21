"""
news_rules.py
-------------
뉴스 수집·분류 규칙 (config/news_rules.json) — collector.py 와 weekly_mail.py 가 함께 쓴다.

  reject_reason(title, source)  → 의견·과거 시점·연예/스포츠·차단 매체면 이유 문자열, 아니면 ""
  annotate(item, kind, country) → 경쟁사·당사·제조업·거점 인근·매체 등급 표시를 붙인다
  score(item, kind, today)      → 중요도 점수 (오늘 핵심 브리핑·정렬에 사용)
  similar(a, b)                 → 같은 사건을 다룬 기사인지 (제목 단어 겹침)
  group_similar(items)          → 같은 사건끼리 묶어 대표 1건 + also(다른 매체 목록)
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import date, datetime

log = logging.getLogger("news_rules")

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RULES_PATH = os.path.join(BASE_DIR, "config", "news_rules.json")
SITES_PATH = os.path.join(BASE_DIR, "config", "sites.csv")

# 연예·가십 기본 목록 (config 의 noise 에 스포츠 등 추가분)
GOSSIP_PATTERN = (
    r"연예|아이돌|걸그룹|보이그룹|가수|(?<![가-힣])배우(?![다고며는기])|드라마|예능|열애|결혼식|이혼|컴백|앨범|뮤직비디오|"
    r"데뷔|소속사|방송인|유튜버|인플루언서|셀럽|스캔들|팬미팅|콘서트|오디션|"
    r"(?i:\bcelebrit|\bsinger\b|\bactor\b|\bactress\b|\bk-?pop\b|\bidol\b|girl group|boy band|\balbum\b|"
    r"\bconcert\b|reality (show|tv)|\bdating\b|\bdivorce|\bwedding\b|box office|\brapper\b|\binfluencer|"
    r"\bred carpet|\bpaparazzi|\bgossip|\bLove Is Blind\b|\bBachelor\b|\bKardashian)"
)

# 분류별 기본 점수: 안전 > HR > 업계 > 정치·경제 > 사회·문화
KIND_BASE = {"safe": 40, "hr": 32, "ind": 26, "pol": 20, "auto": 18, "soc": 10}
SAFETY_SEVERE = {"무력충돌·테러", "자연재해", "대형사고"}
MAJOR_COUNTRIES = {"KR", "US", "DE", "CN", "HU", "ID"}   # 본부·주요 공장 국가

_STOP = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "at", "by", "with", "as", "is", "are",
         "was", "were", "be", "after", "over", "amid", "from", "its", "it", "new", "says", "say", "said"}


def _compile(patterns) -> re.Pattern | None:
    pats = [p for p in (patterns or []) if isinstance(p, str) and p.strip()]
    if not pats:
        return None
    ok = []
    for p in pats:
        try:
            re.compile(p)
            ok.append(f"(?:{p})")
        except re.error as e:
            log.warning(f"[news_rules] 잘못된 정규식 무시: {p!r} ({e})")
    return re.compile("|".join(ok), re.I) if ok else None


def _name_list_regex(names) -> re.Pattern | None:
    names = [n for n in (names or []) if isinstance(n, str) and n.strip()]
    return re.compile("|".join(re.escape(n) for n in names), re.I) if names else None


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning(f"{path} 이 없어 기본 규칙만 사용합니다")
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning(f"{path} 읽기 실패 — 기본 규칙만 사용합니다: {e}")
        return {}


def _load_site_cities(aliases: dict) -> dict:
    """{ISO3: [도시 정규식 조각...]} — 나라 이름과 같은 도시(싱가포르 등)·'전국'은 제외."""
    out: dict = {}
    try:
        with open(SITES_PATH, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f, delimiter="|"))
    except FileNotFoundError:
        return out
    for r in rows:
        iso3 = (r.get("iso3") or "").strip().upper()
        city = (r.get("city") or "").strip()
        if not iso3 or not city or city in ("전국", "Singapore"):
            continue
        names = [city] + list(aliases.get(city, []))
        lst = out.setdefault(iso3, [])
        for n in names:
            if n not in lst:
                lst.append(n)
    return out


class NewsRules:
    def __init__(self, path: str = RULES_PATH):
        raw = _load_json(path)
        self.raw = raw
        self.opinion_re = _compile((raw.get("opinion") or {}).get("title_patterns"))
        self.opinion_src_re = _name_list_regex((raw.get("opinion") or {}).get("sources"))
        self.past_re = _compile((raw.get("past_event") or {}).get("title_patterns"))
        self.gossip_re = re.compile(GOSSIP_PATTERN)
        self.noise_re = _compile((raw.get("noise") or {}).get("title_patterns"))
        nn = raw.get("non_news") or {}
        self.non_news_src_re = _compile(nn.get("source_patterns"))
        self.non_news_name_re = _name_list_regex(nn.get("sources"))
        self.non_news_title_re = _compile(nn.get("title_patterns"))
        src = raw.get("sources") or {}
        self.blocked_re = _name_list_regex(src.get("blocked"))
        self.trusted_re = _name_list_regex(src.get("trusted"))
        comp = raw.get("company") or {}
        self.own_re = _compile([comp.get("own")]) if comp.get("own") else None
        self.competitors = [(name, _compile([pat])) for name, pat in (comp.get("competitors") or {}).items()
                            if not name.startswith("_") and _compile([pat])]
        self.mfg_re = _compile((raw.get("manufacturing") or {}).get("title_patterns"))
        self.hr = raw.get("hr") or {}
        aliases = {k: v for k, v in (raw.get("city_aliases") or {}).items() if not k.startswith("_") and isinstance(v, list)}
        self.site_city_re = {iso3: _name_list_regex(names) for iso3, names in _load_site_cities(aliases).items()}
        tb = raw.get("top_briefing") or {}
        self.top_count = int(tb.get("count", 5))
        self.top_max_days = int(tb.get("max_days", 1))
        self.top_per_kind = int(tb.get("per_kind", 2))

    # ------------------------------------------------------------------ 제외 판정
    def is_gossip(self, title: str) -> bool:
        t = title or ""
        return bool(self.gossip_re.search(t) or (self.noise_re and self.noise_re.search(t)))

    def is_non_news(self, title: str, source: str = "") -> bool:
        """매거진·화보·여행/라이프스타일 등 뉴스가 아닌 콘텐츠."""
        if self.non_news_title_re and self.non_news_title_re.search(title or ""):
            return True                       # 화보·영상·여행 콘텐츠는 매체와 상관없이 제외
        s = source or ""
        if not s or (self.trusted_re and self.trusted_re.search(s)):
            return False                      # 주요 매체·업계 전문지 이름은 매거진 판정에서 예외
        return bool((self.non_news_src_re and self.non_news_src_re.search(s))
                    or (self.non_news_name_re and re.search(rf"^(?:{self.non_news_name_re.pattern})\b", s, re.I)))

    def reject_reason(self, title: str, source: str = "") -> str:
        t = title or ""
        if self.blocked_re and source and self.blocked_re.search(source):
            return "차단 매체"
        if self.opinion_re and self.opinion_re.search(t):
            return "의견 기사"
        if self.opinion_src_re and source and self.opinion_src_re.search(source):
            return "의견 매체"
        if self.is_non_news(t, source):
            return "매거진·비뉴스"
        if self.past_re and self.past_re.search(t):
            return "과거 시점"
        if self.is_gossip(t):
            return "연예·스포츠"
        return ""

    # ------------------------------------------------------------------ 표시(배지)
    def competitor_of(self, title: str) -> str:
        for name, rx in self.competitors:
            if rx.search(title or ""):
                return name
        return ""

    def annotate(self, item: dict, kind: str, iso3: str = "") -> dict:
        """item 에 own / competitor / mfg / near_site / trusted 를 채운다 (원문 제목 기준)."""
        titles = " / ".join(x for x in (item.get("original_title"), item.get("title")) if x)
        item["own"] = bool(self.own_re and self.own_re.search(titles))
        item["competitor"] = self.competitor_of(titles)
        item["mfg"] = bool(self.mfg_re and self.mfg_re.search(titles))
        near = ""
        if kind in ("safe", "hr") and iso3 and self.site_city_re.get(iso3):
            m = self.site_city_re[iso3].search(titles)
            near = m.group(0) if m else ""
        item["near_site"] = near
        item["trusted"] = bool(self.trusted_re and self.trusted_re.search(item.get("source") or ""))
        for k in ("own", "mfg", "trusted"):          # 데이터 크기를 줄이려고 False 는 저장하지 않는다
            if not item[k]:
                item.pop(k)
        for k in ("competitor", "near_site"):
            if not item[k]:
                item.pop(k)
        return item

    # ------------------------------------------------------------------ 점수
    def score(self, item: dict, kind: str, today: str = "", country_code: str = "", n_countries: int = 1) -> int:
        s = KIND_BASE.get(kind, 10)
        if kind == "safe":
            s += 20 if item.get("safety_tag") in SAFETY_SEVERE else 5
        if item.get("law"):
            s += 15
        if item.get("own"):
            s += 30
        if item.get("competitor"):
            s += 12
        if item.get("mfg"):
            s += 10
        if item.get("near_site"):
            s += 15
        if item.get("trusted"):
            s += 5
        s += min(len(item.get("also") or []), 4) * 5          # 여러 매체 동시 보도
        s += min(max(n_countries - 1, 0), 3) * 6               # 여러 나라 동시 보도
        if country_code in MAJOR_COUNTRIES:
            s += 6
        n = days_between(item.get("published"), today)
        if n is not None:
            s += 10 if n <= 0 else 5 if n == 1 else 0 if n < 7 else -20
        return s


# ---------------------------------------------------------------------- 같은 사건 묶기
def _tokens(title: str) -> set:
    t = (title or "").lower()
    words = re.findall(r"[a-z0-9]+|[가-힣]+|[\u4e00-\u9fff\u3040-\u30ff]", t)
    out = set()
    for w in words:
        if w in _STOP or (w.isascii() and len(w) < 3 and not w.isdigit()):
            continue
        if re.match(r"[가-힣]{3,}$", w):          # 한국어는 조사가 붙어 있어 앞 2글자도 같이 본다
            out.add(w[:2])
        out.add(w)
    return out


def similar(a: str, b: str, threshold: float = 0.5) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return False
    inter = len(ta & tb)
    return inter >= 3 and inter / min(len(ta), len(tb)) >= threshold


def key_title(item: dict) -> str:
    return item.get("original_title") or item.get("title") or ""


def add_also(target: dict, other: dict) -> None:
    lst = target.setdefault("also", [])
    if other.get("link") == target.get("link") or any(x.get("link") == other.get("link") for x in lst):
        return
    if len(lst) < 8:
        lst.append({"source": other.get("source", ""), "link": other.get("link", "")})


# ---------------------------------------------------------------------- 날짜
def _to_date(d) -> date | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def days_between(published, today) -> int | None:
    a, b = _to_date(published), _to_date(today or datetime.now().strftime("%Y-%m-%d"))
    return None if not a or not b else (b - a).days


_RULES: NewsRules | None = None


def rules() -> NewsRules:
    global _RULES
    if _RULES is None:
        _RULES = NewsRules()
    return _RULES
