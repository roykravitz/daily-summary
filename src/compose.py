"""הרכבת ההודעה הסופית מתוך הפריטים שסוכמו."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TELEGRAM_LIMIT = 4096
SAFE_CHUNK = 3800

HEADERS = {
    "he": "סיכום מידע יומי",
    "en": "Daily Information Digest",
    "ar": "ملخص المعلومات اليومي",
    "ru": "Ежедневная сводка",
}

EMPTY = {
    "he": "לא נמצא תוכן חדש במקורות שהוגדרו.",
    "en": "No new content found in the configured sources.",
    "ar": "لم يتم العثور على محتوى جديد.",
    "ru": "Нового содержимого не найдено.",
}


def local_now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 - אזור זמן לא חוקי לא מפיל ריצה
        return datetime.now()


def _zone(tz: str):
    try:
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        return timezone.utc


# תבניות גיל: (מפריד עליון בשניות, יחיד, זוגי, רבים עם %d)
_AGE_HE = [
    (60, "עכשיו", "עכשיו", "עכשיו"),
    (3600, "לפני דקה", "לפני שתי דקות", "לפני %d דקות"),
    (86400, "לפני שעה", "לפני שעתיים", "לפני %d שעות"),
    (604800, "אתמול", "לפני יומיים", "לפני %d ימים"),
    (None, "לפני שבוע", "לפני שבועיים", "לפני %d שבועות"),
]
_AGE_EN = [
    (60, "just now", "just now", "just now"),
    (3600, "1 minute ago", "2 minutes ago", "%d minutes ago"),
    (86400, "1 hour ago", "2 hours ago", "%d hours ago"),
    (604800, "yesterday", "2 days ago", "%d days ago"),
    (None, "1 week ago", "2 weeks ago", "%d weeks ago"),
]
_UNIT_SECONDS = [1, 60, 3600, 86400, 604800]


def humanize_age(published: datetime, now: datetime, language: str = "he") -> str:
    """גיל הפריט במילים — זה מה שעונה על 'האם המידע אקטואלי' במבט אחד."""
    seconds = (now - published).total_seconds()
    if seconds < 0:
        seconds = 0

    table = _AGE_HE if language == "he" else _AGE_EN
    for index, (limit, one, two, many) in enumerate(table):
        if limit is None or seconds < limit:
            count = int(seconds // _UNIT_SECONDS[index])
            if index == 0:
                return one
            return one if count == 1 else two if count == 2 else many % count
    return ""


FETCHED_LABEL = {"he": "נשלף", "en": "retrieved"}


def format_published(
    published: datetime | None, tz: str, language: str = "he", is_fetch_time: bool = False
) -> str:
    """שורת תאריך לפריט, למשל: 🕐 11/08/2026 09:30 · לפני 3 שעות

    כשאין תאריך פרסום אמיתי ורק ידוע מתי שלפנו, זה מסומן במפורש כ"נשלף",
    כדי שלא ייראה כאילו התוכן פורסם עכשיו.
    """
    if published is None:
        return ""
    zone = _zone(tz)
    stamp = published.astimezone(zone).strftime("%d/%m/%Y %H:%M")

    if is_fetch_time:
        label = FETCHED_LABEL.get(language, FETCHED_LABEL["en"])
        return f"{label} {stamp}"

    age = humanize_age(published, datetime.now(zone), language)
    return f"{stamp} · {age}" if age else stamp


def source_header(item, tz: str, language: str = "he") -> str:
    """שורת המקור עם זמן הפרסום צמוד אליה.

    למשל: ▪️ CryptoJungle · 10/08/2026 17:15 · לפני 19 שעות
    """
    published = format_published(
        item.published, tz, language, item.published_is_fetch_time
    )
    header = f"▪️ {item.source_name}"
    return f"{header} · {published}" if published else header


def _entry(item, summary: str, tz: str, language: str) -> str:
    lines = [source_header(item, tz, language)]
    if not _title_repeats(item.title, summary):
        lines.append(item.title)
    lines.append(summary)
    lines.append(item.url)
    return "\n".join(lines)


def _by_time(pair):
    """החדש קודם. פריט בלי תאריך יורד לסוף."""
    published = pair[0].published
    return published or datetime.min.replace(tzinfo=timezone.utc)


def build_blocks(summaries: list[tuple], language: str, tz: str) -> list[dict]:
    """מפרק את העדכון לבלוקים לפי סדר הזמן.

    פריט עם תמונה נשלח כתמונה שהטקסט שלו הוא הכיתוב, כדי שהגרף והניתוח
    שנכתב עליו יגיעו יחד — כמו שהם מופיעים במקור.
    """
    if not summaries:
        return [{"type": "text", "text": EMPTY.get(language, EMPTY["en"])}]

    blocks: list[dict] = []
    buffer: list[str] = []

    def flush():
        if buffer:
            blocks.append({"type": "text", "text": "\n\n".join(buffer)})
            buffer.clear()

    for item, summary in sorted(summaries, key=_by_time, reverse=True):
        entry = _entry(item, summary, tz, language)
        if item.images:
            flush()
            blocks.append({"type": "photo", "images": item.images, "caption": entry})
        else:
            buffer.append(entry)
    flush()
    return blocks


def build_message(summaries: list[tuple], language: str, tz: str) -> str:
    """גרסת טקסט אחת של כל העדכון — לאימייל, ל-webhook ולריצת יבש."""
    blocks = build_blocks(summaries, language, tz)
    parts = [b["text"] if b["type"] == "text" else b["caption"] for b in blocks]
    return "\n\n".join(p for p in parts if p).strip()


def _title_repeats(title: str, summary: str) -> bool:
    """בציוץ הכותרת היא הטקסט עצמו — אין טעם להדפיס אותו פעמיים."""
    def norm(text: str) -> str:
        return " ".join(text.split())[:80]

    a, b = norm(title), norm(summary)
    return bool(a) and (a == b or b.startswith(a) or a.startswith(b))


def split_message(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """מפצל לפי שורות כדי לא לחתוך באמצע נקודה."""
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # שורה בודדת ארוכה מהמגבלה
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks
