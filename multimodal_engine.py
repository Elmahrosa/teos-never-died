"""Media and document analysis for RAVEN.

Three independent capabilities, each degrading on its own:

* Vision - image/frame analysis via a local Ollama vision model, OpenAI or
           Groq, whichever is actually configured.
* Docs   - text extraction for .txt/.md/.log/.pdf/.docx/.csv/.json.
* Video  - keyframe extraction with ffmpeg (already on PATH on this host), so no
           heavyweight OpenCV dependency is needed. OpenCV is an optional fallback.

Nothing raises at import time. Every optional dependency and API key is probed,
and `vision_status()` / `capabilities()` report exactly what is usable so the bot
tells the user the truth instead of pretending.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("raven.multimodal")

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import requests  # noqa: F401
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from PIL import Image  # type: ignore
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pypdf  # type: ignore
    PDF_AVAILABLE = True
except ImportError:
    try:
        import PyPDF2 as pypdf  # type: ignore
        PDF_AVAILABLE = True
    except ImportError:
        pypdf = None
        PDF_AVAILABLE = False

try:
    import docx  # type: ignore
    DOCX_AVAILABLE = True
except ImportError:
    docx = None
    DOCX_AVAILABLE = False

try:
    import cv2  # type: ignore
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    CV2_AVAILABLE = False

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FFMPEG_AVAILABLE = bool(FFMPEG)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
VISION_MODEL = os.getenv("RAVEN_VISION_MODEL", "llava").strip()
CLOUD_TIMEOUT = float(os.getenv("CLOUD_AI_TIMEOUT", "60"))

# Local vision on this host is CPU-only: llava is a 7B-class vision model and
# runs roughly an order of magnitude slower than the 3B chat model. Two knobs
# keep an image analysis from running forever or dying mid-answer:
#   RAVEN_VISION_MAX_TOKENS  - cap the generated description (default 200,
#                              ~4-8 minutes of CPU generation).
#   RAVEN_VISION_READ_TIMEOUT - how long to wait for the whole local request
#                              including a cold model load (default 900s).
VISION_MAX_TOKENS = int(os.getenv("RAVEN_VISION_MAX_TOKENS", "200"))
LOCAL_READ_TIMEOUT = float(os.getenv("RAVEN_VISION_READ_TIMEOUT", "900"))

# Cloud media analysis is opt-out. Vision is the expensive path, so media only
# leaves the machine when the operator has explicitly allowed it.
CLOUD_MEDIA = os.getenv("CLOUD_MEDIA_ANALYSIS", "true").lower() in ("1", "true", "yes", "on")

# Hard caps so a hostile or accidental huge upload cannot exhaust the host.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 60 * 1024 * 1024
MAX_DOC_BYTES = 8 * 1024 * 1024
MAX_DOC_CHARS = 12000

SUPPORTED_DOC_EXT = {".txt", ".md", ".log", ".pdf", ".docx", ".csv", ".json", ".yaml", ".yml"}

VISION_PROMPT = (
    "Analyze this image for security threats, network diagrams, code snippets, "
    "or phishing indicators. Provide a structured defensive assessment."
)

VISION_FRAME_PROMPT = (
    "This is a keyframe extracted from a video. Review it for defensive security "
    "purposes: exposed credentials, sensitive data on screen, misconfiguration, "
    "physical security concerns, or signs of social engineering. Note whether it "
    "appears to be a screen recording or a real-world scene."
)


def _key(name: str) -> str:
    """Read a key from the environment or .env, without needing python-dotenv."""
    val = os.getenv(name, "").strip()
    if val:
        return val
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Capability reporting
# ---------------------------------------------------------------------------
async def _local_vision_ready() -> Tuple[bool, str]:
    """A local vision model must exist. Text-only models are still listed by
    /api/tags, so we match on the configured name rather than assuming."""
    if not REQUESTS_AVAILABLE:
        return False, "requests not installed"
    try:
        # Run the blocking HTTP request in a thread to avoid blocking the event loop
        r = await asyncio.to_thread(
            lambda: requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        )
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as exc:
        return False, f"Ollama unreachable ({type(exc).__name__})"

    base = VISION_MODEL.split(":")[0]
    match = next((m for m in models if m == VISION_MODEL or m.split(":")[0] == base), None)
    if not match:
        return False, f"'{VISION_MODEL}' not installed - run: ollama pull {VISION_MODEL}"
    return True, match


async def vision_status() -> Dict[str, Any]:
    """Which vision backends are genuinely usable right now."""
    ok, detail = await _local_vision_ready()
    status = {
        "local": ok,
        "local_detail": detail,
        "openai": bool(_key("OPENAI_API_KEY")),
        "groq": bool(_key("GROQ_API_KEY")),
        "cloud_media_allowed": CLOUD_MEDIA,
    }
    status["usable"] = status["local"] or (
        CLOUD_MEDIA and (status["openai"] or status["groq"])
    )
    return status


async def capabilities() -> Dict[str, Any]:
    return {
        "vision": await vision_status(),
        "documents": {"pdf": PDF_AVAILABLE, "docx": DOCX_AVAILABLE,
                      "plain": True, "csv_json": True},
        "video": {"ffmpeg": FFMPEG_AVAILABLE, "opencv": CV2_AVAILABLE,
                  "usable": FFMPEG_AVAILABLE or CV2_AVAILABLE},
        "pillow": PIL_AVAILABLE,
    }


# ---------------------------------------------------------------------------
# Vision backends
# ---------------------------------------------------------------------------
async def _b64(path: str) -> str:
    # Run the blocking file operations in a thread to avoid blocking the event loop
    def _b64_sync():
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")

    return await asyncio.to_thread(_b64_sync)


async def _vision_local(image_path: str, prompt: str) -> str:
    ok, detail = await _local_vision_ready()
    if not ok:
        raise RuntimeError(detail)
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [await _b64(image_path)]}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": VISION_MAX_TOKENS},
    }
    # Run the blocking HTTP request in a thread to avoid blocking the event loop.
    # The read timeout covers a cold model load plus CPU generation (see the
    # RAVEN_VISION_* config above); Ollama stops generating at num_predict, so
    # the answer is short rather than endless.
    response = await asyncio.to_thread(
        lambda: requests.post(f"{OLLAMA_HOST}/api/chat", json=payload,
                              timeout=(30, LOCAL_READ_TIMEOUT))
    )
    response.raise_for_status()
    return response.json()["message"]["content"].strip()


async def _vision_openai(image_path: str, prompt: str) -> str:
    key = _key("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    b64 = await _b64(image_path)
    # Run the blocking HTTP request in a thread to avoid blocking the event loop
    response = await asyncio.to_thread(
        lambda: requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini"),
                "max_tokens": 900,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
            },
            timeout=CLOUD_TIMEOUT,
        )
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


async def _vision_groq(image_path: str, prompt: str) -> str:
    key = _key("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not configured")
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    b64 = await _b64(image_path)
    # Run the blocking HTTP request in a thread to avoid blocking the event loop
    response = await asyncio.to_thread(
        lambda: requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.getenv("GROQ_VISION_MODEL",
                                   "meta-llama/llama-3.2-11b-vision-preview"),
                "max_tokens": 900,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
            },
            timeout=CLOUD_TIMEOUT,
        )
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


# Local first (free and private); cloud only when configured and allowed.
_BACKENDS = (("local", _vision_local), ("openai", _vision_openai), ("groq", _vision_groq))


async def _no_vision_message(errors: List[str]) -> str:
    """Honest failure message.

    The old wording ("No vision backend is configured") was printed for
    *any* failing backend - including the real cause here, a CPU-only
    llava that cold-loads for ~90s and then generates slowly. Distinguish
    "nothing is installed" from "installed but this attempt failed".
    """
    local_ready, _ = await _local_vision_ready()
    timed_out = any("ReadTimeout" in e or "ConnectTimeout" in e
                    or "timed out" in e.lower() for e in errors)
    if local_ready:
        lines = [
            "\U0001F5BC Local vision is installed but this analysis did not finish.",
            "",
            f"Model:   {VISION_MODEL} (CPU-only host)",
        ]
        if timed_out:
            lines.append(
                "Why:     the request passed the "
                f"{int(LOCAL_READ_TIMEOUT)}s cap - a cold load plus slow "
                f"generation. Next attempt may succeed (the model stays warm "
                f"for ~10 min after loading)."
            )
        lines += [
            "",
            "Options:",
            "  - Retry once - the model may already be loaded now.",
            "  - Cloud: add OPENAI_API_KEY or GROQ_API_KEY to .env "
            "(CLOUD_MEDIA_ANALYSIS is on) for fast GPU-backed analysis.",
        ]
    else:
        lines = ["\U0001F5A5 No vision backend is usable.", ""]
        lines.append(f"Local:  ollama pull {VISION_MODEL}   (~4.7GB download)")
        if not CLOUD_MEDIA:
            lines.append("Cloud:  disabled via CLOUD_MEDIA_ANALYSIS=false")
        else:
            lines.append("Cloud:  add OPENAI_API_KEY or GROQ_API_KEY to .env")
    if errors:
        lines += ["", "Attempts:", *[f"  - {e}" for e in errors[:4]]]
    return "\n".join(lines)


async def analyze_image(image_path: str, prompt: str = VISION_PROMPT) -> str:
    """Analyze one image, trying each configured backend in turn.

    Raises RuntimeError with actionable guidance only when no backend works.
    """
    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)
    size = os.path.getsize(image_path)
    if size > MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"Image is {size/1e6:.1f} MB - limit is {MAX_IMAGE_BYTES/1e6:.0f} MB.")

    errors: List[str] = []
    for name, fn in _BACKENDS:
        if name != "local":
            if not CLOUD_MEDIA:
                continue
            if name == "openai" and not _key("OPENAI_API_KEY"):
                continue
            if name == "groq" and not _key("GROQ_API_KEY"):
                continue
        try:
            text = await fn(image_path, prompt)
            if text:
                return f"[vision:{name}]\n{text}"
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__} {exc}"[:200])
            logger.warning("vision backend %s failed: %s", name, exc)

    raise RuntimeError(await _no_vision_message(errors))


# ---------------------------------------------------------------------------
# Video keyframes
# ---------------------------------------------------------------------------
async def probe_duration(path: str) -> Optional[float]:
    if not FFPROBE:
        return None
    try:
        # Run the blocking subprocess call in a thread to avoid blocking the event loop
        out = await asyncio.to_thread(
            lambda: subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=60,
            )
        )
        return float(out.stdout.strip())
    except Exception:
        return None


async def extract_keyframes(video_path: str, count: int = 4,
                           workdir: Optional[str] = None) -> List[str]:
    """Return up to `count` representative JPEG frames.

    Prefers ffmpeg. Samples evenly spaced timestamps rather than doing scene
    detection, because scene detection needs a full decode pass which is
    disproportionately slow on the CPU-only budget this bot runs on.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(video_path)

    tmp = workdir or tempfile.mkdtemp(prefix="raven_frames_")
    os.makedirs(tmp, exist_ok=True)
    frames: List[str] = []

    if FFMPEG:
        duration = await probe_duration(video_path)
        if duration and duration > 0:
            count = max(1, min(count, 8))
            # Sample 10%..85% of the way in; the first frames are often black.
            if count > 1:
                offsets = [duration * (0.1 + 0.75 * i / (count - 1)) for i in range(count)]
            else:
                offsets = [duration * 0.5]
            for idx, ts in enumerate(offsets):
                out = os.path.join(tmp, f"frame_{idx}.jpg")
                try:
                    # Run the blocking subprocess call in a thread to avoid blocking the event loop
                    await asyncio.to_thread(
                        lambda: subprocess.run(
                            [FFMPEG, "-y", "-ss", f"{ts:.2f}", "-i", video_path,
                             "-frames:v", "1", "-q:v", "3", out],
                            capture_output=True, timeout=120,
                        )
                    )
                    if os.path.isfile(out) and os.path.getsize(out) > 0:
                        frames.append(out)
                except Exception as exc:
                    logger.warning("ffmpeg frame at %.1fs failed: %s", ts, exc)
            if frames:
                return frames
        else:
            logger.warning("could not probe duration; will try single-frame fallback")

    if CV2_AVAILABLE:
        try:
            cap = cv2.VideoCapture(video_path)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            step = max(1, total // max(1, count)) if total else 1
            for i in range(count):
                cap.set(cv2.CAP_PROP_POS_FRAMES, i * step)
                ok, frame = cap.read()
                if not ok:
                    break
                out = os.path.join(tmp, f"frame_{i}.jpg")
                cv2.imwrite(out, frame)
                frames.append(out)
            cap.release()
            if frames:
                return frames
        except Exception as exc:
            logger.warning("opencv keyframe extraction failed: %s", exc)

    if FFMPEG:
        out = os.path.join(tmp, "frame_0.jpg")
        try:
            # Run the blocking subprocess call in a thread to avoid blocking the event loop
            await asyncio.to_thread(
                lambda: subprocess.run([FFMPEG, "-y", "-i", video_path, "-frames:v", "1",
                                        "-q:v", "3", out], capture_output=True, timeout=120)
            )
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                return [out]
        except Exception:
            pass

    raise RuntimeError(
        "Could not extract frames. Install ffmpeg (https://ffmpeg.org) or run "
        "`pip install opencv-python`."
    )


async def analyze_video(video_path: str, max_frames: int = 3,
                       workdir: Optional[str] = None) -> str:
    """Extract keyframes and ask the vision backend to assess each."""
    size = os.path.getsize(video_path)
    if size > MAX_VIDEO_BYTES:
        raise RuntimeError(
            f"Video is {size/1e6:.1f} MB - limit is {MAX_VIDEO_BYTES/1e6:.0f} MB.")
    if not (FFMPEG_AVAILABLE or CV2_AVAILABLE):
        raise RuntimeError(
            "Video analysis needs ffmpeg or OpenCV.\n"
            "  Install ffmpeg: https://ffmpeg.org/download.html\n"
            "  or run: pip install opencv-python"
        )

    frames = await extract_keyframes(video_path, count=max(max_frames, 2), workdir=workdir)
    if not frames:
        raise RuntimeError("No frames could be decoded from that video.")

    duration = await probe_duration(video_path)
    header = [f"\U0001F3AC VIDEO ANALYSIS - {len(frames)} keyframes reviewed"]
    if duration:
        header.append(f"Duration: {duration:.1f}s")
    header.append("")

    findings: List[str] = []
    for path in frames:
        try:
            text = await analyze_image(path, VISION_FRAME_PROMPT)
            findings.append(f"--- {os.path.basename(path)} ---\n{text}")
        except RuntimeError:
            raise
        except Exception as exc:
            findings.append(
                f"--- {os.path.basename(path)} ---\nAnalysis failed: "
                f"{type(exc).__name__}: {exc}")

    return "\n\n".join(header + findings)


# ---------------------------------------------------------------------------
# Document text extraction
# ---------------------------------------------------------------------------
async def _extract_pdf(path: str) -> str:
    if not PDF_AVAILABLE:
        raise RuntimeError("PDF support needs PyPDF2: pip install PyPDF2")
    # Run the blocking PDF extraction in a thread to avoid blocking the event loop
    def _extract_pdf_sync():
        reader = pypdf.PdfReader(path)
        parts = []
        for i, page in enumerate(reader.pages[:200]):
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append(f"[page {i + 1} extraction failed]")
        return "\n".join(parts)

    return await asyncio.to_thread(_extract_pdf_sync)


async def _extract_docx(path: str) -> str:
    if not DOCX_AVAILABLE:
        raise RuntimeError("DOCX support needs python-docx: pip install python-docx")
    # Run the blocking DOCX extraction in a thread to avoid blocking the event loop
    def _extract_docx_sync():
        d = docx.Document(path)
        out: List[str] = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables[:20]:
            for row in table.rows[:50]:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    out.append(" | ".join(cells))
        return "\n".join(out)

    return await asyncio.to_thread(_extract_docx_sync)


async def extract_text(path: str) -> Tuple[str, str]:
    """Return (text, detected_kind). Raises on unsupported or unreadable input."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    size = os.path.getsize(path)
    if size > MAX_DOC_BYTES:
        raise RuntimeError(
            f"Document is {size/1e6:.1f} MB - limit is {MAX_DOC_BYTES/1e6:.0f} MB.")

    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_DOC_EXT:
        raise RuntimeError(
            f"Unsupported file type '{ext or 'unknown'}'. Supported: "
            f"{', '.join(sorted(SUPPORTED_DOC_EXT))}"
        )

    if ext == ".pdf":
        text = await _extract_pdf(path)
        kind = "PDF"
    elif ext == ".docx":
        text = await _extract_docx(path)
        kind = "DOCX"
    else:
        # Run the blocking file operations in a thread to avoid blocking the event loop
        def _extract_plain_text():
            raw = open(path, "rb").read()
            text = None
            for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="replace")
            kind = {".json": "JSON", ".csv": "CSV"}.get(ext, "TEXT")
            if kind == "JSON":
                try:
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                except Exception:
                    kind = "TEXT (malformed JSON)"
            return text, kind

        text, kind = await asyncio.to_thread(_extract_plain_text)

    text = (text or "").strip()
    if not text:
        raise RuntimeError(
            "No text could be extracted. A scanned PDF would need OCR, which is "
            "not installed."
        )
    return text[:MAX_DOC_CHARS], kind


DOC_PROMPT = (
    "Review the following document for defensive security purposes. Surface: "
    "hardcoded credentials, keys or tokens, internal hostnames, IPs, URLs and API "
    "endpoints, vulnerable or outdated dependencies, sensitive data exposure, "
    "suspicious log patterns, and anything that would assist an attacker. Quote "
    "concrete indicators rather than describing them vaguely. Do not produce "
    "exploitation steps."
)


def build_document_prompt(text: str, kind: str, filename: str = "") -> str:
    head = f"Document: {filename or 'attachment'} ({kind})\n"
    if len(text) >= MAX_DOC_CHARS:
        head += f"[truncated to {MAX_DOC_CHARS} characters]\n"
    return f"{head}\n---\n{text}\n---\n\n{DOC_PROMPT}"


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import asyncio

    async def _smoke():
        print(json.dumps(await capabilities(), indent=2))
        try:
            await analyze_image("definitely-missing.jpg")
        except Exception as exc:
            print("expected failure:", exc)

    asyncio.run(_smoke())