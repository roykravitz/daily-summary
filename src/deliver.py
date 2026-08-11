"""שליחת ההודעה ליעדים — טלגרם, אימייל, webhook."""
from __future__ import annotations

import logging
import os
import smtplib
import time
from email.message import EmailMessage

import requests

from .compose import split_message

log = logging.getLogger(__name__)

TIMEOUT = 30


def send_telegram(text: str, target: dict) -> bool:
    # לכל נושא אפשר ערוץ משלו: chat_id ישירות, או שם משתנה סביבה ב-chat_id_env
    token = os.getenv(target.get("bot_token_env") or "TELEGRAM_BOT_TOKEN")
    chat_id = str(target.get("chat_id") or "") or os.getenv(
        target.get("chat_id_env") or "TELEGRAM_CHAT_ID", ""
    )
    if not token:
        log.error("חסר טוקן טלגרם (%s)", target.get("bot_token_env") or "TELEGRAM_BOT_TOKEN")
        return False
    if not chat_id:
        log.error("חסר chat id לטלגרם (%s)", target.get("chat_id_env") or "TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in split_message(text):
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                        "disable_notification": bool(target.get("silent")),
                    },
                    timeout=TIMEOUT,
                )
                if resp.ok:
                    break
                if resp.status_code == 429:
                    wait = resp.json().get("parameters", {}).get("retry_after", 5)
                    log.info("טלגרם ביקש להמתין %ss", wait)
                    time.sleep(wait)
                    continue
                log.error("טלגרם החזיר %s: %s", resp.status_code, resp.text[:300])
                ok = False
                break
            except requests.RequestException as exc:
                log.warning("שליחה לטלגרם נכשלה (ניסיון %d): %s", attempt + 1, exc)
                time.sleep(3)
        else:
            ok = False
        time.sleep(0.5)
    return ok


MAX_PHOTOS_PER_RUN = 10
CAPTION_LIMIT = 1000


def send_telegram_photos(items: list, target: dict, tz: str = "Asia/Jerusalem",
                         language: str = "he") -> int:
    """שולח תמונות שנמצאו בפריטים, אחרי הודעת הטקסט.

    טלגרם מוריד את התמונה בעצמו מהכתובת, ולכן אין כאן העלאה ואין עלות.
    מחזיר כמה נשלחו.
    """
    from .compose import source_header

    token = os.getenv(target.get("bot_token_env") or "TELEGRAM_BOT_TOKEN")
    chat_id = str(target.get("chat_id") or "") or os.getenv(
        target.get("chat_id_env") or "TELEGRAM_CHAT_ID", ""
    )
    if not (token and chat_id):
        return 0

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    sent = 0
    for item in items:
        for photo in item.images:
            if sent >= MAX_PHOTOS_PER_RUN:
                log.info("הגעתי למגבלת %d תמונות בריצה", MAX_PHOTOS_PER_RUN)
                return sent
            caption = f"{source_header(item, tz, language)}\n{item.url}"
            try:
                resp = requests.post(
                    url,
                    data={"chat_id": chat_id, "photo": photo,
                          "caption": caption[:CAPTION_LIMIT]},
                    timeout=TIMEOUT,
                )
                if resp.ok:
                    sent += 1
                else:
                    log.warning("שליחת תמונה נכשלה (%s): %s", resp.status_code,
                                resp.text[:150])
            except requests.RequestException as exc:
                log.warning("שליחת תמונה נכשלה: %s", exc)
            time.sleep(0.5)
    return sent


def send_email(text: str, target: dict) -> bool:
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    to = target.get("to") or user
    if not (user and password and to):
        log.error("חסרים SMTP_USER / SMTP_PASS / נמען")
        return False

    msg = EmailMessage()
    msg["Subject"] = text.split("\n", 1)[0].lstrip("📅 ").strip()
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)

    host = target.get("smtp_host", "smtp.gmail.com")
    port = int(target.get("smtp_port", 587))
    try:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("שליחת מייל נכשלה: %s", exc)
        return False


def send_webhook(text: str, target: dict) -> bool:
    url = target.get("url")
    if not url:
        log.error("ל-webhook חסרה כתובת")
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=TIMEOUT)
        if resp.ok:
            return True
        log.error("webhook החזיר %s", resp.status_code)
    except requests.RequestException as exc:
        log.error("שליחה ל-webhook נכשלה: %s", exc)
    return False


SENDERS = {"telegram": send_telegram, "email": send_email, "webhook": send_webhook}


def deliver(text: str, targets: list[dict]) -> bool:
    """שולח לכל היעדים. מחזיר True אם לפחות אחד הצליח."""
    any_ok = False
    for target in targets:
        sender = SENDERS.get(target.get("type", ""))
        if not sender:
            log.warning("סוג יעד לא מוכר: %s", target.get("type"))
            continue
        if sender(text, target):
            log.info("נשלח ל-%s", target["type"])
            any_ok = True
    return any_ok
