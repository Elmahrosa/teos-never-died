import asyncio
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
import logging
import io
import shutil
import sys
import json
import hashlib
import time
from contextlib import redirect_stdout
import urllib.parse
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from raven import get_reply, now_str, auto_save, auto_load

# SaaS + multimodal support. These modules degrade to inert behaviour when their
# optional dependencies are absent, so the bot must still start if they are
# broken or missing - which is why the import is guarded rather than fatal.
try:
    import database
    import saas_manager as saas
    import multimodal_engine as mm
    import translator
    import voice_engine
    SAAS_AVAILABLE = True
except Exception as _saas_import_error:  # pragma: no cover
    database = None
    saas = None
    mm = None
    translator = None
    voice_engine = None
    SAAS_AVAILABLE = False
    logging.getLogger("raven.bot").error(
        "SaaS/multimodal modules unavailable: %s", _saas_import_error)

# Try to import ccxt for crypto functionality
try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    ccxt = None

# Try to import requests for HIBP API
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    requests = None

# Try to import speech_recognition for voice commands
try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False
    sr = None

# Try to import pydub for audio processing
try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    AudioSegment = None

# Try to import APScheduler for scheduled jobs
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    AsyncIOScheduler = None

# Enable logging - console plus bot.log so failures stay inspectable
# after the launcher window closes.
LOG_FILE = os.getenv("RAVEN_LOG_FILE", "bot.log")
_handlers = [logging.StreamHandler()]
try:
    _file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _handlers.append(_file_handler)
except OSError as exc:  # read-only folder, locked file, etc.
    print(f"[!] Could not open {LOG_FILE} for logging: {exc}")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=_handlers
)
logger = logging.getLogger(__name__)

