"""news_rules.py · 핵심 브리핑 단위 테스트 (네트워크 없음)."""
import pytest

import collector as c
import news_rules as NR

R = NR.rules()


# ---------------------------------------------------------------- 제외 규칙: 잘 빼는지 + 잘못 빼지 않는지
@pytest.mark.parametrize("title, source, reason", [
    ("[사설] 재난 대응 체계 다시 점검해야", "한국경제", "의견 기사"),
    ("[기자수첩] 최저임금 논쟁의 함정", "매일경제", "의견 기사"),
    ("Opinion | Germany's budget is a mistake", "The Guardian", "의견 기사"),
    ("The minimum wage op-ed nobody wanted", "WSJ", "의견 기사"),
    ("25th anniversary of the 1999 earthquake remembered", "Daily Sabah", "과거 시점"),
    ("세월호 10주년 추모식 열려", "연합뉴스", "과거 시점"),
    ("Ten years ago today: general strike", "History", "과거 시점"),
    ("Mexico football team wins World Cup qualifier", "ESPN", "연예·스포츠"),
    ("Goodyear brings superspeedway tire for NASCAR race", "NASCAR.com", "연예·스포츠"),
    ("Global tire market forecast to 2035", "IndexBox", "차단 매체"),
    ("XYZ launches new tire line", "PR Newswire", "차단 매체"),
])
def test_rejects(title, source, reason):
    assert R.reject_reason(title, source) == reason


@pytest.mark.parametrize("title, source", [
    ("Strong earthquake hits Istanbul, buildings damaged", "Reuters"),
    ("물가 1년 전보다 3% 상승", "연합뉴스"),            # '년 전' 이 들어가도 과거 회고가 아님
    ("Germany union strike at auto plants", "Reuters"),
    ("IG Metall calls warning strike", "Spiegel"),
    ("Michelin cuts 2026 outlook as tire demand slows", "Reuters"),
    ("정부, 내년 예산안 국회 제출", "한겨레"),
    ("Hungary raises minimum wage by 9%", "Portfolio"),
])
def test_keeps_real_news(title, source):
    assert R.reject_reason(title, source) == ""


# ---------------------------------------------------------------- 배지·점수
def test_annotate_badges():
    it = R.annotate({"title": "Michelin cuts outlook", "source": "Reuters"}, "ind")
    assert it["competitor"] == "Michelin" and it.get("trusted") and it.get("mfg") is None
    it = R.annotate({"title": "Hankook Tire expands plant", "source": "Tire Business"}, "ind")
    assert it.get("own") and it.get("mfg")
    it = R.annotate({"title": "Flooding near Rácalmás forces evacuation", "source": "Reuters"}, "safe", "HUN")
    assert it["near_site"] == "Rácalmás"
    it = R.annotate({"title": "Budapest budget debate", "source": "x"}, "pol", "HUN")
    assert "near_site" not in it                        # 거점 인근은 안전·HR 에만


def test_score_prefers_manufacturing_and_sites():
    a = R.annotate({"title": "Germany automaker exports jump", "source": "Reuters", "published": "2026-10-21"}, "pol")
    b = R.annotate({"title": "Germany schools reform", "source": "Local", "published": "2026-10-21"}, "pol")
    assert R.score(a, "pol", "2026-10-21") > R.score(b, "pol", "2026-10-21")
    old = dict(a, published="2026-10-01")
    assert R.score(old, "pol", "2026-10-21") < R.score(a, "pol", "2026-10-21")


# ---------------------------------------------------------------- 같은 사건 묶기
def test_similar_titles():
    assert NR.similar("Earthquake shakes Istanbul, buildings damaged", "Istanbul quake: buildings damaged after strong earthquake")
    assert not NR.similar("Germany raises minimum wage", "Germany budget approved by parliament")


def test_pick_groups_same_story():
    seen = set()
    cands = [{"title": "Germany union strike at auto plants", "source": "Reuters", "link": "a"},
             {"title": "Germany union strike hits auto plants nationwide", "source": "AP News", "link": "b"},
             {"title": "Germany parliament passes labor law", "source": "DW", "link": "c"}]
    out = c._pick(cands, "en", 4, seen)
    assert len(out) == 2 and out[0]["also"] == [{"source": "AP News", "link": "b"}]


def test_top_briefing_merges_and_separates():
    de = next(x for x in c.COUNTRIES if x["code"] == "DE")
    hu = next(x for x in c.COUNTRIES if x["code"] == "HU")
    tr = next(x for x in c.COUNTRIES if x["code"] == "TR")
    today = "2026-10-21"
    mk = lambda t, tag="", s=80: {"title": t, "original_title": t, "lang": "en", "source": "Reuters", "link": t,
                                  "published": today, "safety_tag": tag, "score": s}
    countries = [
        {"code": "TR", "news": {"safe": [mk("Strong earthquake hits Istanbul", "자연재해", 95),
                                         mk("Earthquake shakes Istanbul residents flee", "자연재해", 90)]}, "hr_trends": []},
        {"code": "DE", "news": {}, "hr_trends": [{"category": "HR & LABOR", "items": [mk("Germany union strike at auto plants", s=70)]}]},
        {"code": "HU", "news": {}, "hr_trends": [{"category": "HR & LABOR", "items": [mk("Hungary union strike at auto plants", s=68)]}]},
    ]
    assert de and hu and tr
    top = c.build_top_briefing(countries, [], today)
    kinds = [(t["kind"], t["countries"]) for t in top]
    assert kinds.count(("safe", ["TR"])) == 1            # 같은 나라 같은 재난은 1건
    assert ("hr", ["DE"]) in kinds and ("hr", ["HU"]) in kinds   # 다른 나라 비슷한 제목은 따로
