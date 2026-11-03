"""
notify.py — 매일 수집 후 문제가 있으면 알림 메일 (update.yml 에서 실행)

  python src/notify.py --collector-status success|failure|skipped --run-url <Actions 실행 주소>

알림 조건 (config/operations.json → alerts)
  - 수집 단계 실패·시간 초과
  - 기사 0건 국가가 zero_news_countries_max 개 이상
  - 외국어 기사 번역 성공률이 translation_rate_min 미만
  - Google News 차단으로 뉴스 수집 중단 (alert_on_news_blocked)
메일 설정(MAIL_USER·MAIL_PASSWORD·ALERT_TO)이 없으면 로그만 남기고 정상 종료한다 — 알림 때문에 배포가 멈추지 않게.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mailer import mail_configured, parse_recipients, send_mail  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("notify")

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
QUALITY_PATH = os.path.join(BASE_DIR, "collector_quality.json")
OPS_PATH = os.path.join(BASE_DIR, "config", "operations.json")


def _read(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def find_problems(status: str, quality: dict, rules: dict) -> list:
    problems = []
    if status == "failure":
        problems.append("수집 단계가 실패했거나 20분을 넘겨 중단됐습니다. 사이트는 직전 데이터로 만들어졌습니다.")
    if not quality:
        if status != "failure":
            problems.append("품질 보고서(collector_quality.json)가 없습니다. 수집이 끝까지 돌지 못했을 수 있습니다.")
        return problems
    zero = quality.get("zero_news_countries") or []
    if len(zero) >= int(rules.get("zero_news_countries_max", 10)):
        problems.append(f"현지 뉴스가 0건인 나라가 {len(zero)}개국입니다: {', '.join(zero)}")
    rate = float(quality.get("translation_rate", 1.0))
    if quality.get("foreign_items") and rate < float(rules.get("translation_rate_min", 0.5)):
        problems.append(f"번역 성공률이 {rate:.0%}입니다 (외국어 기사 {quality.get('foreign_items')}건 중 "
                        f"{quality.get('untranslated')}건 미번역). 번역 서비스가 막혔을 수 있습니다.")
    if rules.get("alert_on_news_blocked", True) and quality.get("news_stopped"):
        problems.append("Google 뉴스 요청이 연속 실패해 뉴스 수집을 중간에 멈췄습니다 (일부 국가는 이전 기사로 표시).")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collector-status", default="success")
    ap.add_argument("--run-url", default="")
    args = ap.parse_args()

    quality = _read(QUALITY_PATH, {})
    rules = (_read(OPS_PATH, {}) or {}).get("alerts", {})
    problems = find_problems(args.collector_status, quality, rules)
    if not problems:
        log.info("수집 품질 정상 — 알림 없음")
        return 0
    for p in problems:
        log.warning(f"[알림] {p}")

    recipients = parse_recipients(os.environ.get("ALERT_TO", ""))
    if not (mail_configured() and recipients):
        log.warning("ALERT_TO / MAIL_USER / MAIL_PASSWORD 가 없어 알림 메일은 보내지 않았습니다.")
        return 0

    date = (quality.get("generated_at") or "")[:10] or "오늘"
    items = "".join(f"<li style='margin:6px 0'>{html.escape(p)}</li>" for p in problems)
    link = (f"<p><a href='{html.escape(args.run_url)}'>GitHub Actions 실행 기록 열기</a></p>" if args.run_url else "")
    body = (f"<div style='font-family:sans-serif;font-size:14px;color:#1E293B'>"
            f"<h2 style='font-size:18px'>Global HR Radar 수집 확인 필요 ({html.escape(date)})</h2>"
            f"<ul>{items}</ul>{link}"
            f"<p style='color:#64748B;font-size:12px'>대부분 다음 날 수집에서 자동으로 회복됩니다. 이틀 이상 계속되면 로그를 확인해 주세요.</p></div>")
    text = f"Global HR Radar 수집 확인 필요 ({date})\n\n" + "\n".join(f"- {p}" for p in problems) + \
           (f"\n\n실행 기록: {args.run_url}" if args.run_url else "")
    try:
        send_mail(f"[Global HR Radar] 수집 확인 필요 ({date})", body, text, recipients)
    except Exception as e:  # noqa: BLE001
        log.error(f"알림 메일 발송 실패: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
