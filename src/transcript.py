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
    """תמיכה אופציונלית ב-Webshare (מומלץ אם מריצים ב-GitHub Actions)."""
    user = os.getenv("WEBSHARE_PROXY_USERNAME")
    password = os.getenv("WEBSHARE_PROXY_PASSWORD")
    if not (user and password):
        return None
    try:
        from youtube_transcript_api.proxies import WebshareProxyConfig
    except ImportError:
        return None
    return WebshareProxyConfig(proxy_username=user, proxy_password=password)


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


def fetch_transcript(video_id: str, retries: int = 1) -> str:
    """מחזיר תמלול, או מחרוזת ריקה אם כל השיטות נכשלו."""
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
                log.debug("%s נכשל ל-%s (ניסיון %d): %s", method.__name__, video_id, attempt + 1, exc)
                if attempt < retries:
                    time.sleep(2)
    log.warning("לא הצלחתי למשוך תמלול ל-%s", video_id)
    return ""
