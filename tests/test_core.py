"""בדיקות ליחידות שנוטות להישבר בשקט: פענוח פידים, פיצול הודעות, מצב.

    python -m pytest tests -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compose import build_message, split_message  # noqa: E402
from src.config import Source, load_config  # noqa: E402
from src.fetchers import Item, _parse_date, _strip_html, fetch_feed  # noqa: E402
from src.llm import build_prompt  # noqa: E402
from src.state import State  # noqa: E402


# ------------------------------------------------------------ תאריכים

@pytest.mark.parametrize("raw", [
    "Sat, 09 Aug 2026 05:30:00 +0000",   # RSS
    "2026-08-09T05:30:00+00:00",         # Atom
    "2026-08-09T05:30:00Z",
])
def test_parse_date_formats(raw):
    dt = _parse_date(raw)
    assert dt is not None and dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 8


def test_parse_date_garbage():
    assert _parse_date("לא תאריך") is None
    assert _parse_date(None) is None


# ------------------------------------------------------------ ניקוי HTML

def test_strip_html_removes_scripts_and_tags():
    raw = "<div><script>evil()</script><p>שלום <b>עולם</b></p><style>x{}</style></div>"
    text = _strip_html(raw)
    assert "evil" not in text and "<" not in text
    assert "שלום" in text and "עולם" in text


def test_strip_html_unescapes_entities():
    assert "&amp;" not in _strip_html("<p>A &amp; B</p>")


# ------------------------------------------------------------ פענוח פיד

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>פרק ראשון</title>
    <link>https://example.com/1</link>
    <pubDate>Sat, 09 Aug 2026 05:30:00 +0000</pubDate>
    <description>&lt;p&gt;תוכן הפרק&lt;/p&gt;</description>
  </item>
</channel></rss>"""


def test_fetch_feed_parses_rss(monkeypatch):
    class FakeResponse:
        content = RSS_SAMPLE.encode("utf-8")

    monkeypatch.setattr("src.fetchers._get", lambda url, **kw: FakeResponse())
    items = fetch_feed(Source("rss", "בדיקה", "https://x/feed"), 5, {})

    assert len(items) == 1
    assert items[0].title == "פרק ראשון"
    assert items[0].url == "https://example.com/1"
    assert items[0].published.year == 2026
    assert items[0].content == "תוכן הפרק"


# ------------------------------------------------------------ פיצול הודעות

def test_split_short_message_stays_whole():
    assert split_message("שורה קצרה") == ["שורה קצרה"]


def test_split_respects_limit_and_preserves_content():
    text = "\n".join(f"• נקודה מספר {i}" for i in range(400))
    chunks = split_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "נקודה מספר 399" in "".join(chunks)


