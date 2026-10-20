"""collector.py / profile_overlay.py 단위 테스트 — 네트워크 없이 분류·필터·설정 로직만 확인."""
import pandas as pd
import pytest

import collector as c
from profile_overlay import ProfileContext, build_profile, load_countries


# ---------------------------------------------------------------- 현지 안전 동향 분류
@pytest.mark.parametrize("title, tag", [
    ("Strong earthquake shakes Panama", "자연재해"),
    ("Heavy rain causes flooding in Istanbul, 3 dead", "자연재해"),
    ("남부 지방 폭우로 산사태 경보", "자연재해"),
    ("Explosion at chemical plant kills 4", "대형사고"),
    ("공장 폭발 사고로 2명 부상", "대형사고"),
    ("Car bomb explosion in capital", "무력충돌·테러"),
    ("Missile strike on Medina thwarted", "무력충돌·테러"),
    ("Protesters clash with police in Nairobi", "사회불안"),
    ("Singapore battles relentless haze", "보건·환경"),
    ("earthquake drill held in schools", ""),
    ("Turkey tourism hits record", ""),
])
def test_safety_tag(title, tag):
    assert c.safety_tag(title) == tag


# ---------------------------------------------------------------- 연예·가십 제외
@pytest.mark.parametrize("title, gossip", [
    ("'이수만 걸그룹' 아이찡母 '임금 체불' 사과", True),
    ("배우 김모씨 결혼 발표", True),
    ("K-pop idol's agency fined over unpaid wages", True),
    ("중국, 최저임금 인상 발표", False),
    ("근로자들이 새 기술을 배우는 직업훈련 확대", False),
    ("Germany union strike at ports", False),
])
def test_gossip(title, gossip):
    assert c.is_gossip(title) is gossip


# ---------------------------------------------------------------- 제목 끝 언론사 정리
@pytest.mark.parametrize("raw, source, expected", [
    ('임금체불 사과…"딸과 무관" - 머니투데이 - 머니투데이', "머니투데이", '임금체불 사과…"딸과 무관"'),
    ("Brazil approves amendment - Reuters", "Reuters", "Brazil approves amendment"),
    ("Pre - Post agreement signed", "Reuters", "Pre - Post agreement signed"),
])
def test_strip_source_suffix(raw, source, expected):
    assert c.strip_source_suffix(raw, source)[0] == expected


# ---------------------------------------------------------------- 국가 관련성 필터 (config/news_filters.json)
def _country(code):
    return next(x for x in c.COUNTRIES if x["code"] == code)


def test_relevance_filters():
    pa = c.make_relevance_checker(_country("PA"))
    assert pa("Panama Canal traffic rises", "en")
    assert not pa("Panama City Beach man arrested", "en")
    assert pa("파나마 운하 통항 재개", "ko")
    cl = c.make_relevance_checker(_country("CL"))
    assert not cl("Best chile pepper recipes", "en")
    assert cl("Chile's central bank cuts rates", "en")
    us = c.make_relevance_checker(_country("US"))
    assert us("U.S. jobs report beats forecasts", "en")
    assert not us("Latin America growth slows", "en")


# ---------------------------------------------------------------- 환율
def test_cross_series_uses_same_dates_only():
    a = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-05"]))
    b = pd.Series([10.0, 20.0], index=pd.to_datetime(["2026-10-02", "2026-10-05"]))
    out = c._cross_series(a, b)
    assert list(out.values) == [20.0, 60.0]


@pytest.mark.parametrize("value, dec", [(1380, 1), (38.2, 2), (5.4, 3)])
def test_decimals(value, dec):
    assert c.decimals_for(value) == dec


# ---------------------------------------------------------------- 설정·프로필
def test_countries_from_config():
    cs = load_countries()
    assert len(cs) == 39
    kr = next(x for x in cs if x["code"] == "KR")
    assert kr["capital"] == "서울" and kr["region"] == "KR" and kr["iso3"] == "KOR"
    assert kr["hubs"][0] == "Hankook Tire Global Headquarters (Seongnam)"
    de = next(x for x in cs if x["code"] == "DE")
    names = [o["name"] for o in de["official_sources"]]
    assert "Eurofound" in names and names[-1] == "ILO NATLEX"


def test_min_wage_source_rules():
    ctx = ProfileContext()
    se = ctx.by_code["SE"]
    p = build_profile(se, {"min_wage": {"display": "법정 최저임금 없음", "note": "단체협약"}}, {}, "")
    assert p["sources"]["min_wage"] == "각국 노동법 기준"
    us = ctx.by_code["US"]
    p = build_profile(us, {"min_wage": {"display": "$7.25 / 시간", "note": "연방"}}, {}, "")
    assert p["sources"]["min_wage"] == "Department of Labor (정부 공식 발표)"
    auto = {"min_wage": "KRW 10,320 / 시급", "details": {"min_wage": {"source": "보정 파일(HR 확인)"}}}
    p = build_profile(ctx.by_code["KR"], {}, auto, "2026-10-01")
    assert p["sources"]["min_wage"] == "정부 공식 발표" and p["stats_asof"] == "2026-10-01"
