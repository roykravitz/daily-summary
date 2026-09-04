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

TIMEOUT = 60               # תשובה תקינה לוקחת שניות בודדות; לא לחכות דקות לתקועה
MAX_CHARS = 40000          # חיתוך תוכן ארוך לפני שליחה למודל
MAX_OUTPUT_TOKENS = 8192   # נדיב בכוונה — טוקני חשיבה נספרים בתוך המגבלה

LANG_NAMES = {"he": "עברית", "en": "English", "ar": "العربية", "ru": "русский"}


class LLMError(RuntimeError):
    pass


class QuotaError(LLMError):
    """חריגה ממכסה או מקצב הבקשות (429)."""


class TransientProviderError(LLMError):
    """תקלת רשת או שרת אצל הספק שנשארה גם אחרי כל הניסיונות החוזרים.

    בשונה מ-QuotaError (שאומר "המכסה נגמרת"), זה אומר "הספק לא עונה כרגע" —
    לפעמים עומס חולף, לפעמים תקלה אמיתית שנמשכת. אין דרך להבדיל מבפנים,
    ולכן שתיהן מפעילות את אותו מפסק כדי לא לבזבז את כל חלון הריצה על
    ניסיונות שכנראה יכשלו כולם.
    """


class MissingKeyError(LLMError):
    """מפתח ה-API של הספק לא מוגדר. החוליה הזו בשרשרת מדולגת בשקט."""


# מפסק לכל חוליה בשרשרת בנפרד: אחרי כמה כשלים ברצף אין טעם להמשיך לנסות
# את אותו ספק בריצה הזו — בין אם זו מכסה שנגמרה או תקלת רשת/שרת מתמשכת.
# הכישלון של חוליה אחת לא נוגע לאחרות: כשמכסת flash-latest נגמרת עוברים
# ל-flash-lite, וכשגוגל כולה מקרטעת עוברים לספק מחברה אחרת.
QUOTA_STRIKES_BEFORE_STOP = 2
TRANSIENT_STRIKES_BEFORE_STOP = 2
RATE_LIMIT_WAIT = 20

# (ספק, דגם) -> {"quota": n, "transient": n, "disabled": bool}
_strikes: dict[tuple[str, str], dict] = {}


RATE_LIMIT_MAX_WAIT = 60

# 503/500/504 אצל גוגל הם לפעמים עומס חולף (נמדד: ניסיון שלישי אחרי כ-16
# שניות הצליח) ולפעמים תקלה מתמשכת. אותו דבר לגבי timeout ברמת הרשת —
# קריאה יכולה להיתקע בלי שום תשובה. שתי הקטגוריות מקבלות את אותו טיפול:
# כמה ניסיונות עם המתנה גוברת, ואז וויתור עם הודעה ברורה.
SERVER_ERROR_CODES = {500, 502, 503, 504}
SERVER_ERROR_BACKOFF = (5, 10, 20)


def reset_quota_state() -> None:
    _strikes.clear()


def reset_transient_state() -> None:
    _strikes.clear()


def _entry_tripped(entry: tuple[str, str]) -> bool:
    s = _strikes.get(entry)
    if not s:
        return False
    return (
        s.get("disabled", False)
        or s.get("quota", 0) >= QUOTA_STRIKES_BEFORE_STOP
        or s.get("transient", 0) >= TRANSIENT_STRIKES_BEFORE_STOP
    )


def _strike(entry: tuple[str, str], kind: str) -> int:
    s = _strikes.setdefault(entry, {"quota": 0, "transient": 0, "disabled": False})
    s[kind] += 1
    return s[kind]


def _quota_details(resp) -> list[dict]:
    try:
        details = resp.json()["error"].get("details", [])
    except (ValueError, KeyError):
        return []
    return [v for block in details for v in block.get("violations", [])]


