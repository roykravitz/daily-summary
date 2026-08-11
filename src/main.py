"""נקודת הכניסה — אוסף, מסכם, שולח. נושא אחר נושא.

    python -m src.main                    כל הנושאים
    python -m src.main --topic כלכלה      נושא אחד בלבד
    python -m src.main --dry-run          מדפיס למסך במקום לשלוח
    python -m src.main --force            מתעלם מחלון הטריות וממה שכבר נשלח
    python -m src.main --list             מציג את הנושאים והמקורות ויוצא
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .compose import build_message
from .config import ROOT, Config, Topic, load_config, load_env
from .deliver import deliver
from .fetchers import fetch_source, load_content
from .llm import summarize
from .state import State

log = logging.getLogger("digest")


def setup_logging(verbose: bool) -> None:
    # קונסולת חלונות היא cp1252 כברירת מחדל ומתה על עברית
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def collect_topic(topic: Topic, cfg: Config, state: State, force: bool) -> list[tuple]:
    """אוסף ומסכם את כל המקורות של נושא אחד."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=topic.lookback_hours)
    results: list[tuple] = []

    for src in topic.enabled_sources:
        log.info("  — מקור: %s (%s)", src.name, src.type)
        items = fetch_source(src, topic.max_items_per_source, state.channel_ids)
        if not items:
            continue

        fresh = []
        for item in items:
            # המפתח כולל את הנושא: אותו סרטון בשני נושאים יישלח לשני הערוצים
            item.state_key = f"{topic.slug}|{item.uid}"
            if not force and state.was_sent(item.state_key):
                continue
            if not force and item.published and item.published < cutoff:
                continue
            fresh.append(item)
            if len(fresh) >= topic.max_items_per_source:
                break

        if not fresh:
            log.info("    אין תוכן חדש")
            continue

        for item in fresh:
            log.info("    פריט: %s", item.title[:70])
            load_content(src.type, item)
            if not item.has_content:
                log.info("      אין מספיק תוכן לסיכום — מדלג")
                continue
            # מיקוד ברמת הנושא משמש כברירת מחדל למקורות שלא הגדירו משלהם
            item.focus = item.focus or topic.focus
            summary = summarize(
                item, cfg.language, topic.summary_bullets, cfg.llm_provider, cfg.llm_model
            )
            if not summary:
                continue
            log.info("      סוכם (%s, %d תווים)", item.content_kind, len(item.content))
            results.append((item, summary))

    return results


def list_topics(cfg: Config) -> None:
    for topic in cfg.topics:
        mark = "" if topic.enabled else "  [מושבת]"
        print(f"\n{topic.title}{mark}")
        targets = ", ".join(
            t.get("chat_id_env") or t.get("type", "?") for t in topic.targets
        )
        print(f"  יעד: {targets}")
        print(f"  טריות: {topic.lookback_hours}ש  ·  עד {topic.max_items_per_source} פריטים למקור")
        for src in topic.sources:
            state = "" if src.enabled else " (מושבת)"
            print(f"    · [{src.type}] {src.name}{state}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="סיכום מידע יומי")
    parser.add_argument("--topic", action="append", help="הרץ רק נושא זה (אפשר לחזור על הדגל)")
    parser.add_argument("--list", action="store_true", help="הצג נושאים ומקורות וצא")
    parser.add_argument("--dry-run", action="store_true", help="הדפס למסך, אל תשלח")
    parser.add_argument("--force", action="store_true", help="התעלם מטריות ומהיסטוריית שליחה")
    parser.add_argument("--config", type=Path, help="נתיב ל-config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    load_env()
    cfg = load_config(args.config)

    if args.list:
        list_topics(cfg)
        return 0

    topics = cfg.enabled_topics
    if args.topic:
        wanted = {t.strip().lower() for t in args.topic}
        topics = [t for t in topics if t.name.strip().lower() in wanted or t.slug.lower() in wanted]
        if not topics:
            log.error("לא נמצא נושא בשם %s. הרץ --list כדי לראות מה קיים.", ", ".join(args.topic))
            return 1

    if not topics:
        log.error("אין נושאים פעילים עם מקורות ב-config.yaml")
        return 1

    log.info("מתחיל — %d נושאים, מודל %s/%s",
             len(topics), cfg.llm_provider, cfg.llm_model or "ברירת מחדל")

    state = State(ROOT / "state.json")
    sent_any = False
    failed_any = False

    for topic in topics:
        log.info("▪ נושא: %s (%d מקורות)", topic.title, len(topic.enabled_sources))
        summaries = collect_topic(topic, cfg, state, args.force)

        if not summaries:
            log.info("  אין תוכן חדש — לא נשלחת הודעה לנושא הזה")
            continue

        message = build_message(summaries, cfg.language, cfg.timezone)

        if args.dry_run:
            print("\n" + "=" * 60)
            print(message)
            print("=" * 60)
            sent_any = True
            continue

        if not deliver(message, topic.targets):
            log.error("  שליחה נכשלה לנושא %s — יישלח שוב בריצה הבאה", topic.name)
            failed_any = True
            continue

        for item, _ in summaries:
            state.mark_sent(item.state_key)
        sent_any = True
        log.info("  נשלחו %d פריטים", len(summaries))

    if args.dry_run:
        log.info("ריצת יבש — לא נשלח כלום, המצב לא נשמר")
        return 0

    state.save()
    if failed_any and not sent_any:
        return 1
    log.info("הסתיים")
    return 0


if __name__ == "__main__":
    sys.exit(main())
