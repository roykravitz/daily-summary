"""משיכת תמלול של סרטון יוטיוב, עם כמה נפילות אחורה.

הערה חשובה: יוטיוב חוסם לעיתים בקשות תמלול מכתובות IP של ספקי ענן.
לכן יש כאן שתי שיטות עצמאיות, ואפשר להוסיף פרוקסי דרך משתני סביבה.
"""
from __future__ import annotations

import html
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

TIMEOUT = 30
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"

# שפות מועדפות לתמלול, לפי הסדר
PREFERRED_LANGS = ["he", "iw", "en", "en-US"]


def _proxy_config():
    """פרוקסי אופציונלי לתמלולים — נחוץ כשמריצים בענן.

    שתי דרכים, לפי סדר עדיפות:
    1. Webshare — WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD
    2. כל פרוקסי אחר — HTTP_PROXY_URL (וגם HTTPS_PROXY_URL אם שונה)

    חשוב: יוטיוב חוסם כתובות של מרכזי נתונים. פרוקסי מסוג datacenter,
    כולל השכבה החינמית של Webshare, כנראה ייחסם בדיוק כמו שרתי GitHub.
    מה שעובד בפועל הוא פרוקסי residential.
    """
    user = os.getenv("WEBSHARE_PROXY_USERNAME")
    password = os.getenv("WEBSHARE_PROXY_PASSWORD")
    if user and password:
        try:
            from youtube_transcript_api.proxies import WebshareProxyConfig
        except ImportError:
            log.warning("הגרסה המותקנת של youtube-transcript-api לא תומכת בפרוקסי")
            return None
        log.info("משתמש בפרוקסי Webshare")
        return WebshareProxyConfig(proxy_username=user, proxy_password=password)

    http_url = os.getenv("HTTP_PROXY_URL")
    if http_url:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
        except ImportError:
            log.warning("הגרסה המותקנת של youtube-transcript-api לא תומכת בפרוקסי")
            return None
        log.info("משתמש בפרוקסי כללי")
        return GenericProxyConfig(
            http_url=http_url,
            https_url=os.getenv("HTTPS_PROXY_URL") or http_url,
        )

    return None


def proxy_is_configured() -> bool:
    return bool(
        (os.getenv("WEBSHARE_PROXY_USERNAME") and os.getenv("WEBSHARE_PROXY_PASSWORD"))
        or os.getenv("HTTP_PROXY_URL")
    )


def _via_api(video_id: str) -> str:
    """שיטה 1 — youtube-transcript-api."""
    from youtube_transcript_api import YouTubeTranscriptApi

    proxies = _proxy_config()
    api = YouTubeTranscriptApi(proxy_config=proxies) if proxies else YouTubeTranscriptApi()

    try:
        fetched = api.fetch(video_id, languages=PREFERRED_LANGS)
    except Exception:
        # אין את השפות המועדפות — לוקחים כל תמלול שקיים
        transcripts = api.list(video_id)
        transcript = next(iter(transcripts))
        fetched = transcript.fetch()

    return " ".join(snippet.text.strip() for snippet in fetched if snippet.text.strip())


def _via_web(video_id: str) -> str:
    """שיטה 2 — youtubetotranscript.com, כשהשיטה הראשונה חסומה."""
    url = f"https://youtubetotranscript.com/transcript?v={video_id}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()

    segments = re.findall(
        r'<span[^>]*class="[^"]*transcript-segment[^"]*"[^>]*>(.*?)</span>',
        resp.text,
        re.S,
    )
    if not segments:
        block = re.search(r"##\s*Transcript(.*?)(?:##|\Z)", resp.text, re.S)
        segments = [block.group(1)] if block else []

    text = " ".join(html.unescape(re.sub(r"(?s)<[^>]+>", " ", s)) for s in segments)
    return re.sub(r"\s+", " ", text).strip()


_ip_blocked = False

BLOCK_MARKERS = ("IpBlocked", "RequestBlocked", "blocking requests from your IP")


def reset_block_state() -> None:
    global _ip_blocked
    _ip_blocked = False


def _looks_like_ip_block(exc: Exception) -> bool:
    return type(exc).__name__ in BLOCK_MARKERS or any(
        marker in str(exc) for marker in BLOCK_MARKERS
    )


def fetch_transcript(video_id: str, retries: int = 1) -> str:
    """מחזיר תמלול, או מחרוזת ריקה אם כל השיטות נכשלו.

    כשיוטיוב חוסם את הכתובת, כל ניסיון נוסף רק מעמיק את החסימה. לכן
    ברגע שמזוהה חסימה מפסיקים לנסות עד סוף הריצה.
    """
    global _ip_blocked

    if _ip_blocked:
        return ""
    for method in (_via_api, _via_web):
        for attempt in range(retries + 1):
            try:
                text = method(video_id)
                if len(text) >= 200:
                    log.info("תמלול ל-%s דרך %s — %d תווים", video_id, method.__name__, len(text))
                    return text
                log.debug("%s החזיר תמלול קצר מדי ל-%s", method.__name__, video_id)
                break
            except Exception as exc:  # noqa: BLE001 - נופלים לשיטה הבאה
                if _looks_like_ip_block(exc):
                    _ip_blocked = True
                    log.error(
                        "יוטיוב חוסם את כתובת ה-IP הזו. מפסיק למשוך תמלולים בריצה הזו — "
                        "ניסיונות נוספים רק מאריכים את החסימה. היא חולפת מעצמה תוך שעות. "
                        "אם זה חוזר, ראה את הסעיף על תמלולים ב-README."
                    )
                    return ""
                log.debug("%s נכשל ל-%s (ניסיון %d): %s", method.__name__, video_id, attempt + 1, exc)
                if attempt < retries:
                    time.sleep(2)
    if proxy_is_configured():
        log.warning("לא הצלחתי למשוך תמלול ל-%s למרות הפרוקסי", video_id)
    else:
        log.warning(
            "לא הצלחתי למשוך תמלול ל-%s. אם זו ריצה בענן, יוטיוב חוסם את "
            "כתובת השרת — צריך פרוקסי residential. ראה את הסעיף על תמלולים ב-README.",
            video_id,
        )
    return ""
