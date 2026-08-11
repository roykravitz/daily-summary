"""שליחת ההודעה ליעדים — טלגרם, אימייל, webhook."""
from __future__ import annotations

import json
import logging
import os
import smtplib
import time
from email.message import EmailMessage

import requests

from .compose import split_message

log = logging.getLogger(__name__)

TIMEOUT = 30
CAPTION_LIMIT = 1000
MAX_IMAGES_PER_MESSAGE = 10


def _telegram_creds(target: dict) -> tuple[str, str]:
    token = os.getenv(target.get("bot_token_env") or "TELEGRAM_BOT_TOKEN")
    chat_id = str(target.get("chat_id") or "") or os.getenv(
        target.get("chat_id_env") or "TELEGRAM_CHAT_ID", ""
    )
    return token or "", chat_id


def _send_photo_block(block: dict, token: str, chat_id: str) -> bool:
    """שולח תמונה אחת או אלבום, כשהטקסט של הפריט הוא הכיתוב."""
    images = block["images"][:MAX_IMAGES_PER_MESSAGE]
    caption = block["caption"][:CAPTION_LIMIT]

    if len(images) == 1:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {"chat_id": chat_id, "photo": images[0], "caption": caption}
    else:
        url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
        media = [{"type": "photo", "media": u} for u in images]
        media[0]["caption"] = caption
        payload = {"chat_id": chat_id, "media": json.dumps(media)}

    try:
        resp = requests.post(url, data=payload, timeout=TIMEOUT)
        if resp.ok:
            return True
        log.warning("שליחת תמונה נכשלה (%s): %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        log.warning("שליחת תמונה נכשלה: %s", exc)

    # התמונה לא עברה — לפחות שהטקסט יגיע
    return _send_text(caption, token, chat_id, {})


def _send_text(text: str, token: str, chat_id: str, target: dict) -> bool:
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
        time.sleep(0.4)
    return ok


def send_telegram_blocks(blocks: list[dict], target: dict) -> bool:
    """שולח את הבלוקים לפי הסדר, כך שכל תמונה מגיעה עם הטקסט שלה."""
    token, chat_id = _telegram_creds(target)
    if not token:
        log.error("חסר טוקן טלגרם (%s)", target.get("bot_token_env") or "TELEGRAM_BOT_TOKEN")
        return False
    if not chat_id:
        log.error("חסר chat id לטלגרם (%s)", target.get("chat_id_env") or "TELEGRAM_CHAT_ID")
        return False

    ok = True
    for block in blocks:
        if block["type"] == "photo":
            ok = _send_photo_block(block, token, chat_id) and ok
        else:
            ok = _send_text(block["text"], token, chat_id, target) and ok
        time.sleep(0.4)
    return ok


def send_telegram(text: str, target: dict) -> bool:
    """שליחת טקסט בלבד — נשמר עבור קוראים שאין להם בלוקים."""
    token, chat_id = _telegram_creds(target)
    if not (token and chat_id):
        log.error("חסרים פרטי טלגרם")
        return False
    return _send_text(text, token, chat_id, target)


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


def deliver(blocks: list[dict], targets: list[dict], language: str = "he") -> bool:
    """שולח לכל היעדים. מחזיר True אם לפחות אחד הצליח.

    טלגרם מקבל את הבלוקים לפי סדרם, כדי שתמונה תגיע עם הטקסט שלה.
    אימייל ו-webhook מקבלים גרסת טקסט אחת.
    """
    text = "\n\n".join(
        b["text"] if b["type"] == "text" else b["caption"] for b in blocks
    ).strip()

    any_ok = False
    for target in targets:
        kind = target.get("type", "")
        if kind == "telegram":
            ok = send_telegram_blocks(blocks, target)
        elif kind == "email":
            ok = send_email(text, target)
        elif kind == "webhook":
            ok = send_webhook(text, target)
        else:
            log.warning("סוג יעד לא מוכר: %s", kind)
            continue
        if ok:
            log.info("נשלח ל-%s", kind)
            any_ok = True
    return any_ok
