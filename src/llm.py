"""שכבת מודל שפה — Gemini, Anthropic, OpenAI, או claude CLI מקומי.

הכל דרך requests, בלי SDK-ים נוספים, כדי שההתקנה תישאר קלה.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time

import requests

log = logging.getLogger(__name__)

TIMEOUT = 180
MAX_CHARS = 40000          # חיתוך תוכן ארוך לפני שליחה למודל
MAX_OUTPUT_TOKENS = 8192   # נדיב בכוונה — טוקני חשיבה נספרים בתוך המגבלה

LANG_NAMES = {"he": "עברית", "en": "English", "ar": "العربية", "ru": "русский"}


class LLMError(RuntimeError):
    pass


class QuotaError(LLMError):
    """חריגה ממכסה או מקצב הבקשות (429)."""


# מפסק: אחרי כמה כשלי מכסה ברצף אין טעם להמשיך לנסות בריצה הזו
QUOTA_STRIKES_BEFORE_STOP = 2
RATE_LIMIT_WAIT = 20

_quota_strikes = 0


RATE_LIMIT_MAX_WAIT = 60


def reset_quota_state() -> None:
    global _quota_strikes
    _quota_strikes = 0


def _quota_details(resp) -> list[dict]:
    try:
        details = resp.json()["error"].get("details", [])
    except (ValueError, KeyError):
        return []
    return [v for block in details for v in block.get("violations", [])]


def _retry_delay(resp) -> int:
    """שניות ההמתנה ש-Google ביקש, אם ציין."""
    try:
        for block in resp.json()["error"].get("details", []):
            delay = block.get("retryDelay", "")
            if delay.endswith("s"):
                return int(float(delay[:-1])) + 1
    except (ValueError, KeyError, TypeError):
        pass
    return 0


def _quota_message(resp) -> str:
    """הודעה שאומרת איזו מכסה נגמרה — חינמית או בתשלום, ומה הגבול."""
    violations = _quota_details(resp)
    if not violations:
        return f"Gemini 429: {resp.text[:200]}"

    first = violations[0]
    quota_id = first.get("quotaId", "")
    limit = first.get("quotaValue", "?")
    model = first.get("quotaDimensions", {}).get("model", "")
    tier = "המכסה החינמית" if "FreeTier" in quota_id else "המכסה בתשלום"
    return f"{tier} של הדגם {model} נגמרה (גבול: {limit}). מזהה: {quota_id}"


# ---------------------------------------------------------------- ספקים

def _gemini(prompt: str, model: str) -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise LLMError("חסר GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # הדגמים החדשים צורכים טוקני חשיבה שנספרים במגבלת הפלט. בלי תקציב נדיב
    # התשובה נחתכת באמצע משפט, ולפעמים החשיבה עצמה דולפת לתוך הטקסט.
    config = {
        "temperature": 0.2,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "thinkingConfig": {"thinkingLevel": "low"},
    }

    def call(cfg):
        return requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
            timeout=TIMEOUT,
        )

    resp = call(config)
    if resp.status_code == 400 and "thinkingConfig" in json.dumps(config):
        # דגמים ישנים יותר לא מכירים את השדה — מנסים שוב בלעדיו
        log.debug("הדגם %s לא תומך ב-thinkingLevel, מנסה בלעדיו", model)
        config.pop("thinkingConfig")
        resp = call(config)

    if resp.status_code == 429:
        # מגבלת קצב לדקה חולפת מעצמה; מגבלה יומית לא. מנסים פעם אחת ומוותרים.
        wait = min(_retry_delay(resp) or RATE_LIMIT_WAIT, RATE_LIMIT_MAX_WAIT)
        log.info("Gemini החזיר 429 — ממתין %d שניות ומנסה שוב", wait)
        time.sleep(wait)
        resp = call(config)
        if resp.status_code == 429:
            raise QuotaError(_quota_message(resp))

    if not resp.ok:
        raise LLMError(f"Gemini {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        candidate = data["candidates"][0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError) as exc:
        raise LLMError(f"תשובה לא צפויה מ-Gemini: {json.dumps(data)[:300]}") from exc

    if candidate.get("finishReason") == "MAX_TOKENS":
        thoughts = data.get("usageMetadata", {}).get("thoughtsTokenCount", "?")
        log.warning(
            "התשובה נחתכה במגבלת הטוקנים (חשיבה: %s). הגדל את MAX_OUTPUT_TOKENS.", thoughts
        )
    if not text:
        raise LLMError("Gemini החזיר תשובה ריקה")
    return text


def _anthropic(prompt: str, model: str) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("חסר ANTHROPIC_API_KEY")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise LLMError(f"Anthropic {resp.status_code}: {resp.text[:300]}")
    blocks = resp.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _openai(prompt: str, model: str) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise LLMError("חסר OPENAI_API_KEY")
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.2,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise LLMError(f"OpenAI {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


def _claude_cli(prompt: str, model: str) -> str:
    """הרצה מקומית בלבד — דורש claude CLI מותקן ומחובר. לא עובד ב-GitHub Actions."""
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("claude CLI לא נמצא ב-PATH")

    # ההנחיה עוברת כארגומנט והתוכן ב-stdin: חלונות חוסמת שורת פקודה מעל ~32 אלף תווים
    instruction, _, body = prompt.partition("--- הטקסט ---")
    cmd = [exe, "-p", instruction.strip(), "--output-format", "text"]
    if model:
        cmd += ["--model", model]

    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # אחרת הוא מסרב לרוץ בתוך סשן קיים
    try:
        proc = subprocess.run(
            cmd, input=body, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError("claude CLI לא הגיב בזמן") from exc
    if proc.returncode != 0:
        raise LLMError(f"claude CLI יצא עם קוד {proc.returncode}: {(proc.stderr or '')[:300]}")
    return (proc.stdout or "").strip()


def _mock(prompt: str, model: str) -> str:
    """ספק בדיקה — מחזיר נקודות מתוך הטקסט עצמו, בלי לקרוא לשום API."""
    _, _, body = prompt.partition("--- הטקסט ---")
    words = body.replace("--- סוף הטקסט ---", "").split()
    chunk = max(1, len(words) // 4)
    return "\n".join(
        "• " + " ".join(words[i * chunk:(i * chunk) + 12]) for i in range(4)
    )


PROVIDERS = {
    "gemini": _gemini,
    "anthropic": _anthropic,
    "openai": _openai,
    "claude_cli": _claude_cli,
    "mock": _mock,
}


def complete(prompt: str, provider: str, model: str) -> str:
    fn = PROVIDERS.get(provider)
    if not fn:
        raise LLMError(f"ספק לא מוכר: {provider}")
    return fn(prompt, model)


# ---------------------------------------------------------------- הנחיות

def build_prompt(item, language: str, bullets: int) -> str:
    lang_name = LANG_NAMES.get(language, language)
    kind = {
        "transcript": "תמלול מלא של סרטון",
        "description": "תיאור הסרטון בלבד (לא היה תמלול זמין)",
        "feed": "תקציר מתוך פיד",
        "page": "טקסט של עמוד אינטרנט",
    }.get(item.content_kind, "טקסט")

    focus = f"\nהתמקד במיוחד ב: {item.focus}" if item.focus else ""
    low = max(3, bullets - 1)
    high = bullets + 1

    return f"""אתה מסכם תוכן עבור עדכון יומי. לפניך {kind} מהמקור "{item.source_name}".

