"""
weekly_mail.py — 주간 요약 메일 (매주 월요일 아침, .github/workflows/weekly-mail.yml)

  python src/weekly_mail.py              # 요약 만들어 발송
  python src/weekly_mail.py --dry-run    # 발송 없이 weekly_mail_preview.html 만 생성
  python src/weekly_mail.py --no-ai      # Gemini 없이 기사 목록만

1) 지난주(월~일) 기사를 docs/archive/ 에서 모아 기사를 모아 같은 사건을 묶고 중요도 순으로 고른다.
2) Gemini 에 '공개 기사 제목·국가·분류·매체·날짜'만 보내 주간 요약을 받는다 (사내 정보는 보내지 않음).
   Gemini 가 실패하면 AI 요약 없이 기사 목록만으로 보낸다 — 메일이 빠지는 날이 없게.
3) MAIL_TO 에게 숨은 참조로 발송한다.

환경 변수(GitHub Secrets / Variables)
  GEMINI_API_KEY · GEMINI_MODEL(선택) · MAIL_USER · MAIL_PASSWORD · MAIL_TO · SITE_URL(선택)
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_rules as NR  # noqa: E402
from mailer import parse_recipients, send_mail  # noqa: E402
from news_rules import key_title, similar  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("weekly_mail")

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ARCHIVE_DIR = os.path.join(BASE_DIR, "docs", "archive")
OPS_PATH = os.path.join(BASE_DIR, "config", "operations.json")
PREVIEW_PATH = os.path.join(BASE_DIR, "weekly_mail_preview.html")
KST = timezone(timedelta(hours=9))
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

KIND_LABEL = {"safe": "안전·재난", "hr": "HR·노동", "ind": "업계 동향", "auto": "완성차·타이어",
              "pol": "정치·경제", "soc": "사회·문화"}
# 메일 섹션: (키, 제목, 색, 포함 분류) — 건수는 config/operations.json → weekly_mail.sections
SECTIONS = [("safe", "안전·재난", "#B91C1C", ("safe",)),
            ("hr", "HR·노동", "#3730A3", ("hr",)),
            ("pol", "정치·경제·사회", "#475569", ("pol", "soc"))]
SECTION_DEFAULT = {"safe": 5, "hr": 5, "pol": 5}
# HR 섹션 우선순위: 최저임금 > 법·제도 > 그 밖의 노동 이슈 (같은 단계 안에서는 중요도 순)
MIN_WAGE_RE = re.compile(r"(?i)최저임금|최저 임금|minimum wage|living wage|salario m[ií]nimo|sal[aá]rio m[ií]nimo|"
                         r"Mindestlohn|minimálbér|płaca minimalna|minimální mzda|salariul minim|asgari ücret|"
                         r"upah minimum|lương tối thiểu|ค่าแรงขั้นต่ำ|最低工资|最低賃金|SMIC|salaire minimum|salario minimo")
LAW_HR_RE = re.compile(r"(?i)노동법|근로기준|법안|개정|시행령|입법|판결|규정|제도|의무화|\blabou?r (law|code|reform)|"
                       r"\bbill\b|\bact\b|\bamend|\bregulation|\bdecree|\blegislat|\bordinance|\bruling\b|\bcourt\b|"
                       r"\breform\b|\bnew rules?\b")


def hr_priority(g: dict) -> int:
    """0 = 최저임금, 1 = 법·제도, 2 = 그 밖."""
    it = g["item"]
    t = f"{it.get('title', '')} / {it.get('original_title', '')}"
    if MIN_WAGE_RE.search(t):
        return 0
    if it.get("law") or LAW_HR_RE.search(t):
        return 1
    return 2
HIDDEN_CODES = {"KR"}          # 데일리 뉴스·주간 메일에서 빼는 나라 (해외 사업장만)
LINK_HINT = "기사 제목을 누르면 원문으로 이동합니다"
KIND_COLOR = {"safe": ("#B91C1C", "#FEE2E2"), "hr": ("#3730A3", "#E0E7FF"), "ind": ("#9A3412", "#FFEDD5"),
              "auto": ("#9A3412", "#FFEDD5"), "pol": ("#475569", "#F1F5F9"), "soc": ("#475569", "#F1F5F9")}


def load_settings() -> dict:
    try:
        with open(OPS_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("weekly_mail", {})
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------- 1. 모으기
def load_week(end: date, days: int) -> list:
    """[(날짜, 데이터)] — end 포함 과거 days 일. 없는 날은 건너뛴다."""
    out = []
    for i in range(days - 1, -1, -1):
        d = end - timedelta(days=i)
        path = os.path.join(ARCHIVE_DIR, f"{d.isoformat()}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                out.append((d.isoformat(), json.load(f)))
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"{path} 읽기 실패(건너뜀): {e}")
    return out


def _iter_items(data: dict):
    for c in data.get("countries") or []:
        news = c.get("news") or {}
        for kind in ("safe", "pol", "soc"):
            for it in news.get(kind) or []:
                yield kind, c, it
        for t in c.get("hr_trends") or []:
            kind = "hr" if t.get("category") == "HR & LABOR" else "auto"
            for it in t.get("items") or []:
                yield kind, c, it
    for it in data.get("global_industry") or []:
        yield "ind", None, it


def _foreign_checker():
    """예전 아카이브에도 '다른 나라 기사' 거르기를 적용 (수집기 규칙 재사용). 불러오지 못하면 건너뜀."""
    try:
        import collector as C
        by_code = C.PROFILE.by_code
        return lambda it, code: bool(by_code.get(code)) and bool(C.foreign_story_reason(it, by_code[code]))
    except Exception as e:                      # noqa: BLE001
        log.warning(f"다른 나라 기사 거르기를 불러오지 못해 건너뜁니다: {e}")
        return lambda it, code: False


def collect_week(week: list, start: str, end: str) -> dict:
    """같은 사건(같은 링크 또는 비슷한 제목·같은 나라)을 묶고 국가별 건수를 센다."""
    r = NR.rules()
    groups, by_link = [], {}
    names = {}
    is_foreign = _foreign_checker()
    all_codes = set()
    for _day, data in week:
        for c in (data or {}).get("countries") or []:
            if isinstance(c, dict) and c.get("code") and c["code"] not in HIDDEN_CODES:
                all_codes.add(c["code"])
    for day, data in week:
        for kind, c, it in _iter_items(data):
            if not isinstance(it, dict) or it.get("is_fallback") or not it.get("title"):
                continue
            pub = str(it.get("published") or "")[:10] or day
            if not (start <= pub <= end):
                continue                       # 지난주 범위 밖 (이전 기사 캐시·월요일 새벽 기사 등)
            if r.reject_reason(key_title(it), it.get("source", "")):
                continue                       # 옛 아카이브에도 새 제외 규칙 적용
            code = (c or {}).get("code", "")
            if code in HIDDEN_CODES:
                continue                       # 한국(국내)은 대상 아님 — 예전 아카이브에도 적용
            if code and is_foreign(it, code):
                continue                       # 현지 언론의 다른 나라 기사 — 예전 아카이브에도 적용
            if c:
                names[code] = c.get("name_kr", code)
            link = it.get("link") or ""
            g = by_link.get(link) if link else None
            if g is None:
                g = next((g for g in groups if g["kind"] == kind and g["code"] == code
                          and similar(key_title(g["item"]), key_title(it))), None)
            if g is None:
                item = dict(it)
                if "score" not in item:        # 이 기능 이전 아카이브: 지금 규칙으로 점수 계산
                    r.annotate(item, kind, "")
                    item["score"] = r.score(item, kind, pub or day, code)
                g = {"kind": kind, "code": code, "item": item, "days": set(), "sources": set()}
                groups.append(g)
            g["days"].add(day)
            g["sources"].add(it.get("source", ""))
            g["sources"].update(a.get("source", "") for a in it.get("also") or [])
            if link:
                by_link[link] = g
    for g in groups:
        # 여러 날 이어진 이슈·여러 매체 보도는 가산 (날짜 가산점은 주간 비교에서 빼고 다시 계산)
        base = (g["item"].get("score") or 0)
        g["weekly"] = base + min(len(g["days"]) - 1, 4) * 6 + min(len([s for s in g["sources"] if s]) - 1, 4) * 3
    counts = {}
    for g in groups:
        if g["code"] and g["kind"] in ("safe", "hr"):
            row = counts.setdefault(g["code"], {"name": names.get(g["code"], g["code"]), "safe": 0, "hr": 0})
            row[g["kind"]] += 1
    return {"groups": groups, "counts": counts, "names": names, "n_countries": len(all_codes)}


def pick_top(groups: list, top_n: int, per_kind_max: int) -> list:
    out, per_kind = [], {}
    for g in sorted(groups, key=lambda g: (g["weekly"], str(g["item"].get("published") or "")), reverse=True):
        if len(out) >= top_n:
            break
        if is_industry(g):          # 업계 동향·경쟁사는 하단 '업계·경쟁사 동향'으로
            continue
        if per_kind.get(g["kind"], 0) >= per_kind_max:
            continue
        per_kind[g["kind"]] = per_kind.get(g["kind"], 0) + 1
        out.append(g)
    return out


_HANGUL = re.compile(r"[가-힣]")


def is_korean(text: str) -> bool:
    """제목이 '대부분 한국어'인지 (한글 2자 이상 · 글자 중 한글 40% 이상)."""
    t = text or ""
    h = len(_HANGUL.findall(t))
    other = sum(1 for ch in t if ch.isalpha() and not _HANGUL.match(ch))
    return h >= 2 and h / max(1, h + other) >= 0.4


class _Translator:
    """메일에 넣을 기사 중 번역이 안 된 제목을 발송 직전에 다시 번역 (수집기 번역 기능 재사용)."""
    def __init__(self):
        self.ok = None
        self.cache = {}

    def _load(self):
        if self.ok is None:
            try:
                import collector as C
                self.C = C
                self.cache = C.load_translation_cache() or {}
                self.ok = True
            except Exception as e:                       # noqa: BLE001
                log.warning(f"번역기를 불러오지 못해 번역 안 된 기사는 메일에서 뺍니다: {e}")
                self.ok = False
        return self.ok

    def korean(self, it: dict) -> bool:
        """제목이 한국어면 True. 아니면 번역을 시도해 성공하면 제목을 바꾸고 True, 실패하면 False."""
        if is_korean(it.get("title")):
            return True
        if not self._load():
            return False
        src = it.get("original_title") or it.get("title") or ""
        ko = self.cache.get(src) or ""
        if not (ko and self.C.is_good_translation(src, ko)):
            try:
                self.C._TEXT_LANG[src] = it.get("lang") or "auto"
                ko = self.C._translate_one(src) or self.C._translate_split(src)
            except Exception as e:                       # noqa: BLE001
                log.warning(f"번역 실패: {e}")
                ko = ""
        if ko and is_korean(ko):
            self.cache[src] = ko
            it["original_title"], it["title"], it["translated"] = src, ko, True
            log.info(f"메일용 번역: {src[:40]} → {ko[:40]}")
            return True
        log.info(f"번역이 안 돼 메일에서 제외: {src[:60]}")
        return False


def pick_sections(groups: list, counts: dict) -> list:
    """섹션별(안전·재난 / HR·노동 / 정치·경제) 중요도 상위 N건. 업계·경쟁사 기사는 넣지 않는다.
    제목이 번역되지 않은 기사는 발송 직전에 다시 번역하고, 그래도 안 되면 빼고 다음 기사로 채운다."""
    tr = _Translator()
    out = []
    for key, _title, _color, kinds in SECTIONS:
        n = int((counts or {}).get(key, SECTION_DEFAULT[key]))
        lst = [g for g in groups if g["kind"] in kinds and g.get("code") and not is_industry(g)]
        lst.sort(key=lambda g: (g["weekly"], str(g["item"].get("published") or "")), reverse=True)
        if key == "hr":                      # 최저임금·법 제도 우선 (안정 정렬이라 같은 단계는 중요도 순 유지)
            lst.sort(key=hr_priority)
        picked = 0
        for g in lst:
            if picked >= n:
                break
            if not tr.korean(g["item"]):
                continue
            g["section"] = key
            out.append(g)
            picked += 1
    return out


def is_competitor(g: dict) -> bool:
    it = g["item"]
    return bool(it.get("competitor") or it.get("tag") == "경쟁사" or NR.rules().competitor_of(key_title(it)))


def is_industry(g: dict) -> bool:
    """업계 동향(글로벌 산업·완성차·타이어) 또는 경쟁사 기사."""
    return g["kind"] in ("ind", "auto") or is_competitor(g)


def pick_competitors(groups: list, limit: int) -> list:
    """하단 '업계·경쟁사 동향': 경쟁사·업계 기사를 중요도 순으로 (같은 사건은 이미 1건으로 묶여 있음)."""
    comp = [g for g in groups if is_industry(g)]
    comp.sort(key=lambda g: (g["weekly"], str(g["item"].get("published") or "")), reverse=True)
    return comp[:limit]


# ---------------------------------------------------------------------- 2. Gemini
def gemini_summary(groups: list, names: dict, start: str, end: str, settings: dict) -> dict | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log.info("GEMINI_API_KEY 가 없어 AI 요약 칸 없이 보냅니다 (정상)")
        return None
    model = os.environ.get("GEMINI_MODEL", "").strip() or settings.get("gemini_model") or "gemini-2.5-flash"
    lines = []
    for g in sorted(groups, key=lambda g: -g["weekly"])[: int(settings.get("gemini_max_items", 40))]:
        it = g["item"]
        lines.append(f"- [{names.get(g['code'], '글로벌') if g['code'] else '글로벌'}] [{KIND_LABEL.get(g['kind'], '')}] "
                     f"{it.get('title', '')} ({it.get('source', '')}, {str(it.get('published') or '')[:10]})")
    prompt = (
        f"다음은 {start}~{end} 한 주 동안 한국타이어 해외 사업장 관련 공개 뉴스 중 중요도가 높은 기사 제목 목록입니다.\n"
        "한국타이어 Global HR팀이 1분 안에 읽을 주간 주요 이슈 요약을 한국어로 써 주세요.\n"
        "규칙: 목록에 있는 내용만 쓰고 추측하거나 숫자를 지어내지 마세요. 나라 이름을 꼭 밝히세요. 문장은 짧게.\n"
        "아래 JSON 형식으로만 답하세요:\n"
        '{"headline": "이번 주를 한 문장으로", '
        '"sections": [{"title": "안전·재난 | HR·노동 | 업계·경쟁사 | 정치·경제 중 하나", "points": ["1~2문장", "..."]}], '
        '"watch": ["다음 주 지켜볼 점 1~2개"]}\n'
        "sections 는 기사가 있는 분류만, 분류마다 points 1~3개.\n\n기사 목록:\n" + "\n".join(lines)
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}}
    for attempt in range(2):
        try:
            r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=60,
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            if r.status_code != 200:
                log.warning(f"Gemini 응답 {r.status_code} ({attempt + 1}/2): {r.text[:200]}")
                continue
            text = "".join(p.get("text", "") for p in
                           (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []))
            text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
            out = json.loads(text)
            if isinstance(out, dict) and (out.get("headline") or out.get("sections")):
                log.info(f"Gemini 요약 완료 ({model})")
                return out
            log.warning("Gemini 응답 형식이 예상과 다릅니다")
        except Exception as e:  # noqa: BLE001
            log.warning(f"Gemini 호출 실패 ({attempt + 1}/2): {e.__class__.__name__}: {str(e)[:160]}")
    return None


# ---------------------------------------------------------------------- 3. 메일 본문
def _e(s) -> str:
    return html.escape(str(s or ""))


def _badge(kind: str, text: str = "") -> str:
    fg, bg = KIND_COLOR.get(kind, ("#475569", "#F1F5F9"))
    return (f"<span style='display:inline-block;font-size:11px;font-weight:700;color:{fg};background:{bg};"
            f"border-radius:5px;padding:1px 7px'>{_e(text or KIND_LABEL.get(kind, ''))}</span>")


def render(week_info: dict, top: list, comps: list, ai: dict | None, start: str, end: str,
           site_url: str = "", label: str = "지난주") -> tuple:
    names = week_info["names"]
    counts = week_info["counts"]
    n_countries = week_info.get("n_countries") or len(names)
    safe_total = sum(r["safe"] for r in counts.values())
    hr_total = sum(r["hr"] for r in counts.values())
    safe_cty = sum(1 for r in counts.values() if r["safe"])

    def ctry(code):
        return names.get(code, code) if code else "글로벌"

    # AI 요약
    if ai:
        secs = "".join(
            f"<tr><td style='padding:10px 0 2px;font-size:13px;font-weight:800;color:#9A3412'>{_e(s.get('title'))}</td></tr>"
            + "".join(f"<tr><td style='padding:3px 0 3px 12px;font-size:14px;line-height:1.6;color:#1E293B'>• {_e(p)}</td></tr>"
                      for p in (s.get("points") or [])[:3])
            for s in (ai.get("sections") or [])[:5])
        watch = "".join(f"<li style='margin:3px 0'>{_e(w)}</li>" for w in (ai.get("watch") or [])[:2])
        ai_html = (
            "<tr><td style='padding:8px 24px'><table width='100%' cellpadding='0' cellspacing='0' "
            "style='background:#FFF7ED;border:1px solid #FED7AA;border-radius:20px'><tr><td style='padding:18px 20px'>"
            "<div style='font-size:11px;font-weight:700;color:#9A3412;letter-spacing:.02em'>AI 요약 · 원문 확인 필요</div>"
            f"<div style='font-size:17px;font-weight:900;color:#1E293B;margin-top:6px;line-height:1.45'>{_e(ai.get('headline'))}</div>"
            f"<table width='100%' cellpadding='0' cellspacing='0'>{secs}</table>"
            + (f"<div style='font-size:13px;font-weight:800;color:#1E293B;margin-top:12px'>다음 주 지켜볼 점</div>"
               f"<ul style='margin:4px 0 0;padding-left:18px;font-size:14px;color:#334155;line-height:1.6'>{watch}</ul>" if watch else "")
            + "</td></tr></table></td></tr>")
    else:
        ai_html = ""          # Gemini 를 쓰지 않으면 요약 칸 없이 바로 지표·Top 10 으로 시작

    # ---- 사이트의 '오늘 핵심 브리핑' 카드와 같은 모양 (색·배지·번호 동그라미)
    def badge(text, fg, bg, extra=""):
        return (f"<span style='display:inline-block;font-size:12px;font-weight:700;color:{fg};background:{bg};"
                f"border-radius:6px;padding:1px 8px;margin:0 6px 4px 0;{extra}'>{_e(text)}</span>")

    def kind_badge(g):
        it = g["item"]
        if g["kind"] == "safe" and it.get("safety_tag"):
            red = it["safety_tag"] in NR.SAFETY_SEVERE
            return badge(it["safety_tag"], "#B91C1C" if red else "#92400E", "#FEE2E2" if red else "#FEF3C7")
        fg, bg = KIND_COLOR.get(g["kind"], ("#475569", "#F1F5F9"))
        return badge(KIND_LABEL.get(g["kind"], ""), fg, bg)

    def extra_badges(it):
        out = ""
        if it.get("law"):
            out += badge("법·제도", "#3730A3", "#E0E7FF")
        if it.get("own"):
            out += badge("한국타이어", "#FFFFFF", "#C2410C")
        if it.get("competitor"):
            out += badge(f"경쟁사 · {it['competitor']}", "#7C2D12", "#FED7AA")
        if it.get("near_site"):
            out += badge("거점 인근", "#1E40AF", "#DBEAFE")
        return out

    def one_badge(g):
        """사이트와 같이 구분 배지는 1개만: 경쟁사 기사면 '경쟁사', 아니면 분류(안전 유형·HR 등)."""
        if g["item"].get("competitor"):
            return badge("경쟁사", "#7C2D12", "#FED7AA")
        return kind_badge(g)

    def meta(it, n_src=1, days=1):
        tr = ("<span style='display:inline-block;border:1px solid #E2E8F0;border-radius:4px;padding:0 6px;"
              "font-size:11px;color:#64748B;margin-left:6px'>자동번역</span>") if it.get("translated") else ""
        return (f"<div style='font-size:12px;color:#64748B;margin-top:3px'><b style='color:#475569'>{_e(it.get('source'))}</b>"
                + (f"&nbsp; 외 {n_src - 1}개 매체" if n_src > 1 else "")
                + f"&nbsp; {_e(str(it.get('published') or '')[5:10].replace('-', '.'))}"
                + (f"&nbsp;·&nbsp;{days}일 연속 보도" if days > 1 else "") + tr + "</div>")

    def card(title_html, note, body):
        return ("<tr><td style='padding:8px 24px'><table width='100%' cellpadding='0' cellspacing='0' "
                "style='background:#FFFFFF;border:1px solid #FED7AA;border-radius:20px'><tr><td style='padding:22px 24px 10px'>"
                "<table width='100%' cellpadding='0' cellspacing='0'><tr>"
                f"<td style='font-size:18px;font-weight:900;color:#1E293B'>{title_html}</td>"
                f"<td align='right' style='font-size:12px;color:#64748B'>{note}</td></tr></table>"
                f"{body}</td></tr></table></td></tr>")

    def row(i, g, last):
        it = g["item"]
        n_src = len([s for s in g["sources"] if s])
        border = "" if last else "border-bottom:1px solid #F1F5F9;"
        if g["kind"] == "safe":                    # 안전: 유형(자연재해·대형사고…)
            tag = kind_badge(g)
        elif g["kind"] == "hr":                    # HR: 최저임금 / 법·제도만 표시
            pr = hr_priority(g)
            tag = badge("최저임금", "#9A3412", "#FFEDD5") if pr == 0 else (badge("법·제도", "#3730A3", "#E0E7FF") if pr == 1 else "")
        else:                                      # 정치·경제 / 사회·문화 구분
            tag = badge(KIND_LABEL.get(g["kind"], ""), *KIND_COLOR.get(g["kind"], ("#475569", "#F1F5F9")))
        return (
            f"<tr><td style='padding:14px 0;{border}vertical-align:top;width:40px'>"
            f"<div style='width:28px;height:28px;border-radius:14px;background:#FFF7ED;"
            f"color:#9A3412;font-weight:900;font-size:14px;text-align:center;line-height:28px'>{i}</div></td>"
            f"<td style='padding:14px 0;{border}'>"
            f"<div style='font-size:14px;color:#1E293B'>{tag}"
            f"<b style='font-size:14px'>{_e(ctry(g['code']) if g['code'] else '글로벌')}</b></div>"
            f"<a href='{_e(it.get('link'))}' style='display:block;font-size:16px;font-weight:700;color:#1E293B;"
            f"text-decoration:none;line-height:1.5;margin-top:2px'>{_e(it.get('title'))}</a>"
            + meta(it, n_src, len(g["days"])) + "</td></tr>")

    # 분류별 섹션 카드 (안전·재난 / HR·노동 / 정치·경제) — 안내 문구는 첫 카드 오른쪽 위에만
    top_html, first_card = "", True
    for key, title, color, kinds in SECTIONS:
        lst = [g for g in top if g.get("section", g["kind"]) == key or (not g.get("section") and g["kind"] in kinds)]
        if not lst:
            continue
        body = "".join(row(i, g, i == len(lst)) for i, g in enumerate(lst, 1))
        top_html += card(f"<span style='color:{color}'>&#9632;</span>&nbsp;{title}",
                         LINK_HINT if first_card else "",
                         f"<table width='100%' cellpadding='0' cellspacing='0' style='margin-top:4px'>{body}</table>")
        first_card = False
    comp_block = ""          # 업계·경쟁사 동향은 메일에서 뺌 (사이트에서 확인)

    button = (f"<tr><td align='center' style='padding:22px 28px 6px'><a href='{_e(site_url)}' "
              "style='display:inline-block;background:#C2410C;color:#fff;font-weight:700;font-size:14px;"
              "text-decoration:none;border-radius:10px;padding:11px 22px'>Global Daily News 바로가기</a></td></tr>") if site_url else ""

    page = (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width'>"
        f"<title>Global Daily News 주간 브리핑</title></head>"
        "<body style='margin:0;padding:0;background:#F4F5F7;font-family:-apple-system,\"Malgun Gothic\",\"Apple SD Gothic Neo\",sans-serif'>"
        "<table width='100%' cellpadding='0' cellspacing='0' style='background:#F4F5F7'><tr><td align='center' style='padding:24px 12px'>"
        "<table width='100%' cellpadding='0' cellspacing='0' style='max-width:720px;background:#F4F5F7'>"
        "<tr><td style='padding:10px 28px 10px'>"
        "<div style='font-size:12px;font-weight:700;color:#C2410C'>Global Daily News · 주간 요약</div>"
        f"<div style='font-size:24px;font-weight:900;color:#1E293B;margin-top:4px;letter-spacing:-0.02em'>주간 브리핑</div>"
        f"<div style='font-size:13px;color:#64748B;margin-top:2px'>{_e(start.replace('-', '.'))} ~ {_e(end.replace('-', '.'))} · 해외 {n_countries}개국</div>"
        "</td></tr>"
        + ai_html + top_html + comp_block + button +
        "<tr><td style='padding:18px 28px 24px;font-size:11px;color:#94A3B8;line-height:1.6'>"
        "해외 기사 제목은 자동 번역되어 표현이 어색할 수 있습니다. 정확한 내용은 원문을 확인해 주세요.<br>"
        "매주 월요일 아침 자동으로 발송됩니다. 수신자 추가·변경은 G.HR팀 이덕혜에게 요청해 주세요.</td></tr>"
        "<tr><td align='center' style='padding:6px 28px 28px;border-top:1px solid #E2E8F0;font-size:13px;"
        "font-weight:700;color:#475569'>Hankook Tire &amp; Technology Global HR팀</td></tr>"
        "</table></td></tr></table></body></html>")

    text = [f"Global Daily News 주간 브리핑 ({start} ~ {end})", ""]
    if ai:
        text += [f"[AI 요약 · 원문 확인 필요] {ai.get('headline', '')}"]
        for s in ai.get("sections") or []:
            text += [f"■ {s.get('title', '')}"] + [f"  - {p}" for p in s.get("points") or []]
        text.append("")
    text.append("※ 기사 제목(링크)을 누르면 원문 기사로 이동합니다.")
    text.append("")
    for key, title, _c, kinds in SECTIONS:
        lst = [g for g in top if g.get("section", g["kind"]) == key or (not g.get("section") and g["kind"] in kinds)]
        if lst:
            text += ["", f"■ {title}"]
            text += [f"{i}. [{ctry(g['code'])}] {g['item'].get('title', '')}\n   {g['item'].get('link', '')}"
                     for i, g in enumerate(lst, 1)]
    if site_url:
        text += ["", f"Global Daily News 바로가기: {site_url}"]
    text += ["", "Hankook Tire & Technology Global HR팀"]
    return page, "\n".join(text)


# ---------------------------------------------------------------------- 실행
def last_week(today: date) -> tuple:
    """오늘 기준 '지난주' 월요일~일요일. 월요일에 보내면 바로 전날까지의 한 주."""
    end = today - timedelta(days=today.weekday() + 1)      # 직전 일요일
    return end - timedelta(days=6), end


def build(end: date, use_ai: bool = True, ai_override: dict | None = None, today: date | None = None) -> tuple:
    """end = 지난주 일요일. 월요일 새벽 아카이브에도 일요일 기사가 있어 end+1 일치까지 읽고 기사 날짜로 거른다.
    지난주 기록이 아직 없으면(수집을 시작한 지 얼마 안 된 경우 등) 오늘까지 '최근 7일'로 대신 만든다."""
    st = load_settings()
    days = int(st.get("days", 7))
    label = "지난주"
    start_d = end - timedelta(days=days - 1)
    week = load_week(end + timedelta(days=1), days + 1)
    start, end_s = start_d.isoformat(), end.isoformat()
    info = collect_week(week, start, end_s) if week else {"groups": []}
    if not info["groups"]:
        today = today or datetime.now(KST).date()
        log.warning(f"지난주({start}~{end_s}) 기사가 없어 최근 7일(~{today.isoformat()})로 대신 만듭니다")
        end, label = today, "최근 7일"
        start_d = end - timedelta(days=days - 1)
        week = load_week(end, days)
        start, end_s = start_d.isoformat(), end.isoformat()
        if not week:
            raise RuntimeError(f"최근 7일({start}~{end_s}) 아카이브도 없습니다. 매일 수집(Daily Global Brief Update)이 "
                               "한 번 이상 돌아 docs/archive/ 에 파일이 생긴 뒤 다시 실행해 주세요.")
        info = collect_week(week, start, end_s)
    top = pick_sections(info["groups"], st.get("sections") or SECTION_DEFAULT)
    comps = []
    ai = ai_override if ai_override is not None else (gemini_summary(info["groups"], info["names"], start, end_s, st) if use_ai else None)
    site = os.environ.get("SITE_URL", "").strip()
    page, text = render(info, top, comps, ai, start, end_s, site, label)
    subject = (st.get("subject") or "[Global Daily News] 지난주 주요 이슈 ({start} ~ {end})").replace("지난주", label).format(
        start=start.replace("-", "."), end=end_s.replace("-", "."))
    log.info(f"주간 요약: 아카이브 {len(week)}일, 이슈 {len(info['groups'])}건 → 안전 {sum(g['kind'] == 'safe' for g in top)} · HR {sum(g['kind'] == 'hr' for g in top)} · "
             f"정치·경제·사회 {sum(g['kind'] in ('pol', 'soc') for g in top)}건, "
             f"AI 요약 {'있음' if ai else '없음'}")
    return subject, page, text


def _hint(err: Exception) -> str:
    """발송 오류를 사람이 바로 이해할 수 있는 안내로 바꾼다."""
    m = str(err)
    if "SmtpClientAuthentication is disabled" in m:
        return "회사 메일 계정에 SMTP 인증(SMTP AUTH)이 꺼져 있습니다 → IT에 해당 계정의 SMTP AUTH 허용을 요청하세요."
    if "security defaults" in m or "5.7.139" in m:
        return "회사 보안 정책(MFA·보안 기본값)으로 로그인이 막혔습니다 → 앱 암호 또는 MFA 없는 발송 전용 계정이 필요합니다."
    if "5.7.3" in m and "outlook" in m.lower():
        return ("Outlook 로그인 거부(5.7.3): ① 비밀번호 오류 ② 계정에 MFA(추가 인증)/SSO 적용 "
                "③ 회사가 SMTP 인증(SMTP AUTH)을 막음 중 하나입니다 → 비밀번호가 맞다면 IT에 SMTP AUTH 허용·앱 암호를 요청하세요.")
    if "535" in m or "Authentication" in m or "5.7.3" in m or "5.7.8" in m:
        return "로그인 실패: MAIL_USER(메일 주소 전체)와 MAIL_PASSWORD 값을 확인하세요. Gmail이면 앱 비밀번호 16자리를 넣어야 합니다."
    if "5.7.57" in m or "5.7.60" in m:
        return "로그인한 계정으로 보낼 권한이 없습니다 → MAIL_USER와 실제 로그인 계정이 같은지 확인하세요."
    if "설정되지 않았습니다" in m or "받는 사람이 없습니다" in m:
        return "Settings → Secrets and variables → Actions → Repository secrets 에 MAIL_USER, MAIL_PASSWORD, MAIL_TO 가 있는지 확인하세요."
    if "timed out" in m or "Connection" in m or "getaddrinfo" in m:
        return "메일 서버 연결 실패: MAIL_SMTP_HOST / MAIL_SMTP_PORT 값을 확인하세요 (Outlook: smtp.office365.com / 587)."
    return "아래 오류 원문을 확인하세요."


def _report(title: str, lines: list) -> None:
    """실행 요약 화면(Summary)과 오류 표시(Annotations)에 결과를 남긴다."""
    text = " / ".join(x for x in lines if x)
    print(f"::{'notice' if title.startswith('✅') else 'error'} title={title}::{text}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"### {title}\n\n" + "\n".join(f"- {x}" for x in lines if x) + "\n\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 미리보기 파일만 생성")
    ap.add_argument("--no-ai", action="store_true", help="Gemini 요약 없이")
    ap.add_argument("--end", default="", help="요약할 주의 마지막 날(일요일) YYYY-MM-DD (기본: 지난주 일요일)")
    args = ap.parse_args()
    end = date.fromisoformat(args.end) if args.end else last_week(datetime.now(KST).date())[1]

    try:
        subject, page, text = build(end, use_ai=not args.no_ai)
    except Exception as e:
        log.exception("주간 메일 내용 만들기 실패")
        _report("❌ 주간 메일 내용 만들기 실패", [f"오류: {type(e).__name__}: {e}"])
        return 1
    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    log.info(f"미리보기 저장: {PREVIEW_PATH}")
    if args.dry_run:
        _report("✅ 주간 메일 미리보기 생성", [f"제목: {subject}", "실행 화면 맨 아래 Artifacts → weekly-mail-preview 에서 내려받으세요."])
        return 0
    recipients = parse_recipients(os.environ.get("MAIL_TO", ""))
    user = os.environ.get("MAIL_USER", "").strip()
    host = os.environ.get("MAIL_SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = os.environ.get("MAIL_SMTP_PORT", "").strip() or "465"
    log.info(f"발송 설정: 서버 {host}:{port}, 보내는 계정 {user[:3] + '***' + user[user.find('@'):] if '@' in user else '(없음)'}, "
             f"받는 사람 {len(recipients)}명")
    try:
        send_mail(subject, page, text, recipients)
    except Exception as e:
        log.error(f"주간 메일 발송 실패: {type(e).__name__}: {e}")
        _report("❌ 주간 메일 발송 실패", [_hint(e), f"서버: {host}:{port} · 받는 사람 {len(recipients)}명",
                                          f"오류 원문: {type(e).__name__}: {e}"])
        return 1
    _report("✅ 주간 메일 발송 완료", [f"제목: {subject}", f"받는 사람 {len(recipients)}명 (숨은 참조) + 보내는 계정 보낸편지함"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
