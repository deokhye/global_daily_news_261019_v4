"""월간 지표(인플레이션·실업률) 출처 인터페이스와 Trading Economics 웹 구현.

다른 출처(예: Trading Economics 공식 API)로 바꾸려면 MonthlySource 프로토콜을 만족하는
클래스를 새로 만들고 cli.py 에서 교체하면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import Country
from ..fetchers.base import Fetcher
from ..parsers.tradingeconomics import ParseError, parse_indicator_page

INDICATORS = {
    "inflation": {"label": "인플레이션", "te_path": "inflation-cpi"},
    "unemployment": {"label": "실업률", "te_path": "unemployment-rate"},
}


@dataclass
class MonthlyResult:
    ok: bool
    source_url: str
    value: float | None = None
    previous: float | None = None
    ref_period: str = ""
    error: str = ""
    blocked: bool = False


class MonthlySource(Protocol):
    name: str

    def url_for(self, country: Country, indicator: str) -> str: ...

    def fetch(self, country: Country, indicator: str) -> MonthlyResult: ...


class TradingEconomicsWebSource:
    name = "Trading Economics"
    base = "https://ko.tradingeconomics.com"

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def url_for(self, country: Country, indicator: str) -> str:
        return f"{self.base}/{country.te_slug}/{INDICATORS[indicator]['te_path']}"

    def fetch(self, country: Country, indicator: str) -> MonthlyResult:
        url = self.url_for(country, indicator)
        res = self.fetcher.get(url)
        if not res.ok:
            return MonthlyResult(False, url, error=res.error, blocked=res.blocked)
        try:
            p = parse_indicator_page(res.text)
        except ParseError as e:
            return MonthlyResult(False, url, error=f"파싱 실패: {e}")
        return MonthlyResult(True, url, p.value, p.previous, p.ref_period)
