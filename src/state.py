"""זיכרון בין ריצות — מה כבר נשלח, ומטמון של channel_id."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 30


class State:
    def __init__(self, path: Path):
        self.path = path
        self.sent: dict[str, str] = {}
        self.channel_ids: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("לא הצלחתי לקרוא את %s (%s) — מתחיל מאפס", self.path.name, exc)
            return
        self.sent = data.get("sent", {})
        self.channel_ids = data.get("channel_ids", {})

    def was_sent(self, uid: str) -> bool:
        return uid in self.sent

    def mark_sent(self, uid: str) -> None:
        self.sent[uid] = datetime.now(timezone.utc).isoformat()

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
        kept = {}
        for uid, stamp in self.sent.items():
            try:
                if datetime.fromisoformat(stamp) >= cutoff:
                    kept[uid] = stamp
            except ValueError:
                kept[uid] = stamp
        self.sent = kept

    def save(self) -> None:
        self._prune()
        payload = {"sent": self.sent, "channel_ids": self.channel_ids}
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("לא הצלחתי לשמור מצב: %s", exc)
