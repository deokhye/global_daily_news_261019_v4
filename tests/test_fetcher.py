"""PoliteHttpFetcher: robots.txt 준수, 403·캡차 즉시 중단(우회 없음), 재시도 백오프."""
from pathlib import Path

from country_profile.fetchers.http import PoliteHttpFetcher

FIX = Path(__file__).parent / "fixtures"


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, responses, robots="User-agent: *\nDisallow: /private\n"):
        self.responses = list(responses)
        self.robots = robots
        self.calls = []
        self.headers = {}

    def get(self, url, timeout=None):
        if url.endswith("/robots.txt"):
            return Resp(200, self.robots)
        self.calls.append(url)
        return self.responses.pop(0)


def make(session, sleeps):
    return PoliteHttpFetcher("TestBot/1.0", 0, 0, 3, 5, session=session, sleep=sleeps.append)


def test_robots_disallow_is_not_requested():
    s, sleeps = FakeSession([]), []
    r = make(s, sleeps).get("https://example.test/private/page")
    assert not r.ok and r.blocked and "robots" in r.error
    assert s.calls == []


def test_403_is_blocked_without_retry():
    s, sleeps = FakeSession([Resp(403)]), []
    r = make(s, sleeps).get("https://example.test/a")
    assert r.blocked and r.status == 403 and len(s.calls) == 1


def test_captcha_page_is_blocked():
    s, sleeps = FakeSession([Resp(200, (FIX / "te_blocked.html").read_text())]), []
    r = make(s, sleeps).get("https://example.test/a")
    assert r.blocked and not r.ok


def test_retry_with_exponential_backoff():
    s, sleeps = FakeSession([Resp(503), Resp(503), Resp(200, "<table></table>")]), []
    r = make(s, sleeps).get("https://example.test/a")
    assert r.ok and len(s.calls) == 3
    assert [x for x in sleeps if x >= 1] == [2, 4]


def test_gives_up_after_max_retries():
    s, sleeps = FakeSession([Resp(500)] * 3), []
    r = make(s, sleeps).get("https://example.test/a")
    assert not r.ok and "3회 시도" in r.error
