"""
build_site.py
-------------
data/countries_data.json + template/template.html → docs/index.html

- 빌드할 때마다 config/ 와 output/country_profile.json 의 '현재' 값(수도·거점·공식 기관·프로필 지표)을
  덮어쓴다. 그래서 국가 프로필만 갱신된 날에는 뉴스를 다시 수집하지 않고 이 스크립트만 돌리면 된다.
- 데이터가 없거나 깨져도 빈 화면 대신 안내 페이지를 만들고, 오류 내용을 로그에 남긴다.

실행: python src/build_site.py
"""
import html
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python src/xxx.py 외 실행 방식 대비
from profile_overlay import REGIONS, ProfileContext

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("build_site")

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_PATH = os.path.join(BASE_DIR, "data", "countries_data.json")
ARCHIVE_INDEX_PATH = os.path.join(BASE_DIR, "docs", "archive", "index.json")
TEMPLATE_DIR = os.path.join(BASE_DIR, "template")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "index.html")
KST = timezone(timedelta(hours=9))

FALLBACK_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Global Daily News</title>
<style>body{{font-family:system-ui,sans-serif;background:#F4F5F7;color:#334155;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;padding:24px;box-sizing:border-box}}
.box{{max-width:560px;background:#fff;border:1px solid #E2E8F0;border-radius:20px;padding:40px 32px;text-align:center}}
pre{{text-align:left;font-size:12px;background:#F1F5F9;border-radius:8px;padding:12px;white-space:pre-wrap;word-break:break-all}}</style>
</head><body><div class="box"><h1>Global Daily News</h1>
<p>사이트를 만드는 중 문제가 생겨 임시 안내 페이지를 표시합니다. 다음 자동 갱신 때 복구됩니다.</p>
<pre>{timestamp}\n{error}</pre></div></body></html>"""


def load_data() -> dict:
    now = datetime.now(KST)
    skeleton = {"generated_at": now.isoformat(), "as_of_display": f"{now:%Y-%m-%d} (데이터 없음)",
                "regions": REGIONS, "countries": [], "global_industry": []}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("countries"), list):
            raise ValueError("countries 목록이 없습니다")
    except FileNotFoundError:
        log.warning(f"{DATA_PATH} 이 없습니다 — 첫 수집 전입니다. 빈 화면으로 빌드합니다.")
        return skeleton
    except Exception:
        log.error(f"{DATA_PATH} 를 읽지 못했습니다 — 빈 화면으로 빌드합니다.\n{traceback.format_exc()}")
        return skeleton
    for k, v in skeleton.items():
        data.setdefault(k, v)
    return data


def archive_range(today: str) -> tuple:
    try:
        with open(ARCHIVE_INDEX_PATH, "r", encoding="utf-8") as f:
            idx = json.load(f)
        return idx.get("min_date") or today, idx.get("latest") or today
    except FileNotFoundError:
        return today, today
    except Exception as e:  # noqa: BLE001
        log.warning(f"아카이브 목록 읽기 실패: {e}")
        return today, today


def write(text: str) -> None:
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def build() -> None:
    data = load_data()
    try:
        n = ProfileContext().apply(data["countries"])
        log.info(f"최신 설정·프로필 값 반영: {n}개국")
    except Exception:
        log.error(f"프로필 반영 실패 — 수집 당시 값으로 진행합니다.\n{traceback.format_exc()}")

    today = str(data.get("generated_at", ""))[:10] or datetime.now(KST).strftime("%Y-%m-%d")
    archive_min, archive_max = archive_range(today)
    payload = json.dumps({
        "as_of_display": data.get("as_of_display", ""),
        "generated_at": data.get("generated_at", ""),
        "regions": data.get("regions") or REGIONS,
        "countries": data.get("countries", []),
        "global_industry": data.get("global_industry", []),
    }, ensure_ascii=False, separators=(",", ":"), default=str).replace("</", "<\\/")

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    page = env.get_template("template.html").render(
        as_of_display=data.get("as_of_display", ""),
        generated_at_date=today,
        archive_min_date=archive_min,
        archive_max_date=archive_max,
        dashboard_json=Markup(payload),
    )
    write(page)
    log.info(f"생성 완료 → {OUTPUT_PATH} ({len(data.get('countries', []))}개국, 아카이브 {archive_min}~{archive_max})")


def main() -> None:
    try:
        build()
    except Exception:
        err = traceback.format_exc()
        log.error(f"사이트 생성 실패 — 안내 페이지로 대체합니다.\n{err}")
        try:
            write(FALLBACK_HTML.format(timestamp=datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), error=html.escape(err[-1500:])))
        except Exception:
            log.exception("안내 페이지 기록마저 실패했습니다")
            sys.exit(1)


if __name__ == "__main__":
    main()
