"""이력 CSV 누적 저장 (long format).

같은 스냅샷(월간: iso3+indicator+snapshot_date / 연간: iso3+indicator+base_year)을 다시
저장하면 기존 행을 덮어써 중복 행이 생기지 않는다. 다른 스냅샷은 절대 지우지 않는다.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ANNUAL_COLUMNS = ["iso3", "indicator", "base_year", "value", "unit", "currency", "effective_date",
                  "source", "source_url", "status", "collected_at", "region", "note"]
ANNUAL_KEY = ["iso3", "indicator", "base_year"]

MONTHLY_COLUMNS = ["iso3", "indicator", "snapshot_date", "ref_period", "value", "previous_value",
                   "status", "source_url"]
MONTHLY_KEY = ["iso3", "indicator", "snapshot_date"]


def load(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    for c in columns:
        if c not in df.columns:
            df[c] = ""
    return df[columns]


def upsert(path: Path, rows: list[dict], columns: list[str], key: list[str]) -> pd.DataFrame:
    df = load(path, columns)
    new = pd.DataFrame([{c: _fmt(r.get(c, "")) for c in columns} for r in rows], columns=columns)
    if not new.empty:
        new_keys = set(map(tuple, new[key].values.tolist()))
        keep = ~df[key].apply(tuple, axis=1).isin(new_keys) if not df.empty else []
        df = pd.concat([df[keep] if len(df) else df, new], ignore_index=True)
    df = df.sort_values(key).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return df


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v) if v == v else ""   # NaN → ""
    return str(v)


def to_float(s) -> float | None:
    try:
        return float(s) if s not in ("", None) else None
    except (TypeError, ValueError):
        return None
