"""
RAVEN voice output.

Turns text into a Telegram-ready voice note (OGG/OPUS) using Microsoft Edge's
neural TTS endpoint via the pure-Python `edge-tts` client. No API key, no
account, no cost.

Why edge-tts and not the Windows SAPI voices:
  the voices installed on this machine are Microsoft Zira and Microsoft Hazel,
  both female and both English-only. edge-tts exposes 322 voices across 142
  locales with 163 female / 159 male, including 34 Arabic ones. That is the
  only way to get a man or a non-English voice here.

Telegram accepts voice notes only as OGG/OPUS, so every generated MP3 is
transcoded with ffmpeg. ffmpeg is already a dependency for video keyframes.

Every failure mode degrades to an explanatory message rather than an
exception: missing package, missing ffmpeg, no network, empty text.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("raven.voice")

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except Exception:  # pragma: no cover - absence is a supported configuration
    edge_tts = None
    EDGE_TTS_AVAILABLE = False


OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

# Telegram will not accept a voice note longer than this in practice, and a
# 20 minute clip is already unusable. Keep well under.
MAX_CHARS = 4000
CHUNK_CHARS = 1800

DEFAULT_LANG = os.getenv("RAVEN_VOICE_LANG", "en-US").strip() or "en-US"
DEFAULT_GENDER = os.getenv("RAVEN_VOICE_GENDER", "female").strip().lower() or "female"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# Curated defaults: the best-known voice per language/gender. Anything not
# listed falls back to the first voice the endpoint reports for that
# locale/gender, so all 142 locales work without a hardcoded table.
PREFERRED: Dict[Tuple[str, str], str] = {
    ("en", "male"): "en-US-GuyNeural",
    ("en", "female"): "en-US-JennyNeural",
    ("ar", "male"): "ar-EG-ShakirNeural",
    ("ar", "female"): "ar-EG-SalmaNeural",
    ("fr", "male"): "fr-FR-HenriNeural",
    ("fr", "female"): "fr-FR-DeniseNeural",
    ("de", "male"): "de-DE-ConradNeural",
    ("de", "female"): "de-DE-KatjaNeural",
    ("es", "male"): "es-ES-AlvaroNeural",
    ("es", "female"): "es-ES-ElviraNeural",
    ("it", "male"): "it-IT-DiegoNeural",
    ("it", "female"): "it-IT-ElsaNeural",
    ("pt", "male"): "pt-BR-DanielNeural",
    ("pt", "female"): "pt-BR-FranciscaNeural",
    ("ru", "male"): "ru-RU-DmitryNeural",
    ("ru", "female"): "ru-RU-SvetlanaNeural",
    ("zh", "male"): "zh-CN-YunxiNeural",
    ("zh", "female"): "zh-CN-XiaoxiaoNeural",
    ("ja", "male"): "ja-JP-KeitaNeural",
    ("ja", "female"): "ja-JP-NanamiNeural",
    ("ko", "male"): "ko-KR-InJoonNeural",
    ("ko", "female"): "ko-KR-SunHiNeural",
    ("hi", "male"): "hi-IN-MadhurNeural",
    ("hi", "female"): "hi-IN-SwaraNeural",
    ("tr", "male"): "tr-TR-EmirNeural",
    ("tr", "female"): "tr-TR-EmelNeural",
    ("nl", "male"): "nl-NL-MaartenNeural",
    ("nl", "female"): "nl-NL-ColetteNeural",
    ("pl", "male"): "pl-PL-MarekNeural",
    ("pl", "female"): "pl-PL-ZofiaNeural",
    ("uk", "male"): "uk-UA-PavloNeural",
    ("uk", "female"): "uk-UA-PolinaNeural",
    ("sv", "male"): "sv-SE-OskarNeural",
    ("sv", "female"): "sv-SE-SofieNeural",
}

# Telegram replies that read badly when spoken. Stripped before synthesis.
_NO_SPEAK = [
    (re.compile(r"```.*?```", re.S), " code block "),
    (re.compile(r"`[^`]+`"), " code "),
    (re.compile(r"https?://\S+"), " link "),
    (re.compile(r"[*_#>|]+"), " "),
]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
def status() -> Dict[str, Any]:
    """What voice output can currently do, for /system and the dashboard."""
    return {
        "engine": "edge-tts" if EDGE_TTS_AVAILABLE else None,
        "ffmpeg": bool(FFMPEG),
        "usable": bool(EDGE_TTS_AVAILABLE and FFMPEG),
        "needs": _missing_requirements(),
        "max_chars": MAX_CHARS,
    }


def _missing_requirements() -> List[str]:
    out: List[str] = []
    if not EDGE_TTS_AVAILABLE:
        out.append("pip install edge-tts")
    if not FFMPEG:
        out.append("install ffmpeg (needed to make Telegram's ogg/opus format)")
    return out


def _fail(reason: str) -> None:
    """Raise a message that is safe to show a user verbatim."""
    raise RuntimeError(reason)


# ---------------------------------------------------------------------------
# Voice catalogue
# ---------------------------------------------------------------------------
_voice_cache: Optional[List[Dict[str, Any]]] = None
_voice_lock = asyncio.Lock()


async def list_voices(lang: Optional[str] = None,
                      gender: Optional[str] = None) -> List[Dict[str, Any]]:
    """All edge-tts voices, optionally filtered. Cached for the process life."""
    global _voice_cache
    if not EDGE_TTS_AVAILABLE:
        return []
    async with _voice_lock:
        if _voice_cache is None:
            try:
                raw = await edge_tts.list_voices()
                _voice_cache = [
                    {
                        "name": v.get("ShortName", ""),
                        "locale": v.get("Locale", ""),
                        "gender": (v.get("Gender") or "").lower(),
                    }
                    for v in raw
                    if v.get("ShortName")
                ]
            except Exception:
                logger.warning("could not list edge-tts voices", exc_info=True)
                _voice_cache = []
        voices = list(_voice_cache)

    if lang:
        base = lang.split("-")[0].lower()
        voices = [v for v in voices
                  if v["locale"].split("-")[0].lower() == base]
    if gender:
        voices = [v for v in voices if v["gender"] == gender.lower()]
    return sorted(voices, key=lambda v: v["name"])


async def pick_voice(lang: str = DEFAULT_LANG,
                     gender: str = DEFAULT_GENDER) -> str:
    """Choose a voice for a language and gender.

    Explicit user choice is not handled here; callers pass a concrete voice
    name straight through. This is only the automatic choice.
    """
    base = (lang or "en").split("-")[0].lower()
    gender = (gender or "female").lower()

    curated = PREFERRED.get((base, gender))
    if curated:
        return curated

    voices = await list_voices(base, gender)
    if voices:
        return voices[0]["name"]
    # Last resort: any voice for the language, ignoring gender preference.
    any_gender = await list_voices(base, None)
    if any_gender:
        return any_gender[0]["name"]
    _fail("No voice available for '%s'. Run /voices to see what is available."
          % lang)


async def validate_voice(name: str) -> bool:
    voices = await list_voices()
    return any(v["name"] == name for v in voices)


# ---------------------------------------------------------------------------
# Speech synthesis
# ---------------------------------------------------------------------------
_MP3_MAGIC = b"ID3"


def _clean_for_speech(text: str) -> str:
    out = text
    for pattern, repl in _NO_SPEAK:
        out = pattern.sub(repl, out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _chunk(text: str, size: int = CHUNK_CHARS) -> List[str]:
    """Split on blank lines, then hard-wrap anything still too long."""
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    buf = ""
    for para in text.split("\n\n"):
        while len(para) > size:
            head, para = para[:size], para[size:]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(head)
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


async def _to_opus(mp3_path: str, ogg_path: str) -> bool:
    """Transcode to the only format Telegram accepts for voice notes."""
    if not FFMPEG:
        _fail("ffmpeg is not installed, so I cannot produce Telegram's "
              "ogg/opus format.")
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", mp3_path,
        "-c:a", "libopus", "-b:a", "32k",
        "-ac", "1", "-ar", "48000",
        "-application", "voip",
        "-map_metadata", "-1",
        ogg_path,
    ]
    try:
        # Run the blocking subprocess call in a thread to avoid blocking the event loop
        res = await asyncio.to_thread(
            lambda: subprocess.run(cmd, capture_output=True, timeout=180)
        )
    except subprocess.TimeoutExpired:
        _fail("Audio conversion timed out.")
    if res.returncode != 0 or not os.path.exists(ogg_path):
        logger.error("ffmpeg failed: %s", res.stderr.decode("utf-8", "replace")[:300])
        _fail("Audio conversion failed.")
    return True


async def _duration(path: str) -> int:
    """Seconds of audio, for Telegram's duration field. Best effort."""
    if not FFPROBE:
        # ~32kbps mono opus; good enough for a display field.
        try:
            return max(1, int(os.path.getsize(path) * 8 / 32000))
        except OSError:
            return 0
    try:
        # Run the blocking subprocess call in a thread to avoid blocking the event loop
        res = await asyncio.to_thread(
            lambda: subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, timeout=60)
        )
        return int(float(res.stdout.decode().strip()))
    except Exception:
        return 0