def test_split_breaks_single_overlong_line():
    chunks = split_message("א" * 1200, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert sum(len(c) for c in chunks) == 1200


# ------------------------------------------------------------ הרכבת הודעה

def _item(name="ערוץ", title="כותרת"):
    return Item(uid="u1", source_name=name, title=title,
                url="https://youtu.be/x", published=datetime.now(timezone.utc))


def test_build_message_labels_every_item_with_its_source():
    pairs = [(_item("ערוץ א", "סרטון 1"), "• נקודה"),
             (_item("ערוץ א", "סרטון 2"), "• נקודה"),
             (_item("ערוץ ב", "סרטון 3"), "• נקודה")]
    msg = build_message(pairs, "he", "Asia/Jerusalem")
    assert msg.count("▪️ ערוץ א") == 2
    assert msg.count("▪️ ערוץ ב") == 1
    assert "סרטון 3" in msg


def test_build_message_has_no_top_heading():
    """שם הנושא היה חוזר על עצמו — כל נושא נשלח לערוץ משלו ממילא."""
    msg = build_message([(_item(), "• נקודה")], "he", "Asia/Jerusalem")
    assert msg.startswith("▪️")
    assert "📅" not in msg


def test_build_message_empty_says_so():
    assert "לא נמצא תוכן חדש" in build_message([], "he", "Asia/Jerusalem")


# ------------------------------------------------------------ הנחיה למודל

def test_prompt_contains_content_and_grounding_rule():
    item = _item()
    item.content = "טקסט התמלול כאן"
    prompt = build_prompt(item, "he", 5)
    assert "טקסט התמלול כאן" in prompt
    assert "אל תוסיף ידע חיצוני" in prompt
    assert "NO_CONTENT" in prompt


def test_prompt_truncates_huge_content():
    item = _item()
    item.content = "א" * 100000
    assert len(build_prompt(item, "he", 5)) < 45000


# ------------------------------------------------------------ מצב

def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    assert not state.was_sent("yt:abc")
    state.mark_sent("yt:abc")
    state.channel_ids["https://yt/@x"] = "UC" + "x" * 22
    state.save()

    reloaded = State(path)
    assert reloaded.was_sent("yt:abc")
    assert reloaded.channel_ids["https://yt/@x"].startswith("UC")


def test_state_survives_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ לא JSON תקין", encoding="utf-8")
    assert State(path).sent == {}


def test_state_prunes_old_entries(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    state.sent = {"old": "2020-01-01T00:00:00+00:00"}
    state.mark_sent("new")
    state.save()
    assert "old" not in State(path).sent
    assert "new" in State(path).sent


# ------------------------------------------------------------ הגדרות

def test_load_config_reads_example(tmp_path):
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.language == "he"
    assert len(cfg.enabled_topics) == 1
    topic = cfg.enabled_topics[0]
    assert topic.name == "כלכלה"
    assert len(topic.enabled_sources) == 2
    assert topic.targets[0]["chat_id_env"] == "TELEGRAM_CHAT_ID_ECONOMY"


def test_load_config_missing_file_explains(tmp_path):
    with pytest.raises(SystemExit, match="setup.html"):
        load_config(tmp_path / "nope.yaml")


# ------------------------------------------------------------ נושאים

TOPICS_YAML = """
language: he
defaults:
  lookback_hours: 24
  summary_bullets: 5
topics:
  - name: "כלכלה"
    emoji: "💰"
    focus: "מספרים"
    targets:
      - type: telegram
        chat_id_env: TELEGRAM_CHAT_ID_ECONOMY
    sources:
      - type: youtube_channel
        name: "ערוץ"
        url: "https://youtube.com/@a"
  - name: "ביטחון"
    lookback_hours: 6
    summary_bullets: 8
    targets:
      - type: telegram
        chat_id_env: TELEGRAM_CHAT_ID_SECURITY
    sources:
      - type: rss
        name: "פיד"
        url: "https://x/rss"
  - name: "מושבת"
    enabled: false
    sources:
      - type: rss
        name: "פיד"
        url: "https://y/rss"
"""


def _write(tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_topics_get_own_targets_and_overrides(tmp_path):
    cfg = load_config(_write(tmp_path, TOPICS_YAML))
    economy, security = cfg.topics[0], cfg.topics[1]

    assert economy.lookback_hours == 24 and economy.summary_bullets == 5
    assert security.lookback_hours == 6 and security.summary_bullets == 8
    assert economy.targets[0]["chat_id_env"] != security.targets[0]["chat_id_env"]
    assert economy.title == "💰 כלכלה"


def test_disabled_topic_is_skipped(tmp_path):
    cfg = load_config(_write(tmp_path, TOPICS_YAML))
    assert len(cfg.topics) == 3
    assert [t.name for t in cfg.enabled_topics] == ["כלכלה", "ביטחון"]


def test_topic_slug_is_env_safe_and_distinct(tmp_path):
    cfg = load_config(_write(tmp_path, TOPICS_YAML))
    slugs = [t.slug for t in cfg.topics]
    assert len(set(slugs)) == len(slugs)
    assert all(" " not in s and "|" not in s for s in slugs)


LEGACY_YAML = """
language: he
lookback_hours: 48
sources:
  - type: youtube_channel
    name: "ערוץ ישן"
    url: "https://youtube.com/@old"
targets:
  - type: telegram
"""


def test_legacy_flat_config_still_loads(tmp_path):
    """הפורמט הישן בלי topics צריך להמשיך לעבוד, כנושא יחיד."""
    cfg = load_config(_write(tmp_path, LEGACY_YAML))
    assert len(cfg.enabled_topics) == 1
    topic = cfg.enabled_topics[0]
    assert topic.lookback_hours == 48
    assert topic.enabled_sources[0].name == "ערוץ ישן"
    assert topic.targets[0]["type"] == "telegram"


# ------------------------------------------------------------ מקור מסוג עמוד

def test_web_uid_is_stable_across_runs(monkeypatch):
    """uid חייב להיות יציב, אחרת אותו עמוד נשלח מחדש בכל ריצה."""
    from src.fetchers import fetch_web

    class FakeResponse:
        text = "<html><title>כותרת</title><body><p>תוכן קבוע</p></body></html>"

    monkeypatch.setattr("src.fetchers._get", lambda url, **kw: FakeResponse())
    src = Source("web", "עמוד", "https://example.com")
    first = fetch_web(src, 1, {})[0]
    second = fetch_web(src, 1, {})[0]

    assert first.uid == second.uid
    assert first.title == "כותרת"
    assert "תוכן קבוע" in first.content


# ------------------------------------------------------------ יעדי טלגרם

def test_telegram_uses_per_topic_chat_id(monkeypatch):
    """כל נושא חייב להגיע לערוץ שלו, לא לערוץ ברירת המחדל."""
    from src import deliver as d

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "default")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_ECONOMY", "111")

    sent = []

    class FakeResponse:
        ok = True

    monkeypatch.setattr(d.requests, "post",
                        lambda url, data, timeout: sent.append(data) or FakeResponse())
    monkeypatch.setattr(d.time, "sleep", lambda s: None)

    assert d.send_telegram("שלום", {"type": "telegram", "chat_id_env": "TELEGRAM_CHAT_ID_ECONOMY"})
    assert sent[0]["chat_id"] == "111"


def test_telegram_falls_back_to_default_chat_id(monkeypatch):
    from src import deliver as d

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "default")
    sent = []

    class FakeResponse:
        ok = True

    monkeypatch.setattr(d.requests, "post",
                        lambda url, data, timeout: sent.append(data) or FakeResponse())
    monkeypatch.setattr(d.time, "sleep", lambda s: None)

    assert d.send_telegram("שלום", {"type": "telegram"})
    assert sent[0]["chat_id"] == "default"


def test_telegram_missing_chat_id_fails_clearly(monkeypatch):
    from src import deliver as d

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert d.send_telegram("שלום", {"type": "telegram", "chat_id_env": "MISSING"}) is False


def test_source_header_carries_publish_time():
    item = _item("CryptoJungle")
    item.published = datetime.now(timezone.utc) - timedelta(hours=19)
    first = build_message([(item, "• נקודה")], "he", "Asia/Jerusalem").splitlines()[0]
    assert first.startswith("▪️ CryptoJungle")
    assert "לפני 19 שעות" in first


# ------------------------------------------------------------ נרמול נקודות

def test_bullets_drop_leaked_model_reasoning():
    """חשיבה פנימית של המודל דולפת לפעמים כשורות בלי סימון — אסור שתהפוך לנקודה."""
    from src.llm import _to_bullets

    out = _to_bullets(
        "• הביטקוין ירד ל-64,500 דולר.\n"
        "Let's double-check all stats against transcript:\n"
        "half a trillion - checked.\n"
        "• הנאסד\"ק ירד ב-0.29%."
    )
    assert "double-check" not in out
    assert "checked" not in out
    assert out.count("•") == 2


def test_bullets_kept_when_model_used_no_markers():
    from src.llm import _to_bullets

    out = _to_bullets("נקודה ראשונה\nנקודה שנייה")
    assert out == "• נקודה ראשונה\n• נקודה שנייה"


def test_bullets_normalise_dash_markers():
    from src.llm import _to_bullets

    assert _to_bullets("- ראשונה\n– שנייה") == "• ראשונה\n• שנייה"


# ------------------------------------------------------------ תאריך פרסום

from src.compose import format_published, humanize_age  # noqa: E402


@pytest.mark.parametrize("minutes,expected", [
    (0, "עכשיו"),
    (1, "לפני דקה"),
    (2, "לפני שתי דקות"),
    (45, "לפני 45 דקות"),
    (60, "לפני שעה"),
    (120, "לפני שעתיים"),
    (300, "לפני 5 שעות"),
    (1440, "אתמול"),
    (2880, "לפני יומיים"),
    (4320, "לפני 3 ימים"),
])
def test_humanize_age_hebrew(minutes, expected):
    now = datetime.now(timezone.utc)
    assert humanize_age(now - timedelta(minutes=minutes), now, "he") == expected


def test_published_line_has_date_and_age():
    published = datetime.now(timezone.utc) - timedelta(hours=3)
    line = format_published(published, "Asia/Jerusalem", "he")
    assert "·" in line
    assert "לפני 3 שעות" in line
    assert published.strftime("%Y")[-2:] in line  # השנה מופיעה


def test_fetch_time_never_shown_as_publish_time():
    """זמן שליפה חייב להיות מסומן, אחרת תוכן ישן נראה כאילו פורסם עכשיו."""
    now = datetime.now(timezone.utc)
    line = format_published(now, "Asia/Jerusalem", "he", is_fetch_time=True)
    assert "נשלף" in line
    assert "עכשיו" not in line


def test_no_date_line_when_publish_date_unknown():
    assert format_published(None, "Asia/Jerusalem", "he") == ""


def test_publish_time_sits_on_source_line_not_under_title():
    item = _item("CryptoJungle", title="כותרת הסרטון")
    item.published = datetime.now(timezone.utc) - timedelta(hours=2)
    lines = build_message([(item, "• נקודה")], "he", "Asia/Jerusalem").splitlines()

    assert "לפני שעתיים" in lines[0]
    assert lines[1] == "כותרת הסרטון"
    assert not lines[2].startswith("🕐")


# ------------------------------------------------------------ מכסת ספק המודל

def test_quota_exhaustion_stops_further_calls(monkeypatch):
    """כשהמכסה נגמרת אין טעם להמשיך — אחרת כל מקור שורף עוד ניסיון והמתנה."""
    from src import llm

    llm.reset_quota_state()
    calls = []

    def boom(prompt, provider, model):
        calls.append(prompt)
        raise llm.QuotaError("429 quota exceeded")

    monkeypatch.setattr(llm, "complete", boom)
    item = _item()
    item.content = "טקסט ארוך מספיק כדי לעבור את הסף " * 20

    for _ in range(5):
        assert llm.summarize(item, "he", 5, "gemini", "m") == ""

    assert len(calls) == llm.QUOTA_STRIKES_BEFORE_STOP
    llm.reset_quota_state()


def test_ordinary_failure_does_not_trip_the_breaker(monkeypatch):
    from src import llm

    llm.reset_quota_state()
    calls = []

    def boom(prompt, provider, model):
        calls.append(prompt)
        raise llm.LLMError("שגיאה רגילה")

    monkeypatch.setattr(llm, "complete", boom)
    item = _item()
    item.content = "טקסט " * 100

    for _ in range(4):
        llm.summarize(item, "he", 5, "gemini", "m")

    assert len(calls) == 4
    llm.reset_quota_state()


def test_quota_message_names_the_tier_and_limit():
    """ההודעה חייבת לומר אם זו מכסה חינמית או בתשלום — זה מה שקובע מה לעשות."""
    from src.llm import _quota_message, _retry_delay

    class FakeResponse:
        text = ""

        @staticmethod
        def json():
            return {"error": {"details": [
                {"@type": "...QuotaFailure", "violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaValue": "20",
                    "quotaDimensions": {"model": "gemini-3.6-flash"},
                }]},
                {"@type": "...RetryInfo", "retryDelay": "36s"},
            ]}}

    message = _quota_message(FakeResponse())
    assert "החינמית" in message
    assert "20" in message
    assert "gemini-3.6-flash" in message
    assert _retry_delay(FakeResponse()) == 37


