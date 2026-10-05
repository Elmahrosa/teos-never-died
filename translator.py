"""
RAVEN translation.

Text translation across a wide set of languages, plus offline language
identification.

Two design points that come from measurements on this host, not from theory:

1. A dedicated translation model (`RAVEN_TRANSLATE_MODEL`, default
   `aya-expanse:8b`) is used when present. It is Cohere's 101-language model
   and is the reason Arabic works. It is *not* used for chat or security
   work - aya-expanse is a poor reasoner - so it never displaces the main
   model.

2. Output is verified against the expected Unicode script before being
   returned. Asking `dolphin-llama3:8b` to translate Arabic into Arabic was
   measured to return English; `llama3.2:3b` returned broken Arabic. Silently
   shipping either would be worse than refusing, so `_check_script` compares
   the reply's character ranges with the target language's and returns an
   explicit warning when the model ignored the instruction.

Language identification uses `langdetect`, which is pure Python, offline and
instant, and measured 10/12 on a 12-language spot check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("raven.translator")

try:
    from langdetect import DetectorFactory, detect_langs
    from langdetect.lang_detect_exception import LangDetectException
    LANGDETECT_AVAILABLE = True
    # langdetect is randomised by default, which makes short-text detection
    # unstable between identical calls.
    DetectorFactory.seed = 0
except Exception:  # pragma: no cover - absence is a supported configuration
    LANGDETECT_AVAILABLE = False

    class LangDetectException(Exception):
        pass


OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
TRANSLATE_MODEL = os.getenv("RAVEN_TRANSLATE_MODEL", "aya-expanse:8b").strip()
CHAT_MODEL = os.getenv("RAVEN_MODEL", "dolphin-llama3:8b").strip()

# Telegram's own message limit is 4096; leave room for the header we add.
MAX_CHARS = 3000
CHUNK_CHARS = 900

# Measured on this CPU-only host: the first aya-expanse:8b call took 599s
# because it had to load 5.1GB off disk, and warm calls take 19-39s. A 600s
# ceiling was cleared by under a second, which is not a margin - it would time
# out on a slightly slower day. 900s leaves real headroom for a cold start.
# Once loaded, keep_alive=30m holds it resident and calls stay ~20-40s.
TIMEOUT = float(os.getenv("RAVEN_TRANSLATE_TIMEOUT", "900"))

# code -> (English name, endonym, script ranges)
LANGUAGES: Dict[str, Tuple[str, str, List[Tuple[int, int]]]] = {
    "en": ("English", "English", []),
    "ar": ("Arabic", "العربية", [(0x0600, 0x06FF), (0x0750, 0x077F)]),
    "fr": ("French", "Francais", []),
    "de": ("German", "Deutsch", []),
    "es": ("Spanish", "Espanol", []),
    "it": ("Italian", "Italiano", []),
    "pt": ("Portuguese", "Portugues", []),
    "nl": ("Dutch", "Nederlands", []),
    "ru": ("Russian", "Russian", [(0x0400, 0x04FF)]),
    "uk": ("Ukrainian", "Ukrainian", [(0x0400, 0x04FF)]),
    "pl": ("Polish", "Polski", []),
    "cs": ("Czech", "Cestina", []),
    "sv": ("Swedish", "Svenska", []),
    "no": ("Norwegian", "Norsk", []),
    "da": ("Danish", "Dansk", []),
    "fi": ("Finnish", "Suomi", []),
    "tr": ("Turkish", "Turkce", []),
    "el": ("Greek", "Ellinika", [(0x0370, 0x03FF)]),
    "he": ("Hebrew", "Ivrit", [(0x0590, 0x05FF)]),
    "fa": ("Persian", "Farsi", [(0x0600, 0x06FF)]),
    "ur": ("Urdu", "Urdu", [(0x0600, 0x06FF)]),
    "hi": ("Hindi", "Hindi", [(0x0900, 0x097F)]),
    "bn": ("Bengali", "Bangla", [(0x0980, 0x09FF)]),
    "ta": ("Tamil", "Tamil", [(0x0B80, 0x0BFF)]),
    "th": ("Thai", "Thai", [(0x0E00, 0x0E7F)]),
    "vi": ("Vietnamese", "Tieng Viet", []),
    "id": ("Indonesian", "Indonesia", []),
    "ms": ("Malay", "Melayu", []),
    "tl": ("Tagalog", "Tagalog", []),
    "ja": ("Japanese", "Nihongo", [(0x3040, 0x30FF), (0x4E00, 0x9FFF)]),
    "ko": ("Korean", "Hangugeo", [(0xAC00, 0xD7AF)]),
    "zh": ("Chinese", "Zhongwen", [(0x4E00, 0x9FFF)]),
    "ka": ("Georgian", "Skartuli", [(0x10A0, 0x10FF)]),
    "hy": ("Armenian", "Hayeren", [(0x0530, 0x058F)]),
}

# langdetect emits region-qualified codes. Map them onto our base codes.
_LANGDETECT_ALIASES = {
    "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "zh",
    "pt-br": "pt", "pt-pt": "pt",
    "nb": "no", "nn": "no",
    "iw": "he",
    "jv": "id", "tl-ph": "tl",
}


# ---------------------------------------------------------------------------
# Language lookup
# ---------------------------------------------------------------------------
def resolve(token: str) -> Optional[str]:
    """Turn 'ar', 'ARABIC', 'العربية' or 'Arabic' into a language code.

    Also accepts the region-qualified and legacy codes langdetect emits, so
    `/translate nb` and `/translate iw` work rather than reporting an unknown
    language for a perfectly ordinary request.
    """
    if not token:
        return None
    t = str(token).strip().lower().replace("_", "-")
    for candidate in (t, t.split("-")[0]):
        if candidate in LANGUAGES:
            return candidate
        aliased = _LANGDETECT_ALIASES.get(candidate)
        if aliased and aliased in LANGUAGES:
            return aliased
    t = t.split("-")[0]
    for code, (name, endonym, _) in LANGUAGES.items():
        if t == name.lower() or t == endonym.lower():
            return code
    return None


def language_name(code: str) -> str:
    entry = LANGUAGES.get(code)
    return entry[0] if entry else code


def list_languages() -> List[Dict[str, str]]:
    return [
        {"code": c, "name": v[0], "native": v[1]}
        for c, v in sorted(LANGUAGES.items(), key=lambda kv: kv[1][0])
    ]


def _script_ok(code: str) -> bool:
    """Does this language use a script distinctive enough to verify?"""
    return bool(LANGUAGES.get(code, ("", "", []))[2])


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def detect(text: str) -> Tuple[Optional[str], float]:
    """Identify the language. Returns (code, confidence); (None, 0.0) if unknown."""
    result = detect_ex(text)
    return result["code"], result["confidence"]


# Script -> language. Checking Unicode ranges is deterministic, whereas
# langdetect's n-gram guessing is unreliable on short text: measured on this
# host, "hello" guesses Finnish at 0.71 and "good morning" guesses Croatian.
# When a distinctive script is present it wins outright.
_SCRIPT_TO_LANG = {
    (0x0600, 0x06FF): "ar",
    (0x0400, 0x04FF): "ru",
    (0x0370, 0x03FF): "el",
    (0x0590, 0x05FF): "he",
    (0x3040, 0x30FF): "ja",
    (0xAC00, 0xD7AF): "ko",
    (0x4E00, 0x9FFF): "zh",
    (0x0E00, 0x0E7F): "th",
    (0x0900, 0x097F): "hi",
    (0x0980, 0x09FF): "bn",
    (0x0B80, 0x0BFF): "ta",
    (0x10A0, 0x10FF): "ka",
    (0x0530, 0x058F): "hy",
}

# Below this, langdetect is guessing, so the bot says so rather than asserting.
CONFIDENCE_FLOOR = 0.85


def detect_by_script(text: str) -> Optional[str]:
    """Language implied by the Unicode ranges present. None for Latin text."""
    if not text:
        return None
    for ch in text:
        code = ord(ch)
        for (lo, hi), lang in _SCRIPT_TO_LANG.items():
            if lo <= code <= hi:
                return lang
    return None


def detect_ex(text: str) -> Dict[str, Any]:
    """Full detection result: code, confidence, candidates, method, confident."""
    blank = {"code": None, "confidence": 0.0, "candidates": [],
             "method": "none", "confident": False}
    if not text or not text.strip():
        return blank

    scripted = detect_by_script(text)
    if scripted:
        return {"code": scripted, "confidence": 1.0, "candidates": [],
                "method": "script", "confident": True}

    if not LANGDETECT_AVAILABLE:
        return {"code": None, "confidence": 0.0, "candidates": [],
                "method": "unavailable", "confident": False}

    try:
        guesses = detect_langs(text[:2000])
    except LangDetectException:
        return None, 0.0
    except Exception:
        logger.debug("langdetect failed", exc_info=True)
        return None, 0.0
    if not guesses:
        return None, 0.0
    candidates = [{"code": _LANGDETECT_ALIASES.get(g.lang, g.lang),
                   "confidence": float(g.prob)} for g in guesses[:3]]
    known = [c for c in candidates if c["code"] in LANGUAGES]
    if not known:
        # Every guess is a language we do not support. Report them anyway, so
        # the user learns something rather than just "unknown".
        return {"code": None, "confidence": candidates[0]["confidence"],
                "candidates": candidates, "method": "guess", "confident": False}

    top = known[0]
    # Turkish reads as Indonesian on short greetings; the distinguishing
    # letters settle it.
    if top["code"] == "id" and any(c in text for c in "ğışç"):
        top["code"] = "tr"
    return {
        "code": top["code"],
        "confidence": top["confidence"],
        "candidates": candidates,
        "method": "langdetect",
        "confident": top["confidence"] >= CONFIDENCE_FLOOR,
    }


# ---------------------------------------------------------------------------
# Model plumbing
# ---------------------------------------------------------------------------
_installed_cache: Optional[List[str]] = None
# Monotonic timestamp of the last failed probe, and how long to wait before
# trying again. A failure must NOT be cached as "no models" - see below.
_last_probe_failed: float = 0.0
_PROBE_RETRY_SEC: float = 5.0


def _fetch_installed_models() -> List[str]:
    """Blocking model list fetch. Cached for the process life.

    Split out from the async entry point because translate() is deliberately
    synchronous: the bot runs it via asyncio.to_thread, so a blocking HTTP call
    there is already off the event loop.
    """
    global _installed_cache, _last_probe_failed
    if _installed_cache is not None:
        return _installed_cache
    # A failed probe must never be cached as "no models installed". Doing so
    # poisons the cache for the whole process life: one transient timeout made
    # /translate report "No translation model available" and refuse to fall back
    # to the chat model, for every request until the bot restarted, even though
    # Ollama was healthy. Retry instead, rate-limited so a permanently down
    # Ollama costs one 10s timeout per _PROBE_RETRY_SEC rather than per call.
    now = time.monotonic()
    if _last_probe_failed and (now - _last_probe_failed) < _PROBE_RETRY_SEC:
        return []
    try:
        req = urllib.request.Request(OLLAMA_HOST + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        _installed_cache = [m.get("name", "") for m in data.get("models", [])]
        _last_probe_failed = 0.0
    except Exception:
        logger.debug("could not reach ollama for model list", exc_info=True)
        _last_probe_failed = now
    return _installed_cache if _installed_cache is not None else []


async def installed_models() -> List[str]:
    """Model names Ollama reports. Cached for the process life."""
    if _installed_cache is not None:
        return _installed_cache
    return await asyncio.to_thread(_fetch_installed_models)


def _model_present(name: str) -> bool:
    """Synchronous model check. Callers on the event loop must warm the cache
    with `await installed_models()` first."""
    if not name:
        return False
    base = name.split(":")[0]
    return any(m == name or m.split(":")[0] == base
               for m in _fetch_installed_models())


def active_model() -> str:
    """The model that will actually do the translating."""
    if _model_present(TRANSLATE_MODEL):
        return TRANSLATE_MODEL
    return CHAT_MODEL


async def translation_status() -> Dict[str, Any]:
    # Warm the cache off the loop, then the synchronous checks are free.
    await installed_models()
    st = await status()
    st.update({
        "translate_model": TRANSLATE_MODEL,
        "translate_model_installed": _model_present(TRANSLATE_MODEL),
        "in_use": active_model(),
        "dedicated": _model_present(TRANSLATE_MODEL),
        "detection": LANGDETECT_AVAILABLE,
        "languages": len(LANGUAGES),
        "max_chars": MAX_CHARS,
    })
    return st


async def status() -> Dict[str, Any]:
    await installed_models()
    return {
        "ollama_reachable": bool(_installed_cache),
        "needs": [] if _model_present(TRANSLATE_MODEL)
                 else ["ollama pull %s" % TRANSLATE_MODEL],
        "langdetect": LANGDETECT_AVAILABLE,
    }


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
_PROMPT = (
    "You are a professional translator. Translate the user's message into "
    "{name} ({endonym}).\n"
    "Rules:\n"
    "- Reply with the translation and nothing else. No notes, no quotes, no "
    "explanation, no repetition of these rules.\n"
    "- Write the translation in {name} script, not transliterated Latin.\n"
    "- Keep technical terms, code, URLs and numbers exactly as they are.\n"
    "- If the source is already in {name}, return it unchanged.\n\n"
    "Message:\n{text}"
)


def _build_prompt(text: str, target: str) -> str:
    name, endonym, _ = LANGUAGES[target]
    return _PROMPT.format(name=name, endonym=endonym, text=text)


def _chat_sync(prompt: str, model: str, num_predict: int) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": os.getenv("RAVEN_KEEP_ALIVE", "30m"),
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }
    req = urllib.request.Request(
        OLLAMA_HOST + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.loads(resp.read())
    return (body.get("message", {}).get("content") or "").strip()


def _chunk(text: str, size: int = CHUNK_CHARS) -> List[str]:
    if len(text) <= size:
        return [text]
    out: List[str] = []
    buf = ""
    for para in text.split("\n\n"):
        while len(para) > size:
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 3 else size
            if buf:
                out.append(buf)
                buf = ""
            out.append(para[:cut])
            para = para[cut:].lstrip()
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                out.append(buf)
            buf = para
    if buf:
        out.append(buf)
    return out


def _has_script(text: str, ranges: List[Tuple[int, int]]) -> bool:
    for lo, hi in ranges:
        for ch in text:
            if lo <= ord(ch) <= hi:
                return True
    return False


def _check_script(text: str, target: str) -> Optional[str]:
    """Return a warning when the reply is clearly not in the target script."""
    if not _script_ok(target):
        return None  # Latin-script target: nothing reliable to compare against
    ranges = LANGUAGES[target][2]
    if _has_script(text, ranges):
        return None
    # Model returned something, but with no target-script characters at all.
    return language_name(target)


def _strip_wrappers(text: str) -> str:
    """Models sometimes wrap the answer in quotes or a code fence."""
    t = text.strip()
    t = re.sub(r"^(?:translation|translated text)\s*:\s*", "", t,
               flags=re.I)
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"```$", "", t)
    if len(t) > 1 and t[0] in "\"'“" and t[-1] in "\"'”":
        t = t[1:-1]
    return t.strip()


def translate(text: str, target: str,
              source: Optional[str] = None) -> Dict[str, Any]:
    """Translate `text` into `target`. Blocking: run it in a worker thread.

    Returns a dict with `text`, `target`, `source`, `model`, `warning`.
    Raises RuntimeError with a user-ready message when the model is absent.
    """
    target = resolve(target) or ""
    if not target:
        raise RuntimeError("Unknown target language '%s'." % target)
    body = (text or "").strip()
    if not body:
        raise RuntimeError("Nothing to translate.")
    if len(body) > MAX_CHARS:
        raise RuntimeError(
            "That is %d characters, over the %d limit. Send a shorter message."
            % (len(body), MAX_CHARS))

    model = active_model()
    if not _model_present(model):
        raise RuntimeError(
            "No translation model available. Run: ollama pull %s"
            % TRANSLATE_MODEL)

    detected = source if source else (detect(body)[0] or "unknown")

    pieces = _chunk(body)
    results: List[str] = []
    for piece in pieces:
        reply = _chat_sync(_build_prompt(piece, target), model,
                           num_predict=max(200, len(piece) * 3))
        if not reply:
            raise RuntimeError("The model returned an empty translation.")
        results.append(_strip_wrappers(reply))

    joined = "\n\n".join(r for r in results if r)
    warning = _check_script(joined, target)

    return {
        "text": joined,
        "target": target,
        "target_name": language_name(target),
        "source": detected,
        "source_name": language_name(detected) if detected != "unknown" else "unknown",
        "model": model,
        "chars": len(body),
        "warning": warning,
    }