כללים מחייבים:
- כתוב אך ורק מה שנאמר בטקסט שלמטה. אל תוסיף ידע חיצוני, הקשר, או פרשנות משלך.
- אם מספר, תאריך, שם או רמת מחיר לא מופיעים בטקסט — אל תכתוב אותם.
- נסח במילים שלך, לא ציטוט מילולי.
- {low}-{high} נקודות, כל נקודה שורה אחת שמתחילה ב-"• ".
- העדף את מה שקונקרטי: מספרים, נתונים, טענות מפורשות, מסקנות.
- כתוב ב{lang_name}. בלי כותרת, בלי הקדמה, בלי סיכום מסכם — רק הנקודות.
- אל תכתוב שום דבר מלבד הנקודות עצמן: לא הערות, לא בדיקות, לא מחשבות שלך על התהליך.
- כל נקודה משפט אחד שלם. אל תתחיל נקודה שלא תוכל לסיים.
- אם הטקסט קצר או לא ברור מדי מכדי לסכם, החזר בדיוק: NO_CONTENT{focus}

כותרת: {item.title}

--- הטקסט ---
{item.content[:MAX_CHARS]}
--- סוף הטקסט ---"""


# טווחי הכתב של כל שפה — לזיהוי טקסט שצריך תרגום
SCRIPTS = {
    "he": r"֐-׿",
    "ar": r"؀-ۿ",
    "ru": r"Ѐ-ӿ",
    "en": r"A-Za-z",
}


def needs_translation(text: str, language: str) -> bool:
    """האם הטקסט כתוב בשפה אחרת מזו שביקשנו."""
    pattern = SCRIPTS.get(language)
    if not pattern:
        return False
    letters = re.findall(r"[^\W\d_]", text, re.UNICODE)
    if len(letters) < 15:
        return False
    native = re.findall(f"[{pattern}]", text)
    return len(native) / len(letters) < 0.15


def build_translation_prompt(text: str, language: str) -> str:
    lang_name = LANG_NAMES.get(language, language)
    return f"""תרגם את הטקסט הבא ל{lang_name}.

