"""
RAVEN Social Media Manager
==========================
Honest integration layer for social platforms.

- Twitter/X : real posting via `tweepy` when API credentials are set
              (pip install tweepy). Without keys, commands explain setup.
- TikTok / YouTube / Instagram : these platforms require approved developer
              apps and OAuth flows that cannot be automated from a bot token.
              Commands provide AI-generated content + clear setup guidance.

No fake "posted successfully" responses - ever.
"""

import os
import logging

logger = logging.getLogger("raven.social")


def _key(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value and not value.startswith("your_") and value.upper() != "NONE":
        return value
    return ""


def status() -> dict:
    """Which platforms have credentials configured."""
    twitter_ready = all(
        _key(k)
        for k in (
            "TWITTER_API_KEY",
            "TWITTER_API_SECRET",
            "TWITTER_ACCESS_TOKEN",
            "TWITTER_ACCESS_SECRET",
        )
    )
    return {
        "twitter": twitter_ready,
        "tiktok": bool(_key("TIKTOK_ACCESS_TOKEN")),
        "youtube": bool(_key("YOUTUBE_API_KEY")),
        "instagram": bool(_key("INSTAGRAM_SESSION")),
    }


def post_tweet(text: str):
    """Post a tweet via tweepy. Returns (ok, message)."""
    if not status()["twitter"]:
        return False, (
            "🐦 Twitter/X not configured.\n\n"
            "Setup:\n"
            "1. Create an app at https://developer.twitter.com\n"
            "2. Generate API key/secret + Access token/secret\n"
            "3. Add to .env:\n"
            "   TWITTER_API_KEY=...\n"
            "   TWITTER_API_SECRET=...\n"
            "   TWITTER_ACCESS_TOKEN=...\n"
            "   TWITTER_ACCESS_SECRET=...\n"
            "4. pip install tweepy"
        )
    try:
        import tweepy  # optional dependency
    except ImportError:
        return False, "❌ tweepy not installed. Run: pip install tweepy"

    try:
        client = tweepy.Client(
            consumer_key=_key("TWITTER_API_KEY"),
            consumer_secret=_key("TWITTER_API_SECRET"),
            access_token=_key("TWITTER_ACCESS_TOKEN"),
            access_token_secret=_key("TWITTER_ACCESS_SECRET"),
        )
        result = client.create_tweet(text=text[:280])
        return True, f"✅ Tweet posted (ID: {result.data['id']})"
    except Exception as exc:
        logger.error("Tweet failed: %s", exc)
        return False, f"❌ Tweet failed: {exc}"


def platform_guide(platform: str) -> str:
    """Setup instructions for platforms that require developer apps."""
    platform = platform.lower()
    if platform == "tiktok":
        return (
            "🎵 TikTok posting requires an approved developer app:\n"
            "1. Register at https://developers.tiktok.com\n"
            "2. Apply for Content Posting API access\n"
            "3. Set TIKTOK_ACCESS_TOKEN in .env\n"
            "\nMeanwhile, use /create_content to generate the video package\n"
            "(script + caption + hashtags) and post manually from the app."
        )
    if platform == "youtube":
        return (
            "📺 YouTube uploads require OAuth:\n"
            "1. Create a project at https://console.cloud.google.com\n"
            "2. Enable 'YouTube Data API v3'\n"
            "3. Create OAuth credentials and set YOUTUBE_API_KEY in .env\n"
            "\nUse /create_content youtube <topic> for titles, descriptions and tags."
        )
    if platform == "instagram":
        return (
            "📸 Instagram posting requires the Graph API:\n"
            "1. Business/F creator account + Facebook App\n"
            "2. Set INSTAGRAM_SESSION (instagrapi session file) in .env\n"
            "\nUse /create_content instagram <topic> for captions + hashtags."
        )
    return "Unknown platform."