# httpx logs every request at INFO, and python-telegram-bot puts the bot token
# in the URL path:
#   INFO httpx: POST https://api.telegram.org/bot<TOKEN>/getUpdates "200 OK"
# That wrote the live bot token to bot.log on a loop every 10 seconds, so the
# log file was a plaintext credential store. Raise the threshold - these lines
# carry no diagnostic value here, and errors still surface at WARNING.
for _noisy in ("httpx", "httpcore", "telegram", "apscheduler.executors"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Global variables for bot state
conversation_history = []
current_model = os.getenv("RAVEN_MODEL", "dolphin-llama3:8b")
scheduler = None

# Helper function for temporary file cleanup
def cleanup_temp_file(filepath):
    """Safely remove a temporary file"""
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        logger.warning(f"Failed to cleanup temp file {filepath}: {e}")

# Telegram's hard limit per message
TELEGRAM_MAX_LEN = 4096

async def send_long(update: Update, text: str, parse_mode: str = None):
    """Send `text`, splitting on line boundaries when it exceeds Telegram's limit.

    Falls back to plain text if Markdown parsing fails, which happens whenever
    raw AI output contains stray _ * [ or ` characters.

    Returns the last Message that was sent, so callers can edit it later (see
    the voice-note flow). Returns None only if nothing could be delivered.
    """
    if not text:
        return None
    text = str(text)
    if len(text) <= TELEGRAM_MAX_LEN:
        chunks = [text]
    else:
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= TELEGRAM_MAX_LEN:
                chunks.append(remaining)
                break
            window = remaining[:TELEGRAM_MAX_LEN]
            cut = window.rfind("\n")
            if cut < TELEGRAM_MAX_LEN // 2:
                cut = TELEGRAM_MAX_LEN
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")

    sent = None
    for chunk in chunks:
        try:
            sent = await update.message.reply_text(chunk, parse_mode=parse_mode)
        except Exception as exc:
            logger.warning("reply failed (%s), retrying without parse_mode", exc)
            try:
                sent = await update.message.reply_text(chunk)
            except Exception as exc2:
                logger.error("reply failed entirely: %s", exc2)
    return sent

# ── Non-blocking AI access ──────────────────────────────────────────────
# Every local Ollama call is synchronous, and on a CPU-only host a single call
# can block for minutes. Calling one directly from an `async def` handler
# freezes the whole asyncio event loop, so the bot stops answering *anything*
# until the model returns - other commands, the scheduler, even the typing
# indicator. Handlers therefore go through `aget_reply`, which runs the blocking
# call in a worker thread and leaves the loop free.

async def aget_reply(model, messages, stream=False):
    """`raven.get_reply` executed off the event loop."""
    return await asyncio.to_thread(get_reply, model, messages, stream)


# Persona for ordinary chat. The full RAVEN_SYSTEM_PROMPT (~450 tokens) forces
# a rigid 4-phase vulnerability report on *every* message and measured ~234s of
# prefill on a CPU-only host, which is a large part of why plain chat felt
# broken. Commands that genuinely want that format (/scan, /analyze, ...) still
# use it. Override with RAVEN_CHAT_SYSTEM_PROMPT in .env to use it in chat too.
RAVEN_CHAT_SYSTEM_PROMPT = os.getenv(
    "RAVEN_CHAT_SYSTEM_PROMPT",
    "You are RAVEN, a security and crypto intelligence assistant. "
    "Answer directly and technically. Be concise unless detail is asked for. "
    "Never claim to have run a scan, query, or lookup that you did not run.",
)

_STREAM_DONE = object()  # sentinel: producer thread has finished


def _stream_ollama(messages, temperature, max_tokens, on_token, timeout):
    """Blocking worker: stream tokens from local Ollama through `on_token`.

    Mirrors `AIRouter._ollama`'s payload so chat and commands sample
    identically. Runs in a worker thread - never call this from the loop.
    """
    payload = {
        "model": ROUTER.model,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "num_predict": max_tokens,
        },
    }
    parts = []
    with requests.post(
        f"{ROUTER.ollama_host}/api/chat",
        json=payload,
        stream=True,
        timeout=(30, timeout),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            token = (chunk.get("message") or {}).get("content", "")
            if token:
                parts.append(token)
                on_token(token)
            if chunk.get("done"):
                break
    return "".join(parts)


async def stream_ai_reply(update, prompt, max_tokens=400, temperature=0.7):
    """Answer `prompt`, streaming tokens into Telegram as they arrive.

    The user sees progress instead of a frozen "analyzing..." bubble. Always
    finishes with a real message, and falls back to the non-streaming router if
    the local provider cannot stream, so chat can never go silent.
    """
    messages = [
        {"role": "system", "content": RAVEN_CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    placeholder = await update.message.reply_text("\u23f3 RAVEN is thinking...")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    failure = []

    async def _keep_typing():
        # Telegram drops the typing indicator after ~5s.
        while True:
            await asyncio.sleep(4)
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                return

    typing_task = asyncio.create_task(_keep_typing())

    def _produce():
        try:
            _stream_ollama(
                messages,
                temperature,
                max_tokens,
                lambda tok: loop.call_soon_threadsafe(queue.put_nowait, tok),
                OLLAMA_TIMEOUT,
            )
        except Exception as exc:
            failure.append(exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

    producer = asyncio.create_task(asyncio.to_thread(_produce))

    parts = []
    finished = False
    last_edit = 0.0
    try:
        while not (finished and queue.empty()):
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                item = None
            if item is _STREAM_DONE:
                finished = True
            elif item:
                parts.append(item)

            # Telegram rate-limits edits, so refresh at most every ~1.5s.
            now = time.monotonic()
            if parts and now - last_edit >= 1.5:
                last_edit = now
                tail = "".join(parts)[-3000:]
                if len(tail) == 3000:
                    tail = "..." + tail
                try:
                    await placeholder.edit_text(tail + "\u258c")
                except Exception as exc:
                    logger.debug("stream edit skipped: %s", exc)
        await producer
    finally:
        typing_task.cancel()
        if failure:
            logger.warning("chat stream failed (%s), falling back", failure[0])

    text = "".join(parts).strip()
    if not text:
        # Streaming yielded nothing - fall back to the proven router path so the
        # user still gets an answer instead of silence.
        try:
            text, _provider = await asyncio.to_thread(
                ROUTER.chat, messages, temperature, max_tokens
            )
            text = (text or "").strip()
        except Exception as exc:
            logger.error("chat fallback failed: %s", exc)
            try:
                await placeholder.edit_text(
                    f"\u274c AI unavailable: {exc}"[:4000]
                )
            except Exception:
                pass
            return None

    try:
        await placeholder.delete()
    except Exception:
        pass
    await send_long(update, text)
    return text

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    welcome_message = """
🔐 **RAVEN Security Bot** - AI-Powered Penetration Testing Assistant

Welcome to RAVEN, your elite security assessment AI agent powered by TEOS82.

**Available Commands:**
/start - Show this welcome message
/help - List all available commands
/clear - Clear conversation history
/history - Show conversation history
/model [model_name] - Change AI model
/system - Show current system status
/export - Export conversation history
/tokens - Show token usage stats
/save - Save current session
/load - Load previous session

**Security Assessment Commands:**
/scan <target> - Perform target reconnaissance
/cve <CVE-ID> - Get details about a specific CVE
/exploit <type> - Get exploitation techniques for vulnerability type
/harden <system> - Get hardening recommendations for a system
/analyze <target> - Full 4-phase penetration test report

**Intelligence Commands:**
/crypto <symbol> - Get real-time crypto prices from Binance & OKX
/market <keyword> - Analyze crypto market sentiment & news
/leak <target> - Check if email/domain appears in breach data (Real HIBP API)
/darkweb_intel <topic> - Dark web threat intelligence report

**AI Assistance Commands:**
/generate <topic> - Generate security-related content
/scrape <text> - Analyze and extract information from text
/ai_status - Show which AI providers are ready
/dashboard - Unified status board (AI, trading, uptime)

**Trading Commands** (paper mode by default - simulated fills):
/balance - Wallet balances and open positions
/positions - Open positions with P&L
/buy <symbol> <usdt> - Buy $X of a coin (e.g. /buy BTC 100)
/sell <symbol> <usdt> - Sell $X of a coin
/orders [days] - Trade history
/signal <symbol> - AI trade signal with confidence
/report <daily|weekly|monthly> - P&L report
/trade_mode [paper|live] - Show or change trading mode
/emergency_stop - Disable all trading
/resume - Re-enable trading after an emergency stop

**Social & Content Commands:**
/tweet <text> - Post to Twitter/X
/thread <topic> - Draft a Twitter thread with AI
/social_status - Which platforms are configured
/social_guide <platform> - Setup steps for tiktok/youtube/instagram
/create_content <platform> <topic> - Caption + script + hashtags
/content_calendar <platform> [days] - AI content calendar

**Phase 3 Features:**
🎤 Voice Commands - Send voice notes for hands-free operation
📅 Daily Briefings - Automated security intelligence reports

Simply send a message to chat with RAVEN naturally.
Send a voice note for voice command processing.
    """
    await send_long(update, welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_text = """
🔐 **RAVEN Security Bot - Command Reference**

**Basic Commands:**
/start - Welcome message and bot introduction
/help - Show this help message
/clear - Clear conversation history
/history - Display conversation history
/model [model_name] - Change AI model (e.g., /model dolphin-llama3:8b)
/system - Show current bot status and configuration
/export - Export conversation history as JSON
/tokens - Show estimated token usage
/save - Save current conversation session
/load - Load previous conversation session

**Security Assessment Tools:**
/scan <target> - Reconnaissance and information gathering
/cve <CVE-ID> - Detailed CVE analysis and impact assessment
/exploit <type> - Exploitation techniques for specific vulnerability types
/harden <system> - Security hardening recommendations for systems
/analyze <target> - Complete 4-phase penetration test report

**Intelligence Modules:**
/crypto <symbol> - Real-time crypto prices from Binance & OKX (e.g., /crypto BTC)
/market <keyword> - Crypto market sentiment analysis (e.g., /market Bitcoin)
/leak <target> - Breach data check for email/domain (e.g., /leak user@example.com)
/darkweb_intel <topic> - Dark web threat intelligence (e.g., /darkweb_intel ransomware)

**AI Assistance:**
/generate <topic> - Generate security reports, writeups, or documentation
/scrape <text> - Extract key information from security texts or logs

**Multimodal Analysis:**
/ai_status - Which AI providers are ready
/dashboard - Full status: usage, SaaS metrics, backends
Send a photo, video or .txt/.md/.log/.pdf/.docx/.csv/.json/.yaml document and
RAVEN analyses it. Add a caption to tell it what to look for.

**Translation:**
/translate <lang> <text> - Translate into any of 34 languages
/languages - List every supported language
/detect <text> - Identify the language of a message
Includes ar, en, fr, de, es, it, pt, ru, ja, ko, zh, hi, tr. Reply to any
message with /translate <lang> to translate that instead.

**Voice Replies:**
/voice on - Speak every reply aloud (off by default)
/voice off - Text only
/voice male - Deeper voice      /voice female - Higher voice
/voice lang <code> - Speak another language (e.g. /voice lang ar)
/voice_lang <code> - Same, as its own command
/voices [code] [male|female] - Browse available voices
/voice_use <name> - Pin one exact voice

**Plans & Usage:**
/my_usage - Your tier, today's quota and features
/upgrade - Plan comparison

**Trading (paper mode by default):**
/trade_mode <paper|live> - Show or change trading mode
/buy <symbol> <usdt> - Buy notional USDT (e.g. /buy BTC 100)
/sell <symbol> <usdt> - Sell notional USDT
/balance - Cash, positions, equity, unrealized P&L
/positions - Open positions with per-position P&L
/orders [days] - Trade history
/signal <symbol> - AI signal: bias, confidence, entry, stop, target
/report <daily|weekly|monthly> - P&L report
/emergency_stop - Halt all trading
/resume - Re-enable trading after a stop
Amounts are notional. Buy/sell/mode/stop/resume are admin-only.

**Social & Content:**
/tweet <text> - Post to Twitter/X
/thread <topic> - AI-drafted Twitter thread
/create_content <platform> <topic> - Caption + script + hashtags
/content_calendar <platform> [days] - AI content calendar
/social_status - Which social credentials are configured
/social_guide <platform> - Step-by-step setup for a platform
Only Twitter/X posts directly; other platforms need your own approved developer
apps, so those commands build the content and print the setup steps.

**Usage Notes:**
- Security commands trigger RAVEN's 4-phase assessment format
- Natural conversation works too - just send a message
- Sessions are auto-saved and restored
- Crypto needs ccxt, voice commands need SpeechRecognition + pydub,
  briefings need APScheduler, breach checks need a HIBP API key
- Translation needs its own model: ollama pull aya-expanse:8b
- Voice replies need edge-tts + ffmpeg: pip install edge-tts
- The first translation after a restart takes ~10 minutes to load the 5GB
  model; later ones take 20-40s. The typing indicator stays on meanwhile.
    """
    await send_long(update, help_text, parse_mode='Markdown')

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear command."""
    global conversation_history
    conversation_history = []
    # Clear saved session file
    try:
        if os.path.exists("raven_session.json"):
            os.remove("raven_session.json")
    except Exception:
        pass
    await send_long(update, "🗑️ Conversation history cleared.")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command."""
    global conversation_history
    if not conversation_history:
        await send_long(update, "📝 No conversation history.")
        return

    # Format history for display
    history_text = "📜 **Conversation History:**\n\n"
    for i, msg in enumerate(conversation_history[-10:], 1):  # Show last 10 messages
        role = "👤 You" if msg["role"] == "user" else "🤖 RAVEN"
        content = msg["content"][:100] + ("..." if len(msg["content"]) > 100 else "")
        history_text += f"{i}. {role}: {content}\n\n"

    if len(conversation_history) > 10:
        history_text += f"... and {len(conversation_history) - 10} more messages"

    await send_long(update, history_text, parse_mode='Markdown')

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /model command."""
    global current_model
    if context.args:
        new_model = context.args[0]
        current_model = new_model
        await send_long(update, f"🔄 Model changed to: {new_model}")
    else:
        await send_long(update, f"🤖 Current model: {current_model}\nUsage: /model <model_name>")

async def system_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /system command."""
    system_info = f"""
🖥️ **RAVEN System Status**

**Configuration:**
- Model: {current_model}
- Ollama Host: {os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")}
- Session Length: {len(conversation_history)} messages
- Session File: {'raven_session.json' if os.path.exists('raven_session.json') else 'Not found'}
- Scheduler Status: {'Running' if scheduler and scheduler.running else 'Stopped'}

**Capabilities:**
- Security Reconnaissance
- Vulnerability Assessment
- Exploitation Planning
- System Hardening
- AI-Powered Analysis
- Crypto Market Intelligence (Binance/OKX)
- Dark Web OSINT & Threat Intelligence
- Real Breach Data Detection (HIBP API)
- Voice Command Processing
- Automated Daily Briefings
- Image / Video / Document Analysis
- Translation (34 languages incl. Arabic)
- Spoken Voice Replies (male or female)

**Status:** 🟢 Online and ready
    """
    await send_long(update, system_info, parse_mode='Markdown')

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export command."""
    global conversation_history
    try:
        export_data = {
            "export_timestamp": now_str(),
            "total_messages": len(conversation_history),
            "conversation": conversation_history
        }

        filename = f"raven_export_{int(time.time())}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        await send_long(update, f"💾 Conversation exported to `{filename}`", parse_mode='Markdown')
    except Exception as e:
        await send_long(update, f"❌ Export failed: {str(e)}")

async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tokens command."""
    global conversation_history
    # Rough estimation: ~4 characters per token
    total_chars = sum(len(msg["content"]) for msg in conversation_history)
    estimated_tokens = total_chars // 4

    tokens_info = f"""
🔢 **Token Usage Estimation**

**Conversation Stats:**
- Total Messages: {len(conversation_history)}
- Total Characters: {total_chars:,}
- Estimated Tokens: {estimated_tokens:,}

**Notes:**
- This is a rough approximation (4 chars ≈ 1 token)
- Actual token usage depends on the specific model
- Includes both user and AI messages
    """
    await send_long(update, tokens_info, parse_mode='Markdown')

async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /save command."""
    global conversation_history
    if auto_save(conversation_history, current_model):
        await send_long(update, "💾 Session saved successfully.")
    else:
        await send_long(update, "❌ Failed to save session.")

async def load_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /load command."""
    global conversation_history
    loaded_history = auto_load()
    if loaded_history is not None:
        conversation_history = loaded_history
        await send_long(update, f"📂 Session loaded. {len(conversation_history)} messages restored.")
    else:
        await send_long(update, "❌ Failed to load session or no saved session found.")

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command."""
    if not context.args:
        await send_long(update, "❌ Usage: /scan <target>\nExample: /scan 192.168.1.1")
        return

    target = " ".join(context.args)
    await send_long(update, f"🔍 Starting reconnaissance on `{target}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        reconnaissance_prompt = f"Perform comprehensive target reconnaissance on {target}. Provide detailed information about the target's attack surface, technologies, infrastructure, and potential entry points."
        response = await aget_reply(current_model, [{"role": "user", "content": reconnaissance_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/scan {target}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"🎯 **Reconnaissance Results for {target}:**\n\n{response}", parse_mode='Markdown')

async def cve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cve command."""
    if not context.args:
        await send_long(update, "❌ Usage: /cve <CVE-ID>\nExample: /cve CVE-2023-1234")
        return

    cve_id = " ".join(context.args).upper()
    await send_long(update, f"🔍 Analyzing `{cve_id}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        cve_prompt = f"Provide detailed analysis of {cve_id}: vulnerability description, affected systems, CVSS score, exploit availability, patch status, and mitigation strategies."
        response = await aget_reply(current_model, [{"role": "user", "content": cve_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/cve {cve_id}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"🐛 **CVE Analysis for {cve_id}:**\n\n{response}", parse_mode='Markdown')

async def exploit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /exploit command."""
    if not context.args:
        await send_long(update, "❌ Usage: /exploit <type>\nExample: /exploit bufferOverflow")
        return

    vuln_type = " ".join(context.args)
    await send_long(update, f"⚡ Getting exploitation techniques for `{vuln_type}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        exploit_prompt = f"Detail exploitation techniques, payloads, and bypass methods for {vuln_type} vulnerabilities. Include required tools, skill level, and effectiveness ratings."
        response = await aget_reply(current_model, [{"role": "user", "content": exploit_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/exploit {vuln_type}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"⚡ **Exploitation Techniques for {vuln_type}:**\n\n{response}", parse_mode='Markdown')

async def harden_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /harden command."""
    if not context.args:
        await send_long(update, "❌ Usage: /harden <system>\nExample: /harden windowsServer")
        return

    system = " ".join(context.args)
    await send_long(update, f"🛡️ Getting hardening recommendations for `{system}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        harden_prompt = f"Provide comprehensive security hardening recommendations for {system}. Include configuration changes, patch management, network segmentation, monitoring, and incident response improvements."
        response = await aget_reply(current_model, [{"role": "user", "content": harden_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/harden {system}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"🛡️ **Hardening Recommendations for {system}:**\n\n{response}", parse_mode='Markdown')

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /analyze command - triggers full 4-phase pentest report."""
    if not context.args:
        await send_long(update, "❌ Usage: /analyze <target>\nExample: /analyze 10.0.0.5")
        return

    target = " ".join(context.args)
    await send_long(update, f"🎯 Starting full 4-phase penetration test analysis on `{target}`...\\nThis may take a moment.", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        analyze_prompt = f"Conduct a complete 4-phase penetration test assessment on {target}:\\n1. Target Reconnaissance\\n2. Vulnerability Mapping (with severity)\\n3. Exploitation Plan\\n4. Remediation & Hardening Plan\\nProvide detailed, technical, and actionable findings for each phase."
        response = await aget_reply(current_model, [{"role": "user", "content": analyze_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/analyze {target}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"📋 **Full 4-Phase Penetration Test Report for {target}:**\n\n{response}", parse_mode='Markdown')

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /generate command."""
    if not context.args:
        await send_long(update, "❌ Usage: /generate <topic>\nExample: /generate sqlInjectionReport")
        return

    topic = " ".join(context.args)
    await send_long(update, f"📝 Generating content on `{topic}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        generate_prompt = f"Generate professional security content about {topic}. This could be a report, writeup, analysis, or documentation suitable for security professionals."
        response = await aget_reply(current_model, [{"role": "user", "content": generate_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/generate {topic}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"📄 **Generated Content on {topic}:**\n\n{response}", parse_mode='Markdown')

async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scrape command."""
    if not context.args:
        await send_long(update, "❌ Usage: /scrape <text>\nExample: /scrape logFileContents")
        return

    text = " ".join(context.args)
    await send_long(update, f"🔍 Analyzing and extracting information from provided text...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        scrape_prompt = f"Analyze the following text and extract key security-relevant information: indicators of compromise, vulnerabilities, attack patterns, or actionable intelligence:\\n\\n{text}"
        response = await aget_reply(current_model, [{"role": "user", "content": scrape_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/scrape {text[:50]}..." if len(text) > 50 else f"/scrape {text}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"🔎 **Extracted Information:**\n\n{response}", parse_mode='Markdown')

# MODULE A: REAL BREACH DATA (/leak command with HIBP API)

# Reusable response blocks
_NO_BREACH_RESPONSE = """✅ **NO BREACHES DETECTED** - {target}

🔒 **VERIFIED BY**: Have I Been Pwned (HIBP) API
🛡️ **STATUS**: This email address does not appear in any known breach databases.

**RECOMMENDATIONS:**
1. Continue practicing good security hygiene
2. Use unique, strong passwords for each service
3. Enable multi-factor authentication where available
4. Regularly monitor for breach notifications
5. Consider using a password manager
6. Periodically repeat this check (especially after major breach disclosures)

**NOTE:** Data sourced from Have I Been Pwned API (https://haveibeenpwned.com/API/v3)
*Analysis conducted by RAVEN Breach Intelligence Module*"""

_RATE_LIMIT_RESPONSE = """⚠️ **RATE LIMIT EXCEEDED** - {target}

🔒 **VERIFIED BY**: Have I Been Pwned (HIBP) API
📉 **STATUS**: Too many requests. Please wait a moment before trying again.

**RECOMMENDATIONS:**
1. Wait a few minutes before checking again
2. Consider upgrading your HIBP API plan for higher rate limits
3. Use cached results if available for recent checks

*Analysis conducted by RAVEN Breach Intelligence Module*"""

async def leak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /leak command - check if email/domain appears in breach data using HIBP API."""
    if not context.args:
        await send_long(update, "❌ Usage: /leak <target>\nExample: /leak user@example.com or /leak example.com")
        return

    target = " ".join(context.args).strip()
    await send_long(update, f"🔍 Checking `{target}` against breach databases...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        hibp_api_key = os.getenv("HIBP_API_KEY", "").strip()

        # ---- Dependency check ----
        if not REQUESTS_AVAILABLE:
            response = "❌ Real breach detection not available.\nPlease install requests: `pip install requests`"

        # ---- Domain (non-email) target: HIBP v3 only supports emails ----
        elif "@" not in target:
            response = f"""⚠️ **DOMAIN BREACH CHECK LIMITATION** - {target}

The Have I Been Pwned API v3 only supports checking individual email addresses, not domains directly.

For domain breach monitoring, you would need to:
1. Check known email addresses associated with the domain
2. Use alternative breach intelligence services
3. Monitor public paste sites for domain mentions

**RECOMMENDATION:** Try checking specific email addresses from this domain using /leak email@domain.com

*Analysis conducted by RAVEN OSINT Module*"""

        # ---- Real HIBP API (key configured, email target) ----
        elif hibp_api_key and hibp_api_key != "your_haveibeenpwned_api_key_here":
            headers = {
                "hibp-api-key": hibp_api_key,
                "user-agent": "RAVEN-Security-Bot"
            }
            try:
                url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{quote(target)}"
                response_obj = requests.get(
                    url,
                    headers=headers,
                    params={"truncateResponse": "false"},
                    timeout=15
                )

                if response_obj.status_code == 200:
                    breaches = response_obj.json()
                    if isinstance(breaches, list) and len(breaches) > 0:
                        breach_names = [breach.get("Name", "Unknown") for breach in breaches]
                        breach_count = len(breach_names)

                        # Get detailed breach info for the response (first 3)
                        detailed_breaches = []
                        for breach in breaches[:3]:
                            detailed_breaches.append({
                                "Name": breach.get("Name", "Unknown"),
                                "Title": breach.get("Title", "Unknown"),
                                "BreachDate": breach.get("BreachDate", "Unknown"),
                                "PwnCount": breach.get("PwnCount", 0),
                                "DataClasses": breach.get("DataClasses", [])
                            })

                        # Generate damage assessment using RAVEN AI
                        breach_list_str = ", ".join(breach_names[:5])
                        if len(breach_names) > 5:
                            breach_list_str += f" and {len(breach_names) - 5} more"

                        ai_prompt = f"""Generate a comprehensive damage assessment and mitigation plan for the email address {target} which has been found in the following data breaches: {breach_list_str}.

Provide:
1. Immediate actions to take
2. Investigation steps
3. Long-term protection strategies
4. Security hardening recommendations

Format as a professional security report with clear, actionable steps."""

                        ai_response = await aget_reply(current_model, [{"role": "user", "content": ai_prompt}])

                        all_data_classes = [item for b in detailed_breaches for item in b["DataClasses"]]
                        response = f"""🚨 **CONFIRMED BREACH DETECTION** - {target}

🔒 **VERIFIED BY**: Have I Been Pwned (HIBP) API
⚠️ **BREACH COUNT**: {breach_count} confirmed breach(es)

**BREACH DETAILS:**
{chr(10).join([f"• {b['Name']} ({b['Title']}) - {b['BreachDate']} - {b['PwnCount']:,} accounts" for b in detailed_breaches])}

**DATA TYPES COMPROMISED:**
{', '.join(all_data_classes[:10])}{'...' if len(all_data_classes) > 10 else ""}

**DAMAGE ASSESSMENT & MITIGATION PLAN:**

{ai_response}

**NOTE:** Data sourced from Have I Been Pwned API (https://haveibeenpwned.com/API/v3)
*Analysis conducted by RAVEN Breach Intelligence Module*"""
                    else:
                        response = _NO_BREACH_RESPONSE.format(target=target)
                elif response_obj.status_code == 404:
                    response = _NO_BREACH_RESPONSE.format(target=target)
                elif response_obj.status_code == 401:
                    response = f"""❌ **INVALID HIBP API KEY**

The Have I Been Pwned API rejected the configured key (HTTP 401).

**FIX:** Check your `HIBP_API_KEY` in the `.env` file and get a valid key at https://haveibeenpwned.com/API/Key

*Analysis conducted by RAVEN Breach Intelligence Module*"""
                elif response_obj.status_code == 429:
                    response = _RATE_LIMIT_RESPONSE.format(target=target)
                else:
                    response = f"❌ Error from HIBP API: HTTP {response_obj.status_code}\n{response_obj.text}"
            except Exception as e:
                response = f"❌ Error conducting breach check: {str(e)}\n\nPlease check:\n1. HIBP API key validity\n2. Internet connection\n3. Target format (email address)"

        # ---- Simulated fallback (no API key configured) ----
        else:
            response = f"""⚠️ HIBP API key not configured. Using simulated breach check.
Get a free API key at https://haveibeenpwned.com/API/Key and set `HIBP_API_KEY` in `.env` for real data.

"""
            target_hash = hashlib.md5(target.lower().encode()).hexdigest()
            is_breached = int(target_hash[:8], 16) % 7 == 0  # deterministic pseudo-check

            if is_breached:
                breach_count = (int(target_hash[8:12], 16) % 5) + 1  # 1-5 breaches
                latest_breach_year = 2020 + (int(target_hash[12:16], 16) % 4)  # 2020-2023
                data_types = ["Email addresses", "Passwords", "Personal data", "Credit card data"]
                compromised_data = [data_types[int(target_hash[i:i+2], 16) % len(data_types)]
                                    for i in range(16, min(26, len(target_hash)), 2)][:3]

                response += f"""🚨 **BREACH ALERT** - {target}

⚠️ **THREAT DETECTED**: This target has appeared in {breach_count} known data breach(es)

**BREACH DETAILS:**
• Latest Appearance: {latest_breach_year}
• Data Types Compromised: {', '.join(compromised_data)}
• Risk Level: {"HIGH" if breach_count >= 3 else "MEDIUM" if breach_count == 2 else "LOW"}

**DAMAGE ASSESSMENT & MITIGATION PLAN:**

1. **IMMEDIATE ACTIONS:**
   - Force password reset for all associated accounts
   - Enable multi-factor authentication (MFA) everywhere possible
   - Monitor financial accounts for suspicious activity
   - Consider credit freeze if financial data was exposed

2. **INVESTIGATION STEPS:**
   - Identify which specific breaches affected this target
   - Determine exact data points compromised
   - Check for credential reuse across other services
   - Monitor dark web markets for this data appearing

3. **LONG-TERM PROTECTION:**
   - Implement password manager with unique, strong passwords
   - Use email aliases for different services
   - Regular breach monitoring (quarterly checks recommended)
   - Consider identity theft protection services

4. **SECURITY HARDENING:**
   - Review and update security questions/answers
   - Audit connected apps and revoke unnecessary permissions
   - Implement additional authentication factors where available
   - Educate users about phishing risks (breached data often used in social engineering)

**NOTE:** This is a simulated OSINT check. For production use, integrate with:
- Have I Been Pwned API (https://haveibeenpwned.com/API/v3)
- DeHashed API
- IntelligenceX
- Or similar breach intelligence platforms

*Analysis conducted by RAVEN Dark Web OSINT Module*"""
            else:
                response += f"""✅ **NO BREACHES DETECTED** - {target}

🛡️ **STATUS**: This target does not appear in known breach databases (based on simulated check).

**RECOMMENDATIONS:**
1. Continue practicing good security hygiene
2. Use unique, strong passwords for each service
3. Enable multi-factor authentication where available
4. Regularly monitor for breach notifications
5. Consider using a password manager
6. Periodically repeat this check (especially after major breach disclosures)

**NOTE:** This is a simulated OSINT check. For production use, integrate with:
- Have I Been Pwned API (https://haveibeenpwned.com/API/v3)
- DeHashed API
- IntelligenceX
- Or similar breach intelligence platforms

*Analysis conducted by RAVEN Dark Web OSINT Module*"""

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/leak {target}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, response, parse_mode='Markdown')

# ==========================================
# INTELLIGENCE MODULES (crypto / market / darkweb_intel)
# ==========================================
async def crypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /crypto command - get real-time crypto prices from Binance and OKX."""
    if not CCXT_AVAILABLE:
        await send_long(update,
            "❌ Crypto intelligence module not available.\n"
            "Please install ccxt: `pip install ccxt`",
            parse_mode='Markdown'
        )
        return

    if not context.args:
        await send_long(update, "❌ Usage: /crypto <symbol>\nExample: /crypto BTC or /crypto ETH/USDT")
        return

    symbol = " ".join(context.args).upper()
    # Ensure symbol has a market pair format
    if '/' not in symbol and len(symbol) <= 5:  # Assume it's a base currency like BTC
        symbol = f"{symbol}/USDT"

    await send_long(update, f"📊 Fetching real-time data for `{symbol}` from Binance & OKX...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        try:
            # Initialize exchanges
            binance = ccxt.binance({'enableRateLimit': True})
            okx = ccxt.okx({'enableRateLimit': True})

            # Fetch tickers from both exchanges
            binance_ticker = await asyncio.to_thread(binance.fetch_ticker, symbol)
            okx_ticker = await asyncio.to_thread(okx.fetch_ticker, symbol)

            # Format the response
            response = f"""💰 **CRYPTO MARKET INTELLIGENCE REPORT** - {symbol}

**BINANCE:**
• Price: ${binance_ticker['last']:,.4f}
• 24h Volume: {binance_ticker['baseVolume']:,.2f} {symbol.split('/')[0]}
• 24h Change: {binance_ticker['percentage']:+.2f}%
• Bid: ${binance_ticker['bid']:,.4f}
• Ask: ${binance_ticker['ask']:,.4f}
• High/Low: ${binance_ticker['high']:,.4f} / ${binance_ticker['low']:,.4f}

**OKX:**
• Price: ${okx_ticker['last']:,.4f}
• 24h Volume: {okx_ticker['baseVolume']:,.2f} {symbol.split('/')[0]}
• 24h Change: {okx_ticker['percentage']:+.2f}%
• Bid: ${okx_ticker['bid']:,.4f}
• Ask: ${okx_ticker['ask']:,.4f}
• High/Low: ${okx_ticker['high']:,.4f} / ${okx_ticker['low']:,.4f}

**ARBITRAGE OPPORTUNITY:**
• Price Difference: ${abs(binance_ticker['last'] - okx_ticker['last']):,.4f} ({abs(binance_ticker['last'] - okx_ticker['last']) / ((binance_ticker['last'] + okx_ticker['last']) / 2) * 100:.2f}%)

**TIMESTAMP:** {now_str()}
*Data sourced from public APIs of Binance and OKX exchanges*"""

        except Exception as e:
            response = f"❌ Error fetching crypto data: {str(e)}\n\nPlease check:\n1. Symbol format (use format like BTC/USDT)\n2. Exchange availability\n3. Internet connection"

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/crypto {symbol}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, response, parse_mode='Markdown')

async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /market command - AI analysis of crypto market sentiment."""
    if not context.args:
        await send_long(update, "❌ Usage: /market <keyword>\nExample: /market Bitcoin or /market DeFi")
        return

    keyword = " ".join(context.args)
    await send_long(update, f"📈 Analyzing market sentiment for `{keyword}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        market_prompt = f"""As RAVEN, provide a comprehensive crypto market intelligence analysis for the keyword/topic: "{keyword}".

Include:
1. Current market sentiment and trends
2. Key price levels and technical analysis insights
3. Major news and developments affecting this asset/sector
4. Risk factors and potential catalysts
5. Trading volume and liquidity assessment
6. Social media and community sentiment indicators
7. On-chain metrics if applicable
8. Short-term outlook (24-72 hours) and medium-term outlook (1-4 weeks)

Provide actionable insights for traders and investors. Use your elite security analysis mindset to identify potential market manipulation risks or unusual activity patterns."""

        response = await aget_reply(current_model, [{"role": "user", "content": market_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/market {keyword}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"📊 **MARKET INTELLIGENCE ANALYSIS: {keyword}**\n\n{response}", parse_mode='Markdown')

async def darkweb_intel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /darkweb_intel command - generate dark web threat intelligence report."""
    if not context.args:
        await send_long(update, "❌ Usage: /darkweb_intel <topic>\nExample: /darkweb_intel ransomware gangs targeting healthcare")
        return

    topic = " ".join(context.args)
    await send_long(update, f"🕵️‍♂️ Generating dark web intelligence report on `{topic}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        intel_prompt = f"""As RAVEN with expertise in Dark Web OSINT & Threat Intelligence, generate a comprehensive intelligence report on: "{topic}".

Provide:
1. **THREAT OVERVIEW**: Current landscape and key threat actors
2. **RECENT ACTIVITY**: Notable incidents, campaigns, or trends in the last 3-6 months
3. **TTPs (Tactics, Techniques, Procedures)**: How these threats operate
4. **INFRASTRUCTURE**: Known dark web marketplaces, forums, or communication channels used
5. **VICTIMOLOGY**: Typical targets and victim profiles
6. **FINANCIAL ASPECTS**: Estimated revenue, payment methods, and economic impact
7. **LAW ENFORCEMENT RESPONSE**: Recent operations, takedowns, or legal actions
8. **DEFENSIVE RECOMMENDATIONS**: Specific mitigation strategies for organizations
9. **OUTLOOK**: Emerging trends and future threats to watch
10. **CONFIDENCE LEVEL**: Assessment of information reliability and source quality

Structure this as a professional intelligence report suitable for security teams and CISOs. Use clear, actionable language with specific details where possible."""

        response = await aget_reply(current_model, [{"role": "user", "content": intel_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/darkweb_intel {topic}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history, current_model)

    await send_long(update, f"🕵️‍♂️ **DARK WEB THREAT INTELLIGENCE REPORT**\n\nTopic: {topic}\n\n{response}", parse_mode='Markdown')

# MODULE B: VOICE COMMAND SUPPORT
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages by transcribing them and processing as text commands."""
    if not SPEECH_RECOGNITION_AVAILABLE:
        await send_long(update, "❌ Voice command support not available.\\nPlease install SpeechRecognition: `pip install SpeechRecognition`")
        return
    
    if not PYDUB_AVAILABLE:
        await send_long(update, "❌ Audio processing support not available.\\nPlease install pydub: `pip install pydub`")
        return
    
    # Show processing message
    processing_msg = await send_long(update, "🎤 Processing voice note...", parse_mode='Markdown')
    
    temp_ogg_path = None
    temp_wav_path = None
    
    try:
        # Download the voice file
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        temp_ogg_path = f"temp_voice_{update.message.voice.file_id}.ogg"
        await voice_file.download_to_drive(temp_ogg_path)
        
        # Convert OGG to WAV for speech recognition
        audio = AudioSegment.from_ogg(temp_ogg_path)
        temp_wav_path = f"temp_voice_{update.message.voice.file_id}.wav"
        audio.export(temp_wav_path, format="wav")
        
        # Initialize recognizer
        recognizer = sr.Recognizer()
        
        # Transcribe the audio
        with sr.AudioFile(temp_wav_path) as source:
            audio_data = recognizer.record(source)
            try:
                # Using Google's free speech recognition
                text = recognizer.recognize_google(audio_data)
                
                # Send acknowledgment of what was heard
                await processing_msg.edit_text(f"🎤 **Voice Transcribed:**\\n\"{text}\"\\n\\n🤖 RAVEN is processing...", parse_mode='Markdown')
                
                # Process the transcribed text as if it was a regular message
                await handle_message_from_text(update, context, text)
                
            except sr.UnknownValueError:
                await processing_msg.edit_text("❌ Could not understand audio. Please speak more clearly.")
            except sr.RequestError as e:
                await processing_msg.edit_text(f"❌ Speech recognition service error: {str(e)}")
                
    except Exception as e:
        await processing_msg.edit_text(f"❌ Error processing voice note: {str(e)}")
    finally:
        # Clean up temporary files
        cleanup_temp_file(temp_ogg_path)
        cleanup_temp_file(temp_wav_path)

async def _chat_respond(update: Update, prompt: str):
    """Shared chat pipeline: stream the reply, then record it in history."""
    response = await stream_ai_reply(update, prompt)
    if not response:
        return None
    conversation_history.append({"role": "user", "content": prompt})
    conversation_history.append({"role": "assistant", "content": response})
    del conversation_history[:-40]  # keep the saved session bounded
    auto_save(conversation_history, current_model)
    # Voice replies are additive: the text is already delivered by
    # stream_ai_reply, so a failure here can never lose the answer.
    if SAAS_AVAILABLE and voice_engine is not None:
        try:
            await _maybe_speak(update, response)
        except Exception:
            logger.debug("voice attach failed", exc_info=True)
    return response


async def handle_message_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process text (from voice or regular message) and get RAVEN's response."""
    return await _chat_respond(update, text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages."""
    return await _chat_respond(update, update.message.text)


# ═══════════════════════════════════════════════════════════════════════════
# Multimodal handlers (photo / video / document)
#
# Every one of these does its heavy work through asyncio.to_thread, because
# vision inference, ffmpeg and PDF parsing are all synchronous and would
# otherwise stall the event loop - the same class of bug that used to freeze
# normal chat.
# ═══════════════════════════════════════════════════════════════════════════
MEDIA_TMP_DIR = os.getenv("RAVEN_MEDIA_TMP", "raven_media_tmp")


def _media_tmp(name: str) -> str:
    os.makedirs(MEDIA_TMP_DIR, exist_ok=True)
    return os.path.join(MEDIA_TMP_DIR, name)


def _short_id(file_id: str) -> str:
    """Stable short token for a Telegram file_id, safe as a filename."""
    return hashlib.sha256((file_id or "").encode("utf-8")).hexdigest()[:16]


def _human_size(nbytes: int) -> str:
    """Readable size. Avoids printing a misleading '0 KB' for small files."""
    nbytes = int(nbytes or 0)
    if nbytes < 1024:
        return f"{nbytes} B"
    for unit in ("KB", "MB", "GB"):
        nbytes /= 1024.0
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.1f} {unit}"
    return f"{nbytes:.1f} GB"


async def _media_gate(update: Update, action: str) -> bool:
    """Check tier capability + daily quota for a media action.

    Returns True when processing may proceed; otherwise sends the refusal and
    returns False. Admins always pass. Mirrors saas_manager.requires() but is
    written out because MessageHandlers take the same (update, context) shape
    while needing the gate before they touch the file.
    """
    user = getattr(update, "effective_user", None)
    if user is None:
        return False
    if is_admin(user.id):
        return True
    allowed, reason = await saas.evaluate(user.id, action, is_admin=False)
    if not allowed:
        await send_long(update, reason)
        return False
    await saas.record_usage(user.id, action)
    return True


class _typing_forever:
    """Async context manager keeping Telegram's typing indicator alive.

    Telegram clears the indicator after ~5s, and multimodal analysis routinely
    takes minutes, so without this the bot looks hung.
    """

    def __init__(self, update: Update):
        self._update = update
        self._task = None

    async def __aenter__(self):
        async def _pump():
            while True:
                try:
                    await self._update.message.chat.send_action("typing")
                except Exception:
                    return
                await asyncio.sleep(4)

        self._task = asyncio.create_task(_pump())
        return self

    async def __aexit__(self, *exc_info):
        if self._task:
            self._task.cancel()
        return False


async def _download_to(update: Update, context, file_id: str, dest: str) -> str:
    """Fetch a Telegram file to `dest`. Returns the path, or raises."""
    handle = await context.bot.get_file(file_id)
    await handle.download_to_drive(custom_path=dest)
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("Telegram returned an empty file")
    return dest


async def _finish_media(update: Update, note, text: str):
    """Deliver a media analysis result, keeping `note` as a fallback."""
    body = (text or "").strip()
    if not body:
        body = "Analysis completed but the model returned nothing."
    # Leave the placeholder readable in case the long send fails partway.
    try:
        await note.edit_text("\u2705 Analysis complete - sending result...")
    except Exception:
        pass
    await send_long(update, body)
    try:
        await note.delete()
    except Exception:
        pass


def _caption_focus(caption: str) -> str:
    """Turn a Telegram caption into an extra instruction for the model."""
    caption = (caption or "").strip()
    if not caption:
        return ""
    return f"\n\nThe sender added this instruction, honour it: {caption}"


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyze an image for security-relevant content."""
    if not await _media_gate(update, saas.A_IMAGE):
        return

    photos = list(getattr(update.message, "photo", None) or [])
    if not photos:
        await send_long(update, "No image was attached to that message.")
        return

    # Telegram sends every size; the last element is the highest resolution.
    largest = photos[-1]
    note = await update.message.reply_text(
        "\U0001F5BC Downloading image at highest resolution...", parse_mode=None)

    path = _media_tmp(f"img_{_short_id(largest.file_id)}.jpg")
    try:
        async with _typing_forever(update):
            await _download_to(update, context, largest.file_id, path)
            size = os.path.getsize(path)
            focus = _caption_focus(update.message.caption)
            prompt = mm.VISION_PROMPT + focus
            try:
                result = await asyncio.to_thread(mm.analyze_image, path, prompt)
            except Exception as exc:
                logger.warning("image analysis failed: %s", exc)
                result = f"\u274C Image analysis could not run.\n\n{exc}"
            else:
                result = (
                    f"\U0001F5BC IMAGE ANALYSIS - {_human_size(size)}, "
                    f"{largest.width}x{largest.height}\n" + result)

        await _finish_media(update, note, result)
    except Exception as exc:
        logger.exception("handle_photo failed")
        try:
            await note.edit_text(f"\u274C Failed to process image: {exc}"[:4000])
        except Exception:
            pass
    finally:
        cleanup_temp_file(path)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract keyframes from a video and review them."""
    if not await _media_gate(update, saas.A_VIDEO):
        return

    video = getattr(update.message, "video", None)
    if video is None:
        await send_long(update, "No video was attached to that message.")
        return

    note = await update.message.reply_text(
        "\U0001F3AC Downloading video and extracting keyframes...", parse_mode=None)

    token = _short_id(video.file_id)
    path = _media_tmp(f"vid_{token}.mp4")
    frame_dir = _media_tmp(f"frames_{token}")
    try:
        async with _typing_forever(update):
            await _download_to(update, context, video.file_id, path)
            size = os.path.getsize(path)
            focus = _caption_focus(update.message.caption)
            prompt = mm.VISION_FRAME_PROMPT + focus
            try:
                result = await asyncio.to_thread(
                    mm.analyze_video, path, 3, frame_dir)
            except Exception as exc:
                logger.warning("video analysis failed: %s", exc)
                result = f"\u274C Video analysis could not run.\n\n{exc}"
            else:
                result = f"\U0001F3AC VIDEO - {_human_size(size)}\n" + result

        await _finish_media(update, note, result)
    except Exception as exc:
        logger.exception("handle_video failed")
        try:
            await note.edit_text(f"\u274C Failed to process video: {exc}"[:4000])
        except Exception:
            pass
    finally:
        cleanup_temp_file(path)
        shutil.rmtree(frame_dir, ignore_errors=True)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract text from an uploaded document and review it defensively."""
    if not await _media_gate(update, saas.A_DOCUMENT):
        return

    doc = getattr(update.message, "document", None)
    if doc is None:
        await send_long(update, "No document was attached to that message.")
        return

    note = await update.message.reply_text(
        "\U0001F4C4 Extracting text and reviewing...", parse_mode=None)

    token = _short_id(doc.file_id)
    ext = os.path.splitext(doc.file_name or "")[1] or ".bin"
    path = _media_tmp(f"doc_{token}{ext}")

    def _review():
        """Blocking: extract text, then ask the local model to assess it."""
        text, kind = mm.extract_text(path)
        prompt = mm.build_document_prompt(text, kind, doc.file_name or "")
        prompt += _caption_focus(update.message.caption)
        analysis = ai_reply(prompt, max_tokens=900)
        head = (f"\U0001F4C4 DOCUMENT - {doc.file_name or 'attachment'} ({kind}, "
                f"{_human_size(os.path.getsize(path))}, {len(text)} chars)\n")
        return head + analysis

    try:
        async with _typing_forever(update):
            await _download_to(update, context, doc.file_id, path)
            try:
                result = await asyncio.to_thread(_review)
            except RuntimeError as exc:
                # Unsupported type / extraction problem: report it, don't crash.
                logger.info("document rejected: %s", exc)
                result = f"\u274C {exc}"
            except Exception as exc:
                logger.warning("document review failed: %s", exc)
                result = f"\u274C Document review failed.\n\n{type(exc).__name__}: {exc}"

        await _finish_media(update, note, result)
    except Exception as exc:
        logger.exception("handle_document failed")
        try:
            await note.edit_text(f"\u274C Failed to process document: {exc}"[:4000])
        except Exception:
            pass
    finally:
        cleanup_temp_file(path)


# ═══════════════════════════════════════════════════════════════════════════
# Translation (/translate /languages /detect)
#
# Translation runs on a dedicated model (RAVEN_TRANSLATE_MODEL) rather than the
# security model, because aya-expanse is multilingual but a poor reasoner and
# the reverse is also true. Blocking model calls and PDF-free text extraction
# all run through asyncio.to_thread so the event loop keeps serving.
# ═══════════════════════════════════════════════════════════════════════════
def _lang_header(code: str) -> str:
    entry = translator.LANGUAGES.get(code)
    if not entry:
        return code
    return "%s (%s)" % (entry[0], entry[1])


async def _reply_to_text(update: Update, context) -> str:
    """Text to operate on: the argument, else the replied-to message."""
    if context.args:
        return " ".join(context.args).strip()
    msg = update.message
    replied = getattr(msg, "reply_to_message", None)
    if replied is not None:
        body = (replied.text or replied.caption or "").strip()
        if body:
            return body
    return ""


async def languages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /languages - list every supported translation language."""
    # translation_status() probes Ollama; run it off the loop so an unreachable
    # Ollama cannot stall the bot for the length of the HTTP timeout.
    status = await asyncio.to_thread(translator.translation_status)
    rows = translator.list_languages()
    lines = ["\U0001F310 TRANSLATION LANGUAGES (%d)" % len(rows), ""]
    for i in range(0, len(rows), 2):
        pair = rows[i:i + 2]
        cells = ["%-5s %-14s" % (r["code"], r["name"]) for r in pair]
        lines.append("  " + "".join(cells))
    lines += [
        "",
        "Use the code or the name:",
        "  /translate ar <text>",
        "  /translate arabic <text>",
        "  /translate العربية <text>",
        "Reply to any message with /translate <lang> to translate that instead.",
        "",
        "ENGINE",
        "  model    %s%s" % (
            status["in_use"],
            "" if status["dedicated"] else "  (dedicated model not installed)"),
        "  detect   %s" % ("langdetect, offline"
                           if status["detection"] else "NOT AVAILABLE"),
        "  limit    %d chars per message" % status["max_chars"],
    ]
    if not status["dedicated"]:
        lines.append("")
        lines.append("  To add full Arabic/Thai/Korean quality:")
        lines.append("    ollama pull %s" % status["translate_model"])
        lines.append("  Until then the chat model is used, and any reply that")
        lines.append("  comes back in the wrong script is flagged to you.")
    await send_long(update, "\n".join(lines))


async def detect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /detect <text> - identify the language of a message."""
    text = await _reply_to_text(update, context)
    if not text:
        await send_long(update,
                        "\U0001F50E Send some text or reply to a message.\n"
                        "Usage: /detect your text here")
        return
    if not translator.LANGDETECT_AVAILABLE:
        await send_long(update,
                        "❌ Language detection unavailable.\n"
                        "Install with: pip install langdetect")
        return
    result = await asyncio.to_thread(translator.detect_ex, text)
    code = result["code"]

    if code and result["confident"]:
        await send_long(update,
                        "\U0001F50E Detected: %s\n   code: %s   "
                        "confidence: %.0f%%\n\n"
                        "Translate it: /translate %s (replying to this message)"
                        % (_lang_header(code), code, result["confidence"] * 100,
                           code))
        return

    # Low confidence: langdetect is guessing on a short sample. Saying so is
    # more useful than asserting "Finnish" because someone typed "hello".
    guesses = [g for g in result["candidates"]][:3]
    detail = "\n".join(
        "   %-8s %-14s %.0f%%" % (
            g["code"],
            translator.language_name(g["code"])
            if g["code"] in translator.LANGUAGES else g["code"],
            g["confidence"] * 100)
        for g in guesses)
    lines = [
        "\U0001F50E Not confident about that one.",
        "",
        "   best guesses:",
        detail,
        "",
        "Short greetings are genuinely ambiguous - \"hello\" is statistically",
        "as likely Finnish or Croatian as English. Send a longer message and",
        "I can be precise.",
    ]
    if code:
        lines.append("")
        lines.append("If that first guess is right: /translate %s" % code)
    await send_long(update, "\n".join(lines))


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /translate <lang> <text> - translate into any supported language.

    Replies are also spoken when the user has voice on and the translation came
    from a non-Latin script, because reading Arabic as text is awkward for
    many users.
    """
    user = update.effective_user
    if user is None:
        return

    args = context.args
    target_tok = args[0] if args else ""
    body = " ".join(args[1:]).strip()
    if not body:
        # Allow /translate ar on a reply, and bare /translate to reuse the
        # user's saved language.
        replied = getattr(update.message, "reply_to_message", None)
        if replied is not None:
            body = (replied.text or replied.caption or "").strip()
    if not target_tok:
        prefs = await database.aget_prefs(user.id)
        target_tok = prefs.get("translate_lang") or ""
        if not target_tok:
            await send_long(update,
                            "\U0001F310 Which language?\n"
                            "  /translate ar <text>\n"
                            "  /languages to see all 34\n"
                            "  Or reply to a message with /translate <lang>")
            return
    if not body:
        await send_long(update,
                        "\U0001F310 Nothing to translate.\n"
                        "Usage: /translate <lang> <text>\n"
                        "Or reply to a message with /translate <lang>\n"
                        "Example: /translate arabic the server is down")
        return

    target = translator.resolve(target_tok)
    if not target:
        await send_long(update,
                        "❓ I don't support '%s'.\n\n"
                        "/languages lists all %d. Arabic works too:\n"
                        "  /translate ar <text>"
                        % (target_tok, len(translator.LANGUAGES)))
        return

    if not await _media_gate(update, saas.A_TRANSLATE):
        return

    note = await update.message.reply_text(
        "\U0001F310 Translating to %s..." % translator.language_name(target))

    try:
        async with _typing_forever(update):
            result = await asyncio.to_thread(translator.translate, body, target)
    except RuntimeError as exc:
        await note.edit_text(str(exc)[:1900])
        return
    except Exception:
        logger.exception("translation failed")
        await note.edit_text("❌ Translation failed unexpectedly.")
        return

    header = "\U0001F310 %s  \U0001F449  %s\n%s" % (
        _lang_header(result["source"]),
        _lang_header(result["target"]),
        "─" * 30,
    )
    if result["warning"]:
        # The model ignored the instruction or produced text in the wrong
        # script. Say so rather than presenting English as an Arabic
        # translation.
        header += (
            "\n⚠️ WARNING: I asked for %s but the model replied with no %s "
            "characters, so this is probably NOT a real translation.\n"
            "   Fix: ollama pull %s\n%s\n"
            % (result["target_name"], result["target_name"],
               translator.TRANSLATE_MODEL, "─" * 30)
        )
    await note.edit_text(header + "\n" + result["text"][:3500])

    if target_tok:
        try:
            await database.aset_prefs(user.id, translate_lang=target)
        except Exception:
            logger.debug("could not save translate_lang", exc_info=True)

    await _maybe_speak(update, header + "\n" + result["text"],
                       force=False)


# ═══════════════════════════════════════════════════════════════════════════
# Voice replies (/voice /voices)
#
# Off by default and per user. Long security reports read badly as audio, so
# nothing speaks unless the user asked for it. Synthesis is ~1s and the ffmpeg
# transcode runs in a worker thread.
# ═══════════════════════════════════════════════════════════════════════════
VOICE_TMP_DIR = os.getenv("RAVEN_VOICE_TMP", os.path.join(MEDIA_TMP_DIR, "voice"))


class _SubArgs:
    """Adapter so /voice male can reuse the /voice_male handler body."""

    def __init__(self, args):
        if isinstance(args, str):
            args = [args]
        self.args = list(args) if args else []


async def _send_voice(update: Update, text: str, prefs: dict,
                      force: bool = False) -> bool:
    """Attach a spoken version of `text` to an existing text reply.

    Returns True when a voice note was delivered. Never raises: a voice failure
    must not lose the text reply the user already has.
    """
    if not text or not voice_engine.status()["usable"]:
        return False
    if not force and not prefs.get("voice_on"):
        return False
    try:
        result = await voice_engine.speak(
            text,
            lang=prefs.get("voice_lang") or "en-US",
            gender=prefs.get("voice_gender") or "female",
            voice=prefs.get("voice_name") or None,
            out_dir=VOICE_TMP_DIR,
        )
    except RuntimeError as exc:
        logger.info("voice note skipped: %s", exc)
        return False
    except Exception:
        logger.warning("voice note failed", exc_info=True)
        return False

    try:
        with open(result["path"], "rb") as fh:
            await update.message.reply_voice(
                voice=fh,
                duration=result["duration"] or None,
                caption=("\U0001F5E3 %s" % result["voice"])[:200],
            )
        return True
    except Exception:
        logger.warning("could not deliver voice note", exc_info=True)
        return False
    finally:
        voice_engine.cleanup(result["path"])


async def _maybe_speak(update: Update, text: str, force: bool = False) -> bool:
    """Speak `text` only when this user has voice enabled."""
    user = getattr(update, "effective_user", None)
    if user is None:
        return False
    try:
        prefs = await database.aget_prefs(user.id)
    except Exception:
        logger.debug("could not read voice prefs", exc_info=True)
        return False
    return await _send_voice(update, text, prefs, force=force)


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /voice on|off|male|female|lang <code>|status."""
    user = update.effective_user
    if user is None:
        return
    arg = (context.args[0].lower() if context.args else "").strip()

    if not voice_engine.status()["usable"]:
        needs = "\n".join("- " + n for n in voice_engine.status()["needs"])
        await send_long(update,
                        "❌ Voice output is not available yet.\n\n%s" % needs)
        return

    prefs = await database.aget_prefs(user.id)

    # Subcommands, so /voice male and /voice lang ar work without a separate
    # command name each.
    if arg in ("male", "man", "female", "woman"):
        # Wrap in a list: passing the bare string would be iterated into
        # individual characters.
        await voice_gender_command(update, _SubArgs([arg]))
        return
    if arg in ("lang", "language"):
        await voice_lang_command(update, _SubArgs(context.args[1:]))
        return

    if arg in ("status", "show"):
        await _voice_status(update, prefs)
        return
    if arg in ("off", "stop", "disable"):
        await database.aset_prefs(user.id, voice_on=False)
        await send_long(update,
                        "\U0001F507 Voice replies off.\n"
                        "Replies stay text-only.")
        return
    if arg in ("on", "enable", "start"):
        allowed, reason = await saas.evaluate(user.id, saas.A_VOICE,
                                             is_admin=is_admin(user.id))
        if not allowed:
            await send_long(update, reason)
            return
        await database.aset_prefs(user.id, voice_on=True)
        prefs = await database.aget_prefs(user.id)
        await _voice_status(update, prefs, just_enabled=True)
        return

    await _voice_status(update, prefs, help_hint=True)


async def _voice_status(update: Update, prefs: dict,
                        just_enabled: bool = False,
                        help_hint: bool = False) -> None:
    lang = prefs.get("voice_lang") or "en-US"
    gender = prefs.get("voice_gender") or "female"
    chosen = prefs.get("voice_name") or ""
    if not chosen:
        chosen = await voice_engine.pick_voice(lang, gender)
    state = "ON" if prefs.get("voice_on") else "OFF"
    lines = [
        "\U0001F3A4 VOICE REPLIES: %s" % state,
        "─" * 30,
        "  Voice    %s" % chosen,
        "  Language %s" % lang,
        "  Gender   %s" % gender,
    ]
    if just_enabled:
        ok = await _send_voice(update, lines[0], prefs, force=True)
        lines.append("")
        if ok:
            lines.append("\U0001F3A4 \U0001F3A4 Here is how I sound.")
            lines.append("Turn it off with /voice off")
        else:
            lines.append("Enabled, but the test clip did not send.")
    elif help_hint:
        lines += [
            "",
            "  /voice on            speak my replies",
            "  /voice off           text only",
            "  /voice male          deeper voice",
            "  /voice female        higher voice",
            "  /voice lang <code>   speak another language",
            "  /voices              browse available voices",
            "",
            "Long security reports are still sent as text as well.",
        ]
    await send_long(update, "\n".join(lines))


async def voice_gender_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /voice male|female - pick a voice register."""
    user = update.effective_user
    if user is None:
        return
    want = (context.args[0].lower() if context.args else "").strip()
    if want not in ("male", "female", "man", "woman"):
        await send_long(update,
                        "Usage: /voice male   or   /voice female")
        return
    gender = "male" if want in ("male", "man") else "female"
    prefs = await database.aget_prefs(user.id)
    lang = prefs.get("voice_lang") or "en-US"
    chosen = await voice_engine.pick_voice(lang, gender)
    # Clear any explicit voice so the gender choice actually takes effect.
    await database.aset_prefs(user.id, voice_gender=gender, voice_name="")
    await send_long(update,
                    "\U0001F3A4 Voice set to %s: %s\nNow speaking as %s.\n"
                    "%s" % (gender, chosen, gender,
                            "Play a test below." if prefs.get("voice_on")
                            else "Voice replies are off - use /voice on"))
    await _send_voice(update, "This is my %s voice." % gender, prefs,
                      force=True)


async def voice_lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /voice lang <code> - speak replies in another language."""
    user = update.effective_user
    if user is None:
        return
    if not context.args:
        await send_long(update,
                        "Usage: /voice lang <code>\n"
                        "Example: /voice lang ar\n"
                        "/languages lists the codes I know.")
        return
    tok = context.args[0]
    code = translator.resolve(tok)
    if not code:
        await send_long(update,
                        "❓ I don't have voices for '%s'.\n"
                        "Try: /voice lang en   (or ar, fr, de, es, ...)"
                        % tok)
        return
    # edge-tts exposes voices far beyond the translation set, so accept any
    # locale the catalogue actually has, not only LANGUAGES keys.
    base = code
    available = await voice_engine.list_voices(base, None)
    if not available:
        await send_long(update,
                        "❓ No voices found for '%s'." % tok)
        return
    prefs = await database.aget_prefs(user.id)
    gender = prefs.get("voice_gender") or "female"
    lang = available[0]["locale"]
    chosen = await voice_engine.pick_voice(lang, gender)
    await database.aset_prefs(user.id, voice_lang=lang, voice_name="")
    await send_long(update,
                    "\U0001F3A4 Spoken language set to %s\nVoice: %s"
                    % (lang, chosen))
    await _send_voice(update, "Now I will speak %s." % lang, prefs,
                      force=True)


async def voices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /voices [code] [male|female] - browse available voices."""
    user = update.effective_user
    prefs = await database.aget_prefs(user.id) if user else {}
    code = "en"
    gender = ""
    for arg in (context.args or []):
        a = arg.lower()
        if a in ("male", "man", "female", "woman"):
            gender = "male" if a in ("male", "man") else "female"
        elif translator.resolve(a) or len(a) >= 2:
            code = a
    voices = await voice_engine.list_voices(code, gender or None)
    if not voices:
        await send_long(update,
                        "❓ No voices for '%s'%s.\nTry /languages for a code."
                        % (code, (" " + gender) if gender else ""))
        return
    total = len(await voice_engine.list_voices())
    lines = ["\U0001F3A4 VOICES: %s%s  (%d of %d total)" % (
        code.upper(), (" " + gender) if gender else "", len(voices), total), ""]
    for v in voices[:30]:
        lines.append("  %-38s %s" % (v["name"], v["gender"][:1].upper()))
    if len(voices) > 30:
        lines.append("  ... and %d more" % (len(voices) - 30))
    lines += [
        "",
        "Use one with:  /voice_use " + voices[0]["name"],
        "Gender presets: /voice male   /voice female",
    ]
    await send_long(update, "\n".join(lines))


async def voice_use_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /voice_use <ShortName> - pin an exact voice."""
    user = update.effective_user
    if user is None:
        return
    if not context.args:
        await send_long(update, "Usage: /voice_use en-US-GuyNeural\n"
                                "/voices lists them.")
        return
    name = context.args[0].strip()
    if not await voice_engine.validate_voice(name):
        await send_long(update,
                        "❓ No voice called '%s'.\n/voices lists the real names."
                        % name)
        return
    locale = ""
    for v in await voice_engine.list_voices():
        if v["name"] == name:
            locale = v["locale"]
            break
    await database.aset_prefs(user.id, voice_name=name, voice_lang=locale)
    await send_long(update, "\U0001F3A4 Voice pinned: %s\nLanguage: %s"
                    % (name, locale))
    prefs = await database.aget_prefs(user.id)
    await _send_voice(update, "This is my voice now.", prefs, force=True)


async def my_usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /my_usage - tier, today's remaining quota and features."""
    user = update.effective_user
    if user is None:
        return
    summary = await saas.usage_summary(user.id, is_admin=is_admin(user.id))
    history = await database.ahistory(user.id, limit=3)
    text = saas.render_usage(summary)
    if history:
        text += "\n\nRECENT PLAN EVENTS\n" + "\n".join(
            f"  - {h['plan']} via {h['provider']}"
            + (f" ({h['created_at'][:10]})" if h.get("created_at") else "")
            for h in history
        )
    await send_long(update, text)


async def upgrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /upgrade - show the plans and attach a real checkout button per plan.

    One URL cannot serve four products: the buyer has to pick a plan, and the
    webhook reads `price_id` back out of the payload to decide which tier to
    grant. So each plan id gets its own button.

    The buyer's Telegram id travels as `metadata[telegram_id]`, because the
    webhook has no other way to know who to credit. It is urlencoded through
    `urlencode` rather than concatenated, so a gateway that is strict about the
    brackets in `metadata[telegram_id]` still receives a valid query string.

    The links are an inline keyboard, not anchors in the body: legacy Telegram
    Markdown has no link syntax, so `[text](url)` fails to parse and `send_long`
    quietly downgrades to plain text, printing the raw markup at the user. An
    inline button cannot degrade that way.

    A plan with no URL configured still appears in the table, marked
    unconfigured. Hiding it would make a partially configured deployment look
    complete, and the operator would have no way to tell which one is missing.
    """
    user_id = str(update.effective_user.id)

    # Keyed on the database constants and on the literal plan ids, not derived
    # by string surgery. `tier.replace("tier_", "")` yields the wrong key
    # silently if a constant is ever renamed, which drops a paid plan's button
    # with no error anywhere.
    TIER_PLANS = {
        database.TIER_PRO: (
            ("pro_monthly", "Monthly",
             os.getenv("PAYMENT_CHECKOUT_URL_PRO_MONTHLY", "").strip()),
            ("pro_annual", "Annual",
             os.getenv("PAYMENT_CHECKOUT_URL_PRO_ANNUAL", "").strip()),
        ),
        database.TIER_ENTERPRISE: (
            ("enterprise_monthly", "Monthly",
             os.getenv("PAYMENT_CHECKOUT_URL_ENT_MONTHLY", "").strip()),
            ("enterprise_annual", "Annual",
             os.getenv("PAYMENT_CHECKOUT_URL_ENT_ANNUAL", "").strip()),
        ),
    }

    lines = [
        "\U0001F6E8 RAVEN PLANS",
        "-" * 34,
    ]
    buttons: list = []

    for tier in (database.TIER_FREE, database.TIER_PRO, database.TIER_ENTERPRISE):
        cfg = saas.tier_config(tier)
        price = cfg["price_monthly"]
        if price != "$0":
            price += f"/mo or {cfg['price_annual']}"
        lines.append(f"  {cfg['label']:<11} {cfg['blurb']}")
        lines.append(f"              {price}")

        plans = TIER_PLANS.get(tier, ())
        for plan_id, period, url in plans:
            # Whitespace-only counts as unset: a var left as spaces would
            # otherwise render a button that resolves to a broken checkout.
            if not url.strip():
                lines.append(f"              {period:<7} \u26A0 not configured")
                continue
            params = urllib.parse.urlencode({"metadata[telegram_id]": user_id})
            sep = "&" if "?" in url else "?"
            buttons.append([InlineKeyboardButton(
                f"\U0001F9FE {cfg['label']} {period}",
                url=f"{url}{sep}{params}")])
        # Only invite a tap when this tier actually produced a button, so a
        # partly configured deployment does not promise a payment path that
        # isn't there.
        if any(u.strip() for _, _, u in plans):
            lines.append(f"              \u2192 tap below to pay")

    if buttons:
        await send_long(update, "\n".join(lines))
        await update.message.reply_text(
            "\U0001F517 Checkout \u00B7 your Telegram id is attached automatically",
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        lines += ["", "Checkout is not configured on this deployment.",
                  "The operator must set the PAYMENT_CHECKOUT_URL_* vars in .env."]
        await send_long(update, "\n".join(lines))

# MODULE C: AUTOMATED DAILY INTELLIGENCE BRIEFINGS
async def send_daily_briefing(bot):
    """Send daily security intelligence briefing to admin chat."""
    try:
        # Get admin chat ID from environment
        admin_chat_id = os.getenv("ADMIN_CHAT_ID")
        if not admin_chat_id:
            logger.warning("ADMIN_CHAT_ID not set, skipping daily briefing")
            return
        
        # Prepare briefing components
        briefing_parts = []
        
        # 1. Crypto Snapshot
        if CCXT_AVAILABLE:
            try:
                # Initialize exchanges
                binance = ccxt.binance({'enableRateLimit': True})
                okx = ccxt.okx({'enableRateLimit': True})
                
                # Get BTC and ETH prices
                btc_binance = await asyncio.to_thread(binance.fetch_ticker, 'BTC/USDT')
                btc_okx = await asyncio.to_thread(okx.fetch_ticker, 'BTC/USDT')
                eth_binance = await asyncio.to_thread(binance.fetch_ticker, 'ETH/USDT')
                eth_okx = await asyncio.to_thread(okx.fetch_ticker, 'ETH/USDT')
                
                crypto_snapshot = f"""💰 **CRYPTO SNAPSHOT**
• BTC: ${btc_binance['last']:,.2f} (Binance) | ${btc_okx['last']:,.2f} (OKX) 
• ETH: ${eth_binance['last']:,.2f} (Binance) | ${eth_okx['last']:,.2f} (OKX)
• 24h Change: BTC {btc_binance['percentage']:+.2f}% | ETH {eth_binance['percentage']:+.2f}%
                """
                briefing_parts.append(crypto_snapshot)
            except Exception as e:
                logger.warning(f"Failed to fetch crypto data: {e}")
                briefing_parts.append("💰 **CRYPTO SNAPSHOT**\\n• Data temporarily unavailable")
        else:
            briefing_parts.append("💰 **CRYPTO SNAPSHOT**\\n• CCXT not available")
        
        # 2. Threat Intelligence
        keywords = os.getenv("DAILY_BRIEFING_KEYWORDS", "healthcare,finance,government")
        try:
            threat_prompt = f"""Generate a concise dark web threat intelligence summary for: {keywords}
            
            Provide:
            1. Key threat trends observed in the last 24 hours
            2. Notable threat actor activities
            3. Emerging vulnerabilities being discussed
            4. Recommended defensive focus areas
            
            Format as brief, actionable intelligence bullets."""
            
            threat_intel = await aget_reply(current_model, [{"role": "user", "content": threat_prompt}])
            threat_section = f"""🕵️‍♂️ **THREAT INTEL**
{threat_intel}
                """
            briefing_parts.append(threat_section)
        except Exception as e:
            logger.warning(f"Failed to generate threat intel: {e}")
            briefing_parts.append("🕵️‍♂️ **THREAT INTEL**\\n• Service temporarily unavailable")
        
        # 3. Actionable Defense
        try:
            defense_prompt = f"""Based on current threat landscape, provide 3-5 specific, actionable defensive recommendations that organizations should prioritize today.
            
            Focus on:
            - Immediate actions (can be done in <1 hour)
            - High-impact, low-effort measures
            - Based on current threat intelligence
            
            Format as numbered list."""
            
            defense_tips = await aget_reply(current_model, [{"role": "user", "content": defense_prompt}])
            defense_section = f"""🛡️ **ACTIONABLE DEFENSE**
{defense_tips}
                """
            briefing_parts.append(defense_section)
        except Exception as e:
            logger.warning(f"Failed to generate defense tips: {e}")
            briefing_parts.append("🛡️ **ACTIONABLE DEFENSE**\\n• Service temporarily unavailable")
        
        # Combine briefing
        if briefing_parts:
            briefing = f"""📅 **RAVEN DAILY BRIEFING** - {now_str()}

{chr(10).join(briefing_parts)}

---
*Automated RAVEN Security Intelligence*
*Next briefing: {os.getenv("DAILY_BRIEFING_TIME", "08:00")} daily*"""
            
            # Send to admin chat
            await bot.send_message(
                chat_id=admin_chat_id,
                text=briefing,
                parse_mode='Markdown'
            )
            
            logger.info(f"Daily briefing sent to admin chat {admin_chat_id}")
        else:
            logger.warning("No briefing components generated")
            
    except Exception as e:
        logger.error(f"Error sending daily briefing: {e}")

def setup_scheduler(application):
    """Set up the daily briefing scheduler.

    NOTE: This must be called from inside a running asyncio event loop
    (i.e. via the application's post_init callback), otherwise
    AsyncIOScheduler has no loop to attach to.
    """
    global scheduler
    if not APSCHEDULER_AVAILABLE:
        logger.warning("APScheduler not available, daily briefings disabled")
        return None
    
    try:
        scheduler = AsyncIOScheduler()
        
        # Parse time from environment (format: HH:MM)
        time_str = os.getenv("DAILY_BRIEFING_TIME", "08:00")
        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError:
            hour, minute = 8, 0  # Default to 08:00
            logger.warning(f"Invalid time format '{time_str}', using 08:00")
        
        # Add daily job - wrapper to provide bot instance
        async def daily_briefing_wrapper():
            await send_daily_briefing(application.bot)
        
        scheduler.add_job(
            daily_briefing_wrapper,
            'cron',
            hour=hour,
            minute=minute,
            id='daily_briefing',
            replace_existing=True
        )
        
        scheduler.start()
        logger.info(f"Daily briefing scheduler started - will run daily at {hour:02d}:{minute:02d}")
        return scheduler
    except Exception as e:
        logger.error(f"Failed to setup scheduler: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
#  TRADING  ·  SOCIAL  ·  CONTENT  ·  ANALYTICS  COMMAND LAYER
#  Wired to: trading_engine.py, multi_provider_ai.py,
#            social_media_manager.py, content_creator.py, analytics.py
# ══════════════════════════════════════════════════════════════════════

import analytics as raven_analytics
import content_creator
import social_media_manager
from multi_provider_ai import ROUTER, RAVEN_SYSTEM_PROMPT, OLLAMA_TIMEOUT
from trading_engine import TradingEngine

# Shared engine instance (paper mode by default - see .env TRADING_MODE)
TRADING = TradingEngine()

# Emergency kill switch - flipped by /emergency_stop, blocks all trades
KILL_SWITCH = False

# Bot start time for uptime reporting
START_TIME = time.time()

# Simple per-user rate limiting: {user_id: [timestamps]}
RATE_LIMITS = {}
RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", "10"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))


def is_admin(user_id: int) -> bool:
    """Return True if the user is the configured admin.

    If ADMIN_USER_ID is unset the bot is open (single-user local setup);
    a warning is logged once so the owner knows the gate is disabled.

    The configured value is normalised first. It is routinely pasted out of a
    chat bubble as "(12345)" or "[12345]", and the stray bracket used to make
    this compare against "12345)" - which silently refused /buy, /sell,
    /trade_mode, /emergency_stop and /resume for the owner, with no obvious
    cause. A malformed value now logs loudly and fails closed rather than
    quietly denying everything.
    """
    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    if not admin_id:
        logger.warning("ADMIN_USER_ID not set - privileged commands are open to all users")
        return True
    cleaned = admin_id.strip("()[]{}<> \t\"'")
    if not cleaned.isdigit():
        logger.error(
            "ADMIN_USER_ID=%r is not a numeric Telegram id - privileged commands "
            "(/buy /sell /trade_mode /emergency_stop /resume) will be refused for "
            "everyone, including you. Fix the value in .env.",
            admin_id,
        )
        return False
    return str(user_id) == cleaned


def rate_limited(user_id: int) -> bool:
    """Return True when the user exceeded RATE_LIMIT_COUNT in the window."""
    now = time.time()
    hits = [t for t in RATE_LIMITS.get(user_id, []) if now - t < RATE_LIMIT_WINDOW]
    hits.append(now)
    RATE_LIMITS[user_id] = hits
    return len(hits) > RATE_LIMIT_COUNT


def ai_reply(prompt: str, max_tokens: int = 400) -> str:
    """Run `prompt` through the multi-provider router and return the text.

    Uses RAVEN's system prompt so replies match the bot's persona. Raises
    RuntimeError if every configured provider fails.
    """
    messages = [
        {"role": "system", "content": RAVEN_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    text, _provider = ROUTER.chat(messages, max_tokens=max_tokens)
    return text


def portfolio_text(snapshot: dict) -> str:
    """Render a portfolio snapshot as a Telegram-friendly block."""
    mode = snapshot.get("mode", "paper").upper()
    lines = [
        f"PORTFOLIO ({mode} MODE)",
        "-" * 34,
        f"Cash        ${snapshot['cash']:>12,.2f}",
        f"Positions   ${snapshot['positions_value']:>12,.2f}",
        f"Equity      ${snapshot['equity']:>12,.2f}",
        f"Unrealized  ${snapshot['unrealized_pnl']:>+12,.2f}",
    ]
    rows = snapshot.get("positions") or []
    if rows:
        lines.append("-" * 34)
        for row in rows:
            lines.append(
                f"{row['symbol']:<12} {row['qty']:.6f} @ {row['avg_price']:,.2f}"
                f"  now {row['price']:,.2f}  {row['pnl_pct']:+.2f}%"
            )
    else:
        lines.append("-" * 34)
        lines.append("No open positions.")
    return "```\n" + "\n".join(lines) + "\n```"


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /balance - show wallet balances and open positions."""
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return
    try:
        snapshot = TRADING.portfolio_snapshot()
    except Exception as exc:
        logger.error("portfolio_snapshot failed: %s", exc)
        await send_long(update, f"❌ Could not fetch prices: {exc}")
        return
    await send_long(update, portfolio_text(snapshot))


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /positions - list open positions only."""
    try:
        snapshot = TRADING.portfolio_snapshot()
    except Exception as exc:
        await send_long(update, f"❌ Could not fetch prices: {exc}")
        return
    rows = snapshot.get("positions") or []
    if not rows:
        await send_long(update, "📭 No open positions.")
        return
    lines = ["OPEN POSITIONS", "-" * 34]
    for row in rows:
        lines.append(
            f"{row['symbol']:<12} qty {row['qty']:.6f}\n"
            f"  avg {row['avg_price']:,.2f} | now {row['price']:,.2f} | "
            f"value ${row['value']:,.2f} | P&L {row['pnl_pct']:+.2f}%"
        )
    await send_long(update, "```\n" + "\n".join(lines) + "\n```")


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str):
    """Shared implementation for /buy and /sell.

    Amounts are notional in USDT: /buy BTC 100 buys $100 of BTC.
    """
    if not is_admin(update.effective_user.id):
        await send_long(update, "⛔ Admin only.")
        return
    if KILL_SWITCH:
        await send_long(update, "🛑 Emergency stop is active. Use /resume to re-enable trading.")
        return
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return

    args = context.args
    if len(args) < 2:
        await send_long(
            update,
            f"❌ Usage: /{side} <symbol> <amount_usdt>\n"
            f"Example: /{side} BTC 100",
        )
        return

    symbol = TRADING.normalize_symbol(args[0])
    try:
        amount = float(args[1])
    except ValueError:
        await send_long(update, "❌ Amount must be a number, e.g. /buy BTC 100")
        return

    ok, reason = TRADING.check_risk(side, symbol, amount)
    if not ok:
        await send_long(update, f"🚫 Blocked: {reason}")
        return

    await send_long(update, f"⏳ Executing {side.upper()} {symbol} ${amount:,.2f}...")
    try:
        receipt = TRADING.execute(side, symbol, amount, update.effective_user.id)
    except Exception as exc:
        logger.exception("trade execution failed")
        await send_long(update, f"❌ Order failed: {exc}")
        return

    tag = "LIVE" if receipt["mode"] == "live" else "PAPER"
    await send_long(
        update,
        f"✅ [{tag}] {side.upper()} {receipt['symbol']}\n"
        f"   ${receipt['amount_usdt']:,.2f} @ {receipt['price']:,.2f}\n"
        f"   qty {receipt['qty']:.6f} · {receipt['ts']}",
    )


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /buy <symbol> <amount_usdt>."""
    await trade_command(update, context, "buy")


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /sell <symbol> <amount_usdt>."""
    await trade_command(update, context, "sell")


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /orders - recent trade history and open orders."""
    days = 7
    if context.args:
        try:
            days = max(1, min(90, int(context.args[0])))
        except ValueError:
            pass
    trades = TRADING.trades_report(days)
    if not trades:
        await send_long(update, f"📭 No trades in the last {days} day(s).")
        return
    lines = [f"TRADE HISTORY (last {days}d)", "-" * 40]
    for trade in trades[-20:]:
        lines.append(
            f"{trade['date']} {trade['side'].upper():<4} "
            f"{trade['symbol']:<12} ${trade['amount_usdt']:>9,.2f} @ {trade['price']:>10,.2f}"
            + (f"  P&L {trade['pnl']:+,.2f}" if trade["side"] == "sell" else "")
        )
    if len(trades) > 20:
        lines.append(f"... {len(trades) - 20} older trades omitted")
    await send_long(update, "```\n" + "\n".join(lines) + "\n```")


async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /signal <symbol> - AI-generated trade signal."""
    if not context.args:
        await send_long(update, "❌ Usage: /signal <symbol>\nExample: /signal BTC")
        return
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return
    symbol = TRADING.normalize_symbol(context.args[0])
    try:
        price = await asyncio.to_thread(TRADING.get_price, symbol)
    except Exception as exc:
        await send_long(update, f"❌ Could not fetch price for {symbol}: {exc}")
        return

    await send_long(update, f"🧠 Generating signal for {symbol} @ {price:,.2f}...")
    prompt = (
        f"Give a trading signal for {symbol} currently at {price}.\n"
        "Reply in exactly this format:\n"
        "BIAS: BUY | SELL | HOLD\n"
        "CONFIDENCE: <0-100>%\n"
        "ENTRY: <price zone>\n"
        "STOP-LOSS: <price>\n"
        "TAKE-PROFIT: <price>\n"
        "RATIONALE: <one sentence>"
    )
    try:
        text = await asyncio.to_thread(ai_reply, prompt, max_tokens=300)
    except Exception as exc:
        logger.exception("signal generation failed")
        await send_long(update, f"❌ AI unavailable: {exc}")
        return
    await send_long(update, f"📊 SIGNAL · {symbol} @ {price:,.2f}\n\n{text}")


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /report <daily|weekly|monthly> - P&L report."""
    period = (context.args[0].lower() if context.args else "daily")
    if period not in ("daily", "weekly", "monthly"):
        await send_long(update, "❌ Usage: /report <daily|weekly|monthly>")
        return
    await send_long(update, raven_analytics.trading_report(period))


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dashboard - unified status board, plus SaaS metrics for admins."""
    body = raven_analytics.system_stats(
        time.time() - START_TIME, router=ROUTER, engine=TRADING
    )
    body += await _saas_dashboard_section(update)
    body += _vision_capability_section()
    body += _language_capability_section()
    await send_long(update, body)


async def _saas_dashboard_section(update: Update) -> str:
    """SaaS metrics. Admin-only, and omitted entirely when SaaS is off."""
    if not SAAS_AVAILABLE:
        return "\n\nSaaS layer: unavailable (see startup log)"
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return ""

    stats = await database.astats()
    by_tier = stats.get("by_tier", {})
    lines = [
        "",
        "SAAS METRICS",
        "-" * 34,
        f"  Storage        {stats.get('storage', '?')}",
        f"  Total users    {stats.get('total_users', 0)}",
        f"  Free           {by_tier.get(database.TIER_FREE, 0)}",
        f"  Pro            {by_tier.get(database.TIER_PRO, 0)}",
        f"  Enterprise     {by_tier.get(database.TIER_ENTERPRISE, 0)}",
        f"  Payments recd  {stats.get('subscriptions', 0)}",
    ]

    # Revenue is only reported as a plan tally. This repository has no payment
    # processor wired up and no price table, so any currency figure would be
    # invented. Count subscriptions per plan instead of pretending to do maths.
    counts = await database.aplan_counts(50)
    if counts:
        lines.append("  Plans sold     " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        lines.append("  Plans sold     none recorded yet")

    lines.append("")
    lines.append("  Revenue: not computable here - no payment processor")
    lines.append("  or price table is configured. Plans sold is the")
    lines.append("  only figure this deployment can state truthfully.")
    return "\n".join(lines)


def _vision_capability_section() -> str:
    """What multimodal backends are actually usable right now."""
    if not SAAS_AVAILABLE or mm is None:
        return ""
    caps = mm.capabilities()
    vis = caps["vision"]
    lines = ["", "MULTIMODAL", "-" * 34]
    lines.append(f"  Vision         {'ready' if vis['usable'] else 'NOT CONFIGURED'}")
    if vis["local"]:
        lines.append(f"    local        {vis['local_detail']}")
    else:
        lines.append(f"    local        {vis['local_detail']}")
    lines.append(f"    openai       {'key set' if vis['openai'] else 'no key'}")
    lines.append(f"    groq         {'key set' if vis['groq'] else 'no key'}")
    doc = caps["documents"]
    lines.append(f"  Documents      pdf={doc['pdf']} docx={doc['docx']} "
                 f"txt/csv/json=yes")
    vid = caps["video"]
    lines.append(f"  Video          ffmpeg={vid['ffmpeg']} opencv={vid['opencv']}")
    return "\n".join(lines)


def _language_capability_section() -> str:
    """What translation and voice can actually do right now.

    Relies on the model cache warmed at boot by _log_startup_warnings(); this
    runs synchronously inside dashboard_command and must not do network I/O.
    """
    if not SAAS_AVAILABLE or translator is None or voice_engine is None:
        return ""
    t = translator.translation_status()
    v = voice_engine.status()
    lines = ["", "LANGUAGE & VOICE", "-" * 34]
    lines.append(f"  Translate      {t['languages']} languages")
    if t["dedicated"]:
        lines.append(f"    model        {t['in_use']} (dedicated)")
    else:
        lines.append(f"    model        {t['in_use']}  <- chat model, NOT dedicated")
        lines.append(f"    fix          ollama pull {t['translate_model']}")
    lines.append(f"    detection    {'langdetect' if t['detection'] else 'MISSING'}")
    lines.append(f"    cold start   ~600s on this CPU, ~20-40s once resident")
    if v["usable"]:
        lines.append("  Voice          ready (edge-tts -> ogg/opus)")
    else:
        lines.append("  Voice          NOT AVAILABLE")
        for need in v["needs"]:
            lines.append(f"    needs        {need}")
    return "\n".join(lines)


async def ai_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ai_status - show which AI providers are ready."""
    status = ROUTER.providers_status()
    lines = ["AI PROVIDER STATUS", "-" * 30]
    for name, ready in status.items():
        lines.append(f"{'READY ' if ready else 'OFF   '} {name}")
    lines.append("-" * 30)
    lines.append(f"Priority: {' > '.join(ROUTER.priority)}")
    lines.append(f"Model:    {ROUTER.model}")
    await send_long(update, "```\n" + "\n".join(lines) + "\n```")


async def emergency_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /emergency_stop - disable all trading."""
    global KILL_SWITCH
    if not is_admin(update.effective_user.id):
        await send_long(update, "⛔ Admin only.")
        return
    KILL_SWITCH = True
    logger.warning("EMERGENCY STOP activated by %s", update.effective_user.id)
    await send_long(update, "🛑 EMERGENCY STOP\nAll trading is disabled.\nUse /resume to re-enable.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resume - re-enable trading after an emergency stop."""
    global KILL_SWITCH
    if not is_admin(update.effective_user.id):
        await send_long(update, "⛔ Admin only.")
        return
    KILL_SWITCH = False
    logger.warning("Trading resumed by %s", update.effective_user.id)
    await send_long(update, "▶️ Trading re-enabled.")


async def trade_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /trade_mode [paper|live] - show or change the trading mode."""
    global TRADING
    if not is_admin(update.effective_user.id):
        await send_long(update, "⛔ Admin only.")
        return
    if not context.args:
        await send_long(
            update,
            f"Current mode: {TRADING.mode.upper()}\n"
            "⚠️  /trade_mode live places REAL orders and needs exchange keys\n"
            "    with TRADE-only permission (no withdrawal).",
        )
        return
    mode = context.args[0].lower()
    if mode not in ("paper", "live"):
        await send_long(update, "❌ Usage: /trade_mode <paper|live>")
        return
    if mode == "live":
        if not is_admin(update.effective_user.id):
            await send_long(update, "⛔ Admin only.")
            return
        TRADING.mode = "live"
        logger.warning("TRADING MODE SET TO LIVE by %s", update.effective_user.id)
        await send_long(update, "🔴 LIVE MODE ENABLED - real orders will be placed.")
    else:
        TRADING.mode = "paper"
        logger.warning("TRADING MODE SET TO PAPER by %s", update.effective_user.id)
        await send_long(update, "🟢 PAPER MODE - simulated fills only.")


async def tweet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tweet <text> - post to Twitter/X."""
    if not context.args:
        await send_long(update, "❌ Usage: /tweet <text>")
        return
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return
    ok, message = social_media_manager.post_tweet(" ".join(context.args))
    await send_long(update, message)


async def thread_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /thread <text> - draft a Twitter thread with the AI."""
    if not context.args:
        await send_long(update, "❌ Usage: /thread <topic or first post>")
        return
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return
    await send_long(update, "🧵 Drafting thread...")
    prompt = (
        f"Write a Twitter/X thread about: {' '.join(context.args)}\n"
        "Number each post as '1/6', '2/6' etc. Max 270 characters per post. "
        "End with a call to action. No hashtags in the middle posts."
    )
    try:
        text = await asyncio.to_thread(ai_reply, prompt, max_tokens=700)
    except Exception as exc:
        logger.exception("thread generation failed")
        await send_long(update, f"❌ AI unavailable: {exc}")
        return
    await send_long(update, f"🧵 THREAD DRAFT\n\n{text}")


async def social_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /social_status - which platforms have credentials configured."""
    status = social_media_manager.status()
    lines = ["SOCIAL PLATFORM STATUS", "-" * 30]
    for name, ready in status.items():
        lines.append(f"{'READY ' if ready else 'SETUP '} {name}")
    lines.append("-" * 30)
    lines.append("Only Twitter/X can post directly from the bot.")
    lines.append("TikTok/YouTube/Instagram need developer apps:")
    lines.append("/social_guide tiktok|youtube|instagram")
    await send_long(update, "```\n" + "\n".join(lines) + "\n```")


async def social_guide_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /social_guide <platform> - show the setup steps for a platform."""
    platform = context.args[0] if context.args else "tiktok"
    await send_long(update, social_media_manager.platform_guide(platform))


async def create_content_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /create_content <platform> <topic> - full content package."""
    if len(context.args) < 2:
        await send_long(
            update,
            "❌ Usage: /create_content <platform> <topic>\n"
            "Platforms: tiktok, twitter, youtube, instagram",
        )
        return
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return
    platform = context.args[0].lower()
    topic = " ".join(context.args[1:])
    await send_long(update, f"🎬 Generating {platform} content package for: {topic}")
    try:
        package = await asyncio.to_thread(content_creator.content_package, platform, topic)
    except Exception as exc:
        logger.exception("content generation failed")
        await send_long(update, f"❌ AI unavailable: {exc}")
        return
    await send_long(update, f"CONTENT PACKAGE · {platform.upper()}\n\n{package}")


async def content_calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /content_calendar <platform> [days] - AI content calendar."""
    if not context.args:
        await send_long(update, "❌ Usage: /content_calendar <platform> [days]")
        return
    platform = context.args[0].lower()
    days = 7
    if len(context.args) > 1:
        try:
            days = max(1, min(30, int(context.args[1])))
        except ValueError:
            pass
    if rate_limited(update.effective_user.id):
        await send_long(update, "⏱️ Rate limit reached. Try again in a minute.")
        return
    await send_long(update, f"📅 Building a {days}-day {platform} calendar...")
    try:
        text = await asyncio.to_thread(content_creator.content_calendar, platform, days)
    except Exception as exc:
        logger.exception("content calendar failed")
        await send_long(update, f"❌ AI unavailable: {exc}")
        return
    await send_long(update, f"CONTENT CALENDAR · {platform.upper()}\n\n{text}")


async def post_init(application):
    """Runs inside the event loop once the bot is initialized - safe place to start the scheduler."""
    setup_scheduler(application)
    _log_startup_warnings()


def _log_startup_warnings():
    """Say plainly, once at boot, what is and is not actually working.

    The alternative is a bot that looks fully functional and silently is not -
    vision analysis with no vision model, an admin gate that refuses its owner,
    tiers held in memory that vanish on restart.
    """
    if not os.getenv("ADMIN_USER_ID", "").strip():
        logger.warning(
            "ADMIN_USER_ID is not set: /buy /sell /trade_mode /emergency_stop /resume "
            "are OPEN TO EVERYONE. Set it before sharing this bot with anyone."
        )
    else:
        cleaned = os.getenv("ADMIN_USER_ID", "").strip("()[]{}<> \t\"'")
        if not cleaned.isdigit():
            logger.error(
                "ADMIN_USER_ID=%r is malformed: privileged commands are refused for "
                "everyone, including you. Fix it in .env.",
                os.getenv("ADMIN_USER_ID"),
            )
        else:
            logger.info("Admin gate active for user %s", cleaned)

    if SAAS_AVAILABLE and mm is not None:
        vis = mm.vision_status()
        if not vis["usable"]:
            logger.warning(
                "No vision backend configured - images and video will return a setup "
                "message. Fix: ollama pull %s  (or add a cloud key).",
                mm.VISION_MODEL,
            )
        else:
            logger.info("Vision backend ready: %s", vis["local_detail"])

        if translator is not None:
            # Warm the model cache at boot so the first /translate or /languages
            # does not pay a probe on the event loop.
            t = translator.translation_status()
            if t["dedicated"]:
                logger.info("Translation model ready: %s (%d languages)",
                            t["in_use"], t["languages"])
            else:
                logger.warning(
                    "No dedicated translation model - /translate will use %s, which "
                    "cannot write Arabic and will return English instead. "
                    "Fix: ollama pull %s",
                    t["in_use"], t["translate_model"],
                )
            if not t["detection"]:
                logger.warning(
                    "langdetect is not installed - /detect will not work. "
                    "Fix: pip install langdetect")

        if voice_engine is not None:
            v = voice_engine.status()
            if v["usable"]:
                logger.info("Voice replies ready (edge-tts -> ogg/opus)")
            else:
                logger.warning(
                    "Voice replies unavailable - %s. /voice will explain.",
                    "; ".join(v["needs"]))

        if not database.db_available():
            logger.warning(
                "SaaS database unavailable (SQLAlchemy missing?) - tiers and usage "
                "reset on every restart."
            )
    elif not SAAS_AVAILABLE:
        logger.warning(
            "SaaS/multimodal modules did not import - /my_usage, /upgrade and media "
            "handlers are disabled. Check the startup log."
        )

    ka = os.getenv("RAVEN_KEEP_ALIVE", "30m").strip()
    if ka:
        logger.info("Ollama keep-alive: %s (model stays resident between messages)", ka)

async def post_shutdown(application):
    """Stop the scheduler cleanly when the bot shuts down."""
    global scheduler
    if scheduler and scheduler.running:
        try:
            scheduler.shutdown()
            logger.info("Daily briefing scheduler shut down")
        except Exception as e:
            logger.warning(f"Scheduler shutdown issue: {e}")

def main():
    """Start the bot."""
    # Get token from environment variable
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set!")
        return

    # Create the Application (with post_init/post_shutdown to manage the scheduler
    # inside the running event loop)
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("system", system_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("tokens", tokens_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("load", load_command))

    # Security assessment handlers
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("cve", cve_command))
    application.add_handler(CommandHandler("exploit", exploit_command))
    application.add_handler(CommandHandler("harden", harden_command))
    application.add_handler(CommandHandler("analyze", analyze_command))

    # Intelligence modules
    application.add_handler(CommandHandler("crypto", crypto_command))
    application.add_handler(CommandHandler("market", market_command))
    application.add_handler(CommandHandler("leak", leak_command))
    application.add_handler(CommandHandler("darkweb_intel", darkweb_intel_command))

    # AI assistance handlers
    application.add_handler(CommandHandler("generate", generate_command))
    application.add_handler(CommandHandler("scrape", scrape_command))

    # Trading handlers (paper mode by default, admin-gated)
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("sell", sell_command))
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("signal", signal_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("trade_mode", trade_mode_command))
    application.add_handler(CommandHandler("emergency_stop", emergency_stop_command))
    application.add_handler(CommandHandler("resume", resume_command))

    # Analytics + AI provider status
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("ai_status", ai_status_command))

    # Social media + content creation
    application.add_handler(CommandHandler("tweet", tweet_command))
    application.add_handler(CommandHandler("thread", thread_command))
    application.add_handler(CommandHandler("social_status", social_status_command))
    application.add_handler(CommandHandler("social_guide", social_guide_command))
    application.add_handler(CommandHandler("create_content", create_content_command))
    application.add_handler(CommandHandler("content_calendar", content_calendar_command))

    # SaaS tiers, quota visibility and plan information
    if SAAS_AVAILABLE:
        application.add_handler(CommandHandler("my_usage", my_usage_command))
        application.add_handler(CommandHandler("upgrade", upgrade_command))

        # Translation. Translation itself runs on the local model, so it is
        # available on every tier; voice is Pro+ because it calls an external
        # speech service.
        application.add_handler(CommandHandler("translate", translate_command))
        application.add_handler(CommandHandler("languages", languages_command))
        application.add_handler(CommandHandler("detect", detect_command))
        application.add_handler(CommandHandler("voice", voice_command))
        application.add_handler(CommandHandler("voice_lang", voice_lang_command))
        application.add_handler(CommandHandler("voice_use", voice_use_command))
        application.add_handler(CommandHandler("voices", voices_command))

    # Voice message handler
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Multimodal analysis handlers. Registered as their own group so a media
    # message can never fall through to the plain-text chat handler.
    if SAAS_AVAILABLE:
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_handler(MessageHandler(filters.VIDEO, handle_video))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Message handler for natural conversation
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # NOTE: The daily-briefing scheduler is now started in post_init()
    # (inside the running event loop), so it is not started here anymore.

    # Start the bot
    logger.info("Starting RAVEN Security Bot with Phase 3 features...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()