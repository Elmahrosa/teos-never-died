"""
RAVEN Content Creation Engine
=============================
AI-powered content generation for social platforms. Fully functional with
the local Ollama model - no external credentials required.
"""

import logging
from multi_provider_ai import ROUTER, RAVEN_SYSTEM_PROMPT

logger = logging.getLogger("raven.content")


def _ai(prompt: str) -> str:
    messages = [
        {"role": "system", "content": RAVEN_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    text, _provider = ROUTER.chat(messages, max_tokens=400)
    return text


def generate_caption(platform: str, topic: str) -> str:
    prompt = (
        f"Write a high-engagement {platform} caption about: {topic}.\n"
        f"Style: scroll-stopping hook first line, value-packed body, "
        f"strong call-to-action. Max 150 words. Include 5 relevant hashtags."
    )
    return _ai(prompt)


def generate_script(topic: str, duration: int = 60) -> str:
    prompt = (
        f"Write a {duration}-second short-video script about: {topic}.\n"
        f"Format with timestamps [0-5s], [5-15s], etc. Include hook, 3 value points "
        f"and CTA. Language: punchy, spoken-word style."
    )
    return _ai(prompt)


def generate_hashtags(platform: str, topic: str) -> str:
    prompt = (
        f"Generate 20 high-reach {platform} hashtags for: {topic}.\n"
        f"Mix 40%% broad, 40%% niche, 20%% branded. Comma-separated, no explanations."
    )
    return _ai(prompt)


def generate_seo_package(title_topic: str) -> str:
    prompt = (
        f"Create an SEO package for a video about: {title_topic}.\n"
        f"Provide: 5 title options (under 60 chars), a keyword-rich 200-word "
        f"description, 15 tags, and 3 thumbnail concept ideas."
    )
    return _ai(prompt)


def content_calendar(platform: str, days: int = 7) -> str:
    prompt = (
        f"Create a {days}-day content calendar for {platform} for a security/crypto "
        f"education channel. For each day: topic, format (short/live/thread), "
        f"best posting time, and a one-line hook. Use a compact table."
    )
    return _ai(prompt)


def content_package(platform: str, topic: str) -> str:
    """Full package: caption + hashtags + script + thumbnail ideas."""
    parts = []
    try:
        parts.append("📝 CAPTION\n" + generate_caption(platform, topic))
    except Exception as exc:
        parts.append(f"📝 CAPTION\n❌ {exc}")
    try:
        parts.append("🎬 SCRIPT (60s)\n" + generate_script(topic, 60))
    except Exception as exc:
        parts.append(f"🎬 SCRIPT\n❌ {exc}")
    try:
        parts.append("#️⃣ HASHTAGS\n" + generate_hashtags(platform, topic))
    except Exception as exc:
        parts.append(f"#️⃣ HASHTAGS\n❌ {exc}")
    return "\n\n" + "=" * 30 + "\n\n".join(parts)