כללים מחייבים:
- תרגום נאמן ומלא. אל תקצר, אל תסכם, ואל תוסיף שום דבר משלך.
- שמור מספרים, אחוזים, רמות מחיר וסימולי מניות ($BTC, $INIT) בדיוק כפי שהם.
- שמור על מבנה השורות המקורי.
- החזר אך ורק את התרגום, בלי הקדמה ובלי הערות.

--- הטקסט ---
{text[:MAX_CHARS]}
--- סוף הטקסט ---"""


def translate(text: str, language: str, provider: str, model: str) -> str:
    """מחזיר תרגום, או מחרוזת ריקה אם נכשל — ואז מוצג המקור."""
    global _quota_strikes

    if _quota_strikes >= QUOTA_STRIKES_BEFORE_STOP:
        return ""
    try:
        out = complete(build_translation_prompt(text, language), provider, model)
    except QuotaError as exc:
        _quota_strikes += 1
        log.warning("תרגום נכשל, מכסה: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("תרגום נכשל: %s", exc)
        return ""
    return out.strip()


def summarize(item, language: str, bullets: int, provider: str, model: str) -> str:
    """מחזיר נקודות סיכום, או מחרוזת ריקה אם אין מספיק תוכן."""
    global _quota_strikes

    if _quota_strikes >= QUOTA_STRIKES_BEFORE_STOP:
        return ""

    try:
        out = complete(build_prompt(item, language, bullets), provider, model)
    except QuotaError as exc:
        _quota_strikes += 1
        if _quota_strikes >= QUOTA_STRIKES_BEFORE_STOP:
            log.error(
                "המכסה של ספק המודל נגמרה — מדלג על שאר הפריטים בריצה הזו. "
                "המכסה החינמית מתאפסת מדי יום. (%s)", exc
            )
        else:
            log.warning("מכסה נגמרה עבור %s: %s", item.url, exc)
        return ""
    except LLMError as exc:
        log.warning("סיכום נכשל עבור %s: %s", item.url, exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("שגיאה בסיכום %s: %s", item.url, exc)
        return ""

    if "NO_CONTENT" in out or len(out.strip()) < 20:
        log.info("אין מספיק תוכן לסיכום: %s", item.url)
        return ""

    return _to_bullets(out)


BULLET_MARKERS = ("•", "*", "-", "–", "—")


def _to_bullets(out: str) -> str:
    """מנרמל את הפלט לנקודות.

    אם המודל סימן נקודות בעצמו — לוקחים רק אותן. זה מסנן שורות מטא כמו
    "בוא נבדוק את המספרים", שדולפות לפעמים מהחשיבה הפנימית של המודל.
    """
    raw = [line.strip() for line in out.splitlines() if line.strip()]
    marked = [line for line in raw if line.startswith(BULLET_MARKERS)]
    chosen = marked or raw

    lines = []
    for line in chosen:
        line = line.lstrip("".join(BULLET_MARKERS)).strip()
        if line:
            lines.append("• " + line)
    return "\n".join(lines)
