"""שליפת פריטים חדשים מכל סוג מקור.

כל fetcher מחזיר רשימת Item ממויינת מהחדש לישן. הטקסט עצמו (content) הוא
מה שנאמר במקור בפועל — תמלול, תקציר הפיד, או טקסט העמוד. אין כאן שום
העשרה ממקורות חיצוניים.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import requests

from .transcript import fetch_transcript

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "he,en;q=0.8"}
TIMEOUT = 25

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

MIN_CONTENT = 30      # מתחת לזה אין מה להציג
VERBATIM_MAX = 600    # מתחת לזה מציגים כלשונו, בלי לפנות למודל
MAX_IMAGES_PER_ITEM = 4


@dataclass
class Item:
    """פריט תוכן יחיד ממקור."""
    uid: str
    source_name: str
    title: str
    url: str
    published: datetime | None
    content: str = ""
    content_kind: str = "text"       # transcript | feed | page
    focus: str = ""
    meta: dict = field(default_factory=dict)
    state_key: str = ""              # uid בתוספת הנושא — נקבע בזמן האיסוף
    # לחלק מהמקורות אין תאריך פרסום ואנחנו יודעים רק מתי שלפנו. אסור להציג
    # את זה כזמן פרסום — זה בדיוק האות שגורם לחשוב שתוכן ישן הוא טרי.
    published_is_fetch_time: bool = False
    images: list[str] = field(default_factory=list)
    # בציוץ הכותרת היא הטקסט עצמו. בלי הסימון הזה, טקסט שתורגם היה מודפס
    # פעמיים — פעם במקור ופעם בתרגום — כי השניים כבר לא נראים זהים.
    title_is_content: bool = False

    @property
    def has_content(self) -> bool:
        return len(self.content.strip()) >= MIN_CONTENT

    @property
    def needs_summary(self) -> bool:
        """טקסט קצר מוצג כלשונו. ציוץ הוא כבר תמציתי — סיכום שלו רק מרחיק מהמקור."""
        return len(self.content.strip()) >= VERBATIM_MAX


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.replace("+00:00", "Z"), fmt)
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            continue
    log.debug("לא הצלחתי לפענח תאריך: %r", text)
    return None


_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def _extract_images(raw_html: str) -> list[str]:
    """כתובות התמונות שבפריט.

    מראות Nitter מגישות תמונות דרך /pic/, ומתרגמות ל-CDN של טוויטר.
    עדיף להצביע ישירות על המקור — הוא יציב, בעוד מראות מתחלפות ונופלות.
    """
    urls = []
    for src in _IMG_RE.findall(html.unescape(raw_html)):
        if "/pic/" in src:
            path = unquote(src.split("/pic/", 1)[1])
            src = "https://pbs.twimg.com/" + path.lstrip("/")
        if src.startswith("http") and src not in urls:
            urls.append(src)
    return urls[:MAX_IMAGES_PER_ITEM]


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


# ---------------------------------------------------------------- YouTube

_YT_FEED = "https://www.youtube.com/feeds/videos.xml"
_CHANNEL_ID_RE = re.compile(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{22})"')


def resolve_channel_id(url: str, cache: dict) -> str | None:
    """ממיר כתובת ערוץ כלשהי (@handle, /c/, /user/, קישור לסרטון) ל-channel_id."""
    m = re.search(r"(UC[\w-]{22})", url)
    if m:
        return m.group(1)
    if url in cache:
        return cache[url]
    try:
        page = _get(url).text
    except requests.RequestException as exc:
        log.warning("לא הצלחתי לפתוח את כתובת הערוץ %s: %s", url, exc)
        return None
    m = _CHANNEL_ID_RE.search(page)
    if not m:
        log.warning("לא מצאתי channel_id בעמוד %s", url)
        return None
    cache[url] = m.group(1)
    return m.group(1)


def _yt_entries(feed_url: str, source_name: str, focus: str, limit: int) -> list[Item]:
    root = ET.fromstring(_get(feed_url).content)
    items: list[Item] = []
    for entry in root.findall("atom:entry", NS)[: limit * 3]:
        vid = entry.findtext("{http://www.youtube.com/xml/schemas/2015}videoId")
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        published = _parse_date(entry.findtext("atom:published", namespaces=NS))
        if not vid:
            continue
        group = entry.find("media:group", NS)
        desc = (group.findtext("media:description", default="", namespaces=NS) or "") if group is not None else ""
        items.append(
            Item(
                uid=f"yt:{vid}",
                source_name=source_name,
                title=title,
                url=f"https://www.youtube.com/watch?v={vid}",
                published=published,
                focus=focus,
                meta={"video_id": vid, "description": desc.strip()},
            )
        )
    return items


def fetch_youtube_channel(src, limit: int, cache: dict) -> list[Item]:
    cid = resolve_channel_id(src.url, cache)
    if not cid:
        return []
    return _yt_entries(f"{_YT_FEED}?channel_id={cid}", src.name, src.focus, limit)


def fetch_youtube_playlist(src, limit: int, cache: dict) -> list[Item]:
    m = re.search(r"list=([\w-]+)", src.url)
    if not m:
        log.warning("לא מצאתי playlist id בכתובת %s", src.url)
        return []
    return _yt_entries(f"{_YT_FEED}?playlist_id={m.group(1)}", src.name, src.focus, limit)


def fetch_youtube_video(src, limit: int, cache: dict) -> list[Item]:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})", src.url)
    if not m:
        log.warning("לא מצאתי video id בכתובת %s", src.url)
        return []
    vid = m.group(1)
    return [
        Item(
            uid=f"yt:{vid}",
            source_name=src.name,
            title=src.name,
            url=f"https://www.youtube.com/watch?v={vid}",
            published=datetime.now(timezone.utc),
            published_is_fetch_time=True,
            focus=src.focus,
            meta={"video_id": vid},
        )
    ]


def load_youtube_content(item: Item) -> None:
    """ממלא את item.content בתמלול הסרטון. נופל לתיאור אם אין תמלול."""
    vid = item.meta.get("video_id")
    text = fetch_transcript(vid) if vid else ""
    if text:
        item.content, item.content_kind = text, "transcript"
        return
    desc = item.meta.get("description", "")
    if len(desc) >= 200:
        log.info("אין תמלול ל-%s — משתמש בתיאור הסרטון", vid)
        item.content, item.content_kind = desc, "description"


# ---------------------------------------------------------------- RSS / פודקאסט

def fetch_feed(src, limit: int, cache: dict) -> list[Item]:
    """RSS 2.0 או Atom — פודקאסטים, בלוגים, אתרי חדשות."""
    root = ET.fromstring(_get(src.url).content)
    items: list[Item] = []

    entries = root.findall(".//item") or root.findall("atom:entry", NS)
    for entry in entries[: limit * 3]:
        def pick(*paths: str) -> str:
            for p in paths:
                val = entry.findtext(p, namespaces=NS)
                if val:
                    return val.strip()
            return ""

        title = pick("title", "atom:title")
        link = pick("link", "guid")
        if not link:
            node = entry.find("atom:link", NS)
            link = node.get("href", "") if node is not None else ""
        published = _parse_date(pick("pubDate", "atom:published", "atom:updated", "date"))
        body = pick("content:encoded", "description", "atom:content", "atom:summary")
        if not link and not title:
            continue
        content = _strip_html(body)
        items.append(
            Item(
                uid=f"feed:{link or title}",
                source_name=src.name,
                title=title or link,
                url=link,
                published=published,
                content=content,
                content_kind="feed",
                focus=src.focus,
                images=_extract_images(body),
                title_is_content=_same_text(title, content),
            )
        )
    return items


def _same_text(a: str, b: str) -> bool:
    """האם שני הטקסטים זהים עד כדי רווחים. פידים חותכים לפעמים את הכותרת."""
    norm_a, norm_b = " ".join(a.split()), " ".join(b.split())
    if not norm_a or not norm_b:
        return False
    shorter, longer = sorted((norm_a, norm_b), key=len)
    return longer.startswith(shorter[:120])


def load_feed_content(item: Item) -> None:
    """אם תקציר הפיד קצר מדי — מושך את העמוד עצמו.

    פריט קצר אך תקין (ציוץ, למשל) נשאר כמו שהוא ולא נמשך מחדש.
    """
    if item.has_content or not item.url.startswith("http"):
        return
    try:
        text = _strip_html(_get(item.url).text)
    except requests.RequestException as exc:
        log.warning("לא הצלחתי למשוך את %s: %s", item.url, exc)
        return
    if len(text) > len(item.content):
        item.content, item.content_kind = text[:20000], "page"


# ---------------------------------------------------------------- עמוד אינטרנט

def fetch_web(src, limit: int, cache: dict) -> list[Item]:
    raw = _get(src.url).text
    text = _strip_html(raw)
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = html.unescape(m.group(1)).strip() if m else ""
    # מזהה יציב בין ריצות: אותו עמוד ללא שינוי לא יישלח שוב
    digest = hashlib.sha1(text[:5000].encode("utf-8")).hexdigest()[:12]
    return [
        Item(
            uid=f"web:{src.url}:{digest}",
            source_name=src.name,
            title=title or src.name,
            url=src.url,
            published=datetime.now(timezone.utc),
            published_is_fetch_time=True,
            content=text[:20000],
            content_kind="page",
            focus=src.focus,
        )
    ]


# ---------------------------------------------------------------- טוויטר / X

NITTER_HOSTS = ["nitter.net", "nitter.poast.org", "nitter.privacydev.net"]


def fetch_twitter(src, limit: int, cache: dict) -> list[Item]:
    """X לא מציע פיד ציבורי חינמי. מנסים מראות Nitter; אם כולן נופלות — מדלגים."""
    handle = src.url.rstrip("/").split("/")[-1].lstrip("@")
    if src.url.startswith("http") and "nitter" in src.url:
        candidates = [src.url]
    else:
        candidates = [f"https://{h}/{handle}/rss" for h in NITTER_HOSTS]

    for feed_url in candidates:
        try:
            probe = type(src)(type="rss", name=src.name, url=feed_url, focus=src.focus)
            items = fetch_feed(probe, limit, cache)
            if items:
                return items
        except Exception as exc:  # noqa: BLE001 - כל מראה עלולה ליפול אחרת
            log.debug("מראת Nitter %s נכשלה: %s", feed_url, exc)
    log.warning("לא הצלחתי לשלוף את %s — אין מראת Nitter זמינה. מדלג.", src.name)
    return []


# ---------------------------------------------------------------- ניתוב

FETCHERS = {
    "youtube_channel": fetch_youtube_channel,
    "youtube_playlist": fetch_youtube_playlist,
    "youtube_video": fetch_youtube_video,
    "rss": fetch_feed,
    "podcast": fetch_feed,
    "twitter": fetch_twitter,
    "web": fetch_web,
}

LOADERS = {
    "youtube_channel": load_youtube_content,
    "youtube_playlist": load_youtube_content,
    "youtube_video": load_youtube_content,
    "rss": load_feed_content,
    "podcast": load_feed_content,
    "twitter": load_feed_content,
    "web": lambda item: None,
}


def fetch_source(src, limit: int, cache: dict) -> list[Item]:
    fetcher = FETCHERS.get(src.type)
    if not fetcher:
        log.warning("סוג מקור לא מוכר: %s (%s)", src.type, src.name)
        return []
    try:
        return fetcher(src, limit, cache)
    except Exception as exc:  # noqa: BLE001 - מקור שנופל לא מפיל את הריצה
        log.warning("שליפה נכשלה עבור %s: %s", src.name, exc)
        return []


def load_content(src_type: str, item: Item) -> None:
    loader = LOADERS.get(src_type)
    if not loader:
        return
    try:
        loader(item)
    except Exception as exc:  # noqa: BLE001
        log.warning("טעינת תוכן נכשלה עבור %s: %s", item.url, exc)