# ------------------------------------------------------------ פרוקסי לתמלולים

def test_no_proxy_by_default(monkeypatch):
    from src import transcript

    for var in ("WEBSHARE_PROXY_USERNAME", "WEBSHARE_PROXY_PASSWORD",
                "HTTP_PROXY_URL", "HTTPS_PROXY_URL"):
        monkeypatch.delenv(var, raising=False)
    assert transcript._proxy_config() is None
    assert transcript.proxy_is_configured() is False


def test_webshare_proxy_detected(monkeypatch):
    from src import transcript

    monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "u")
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "p")
    assert transcript.proxy_is_configured() is True
    assert type(transcript._proxy_config()).__name__ == "WebshareProxyConfig"


def test_generic_proxy_detected(monkeypatch):
    from src import transcript

    monkeypatch.delenv("WEBSHARE_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("WEBSHARE_PROXY_PASSWORD", raising=False)
    monkeypatch.setenv("HTTP_PROXY_URL", "http://user:pass@host:8080")
    assert transcript.proxy_is_configured() is True
    assert type(transcript._proxy_config()).__name__ == "GenericProxyConfig"


# ------------------------------------------------------------ פריטים קצרים ותמונות

def test_short_item_is_kept_not_dropped():
    """ציוץ קצר הוא תוכן לגיטימי. הסף הישן של 200 תווים מחק אותו בשקט."""
    item = _item()
    item.content = "GM traders. $INIT מעניין לטווח קצר, רמת תמיכה 1.20"
    assert item.has_content is True
    assert item.needs_summary is False


def test_empty_item_is_dropped():
    item = _item()
    item.content = "  "
    assert item.has_content is False


def test_long_item_still_summarised():
    item = _item()
    item.content = "מילה " * 300
    assert item.needs_summary is True


def test_images_rewritten_to_twitter_cdn():
    """מראות Nitter מתחלפות ונופלות — עדיף להצביע על ה-CDN המקורי."""
    from src.fetchers import _extract_images

    body = '<p>טקסט</p><img src="https://nitter.net/pic/media%2FABC123.jpg" />'
    assert _extract_images(body) == ["https://pbs.twimg.com/media/ABC123.jpg"]


def test_images_deduplicated_and_capped():
    from src.fetchers import MAX_IMAGES_PER_ITEM, _extract_images

    body = '<img src="https://x/a.jpg">' * 3 + "".join(
        f'<img src="https://x/{i}.jpg">' for i in range(10)
    )
    urls = _extract_images(body)
    assert len(urls) <= MAX_IMAGES_PER_ITEM
    assert len(set(urls)) == len(urls)


def test_feed_item_carries_images(monkeypatch):
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>ציוץ עם גרף</title><link>https://x.com/u/status/1</link>
      <pubDate>Tue, 11 Aug 2026 15:42:54 GMT</pubDate>
      <description>&lt;p&gt;רמות תמיכה&lt;/p&gt;&lt;img src="https://nitter.net/pic/media%2FZZ.jpg"/&gt;</description>
    </item></channel></rss>"""

    class FakeResponse:
        content = feed.encode("utf-8")

    monkeypatch.setattr("src.fetchers._get", lambda url, **kw: FakeResponse())
    item = fetch_feed(Source("twitter", "EliZ", "https://x/rss"), 5, {})[0]
    assert item.images == ["https://pbs.twimg.com/media/ZZ.jpg"]
    assert item.published is not None


def test_message_does_not_repeat_tweet_text_twice():
    """בציוץ הכותרת והתוכן זהים — אסור להדפיס פעמיים."""
    item = _item(title="רמות תמיכה 1.20")
    msg = build_message([(item, "רמות תמיכה 1.20")], "he", "Asia/Jerusalem")
    assert msg.count("רמות תמיכה 1.20") == 1


def test_message_keeps_distinct_title():
    item = _item(title="כותרת הסרטון")
    msg = build_message([(item, "• נקודה אחרת לגמרי")], "he", "Asia/Jerusalem")
    assert "כותרת הסרטון" in msg and "נקודה אחרת לגמרי" in msg


# ------------------------------------------------------------ סדר, בלוקים ותרגום

def _timed(name, minutes_ago, images=None):
    item = _item(name)
    item.published = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    item.images = images or []
    return item


def test_items_sorted_newest_first_across_sources():
    """הסדר צריך להיות ציר זמן אחד, לא ערבוב לפי סדר המקורות בקובץ."""
    from src.compose import build_blocks

    pairs = [(_timed("ישן", 600), "א"), (_timed("חדש", 5), "ב"), (_timed("אמצע", 120), "ג")]
    text = build_message(pairs, "he", "Asia/Jerusalem")
    assert text.index("חדש") < text.index("אמצע") < text.index("ישן")
    assert len(build_blocks(pairs, "he", "Asia/Jerusalem")) == 1


def test_item_without_date_sinks_to_the_end():
    item = _item("בלי תאריך")
    item.published = None
    pairs = [(item, "א"), (_timed("עם תאריך", 300), "ב")]
    text = build_message(pairs, "he", "Asia/Jerusalem")
    assert text.index("עם תאריך") < text.index("בלי תאריך")


def test_image_becomes_its_own_block_with_the_text_as_caption():
    """הגרף והניתוח שנכתב עליו חייבים להגיע יחד, כמו במקור."""
    from src.compose import build_blocks

    pairs = [
        (_timed("בלי", 10), "טקסט רגיל"),
        (_timed("עם גרף", 5, ["https://pbs.twimg.com/media/A.jpg"]), "רמת תמיכה 63,800"),
    ]
    blocks = build_blocks(pairs, "he", "Asia/Jerusalem")

    assert [b["type"] for b in blocks] == ["photo", "text"]   # הפריט עם הגרף חדש יותר
    photo = blocks[0]
    assert photo["images"] == ["https://pbs.twimg.com/media/A.jpg"]
    assert "רמת תמיכה 63,800" in photo["caption"]
    assert "עם גרף" in photo["caption"]


def test_text_version_keeps_every_item():
    """אימייל ו-webhook מקבלים טקסט אחד — אסור שפריט עם תמונה ייעלם."""
    pairs = [(_timed("א", 10), "ראשון"),
             (_timed("ב", 5, ["https://x/1.jpg"]), "שני")]
    text = build_message(pairs, "he", "Asia/Jerusalem")
    assert "ראשון" in text and "שני" in text


@pytest.mark.parametrize("text,language,expected", [
    ("After Monday close 2 trigger in play, now time to sleep", "he", True),
    ("הנפט מזנק והשוק נבלם היום בבורסה בתל אביב", "he", False),
    ("GM 👋", "he", False),
    ("הנפט מזנק והשוק נבלם", "en", True),
])
def test_translation_detection(text, language, expected):
    from src.llm import needs_translation

    assert needs_translation(text, language) is expected


def test_translation_prompt_forbids_summarising():
    from src.llm import build_translation_prompt

    prompt = build_translation_prompt("$BTC support at 63,800", "he")
    assert "$BTC support at 63,800" in prompt
    assert "אל תקצר" in prompt
    assert "עברית" in prompt


# ------------------------------------------------------------ חסימת IP

def test_ip_block_stops_further_transcript_attempts(monkeypatch):
    """כל ניסיון נוסף אחרי חסימה רק מאריך אותה — צריך לעצור מיד."""
    from src import transcript

    transcript.reset_block_state()
    calls = []

    class IpBlocked(Exception):
        pass

    def boom(video_id):
        calls.append(video_id)
        raise IpBlocked("YouTube is blocking requests from your IP")

    monkeypatch.setattr(transcript, "_via_api", boom)
    monkeypatch.setattr(transcript, "_via_web", boom)

    assert transcript.fetch_transcript("aaa") == ""
    assert transcript.fetch_transcript("bbb") == ""
    assert transcript.fetch_transcript("ccc") == ""
    assert calls == ["aaa"]          # רק הראשון ניסה
    transcript.reset_block_state()


def test_ordinary_transcript_failure_still_tries_both_methods(monkeypatch):
    from src import transcript

    transcript.reset_block_state()
    calls = []

    def api_fails(video_id):
        calls.append("api")
        raise ValueError("אין כתוביות")

    def web_works(video_id):
        calls.append("web")
        return "טקסט " * 100

    monkeypatch.setattr(transcript, "_via_api", api_fails)
    monkeypatch.setattr(transcript, "_via_web", web_works)
    monkeypatch.setattr(transcript.time, "sleep", lambda s: None)

    assert len(transcript.fetch_transcript("x")) > 200
    assert "web" in calls
    transcript.reset_block_state()


# ------------------------------------------------------------ ציוץ מתורגם

TWEET_FEED = """<?xml version="1.0"?><rss version="2.0"><channel><item>
  <title>GN We have now reached the limit of what we can bear. Three months of trading.</title>
  <link>https://x.com/u/status/1</link>
  <pubDate>Fri, 14 Aug 2026 03:15:00 GMT</pubDate>
  <description>&lt;p&gt;GN&lt;br&gt;&lt;br&gt;We have now reached the limit of what we can bear. Three months of trading.&lt;/p&gt;</description>
</item></channel></rss>"""


def test_tweet_title_marked_as_content(monkeypatch):
    class FakeResponse:
        content = TWEET_FEED.encode("utf-8")

    monkeypatch.setattr("src.fetchers._get", lambda url, **kw: FakeResponse())
    item = fetch_feed(Source("twitter", "EliZ", "https://x/rss"), 5, {})[0]
    assert item.title_is_content is True


def test_translated_tweet_not_printed_twice():
    """התרגום גורם לכותרת ולתוכן להיראות שונים — בלי הסימון הציוץ הודפס פעמיים."""
    item = _item("EliZ", title="GN We have now reached the limit of what we can bear.")
    item.title_is_content = True
    msg = build_message([(item, "GN הגענו לגבול של מה שאנחנו יכולים לשאת.")], "he", "Asia/Jerusalem")

    assert "We have now reached" not in msg      # המקור באנגלית לא מודפס
    assert "הגענו לגבול" in msg                   # התרגום כן
    assert msg.count("EliZ") == 1


def test_article_title_still_printed():
    """בכתבה הכותרת שונה מהגוף וחייבת להישאר."""
    item = _item("פיד", title="הנפט מזנק 4%")
    assert item.title_is_content is False
    msg = build_message([(item, "• תוכן הכתבה על משהו אחר לגמרי")], "he", "Asia/Jerusalem")
    assert "הנפט מזנק 4%" in msg
