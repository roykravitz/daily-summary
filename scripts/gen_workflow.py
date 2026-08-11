"""מעדכן את שורות ה-cron ב-.github/workflows/digest.yml לפי schedule שב-config.yaml.

    python scripts/gen_workflow.py

ממיר את השעות המקומיות ל-UTC, כי GitHub Actions מריץ cron ב-UTC בלבד.
שים לב: ההמרה מתבצעת לפי מצב שעון הקיץ הנוכחי. אחרי מעבר שעון, הרץ שוב.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "digest.yml"


def to_utc_cron(times: list[str], tz_name: str) -> list[str]:
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    lines = []
    for value in times:
        hour, _, minute = value.partition(":")
        local = datetime(today.year, today.month, today.day,
                         int(hour), int(minute or 0), tzinfo=tz)
        utc = local.astimezone(ZoneInfo("UTC"))
        lines.append(f'    - cron: "{utc.minute} {utc.hour} * * *"')
    return lines


def main() -> int:
    config_path = ROOT / "config.yaml"
    if not config_path.exists():
        print("לא נמצא config.yaml — צור אותו קודם דרך setup.html", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    schedule = cfg.get("schedule") or {}
    times = schedule.get("run_at") or ["08:00"]
    tz_name = cfg.get("timezone", "Asia/Jerusalem")

    cron_lines = to_utc_cron(times, tz_name)
    text = WORKFLOW.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(  schedule:\n)(?:    - cron: .*\n)+",
        lambda m: m.group(1) + "\n".join(cron_lines) + "\n",
        text,
        count=1,
    )
    if not count:
        print("לא מצאתי בלוק schedule ב-digest.yml", file=sys.stderr)
        return 1

    WORKFLOW.write_text(updated, encoding="utf-8")
    print(f"עודכן {WORKFLOW.name}:")
    for local, line in zip(times, cron_lines):
        print(f"  {local} {tz_name}  ->  {line.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