def _retry_delay(resp) -> int:
    """שניות ההמתנה שהספק ביקש, אם ציין.

    קודם בודקים את תקן ה-Retry-After הרגיל (אנתרופיק ורוב הספקים),
    ובנפילה אחורה את הפורמט הספציפי של גוגל (retryDelay בתוך גוף השגיאה).
    """
    headers = getattr(resp, "headers", None)
    if headers:
        value = headers.get("retry-after")
        if value:
            try:
                return int(float(value)) + 1
            except ValueError:
                pass
    try:
        for block in resp.json()["error"].get("details", []):
            delay = block.get("retryDelay", "")
            if delay.endswith("s"):
                return int(float(delay[:-1])) + 1
    except (ValueError, KeyError, TypeError):
        pass
    return 0


def _generic_quota_message(resp, provider: str) -> str:
    """הודעת מכסה לספקים שלא מפרטים כמו גוגל (מבנה השגיאה של אנתרופיק ו-OpenAI פשוט יותר)."""
    try:
        msg = resp.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        msg = ""
    return f"המכסה או קצב הבקשות של {provider} מוצו. {msg}"[:300]


_UNSET = object()


def _call_with_retries(call, server_error_codes: set[int], first_resp=_UNSET):
    """מפעיל call() עם ניסיונות חוזרים על 429 ועל שגיאות שרת/רשת חולפות.

    משותף לכל הספקים: call() מבצעת קריאת HTTP בודדת ומחזירה Response,
    או None אם נכשלה ברמת הרשת (טיימאאוט, ניתוק). מחזיר את ה-Response
    האחרון שהתקבל (עשוי עדיין להיות שגיאה) או None אם כל הניסיונות
    נכשלו ברמת הרשת. הקורא אחראי לפרש את התוצאה הסופית (429/5xx/הצלחה).
    """
    def needs_retry(r) -> bool:
        return r is None or r.status_code in server_error_codes

    resp = call() if first_resp is _UNSET else first_resp

    if resp is not None and resp.status_code == 429:
        # מגבלת קצב לדקה חולפת מעצמה; מגבלה יומית לא. מנסים פעם אחת ומוותרים.
        wait = min(_retry_delay(resp) or RATE_LIMIT_WAIT, RATE_LIMIT_MAX_WAIT)
        log.info("קיבלתי 429 — ממתין %d שניות ומנסה שוב", wait)
        time.sleep(wait)
        resp = call()

    if needs_retry(resp):
        # עומס זמני, שגיאת שרת, או timeout ברשת — לפעמים חולף תוך שניות,
        # לפעמים תקלה מתמשכת. מוותרים רק אחרי כמה ניסיונות עם המתנה גוברת.
        for attempt, wait in enumerate(SERVER_ERROR_BACKOFF, start=1):
            reason = f"{resp.status_code}" if resp is not None else "timeout/ניתוק רשת"
            log.info(
                "לא התקבלה תשובה תקינה (%s) — ממתין %d שניות ומנסה שוב (ניסיון %d/%d)",
                reason, wait, attempt, len(SERVER_ERROR_BACKOFF),
            )
            time.sleep(wait)
            resp = call()
            if not needs_retry(resp):
                break

    return resp


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
        raise MissingKeyError("חסר GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # הדגמים החדשים צורכים טוקני חשיבה שנספרים במגבלת הפלט. בלי תקציב נדיב
    # התשובה נחתכת באמצע משפט, ולפעמים החשיבה עצמה דולפת לתוך הטקסט.
    config = {
        "temperature": 0.2,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "thinkingConfig": {"thinkingLevel": "low"},
    }

    def call():
        """מחזיר תגובה, או None אם הקריאה נכשלה ברמת הרשת (timeout, ניתוק)."""
        try:
            return requests.post(
                url,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            log.debug("קריאה ל-Gemini נכשלה ברשת: %s", exc)
            return None

    resp = call()
    if resp is not None and resp.status_code == 400 and "thinkingConfig" in json.dumps(config):
        # דגמים ישנים יותר לא מכירים את השדה — מנסים שוב בלעדיו
        log.debug("הדגם %s לא תומך ב-thinkingLevel, מנסה בלעדיו", model)
        config.pop("thinkingConfig")
        resp = call()

    resp = _call_with_retries(call, SERVER_ERROR_CODES, first_resp=resp)

    if resp is None:
        raise TransientProviderError("Gemini לא הגיב אחרי כל הניסיונות (בעיית רשת או תקלת שרת מתמשכת)")
    if resp.status_code == 429:
        raise QuotaError(_quota_message(resp))
    if not resp.ok:
        if resp.status_code in SERVER_ERROR_CODES:
            raise TransientProviderError(f"Gemini {resp.status_code}: {resp.text[:300]}")
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


# 529 הוא overloaded_error הספציפי של אנתרופיק, בנוסף לקודי השרת הרגילים
ANTHROPIC_SERVER_ERROR_CODES = {500, 502, 503, 504, 529}


def _anthropic(prompt: str, model: str) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise MissingKeyError("חסר ANTHROPIC_API_KEY")

    def call():
        try:
            return requests.post(
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
        except requests.exceptions.RequestException as exc:
            log.debug("קריאה ל-Anthropic נכשלה ברשת: %s", exc)
            return None

    resp = _call_with_retries(call, ANTHROPIC_SERVER_ERROR_CODES)

    if resp is None:
        raise TransientProviderError("Anthropic לא הגיב אחרי כל הניסיונות (בעיית רשת או תקלת שרת מתמשכת)")
    if resp.status_code == 429:
        raise QuotaError(_generic_quota_message(resp, "Anthropic"))
    if not resp.ok:
        if resp.status_code in ANTHROPIC_SERVER_ERROR_CODES:
            raise TransientProviderError(f"Anthropic {resp.status_code}: {resp.text[:300]}")
        raise LLMError(f"Anthropic {resp.status_code}: {resp.text[:300]}")

    blocks = resp.json().get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    if not text:
        raise LLMError("Anthropic החזיר תשובה ריקה")
    return text


def _openai_compatible(prompt: str, model: str, *, url: str, key_env: str, name: str) -> str:
    """ספקים עם API תואם OpenAI (OpenAI עצמו, Groq ועוד) — אותה עמידות כמו Gemini."""
    key = os.getenv(key_env)
    if not key:
        raise MissingKeyError(f"חסר {key_env}")

    def call():
        try:
            return requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.2,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            log.debug("קריאה ל-%s נכשלה ברשת: %s", name, exc)
            return None

    resp = _call_with_retries(call, SERVER_ERROR_CODES)

    if resp is None:
        raise TransientProviderError(f"{name} לא הגיב אחרי כל הניסיונות (בעיית רשת או תקלת שרת מתמשכת)")
    if resp.status_code == 429:
        raise QuotaError(_generic_quota_message(resp, name))
    if not resp.ok:
        if resp.status_code in SERVER_ERROR_CODES:
            raise TransientProviderError(f"{name} {resp.status_code}: {resp.text[:300]}")
        raise LLMError(f"{name} {resp.status_code}: {resp.text[:300]}")

    try:
        text = resp.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError(f"תשובה לא צפויה מ-{name}: {resp.text[:300]}") from exc
    if not text:
        raise LLMError(f"{name} החזיר תשובה ריקה")
    return text


def _openai(prompt: str, model: str) -> str:
    return _openai_compatible(
        prompt, model,
        url="https://api.openai.com/v1/chat/completions",
        key_env="OPENAI_API_KEY", name="OpenAI",
    )


def _groq(prompt: str, model: str) -> str:
    """Groq — שכבה חינמית נדיבה, מהיר. ספק מחברה אחרת מגוגל, ולכן גיבוי
    אמיתי ליום שבו כל דגמי Gemini מקרטעים יחד. בלי מפתח — החוליה מדולגת."""
    return _openai_compatible(
        prompt, model,
        url="https://api.groq.com/openai/v1/chat/completions",
        key_env="GROQ_API_KEY", name="Groq",
    )


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
    "groq": _groq,
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
- {low}-{high} נקודות, כל אחת מתחילה ב-"• ".
- **כל נקודה צריכה להיות עשירה, לא שורה רזה.** נקודה טובה מכילה את הטענה,
  את המספרים שנאמרו עליה, ואת ההקשר או הסיבה שניתנו — הכל במשפט אחד או שניים.
  אורך של שתיים-שלוש שורות לנקודה הוא תקין ואף רצוי.
- **הצמד כל מספר שנאמר**: אחוזים, רמות מחיר, תאריכים, שמות מדדים וסימולי מניות,
  שווי בדולרים, יעדים. אם נאמרו כמה רמות — כתוב את כולן.
- כשהדובר הסביר למה משהו קרה או מה המשמעות — כלול את זה בתוך אותה נקודה.
- אם מוזכר שם מלא לצד ראשי תיבות או סימול, כתוב את שניהם.
- **אסור לכתוב נקודה כללית שרק מונה נושאים** בסגנון "הסרטון עוסק גם ב...".
  אם נושא ראוי להיכנס — כתוב עליו נקודה מלאה עם התוכן שנאמר. אחרת השמט אותו.
- כתוב ב{lang_name}. בלי כותרת, בלי הקדמה, בלי סיכום מסכם — רק הנקודות.
- אל תכתוב שום דבר מלבד הנקודות עצמן: לא הערות, לא בדיקות, לא מחשבות שלך על התהליך.
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


def _run_chain(prompt: str, chain: list[tuple[str, str]], label: str) -> str:
    """מנסה כל חוליה בשרשרת לפי הסדר עד שאחת מצליחה.

    חוליה שנגמרה לה המכסה או שלא מגיבה מסומנת ומדולגת בהמשך הריצה, כך
    שהפריט הבא לא משלם שוב את כל ההמתנות עליה. חוליה בלי מפתח API
    מדולגת בשקט מהרגע הראשון. מחזיר מחרוזת ריקה רק אם כל השרשרת נכשלה.
    """
    for index, entry in enumerate(chain):
        provider, model = entry
        if _entry_tripped(entry):
            continue
        has_next = any(not _entry_tripped(e) for e in chain[index + 1:])
        try:
            return complete(prompt, provider, model)
        except MissingKeyError as exc:
            _strikes.setdefault(entry, {"quota": 0, "transient": 0})["disabled"] = True
            log.info("%s/%s לא מוגדר (%s) — מדלג עליו בריצה הזו", provider, model, exc)
        except QuotaError as exc:
            if _strike(entry, "quota") >= QUOTA_STRIKES_BEFORE_STOP:
                log.error("המכסה של %s/%s נגמרה — מדלג עליו בשאר הריצה. (%s)", provider, model, exc)
            else:
                log.warning("מכסה נגמרה ב-%s/%s עבור %s: %s", provider, model, label, exc)
        except TransientProviderError as exc:
            if _strike(entry, "transient") >= TRANSIENT_STRIKES_BEFORE_STOP:
                log.error("%s/%s לא מגיב (רשת/שרת) — מדלג עליו בשאר הריצה. (%s)", provider, model, exc)
            else:
                log.warning("תקלת ספק ב-%s/%s עבור %s: %s", provider, model, label, exc)
        except LLMError as exc:
            log.warning("%s/%s נכשל עבור %s: %s", provider, model, label, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("שגיאה ב-%s/%s עבור %s: %s", provider, model, label, exc)
        if has_next:
            log.info("עובר לחוליית הגיבוי הבאה בשרשרת")
    return ""


def translate(text: str, language: str, chain: list[tuple[str, str]]) -> str:
    """מחזיר תרגום, או מחרוזת ריקה אם נכשל — ואז מוצג המקור."""
    out = _run_chain(build_translation_prompt(text, language), chain, "תרגום")
    return out.strip()


def summarize(item, language: str, bullets: int, chain: list[tuple[str, str]]) -> str:
    """מחזיר נקודות סיכום, או מחרוזת ריקה אם אין מספיק תוכן."""
    out = _run_chain(build_prompt(item, language, bullets), chain, item.url)
    if not out:
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
