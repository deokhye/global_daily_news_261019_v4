"""
mailer.py — 메일 발송 공용 함수 (notify.py · weekly_mail.py 가 사용)

GitHub Secrets → 환경 변수
  MAIL_USER       보내는 계정 (예: 발송용 Gmail 주소)
  MAIL_PASSWORD   앱 비밀번호 (Gmail 로그인 비밀번호가 아님)
  MAIL_SMTP_HOST  (선택) 기본 smtp.gmail.com
  MAIL_SMTP_PORT  (선택) 기본 465 (SSL). 587 이면 STARTTLS
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

log = logging.getLogger("mailer")


def parse_recipients(value: str) -> list:
    return [x.strip() for x in (value or "").replace(";", ",").replace("\n", ",").split(",") if "@" in x]


def mail_configured() -> bool:
    return bool(os.environ.get("MAIL_USER") and os.environ.get("MAIL_PASSWORD"))


def send_mail(subject: str, html: str, text: str, recipients: list, sender_name: str = "Global Daily News") -> None:
    """받는 사람끼리 주소가 보이지 않도록 숨은 참조(Bcc)로 보낸다. 실패하면 예외."""
    user = os.environ.get("MAIL_USER", "").strip()
    password = os.environ.get("MAIL_PASSWORD", "").strip()
    host = os.environ.get("MAIL_SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(os.environ.get("MAIL_SMTP_PORT", "").strip() or 465)
    if not user or not password:
        raise RuntimeError("MAIL_USER / MAIL_PASSWORD 가 설정되지 않았습니다 (GitHub Secrets 확인)")
    if not recipients:
        raise RuntimeError("받는 사람이 없습니다 (MAIL_TO / ALERT_TO 확인)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr((str(Header(sender_name, "utf-8")), user))
    msg["To"] = formataddr((str(Header(sender_name, "utf-8")), user))
    msg["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.sendmail(user, [user] + recipients, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.sendmail(user, [user] + recipients, msg.as_string())
    log.info(f"메일 발송 완료: {len(recipients)}명 (숨은 참조)")
