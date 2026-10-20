"""Trading Economics 지표 페이지 파서 — HTML 선택자/구조 가정은 전부 이 파일에만 둔다.

확인된 페이지 구조 (2026-10, ko.tradingeconomics.com/south-korea/inflation-cpi 등):
  1) 요약 표: 헤더 [빈칸, 실제, 이전, 최고, 최저, 날짜, 단위, 업데이트 주기, 빈칸]
              데이터 [빈칸, 2.90, 3.10, 32.50, -0.40, 1966 - 2026, 퍼센트, 매월, NSA]
  2) 캘린더 표: 행 [발표일 YYYY-MM-DD, 시각, 지표명, 기준기간(Aug / Q2 …), 실제, 이전, 컨센서스, 예측]
                 (헤더는 4칸이지만 데이터 행은 7칸 이상 — 행 단위로 읽는다)
  3) 본문 첫 문단: "2026년 9월 한국의 연간 인플레이션율은 …" / "… 2026년 2분기에 9.5% …"

가정(검증 필요): 영문 페이지(Actual/Previous)도 같은 구조라고 보고 영문 헤더도 함께 인식한다.
구조가 바뀌면 tests/fixtures 의 샘플 HTML을 새로 저장(python -m country_profile save-samples)하고
테스트를 돌려 어느 단계가 깨졌는지 확인한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

ACTUAL_HEADERS = {"실제", "actual"}
PREVIOUS_HEADERS = {"이전", "previous"}
FREQUENCY_HEADERS = {"업데이트 주기", "빈도", "frequency"}
UNIT_HEADERS = {"단위", "unit"}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
LEAD_MONTH_RE = re.compile(r"(\d{4})년\s*(\d{1,2})월")
LEAD_QUARTER_RE = re.compile(r"(\d{4})년\s*([1-4])\s*분기")
LEAD_HALF_RE = re.compile(r"(\d{4})년\s*(상|하)반기")


class ParseError(ValueError):
    pass


@dataclass
class ParsedIndicator:
    value: float
    previous: float | None
    ref_period: str          # YYYY-MM / YYYY-Qn / YYYY-Hn / YYYY
    frequency: str = ""
    unit: str = ""
    ref_source: str = ""     # 기준기간을 어디서 읽었는지(calendar/lead) — 디버깅용


def _num(text: str) -> float | None:
    if text is None:
        return None
    m = NUM_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cells(tr) -> list[str]:
    return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]


def _find_summary(soup) -> dict:
    """'실제'·'이전' 헤더가 있는 표에서 첫 번째 숫자 데이터 행을 읽는다."""
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [h.lower() for h in _cells(rows[0])]
        idx_actual = next((i for i, h in enumerate(header) if h in ACTUAL_HEADERS), None)
        idx_prev = next((i for i, h in enumerate(header) if h in PREVIOUS_HEADERS), None)
        if idx_actual is None or idx_prev is None:
            continue
        # 캘린더 표(첫 칸이 날짜)는 제외
        for tr in rows[1:]:
            cells = _cells(tr)
            if not cells or DATE_RE.match(cells[0]):
                break
            if len(cells) <= max(idx_actual, idx_prev):
                continue
            actual = _num(cells[idx_actual])
            if actual is None:
                continue
            out = {"actual": actual, "previous": _num(cells[idx_prev])}
            for key, names in (("frequency", FREQUENCY_HEADERS), ("unit", UNIT_HEADERS)):
                i = next((i for i, h in enumerate(header) if h in names), None)
                out[key] = cells[i] if i is not None and i < len(cells) else ""
            return out
    return {}


def normalize_ref(label: str, release: date) -> str | None:
    """캘린더의 기준기간 표기(Sep, Q2, H1, 2025 …)를 발표일 기준으로 연도를 붙여 정규화."""
    s = (label or "").strip()
    if not s:
        return None
    low = s.lower()[:3]
    if low in MONTHS:
        m = MONTHS[low]
        year = release.year if m <= release.month else release.year - 1
        return f"{year}-{m:02d}"
    q = re.fullmatch(r"Q([1-4])(?:\s*(\d{4}))?", s, re.IGNORECASE)
    if q:
        n = int(q.group(1))
        if q.group(2):
            return f"{q.group(2)}-Q{n}"
        year = release.year if n * 3 <= release.month else release.year - 1
        return f"{year}-Q{n}"
    h = re.fullmatch(r"H([12])", s, re.IGNORECASE)
    if h:
        n = int(h.group(1))
        year = release.year if n * 6 <= release.month else release.year - 1
        return f"{year}-H{n}"
    if re.fullmatch(r"\d{4}", s):
        return s
    return None


def _find_calendar(soup) -> dict:
    """발표일(YYYY-MM-DD)로 시작하는 행 중 '실제' 값이 있는 마지막 행."""
    latest = None
    for tr in soup.find_all("tr"):
        cells = _cells(tr)
        if len(cells) < 5 or not DATE_RE.match(cells[0]):
            continue
        actual = _num(cells[4])
        if actual is None:
            continue
        try:
            release = date.fromisoformat(cells[0])
        except ValueError:
            continue
        ref = normalize_ref(cells[3], release)
        if ref and (latest is None or release >= latest["release"]):
            latest = {"release": release, "ref": ref, "actual": actual, "previous": _num(cells[5]) if len(cells) > 5 else None}
    return latest or {}


def _ref_from_lead(soup) -> str | None:
    texts = []
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        texts.append(meta["content"])
    for p in soup.find_all(["p", "h2"], limit=6):
        texts.append(p.get_text(" ", strip=True))
    for t in texts:
        m = LEAD_MONTH_RE.search(t)
        if m and 1 <= int(m.group(2)) <= 12:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
        m = LEAD_QUARTER_RE.search(t)
        if m:
            return f"{m.group(1)}-Q{m.group(2)}"
        m = LEAD_HALF_RE.search(t)
        if m:
            return f"{m.group(1)}-H{1 if m.group(2) == '상' else 2}"
    return None


def parse_indicator_page(html: str) -> ParsedIndicator:
    soup = BeautifulSoup(html, "html.parser")
    summary = _find_summary(soup)
    calendar = _find_calendar(soup)

    value = summary.get("actual", calendar.get("actual"))
    if value is None:
        raise ParseError("최신 발표값(실제)을 찾지 못함")
    previous = summary.get("previous") if summary else calendar.get("previous")

    ref, ref_source = None, ""
    # 캘린더의 최신 실제값이 요약 표 값과 같을 때만 그 기준기간을 신뢰
    if calendar and (not summary or abs(calendar["actual"] - value) < 1e-9):
        ref, ref_source = calendar["ref"], "calendar"
    if not ref:
        ref = _ref_from_lead(soup)
        ref_source = "lead" if ref else ""
    if not ref:
        raise ParseError("최신 값의 기준 기간을 찾지 못함")

    return ParsedIndicator(value=value, previous=previous, ref_period=ref,
                           frequency=summary.get("frequency", ""), unit=summary.get("unit", ""),
                           ref_source=ref_source)