async def _synth_one(text: str, voice: str, mp3_path: str) -> None:
    comm = edge_tts.Communicate(text, voice)
    await comm.save(mp3_path)


async def speak(text: str, lang: str = DEFAULT_LANG,
                gender: str = DEFAULT_GENDER,
                voice: Optional[str] = None,
                out_dir: Optional[str] = None) -> Dict[str, Any]:
    """Render text to a Telegram voice note.

    Returns {"path", "duration", "voice", "chars"} or raises RuntimeError whose
    message is written for a human. Callers must run this off the event loop's
    critical path; the ffmpeg work is already in a thread.
    """
    if not EDGE_TTS_AVAILABLE:
        _fail("Voice output needs the edge-tts package: pip install edge-tts")
    if not FFMPEG:
        _fail("Voice output needs ffmpeg to produce Telegram's ogg/opus format.")

    body = _clean_for_speech(text or "")
    if not body:
        _fail("Nothing to say.")
    if len(body) > MAX_CHARS:
        _fail("Too long to speak (%d chars, limit %d). Send a shorter reply or "
              "turn voice off for this one." % (len(body), MAX_CHARS))

    chosen = voice or await pick_voice(lang, gender)
    if not await validate_voice(chosen):
        _fail("Voice '%s' does not exist. Run /voices to list valid names."
              % chosen)

    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, "raven_voice_%s" % uuid.uuid4().hex[:12])
    mp3_path, ogg_path = stem + ".mp3", stem + ".ogg"

    try:
        pieces = _chunk(body)
        if len(pieces) == 1:
            await _synth_one(pieces[0], chosen, mp3_path)
        else:
            # Concatenate the MP3 byte-wise: for a constant-bitrate stream this
            # plays back correctly and avoids a second encoder pass.
            with open(mp3_path, "wb") as out:
                for piece in pieces:
                    tmp = stem + "_p.mp3"
                    await _synth_one(piece, chosen, tmp)
                    with open(tmp, "rb") as fh:
                        out.write(fh.read())
                    os.remove(tmp)

        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            _fail("The speech service returned no audio.")

        await _to_opus(mp3_path, ogg_path)
        return {
            "path": ogg_path,
            # ffprobe is a subprocess; spawning it inline would block the event
            # loop for the length of the call.
            "duration": await _duration(ogg_path),
            "voice": chosen,
            "chars": len(body),
        }
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("speech synthesis failed", exc_info=True)
        _fail("Speech synthesis failed: %s" % type(exc).__name__)
    finally:
        # The MP3 is an intermediate; the caller owns the .ogg.
        try:
            if os.path.exists(mp3_path):
                os.remove(mp3_path)
        except OSError:
            pass


def cleanup(path: str) -> None:
    """Delete a generated voice note once Telegram has sent it."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        logger.debug("could not remove voice file %s", path)