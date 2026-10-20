"""월간 수집: 인플레이션·실업률 (매월 1일)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from . import storage
from .config import Country, Paths, Settings
from .sources.monthly import INDICATORS, MonthlySource

log = logging.getLogger(__name__)

STATUS_OK = "정상"
STATUS_NO_UPDATE = "갱신 없음"
STATUS_STALE = "미갱신"


@dataclass
class RunSummary:
    title: str
    ok: list[str] = field(default_factory=list)        # "KOR 인플레이션"
    failed: list[str] = field(default_factory=list)    # "KOR 인플레이션: 사유"
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blocked_count: int = 0


def snapshot_date_for(today: date) -> date:
    return today.replace(day=1)


def _prev_row(hist, iso3: str, indicator: str, snapshot: str):
    if hist.empty:
        return None
    sub = hist[(hist.iso3 == iso3) & (hist.indicator == indicator) & (hist.snapshot_date < snapshot)]
    if sub.empty:
        return None
    return sub.sort_values("snapshot_date").iloc[-1]


def run_monthly(countries: list[Country], settings: Settings, paths: Paths,
                source: MonthlySource, today: date) -> RunSummary:
    snapshot = snapshot_date_for(today).isoformat()
    hist = storage.load(paths.monthly_history, storage.MONTHLY_COLUMNS)
    first_run = hist.empty
    summary = RunSummary(title=f"월간 수집 (스냅샷 {snapshot})")
    rows, bad_urls = [], []

    for c in countries:
        for ind, meta in INDICATORS.items():
            label = f"{c.iso3} {c.name_ko} {meta['label']}"
            prev = _prev_row(hist, c.iso3, ind, snapshot)
            res = source.fetch(c, ind)
            if res.ok:
                status = STATUS_OK
                if prev is not None and prev["ref_period"] == res.ref_period:
                    status = STATUS_NO_UPDATE
                baseline = storage.to_float(prev["value"]) if prev is not None else res.previous
                if baseline is not None and res.value is not None:
                    diff = res.value - baseline
                    if abs(diff) > settings.change_alert_pp:
                        status += f" / 경고: 급변 {diff:+.1f}%p"
                        summary.warnings.append(f"{label}: {baseline} → {res.value} ({diff:+.1f}%p)")
                rows.append({"iso3": c.iso3, "indicator": ind, "snapshot_date": snapshot,
                             "ref_period": res.ref_period, "value": res.value,
                             "previous_value": res.previous, "status": status, "source_url": res.source_url})
                summary.ok.append(label)
            else:
                if res.blocked:
                    summary.blocked_count += 1
                bad_urls.append(f"{c.te_slug} ({meta['label']}): {res.error}")
                rows.append({
                    "iso3": c.iso3, "indicator": ind, "snapshot_date": snapshot,
                    "ref_period": prev["ref_period"] if prev is not None else "",
                    "value": prev["value"] if prev is not None else "",
                    "previous_value": prev["previous_value"] if prev is not None else "",
                    "status": f"{STATUS_STALE} ({res.error})", "source_url": res.source_url,
                })
                summary.failed.append(f"{label}: {res.error}")

    storage.upsert(paths.monthly_history, rows, storage.MONTHLY_COLUMNS, storage.MONTHLY_KEY)

    if first_run:
        total = len(countries) * len(INDICATORS)
        summary.notes.append(f"첫 실행 URL 점검: {total - len(bad_urls)}/{total}개 정상")
        if bad_urls:
            summary.notes.append("열리지 않거나 해석되지 않은 slug: " + "; ".join(bad_urls))
    if summary.blocked_count:
        summary.warnings.append(f"차단(403/캡차/robots) {summary.blocked_count}건 — 우회하지 않고 실패로 기록했습니다.")
    return summary
