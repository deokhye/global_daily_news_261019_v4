"""requests 기반 '예의 바른' HTTP 수집기.

- robots.txt 확인(호스트별 1회 캐시) → 금지 경로는 요청하지 않고 실패로 기록
- 요청 사이 무작위 대기(min_delay~max_delay초)
- User-Agent 명시
- 네트워크 오류·429·5xx 는 지수 백오프로 최대 max_retries회 재시도
- 403 또는 캡차/봇 차단 페이지는 우회하지 않고 즉시 blocked 로 기록
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

from .base import FetchResult

log = logging.getLogger(__name__)

# 봇 차단/캡차 페이지 판별용 문구 (가정: 실제 차단 페이지에서 확인 후 보강 필요)
BLOCK_PATTERNS = re.compile(
    r"captcha|cf-challenge|challenge-platform|Just a moment\.\.\.|Access denied|are you a robot",
    re.IGNORECASE,
)


class PoliteHttpFetcher:
    def __init__(self, user_agent: str, min_delay: float, max_delay: float,
                 max_retries: int, timeout: float, respect_robots: bool = True,
                 session: requests.Session | None = None, sleep=time.sleep):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "ko,en;q=0.8"})
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last_request = 0.0
        self._sleep = sleep

    # -- robots.txt -------------------------------------------------------
    def _robots_for(self, url: str):
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(f"{host}/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                    self._robots[host] = rp
                else:
                    # robots.txt 가 없으면(404 등) 표준상 전체 허용
                    self._robots[host] = None
            except requests.RequestException as e:
                log.warning("robots.txt 확인 실패(%s): %s — 요청을 보류합니다", host, e)
                self._robots[host] = "unreachable"  # type: ignore[assignment]
        return self._robots[host]

    def allowed(self, url: str) -> tuple[bool, str]:
        if not self.respect_robots:
            return True, ""
        rp = self._robots_for(url)
        if rp == "unreachable":
            return False, "robots.txt 확인 불가"
        if rp is None:
            return True, ""
        return (True, "") if rp.can_fetch(self.user_agent, url) else (False, "robots.txt 금지 경로")

    # -- 요청 --------------------------------------------------------------
    def _wait_turn(self):
        gap = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < gap:
            self._sleep(gap - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str) -> FetchResult:
        ok, why = self.allowed(url)
        if not ok:
            return FetchResult(url, False, error=why, blocked=True)

        last_err = ""
        for attempt in range(self.max_retries):
            self._wait_turn()
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = f"네트워크 오류: {e.__class__.__name__}"
            else:
                if r.status_code == 403:
                    return FetchResult(url, False, 403, error="HTTP 403 (차단)", blocked=True)
                if r.status_code == 200:
                    text = r.text
                    if BLOCK_PATTERNS.search(text[:5000]) and "<table" not in text:
                        return FetchResult(url, False, 200, error="캡차/봇 차단 페이지", blocked=True)
                    return FetchResult(url, True, 200, text=text)
                if r.status_code == 404:
                    return FetchResult(url, False, 404, error="HTTP 404 (주소 없음)")
                last_err = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    return FetchResult(url, False, r.status_code, error=last_err)
            if attempt < self.max_retries - 1:
                self._sleep(2 ** (attempt + 1))
        return FetchResult(url, False, error=f"{last_err} ({self.max_retries}회 시도)")
