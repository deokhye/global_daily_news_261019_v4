"""수집기(fetcher) 인터페이스.

모든 네트워크 접근은 이 인터페이스를 통한다. 실제 HTTP 구현(http.py) 외에
테스트용 가짜 구현이나, 나중에 유료 API·다른 출처 구현으로 바꿔 끼울 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    text: str = ""
    error: str = ""
    blocked: bool = False   # 403·캡차·robots.txt 차단 — 우회하지 않고 실패로 기록

    def json(self):
        import json
        return json.loads(self.text)


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...
