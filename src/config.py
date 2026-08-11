"""טעינת config.yaml ו-.env.

המבנה מאורגן סביב **נושאים**: לכל נושא (כלכלה, ביטחון, ספורט...) יש רשימת
מקורות משלו ויעד שליחה משלו — למשל ערוץ טלגרם נפרד.

הפורמט הישן והשטוח (sources ו-targets ברמה העליונה) עדיין נתמך, ונטען
כנושא יחיד בלי שם.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """טוען .env לתוך os.environ. משתני סביבה קיימים גוברים (חשוב ל-GitHub Actions)."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Source:
    type: str
    name: str
    url: str
    focus: str = ""
    enabled: bool = True


@dataclass
class Topic:
    """נושא אחד — קבוצת מקורות שנשלחת ליעד משלה."""
    name: str
    emoji: str = ""
    enabled: bool = True
    lookback_hours: int = 24
    max_items_per_source: int = 2
    summary_bullets: int = 5
    focus: str = ""
    targets: list[dict] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """מזהה יציב לשימוש בקובץ המצב ובשמות משתני סביבה."""
        cleaned = re.sub(r"[^\w]+", "_", self.name, flags=re.UNICODE).strip("_")
        return cleaned or "topic"

    @property
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled and s.url]

    @property
    def title(self) -> str:
        return f"{self.emoji} {self.name}".strip()


@dataclass
class Config:
    language: str = "he"
    timezone: str = "Asia/Jerusalem"
    llm_provider: str = "gemini"
    llm_model: str = ""
    topics: list[Topic] = field(default_factory=list)

    @property
    def enabled_topics(self) -> list[Topic]:
        return [t for t in self.topics if t.enabled and t.enabled_sources]


DEFAULT_MODELS = {
    "gemini": "gemini-flash-latest",
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
    "claude_cli": "",
    "mock": "",
}

DEFAULTS = {"lookback_hours": 24, "max_items_per_source": 2, "summary_bullets": 5}


def _parse_sources(raw_list) -> list[Source]:
    sources = []
    for item in raw_list or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        sources.append(
            Source(
                type=item.get("type", "web"),
                name=item.get("name") or item["url"],
                url=item["url"],
                focus=item.get("focus", ""),
                enabled=item.get("enabled", True),
            )
        )
    return sources


def _parse_topic(raw: dict, defaults: dict) -> Topic:
    def pick(key: str):
        value = raw.get(key, defaults.get(key))
        return int(value) if value is not None else DEFAULTS[key]

    return Topic(
        name=raw.get("name", "כללי"),
        emoji=raw.get("emoji", ""),
        enabled=raw.get("enabled", True),
        lookback_hours=pick("lookback_hours"),
        max_items_per_source=pick("max_items_per_source"),
        summary_bullets=pick("summary_bullets"),
        focus=raw.get("focus", ""),
        targets=raw.get("targets") or [{"type": "telegram"}],
        sources=_parse_sources(raw.get("sources")),
    )


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    if not path.exists():
        raise SystemExit(
            f"לא נמצא קובץ הגדרות: {path}\n"
            "פתח את setup.html, מלא את הטופס והורד config.yaml לתיקייה הזו."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    llm = raw.get("llm") or {}
    provider = llm.get("provider", "gemini")

    # ברירות מחדל: מבלוק defaults, ובנפילה אחורה לשדות של הפורמט הישן
    defaults = {**DEFAULTS}
    for key in DEFAULTS:
        if key in raw:
            defaults[key] = raw[key]
    defaults.update(raw.get("defaults") or {})

    if raw.get("topics"):
        topics = [_parse_topic(t, defaults) for t in raw["topics"] if isinstance(t, dict)]
    else:
        # פורמט ישן ושטוח — נושא יחיד
        topics = [
            _parse_topic(
                {
                    "name": raw.get("name", "עדכון"),
                    "targets": raw.get("targets"),
                    "sources": raw.get("sources"),
                },
                defaults,
            )
        ]

    return Config(
        language=raw.get("language", "he"),
        timezone=raw.get("timezone", "Asia/Jerusalem"),
        llm_provider=provider,
        llm_model=llm.get("model") or DEFAULT_MODELS.get(provider, ""),
        topics=topics,
    )
