"""news_rules.py · 핵심 브리핑 · 알림 · 주간 메일 단위 테스트 (네트워크 없음)."""
import json

import pytest

import collector as c
import news_rules as NR
try:                       # 메일 파일(notify.py·weekly_mail.py)을 아직 안 올린 저장소에서도 테스트가 돌도록
    import notify
    import weekly_mail as W
except ImportError:        # pragma: no cover
    notify = W = None
needs_mail = pytest.mark.skipif(W is None, reason="메일 기능 파일이 없음")

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
    mk = lambda t, tag="", s=80: {"title": t, "original_title": t, "lang": "en", "translated": True, "source": "Reuters", "link": t,
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


# ---------------------------------------------------------------- 알림 조건
@needs_mail
def test_notify_conditions():
    rules = {"zero_news_countries_max": 10, "translation_rate_min": 0.5}
    ok = {"zero_news_countries": [], "translation_rate": 0.95, "foreign_items": 100, "news_stopped": False}
    assert notify.find_problems("success", ok, rules) == []
    assert len(notify.find_problems("failure", ok, rules)) == 1
    bad = dict(ok, zero_news_countries=list("ABCDEFGHIJK"), translation_rate=0.2, news_stopped=True)
    assert len(notify.find_problems("success", bad, rules)) == 3


# ---------------------------------------------------------------- 주간 메일: Gemini 실패해도 발송 가능
@needs_mail
def test_weekly_gemini_fallback(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    class Bad:
        status_code = 500
        text = "error"
    monkeypatch.setattr(W.requests, "post", lambda *a, **k: Bad())
    assert W.gemini_summary([], {}, "2026-10-13", "2026-10-19", {}) is None


@needs_mail
def test_weekly_gemini_parses_json(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    payload = {"headline": "요약", "sections": [{"title": "HR·노동", "points": ["a"]}], "watch": []}

    class Ok:
        status_code = 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(payload) + "\n```"}]}}]}
    monkeypatch.setattr(W.requests, "post", lambda *a, **k: Ok())
    assert W.gemini_summary([], {}, "2026-10-13", "2026-10-19", {})["headline"] == "요약"


@needs_mail
def test_weekly_render_without_ai():
    g = {"kind": "hr", "code": "DE", "item": {"title": "IG Metall strike", "source": "Spiegel", "link": "https://x",
                                               "published": "2026-10-17", "law": True}, "days": {"2026-10-17"},
         "sources": {"Spiegel"}, "weekly": 70}
    info = {"groups": [g], "counts": {"DE": {"name": "독일", "safe": 0, "hr": 1}}, "names": {"DE": "독일"}}
    page, text = W.render(info, [g], [], None, "2026-10-13", "2026-10-19")
    assert "AI 요약" not in page and "IG Metall strike" in page and "독일" in text


# ---------------------------------------------------------------- 번역 품질
@pytest.mark.parametrize("ko, good", [
    ("2026년 11월 20일, CGIL PROCLAMA LO SCIOPERO Generale", False),
    ("IT-ul de pariuri 낚시를 좋아하는 경우: Betfair urmează să renunțe la aproximativ 300 de angajaşi", False),
    ("Płaca Minimalna w 2027 roku. Znacząca podwyżka od stycznia. 즈나미 콰티", False),
    ("Zelenskyy는 Rubio와의 통화 후 러시아 미사일 공격 조기 경보가 재개된다고 말했다", True),
    ("이탈리아 CGIL, 11월 20일 총파업 선포", True),
])
def test_translation_quality(ko, good):
    assert c.is_good_translation("src", ko) is good


def test_all_caps_prepared():
    assert c._prep_for_translation("CGIL PROCLAMA LO SCIOPERO GENERALE") == "CGIL Proclama LO Sciopero Generale"
    assert c._prep_for_translation("Michelin cuts outlook") == "Michelin cuts outlook"


def test_betting_and_arms_deals_not_safety():
    assert R.reject_reason("Spain (EDEN) vs England (STORM) odds", "FanDuel Sportsbook")
    assert c.safety_tag("Poland orders new batch of HOMAR-K missile launchers") == ""
    assert c.safety_tag("Missile strike on Kyiv kills 3") == "무력충돌·테러"


# ---------------------------------------------------------------- 분류별 최대 기사 나이
def test_max_age():
    today = "2026-10-21"
    assert R.too_old({"published": "2026-10-17"}, "safe", today)            # 안전 3일 초과
    assert not R.too_old({"published": "2026-10-18"}, "safe", today)
    assert not R.too_old({"published": "2026-10-10"}, "hr", today)          # HR 14일 이내
    assert R.too_old({"published": ""}, "pol", today) is False               # 날짜 없는 새 기사는 유지
    assert R.too_old({"published": "", "_cached": True}, "pol", today)       # 날짜 없는 이전 수집분은 제외


# ---------------------------------------------------------------- HR·안전 판정 (화면에서 문제 된 사례)
@pytest.mark.parametrize("title, ok", [
    ("UK deploys Typhoon jets to defend Saudi Arabia from Houthi missile and drone attacks", False),
    ("Israel launches air strikes on Lebanon", False),
    ("EU leaders agree new sanctions package", False),
    ("Italy's CGIL calls general strike on November 20", True),
    ("French unions call strike against pension reform", True),
    ("Betfair to cut around 300 jobs in Cluj", True),
    ("IG Metall droht mit Warnstreik bei Autozulieferern", True),
    ("미국, 이란에 공습 단행", False),
])
def test_hr_ok(title, ok):
    assert R.hr_ok(title) is ok


@pytest.mark.parametrize("title, tag", [
    ("Zelenskyy says early warning system for Russian missile attacks to resume after call with Rubio", ""),
    ("UK deploys Typhoon jets to defend Saudi Arabia from missile and drone attacks", ""),
    ("France's riot law could cost insurers $1.3bn, lessons for US airlines", ""),
    ("Russian missile strike on Kyiv kills 5, injures 20", "무력충돌·테러"),
    ("Typhoon Kong-rey slams Taiwan, thousands evacuated", "자연재해"),
])
def test_safety_actual_events(title, tag):
    assert c.safety_tag(title) == tag


def test_forecast_is_not_incident():
    assert c.safety_tag("Typhoon could hit Taiwan this weekend, forecasters warn") == "자연재해"
    assert not c.is_incident("Typhoon could hit Taiwan this weekend, forecasters warn")
    assert c.is_incident("Hurricane makes landfall on Mexico's Pacific coast, factories halted")


def test_forum_and_past_year():
    assert R.reject_reason("Thailand new visa rule: 300 comments", "Reddit")
    assert R.past_reference("Remembering the 2019 floods in Jakarta", "2026-10-21")
    assert not R.past_reference("Minimum wage rises for first time since 2019", "2026-10-21")
