"""연간 수집: 총인구·GDP(IMF → World Bank 대체)·최저임금(보정 파일 > Eurostat > ILOSTAT)."""
from __future__ import annotations

import logging
from datetime import date, datetime

from . import storage
from .config import KST, Country, Paths, Settings
from .pipeline_monthly import RunSummary
from .sources.annual import (ANNUAL_INDICATORS, Eurostat, Ilostat, ImfDataMapper, MinWage, WorldBank,
                             _sortable, load_overrides, parse_date, pick_override)

log = logging.getLogger(__name__)

STATUS_OK = "정상"
STATUS_FALLBACK = "대체 출처(World Bank)"
STATUS_REVIEW = "검토 필요"
STATUS_STALE = "미갱신"
STATUS_NO_STATUTORY = "법정 최저임금 없음"


def _prev_annual(hist, iso3: str, indicator: str, base_year: int):
    if hist.empty:
        return None
    sub = hist[(hist.iso3 == iso3) & (hist.indicator == indicator)]
    sub = sub[sub.base_year.astype(int) < base_year] if not sub.empty else sub
    sub = sub[sub.value != ""] if not sub.empty else sub
    if sub.empty:
        return None
    return sub.sort_values("base_year").iloc[-1]


def _stale_row(c: Country, indicator: str, base_year: int, prev, reason: str, collected_at: str,
               default_url: str) -> dict:
    if prev is None:
        return {"iso3": c.iso3, "indicator": indicator, "base_year": base_year, "value": "",
                "status": f"{STATUS_STALE} ({reason}; 직전 값 없음)", "collected_at": collected_at,
                "source_url": default_url}
    row = {k: prev[k] for k in storage.ANNUAL_COLUMNS}
    row.update({"base_year": base_year, "collected_at": collected_at,
                "status": f"{STATUS_STALE} ({reason}; {prev['base_year']}년 값 유지)"})
    return row


def _minwage_row(c: Country, base_year: int, mw: MinWage, status: str, collected_at: str) -> dict:
    return {"iso3": c.iso3, "indicator": "min_wage", "base_year": base_year,
            "value": "" if mw.no_statutory else mw.amount, "unit": mw.unit, "currency": mw.currency,
            "effective_date": mw.effective_date, "source": mw.source, "source_url": mw.source_url,
            "status": status, "collected_at": collected_at, "region": mw.region, "note": mw.note}


def resolve_min_wage(c: Country, overrides: list[MinWage], eurostat: MinWage | None,
                     ilostat: MinWage | None, today: date, stale_days: int) -> tuple[MinWage | None, list[str]]:
    """우선순위에 따라 하나를 고르고 '검토 필요' 사유 목록을 함께 돌려준다."""
    reasons: list[str] = []
    chosen = pick_override(overrides, today) if overrides else None
    autos = [m for m in (eurostat, ilostat) if m is not None]

    if chosen is None:
        chosen = autos[0] if autos else None
        if chosen is not None and chosen.ref_year and chosen.ref_year < today.year:
            reasons.append(f"자동 출처 값이 {chosen.ref_year}년 기준(실행 연도 이전)")
    else:
        checked = parse_date(chosen.checked_date)
        if checked is None:
            reasons.append("보정 파일 확인일 비어 있음")
        elif (today - checked).days > stale_days:
            reasons.append(f"보정 파일 확인일 {chosen.checked_date} (1년 경과)")

    if chosen is not None and not chosen.no_statutory:
        mine = _sortable(chosen.effective_date)
        for other in autos:
            if other is chosen:
                continue
            if _sortable(other.effective_date) > mine:
                reasons.append(f"{other.source.split(' (')[0]}에 더 최근 값({other.effective_date}) 있음")
    elif chosen is not None and chosen.no_statutory:
        newer = [o for o in autos if o.amount is not None]
        if newer:
            reasons.append(f"'법정 최저임금 없음'으로 입력됐지만 {newer[0].source.split(' (')[0]}에 값 있음")
    return chosen, reasons


def run_annual(countries: list[Country], settings: Settings, paths: Paths, imf: ImfDataMapper,
               wb: WorldBank, eurostat: Eurostat, ilostat: Ilostat, today: date, base_year: int) -> RunSummary:
    collected_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    hist = storage.load(paths.annual_history, storage.ANNUAL_COLUMNS)
    overrides = load_overrides(paths.overrides)
    summary = RunSummary(title=f"연간 수집 (기준연도 {base_year})")
    rows = []

    for c in countries:
        # -- 총인구 / GDP ------------------------------------------------
        for ind, spec in ANNUAL_INDICATORS.items():
            label = f"{c.iso3} {c.name_ko} {spec['label']}"
            v = imf.get(spec["imf"], c.iso3, base_year)
            status = STATUS_OK
            if v.value is None:
                wbv = wb.get(spec["wb"], c, base_year, spec["wb_divisor"])
                if wbv.value is not None:
                    v, status = wbv, STATUS_FALLBACK
                    summary.warnings.append(f"{label}: IMF 값 없음 → World Bank 대체")
                else:
                    reason = f"{v.error}; {wbv.error}"
                    rows.append(_stale_row(c, ind, base_year, _prev_annual(hist, c.iso3, ind, base_year),
                                           reason, collected_at, v.source_url))
                    summary.failed.append(f"{label}: {reason}")
                    continue
            rows.append({"iso3": c.iso3, "indicator": ind, "base_year": base_year, "value": v.value,
                         "unit": spec["unit"], "currency": spec["currency"], "effective_date": str(base_year),
                         "source": v.source, "source_url": v.source_url, "status": status,
                         "collected_at": collected_at})
            summary.ok.append(label)

        # -- 최저임금 ----------------------------------------------------
        label = f"{c.iso3} {c.name_ko} 최저임금"
        eu = eurostat.get(c, today)
        # 보정 파일이 있어도 '다른 출처에 더 최근 값' 검사를 위해 자동 출처를 함께 조회한다
        ilo, ilo_err = ilostat.get(c)
        chosen, reasons = resolve_min_wage(c, overrides.get(c.iso3, []), eu, ilo, today, settings.override_stale_days)
        if chosen is None:
            reason = "; ".join(x for x in ("보정 파일 없음", eurostat.error or ("Eurostat 대상 아님" if not c.eurostat_geo else "Eurostat 값 없음"), ilo_err) if x)
            rows.append(_stale_row(c, "min_wage", base_year, _prev_annual(hist, c.iso3, "min_wage", base_year),
                                   reason, collected_at, ""))
            summary.failed.append(f"{label}: {reason}")
            continue
        if chosen.no_statutory:
            status = STATUS_NO_STATUTORY
        else:
            status = STATUS_OK
        if reasons:
            status = f"{status} / {STATUS_REVIEW}: " + "; ".join(reasons) if chosen.no_statutory \
                else f"{STATUS_REVIEW}: " + "; ".join(reasons)
            summary.warnings.append(f"{label}: " + "; ".join(reasons))
        rows.append(_minwage_row(c, base_year, chosen, status, collected_at))
        summary.ok.append(label)

    storage.upsert(paths.annual_history, rows, storage.ANNUAL_COLUMNS, storage.ANNUAL_KEY)
    return summary
