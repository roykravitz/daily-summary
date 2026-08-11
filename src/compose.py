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


def build_message(summaries: list[tuple], language: str, tz: str) -> str:
    """summaries: רשימת (Item, טקסט סיכום). מחזיר טקסט רגיל להודעה.

    אין כותרת עליונה: כל נושא נשלח לערוץ משלו, ושם הנושא היה חוזר על עצמו.
    """
    if not summaries:
        return EMPTY.get(language, EMPTY["en"])

    lines = []
    for item, summary in summaries:
        lines.append(source_header(item, tz, language))
        lines.append(item.title)
        lines.append(summary)
        lines.append(item.url)
        lines.append("")

    return "\n".join(lines).strip()


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
