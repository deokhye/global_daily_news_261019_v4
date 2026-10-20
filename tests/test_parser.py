"""Trading Economics 파서 단위 테스트 (저장된 HTML 샘플 기반)."""
from datetime import date
from pathlib import Path

import pytest

from country_profile.parsers.tradingeconomics import ParseError, normalize_ref, parse_indicator_page

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_monthly_page_korea_inflation():
    p = parse_indicator_page(load("te_south-korea_inflation.html"))
    assert p.value == 2.9
    assert p.previous == 3.1
    assert p.ref_period == "2026-09"
    assert p.ref_source == "calendar"
    assert p.frequency == "매월"
    assert p.unit == "퍼센트"


def test_quarterly_page_morocco_unemployment():
    p = parse_indicator_page(load("te_morocco_unemployment.html"))
    assert p.value == 9.5
    assert p.previous == 10.8
    assert p.ref_period == "2026-Q2"


def test_lead_paragraph_fallback_when_no_calendar():
    p = parse_indicator_page(load("te_no_calendar.html"))
    assert (p.value, p.previous, p.ref_period, p.ref_source) == (4.2, 4.4, "2026-08", "lead")


def test_blocked_page_raises():
    with pytest.raises(ParseError):
        parse_indicator_page(load("te_blocked.html"))


@pytest.mark.parametrize("label, release, expected", [
    ("Sep", date(2026, 10, 1), "2026-09"),
    ("Dec", date(2027, 1, 15), "2026-12"),     # 1월 발표 12월치 → 전년도
    ("Q4", date(2027, 2, 10), "2026-Q4"),
    ("Q2", date(2026, 8, 4), "2026-Q2"),
    ("H2", date(2027, 3, 1), "2026-H2"),
    ("2025", date(2026, 4, 1), "2025"),
    ("", date(2026, 1, 1), None),
])
def test_normalize_ref(label, release, expected):
    assert normalize_ref(label, release) == expected


def test_calendar_value_mismatch_uses_lead():
    # 요약 표(3.0)와 캘린더 최신 실제값(2.9)이 다르면 캘린더 기준기간을 믿지 않고 문단을 쓴다
    html = load("te_south-korea_inflation.html").replace("<td>2.90</td>", "<td>3.00</td>")
    p = parse_indicator_page(html)
    assert p.value == 3.0
    assert p.ref_source == "lead" and p.ref_period == "2026-09"
