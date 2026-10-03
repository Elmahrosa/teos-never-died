import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
import logging
import io
import sys
import json
import time
import hashlib
from contextlib import redirect_stdout
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from raven import get_reply, now_str, auto_save, auto_load

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

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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

**Phase 3 Features:**
🎤 Voice Commands - Send voice notes for hands-free operation
📅 Daily Briefings - Automated security intelligence reports

Simply send a message to chat with RAVEN naturally.
Send a voice note for voice command processing.
    """
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

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

**Usage Notes:**
- All security commands trigger RAVEN's specialized 4-phase assessment format
- Natural conversation is also supported - just send a message
- Session history is automatically saved and loaded
- For best results, be specific with your targets and questions
- Crypto commands require ccxt library: pip install ccxt
- Voice commands require SpeechRecognition and pydub: pip install SpeechRecognition pydub
- Daily briefings require APScheduler: pip install APScheduler
- Real breach detection requires requests library and HIBP API key
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

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
    await update.message.reply_text("🗑️ Conversation history cleared.")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command."""
    global conversation_history
    if not conversation_history:
        await update.message.reply_text("📝 No conversation history.")
        return

    # Format history for display
    history_text = "📜 **Conversation History:**\n\n"
    for i, msg in enumerate(conversation_history[-10:], 1):  # Show last 10 messages
        role = "👤 You" if msg["role"] == "user" else "🤖 RAVEN"
        content = msg["content"][:100] + ("..." if len(msg["content"]) > 100 else "")
        history_text += f"{i}. {role}: {content}\n\n"

    if len(conversation_history) > 10:
        history_text += f"... and {len(conversation_history) - 10} more messages"

    await update.message.reply_text(history_text, parse_mode='Markdown')

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /model command."""
    global current_model
    if context.args:
        new_model = context.args[0]
        current_model = new_model
        await update.message.reply_text(f"🔄 Model changed to: {new_model}")
    else:
        await update.message.reply_text(f"🤖 Current model: {current_model}\nUsage: /model <model_name>")

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

**Status:** 🟢 Online and ready
    """
    await update.message.reply_text(system_info, parse_mode='Markdown')

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

        await update.message.reply_text(f"💾 Conversation exported to `{filename}`", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Export failed: {str(e)}")

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
    await update.message.reply_text(tokens_info, parse_mode='Markdown')

async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /save command."""
    global conversation_history
    if auto_save(conversation_history):
        await update.message.reply_text("💾 Session saved successfully.")
    else:
        await update.message.reply_text("❌ Failed to save session.")

async def load_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /load command."""
    global conversation_history
    loaded_history = auto_load()
    if loaded_history is not None:
        conversation_history = loaded_history
        await update.message.reply_text(f"📂 Session loaded. {len(conversation_history)} messages restored.")
    else:
        await update.message.reply_text("❌ Failed to load session or no saved session found.")

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /scan <target>\nExample: /scan 192.168.1.1")
        return

    target = " ".join(context.args)
    await update.message.reply_text(f"🔍 Starting reconnaissance on `{target}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        reconnaissance_prompt = f"Perform comprehensive target reconnaissance on {target}. Provide detailed information about the target's attack surface, technologies, infrastructure, and potential entry points."
        response = get_reply(current_model, [{"role": "user", "content": reconnaissance_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/scan {target}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"🎯 **Reconnaissance Results for {target}:**\n\n{response}", parse_mode='Markdown')

async def cve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cve command."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /cve <CVE-ID>\nExample: /cve CVE-2023-1234")
        return

    cve_id = " ".join(context.args).upper()
    await update.message.reply_text(f"🔍 Analyzing `{cve_id}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        cve_prompt = f"Provide detailed analysis of {cve_id}: vulnerability description, affected systems, CVSS score, exploit availability, patch status, and mitigation strategies."
        response = get_reply(current_model, [{"role": "user", "content": cve_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/cve {cve_id}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"🐛 **CVE Analysis for {cve_id}:**\n\n{response}", parse_mode='Markdown')

async def exploit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /exploit command."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /exploit <type>\nExample: /exploit bufferOverflow")
        return

    vuln_type = " ".join(context.args)
    await update.message.reply_text(f"⚡ Getting exploitation techniques for `{vuln_type}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        exploit_prompt = f"Detail exploitation techniques, payloads, and bypass methods for {vuln_type} vulnerabilities. Include required tools, skill level, and effectiveness ratings."
        response = get_reply(current_model, [{"role": "user", "content": exploit_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/exploit {vuln_type}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"⚡ **Exploitation Techniques for {vuln_type}:**\n\n{response}", parse_mode='Markdown')

async def harden_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /harden command."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /harden <system>\nExample: /harden windowsServer")
        return

    system = " ".join(context.args)
    await update.message.reply_text(f"🛡️ Getting hardening recommendations for `{system}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        harden_prompt = f"Provide comprehensive security hardening recommendations for {system}. Include configuration changes, patch management, network segmentation, monitoring, and incident response improvements."
        response = get_reply(current_model, [{"role": "user", "content": harden_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/harden {system}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"🛡️ **Hardening Recommendations for {system}:**\n\n{response}", parse_mode='Markdown')

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /analyze command - triggers full 4-phase pentest report."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /analyze <target>\nExample: /analyze 10.0.0.5")
        return

    target = " ".join(context.args)
    await update.message.reply_text(f"🎯 Starting full 4-phase penetration test analysis on `{target}`...\\nThis may take a moment.", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        analyze_prompt = f"Conduct a complete 4-phase penetration test assessment on {target}:\\n1. Target Reconnaissance\\n2. Vulnerability Mapping (with severity)\\n3. Exploitation Plan\\n4. Remediation & Hardening Plan\\nProvide detailed, technical, and actionable findings for each phase."
        response = get_reply(current_model, [{"role": "user", "content": analyze_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/analyze {target}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"📋 **Full 4-Phase Penetration Test Report for {target}:**\n\n{response}", parse_mode='Markdown')

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /generate command."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /generate <topic>\nExample: /generate sqlInjectionReport")
        return

    topic = " ".join(context.args)
    await update.message.reply_text(f"📝 Generating content on `{topic}`...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        generate_prompt = f"Generate professional security content about {topic}. This could be a report, writeup, analysis, or documentation suitable for security professionals."
        response = get_reply(current_model, [{"role": "user", "content": generate_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/generate {topic}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"📄 **Generated Content on {topic}:**\n\n{response}", parse_mode='Markdown')

async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scrape command."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /scrape <text>\nExample: /scrape logFileContents")
        return

    text = " ".join(context.args)
    await update.message.reply_text(f"🔍 Analyzing and extracting information from provided text...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        scrape_prompt = f"Analyze the following text and extract key security-relevant information: indicators of compromise, vulnerabilities, attack patterns, or actionable intelligence:\\n\\n{text}"
        response = get_reply(current_model, [{"role": "user", "content": scrape_prompt}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/scrape {text[:50]}..." if len(text) > 50 else f"/scrape {text}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(f"🔎 **Extracted Information:**\n\n{response}", parse_mode='Markdown')

# MODULE A: REAL BREACH DATA (/leak command with HIBP API)
async def leak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /leak command - check if email/domain appears in breach data using HIBP API."""
    if not context.args:
        await update.message.reply_text("❌ Usage: /leak <target>\nExample: /leak user@example.com or /leak example.com")
        return

    target = " ".join(context.args).strip()
    await update.message.reply_text(f"🔍 Checking `{target}` against breach databases...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        # Check if we have the required dependencies
        if not REQUESTS_AVAILABLE:
            response = f"❌ Real breach detection not available.\\nPlease install requests: `pip install requests`"
        elif not os.getenv("HIBP_API_KEY"):
            response = f"⚠️ HIBP API key not configured. Using simulated breach check.\\nGet a free API key at https://haveibeenpwned.com/API/Key"
            # Fall back to simulated response
            target_hash = hashlib.md5(target.lower().encode()).hexdigest()
            is_breached = int(target_hash[:8], 16) % 7 == 0  # ~14% chance of being "found"
            
            if is_breached:
                # Simulate breach data
                breach_count = (int(target_hash[8:12], 16) % 5) + 1  # 1-5 breaches
                latest_breach_year = 2020 + (int(target_hash[12:16], 16) % 4)  # 2020-2023
                data_types = ["Email addresses", "Passwords", "Personal data", "Credit card data"]
                compromised_data = [data_types[int(target_hash[i:i+2], 16) % len(data_types)]
                                 for i in range(16, min(26, len(target_hash)), 2)][:3]
                
                response = f"""🚨 **BREACH ALERT** - {target}

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
                response = f"""✅ **NO BREACHES DETECTED** - {target}

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
        else:
            # Use real HIBP API
            hibp_api_key = os.getenv("HIBP_API_KEY")
            headers = {
                "hibp-api-key": hibp_api_key,
                "user-agent": "RAVEN-Security-Bot"
            }
            
            try:
                # For email addresses
                if "@" in target:
                    url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{target}"
                    params = {"truncateResponse": "false"}
                # For domains, we need to search differently (HIBP doesn't have direct domain search)
                # We'll use the pastebin search or note this limitation
                else:
                    # Note: HIBP API v3 doesn't have a direct domain endpoint
                    # We would need to use alternative methods or admit limitation
                    response = f"""⚠️ **DOMAIN BREACH CHECK LIMITATION** - {target}

The Have I Been Pwned API v3 only supports checking individual email addresses, not domains directly.

For domain breach monitoring, you would need to:
1. Check known email addresses associated with the domain
2. Use alternative breach intelligence services
3. Monitor public paste sites for domain mentions

**RECOMMENDATION:** Try checking specific email addresses from this domain using /leak email@domain.com

*Analysis conducted by RAVEN OSINT Module*"""
                    # Add to conversation history even for limitation notice
                    conversation_history.append({"role": "user", "content": f"/leak {target}"})
                    conversation_history.append({"role": "assistant", "content": response})
                    auto_save(conversation_history)
                    await update.message.reply_text(response, parse_mode='Markdown')
                    return
                
                if "@" in target:  # Only make API call for emails
                    response_obj = requests.get(url, headers=headers, params=params)
                    
                    if response_obj.status_code == 200:
                        breaches = response_obj.json()
                        if isinstance(breaches, list) and len(breaches) > 0:
                            breach_names = [breach.get("Name", "Unknown") for breach in breaches]
                            breach_count = len(breach_names)
                            
                            # Get detailed breach info for the response
                            detailed_breaches = []
                            for breach in breaches[:3]:  # Limit to first 3 for readability
                                detailed_breaches.append({
                                    "Name": breach.get("Name", "Unknown"),
                                    "Title": breach.get("Title", "Unknown"),
                                    "Domain": breach.get("Domain", "Unknown"),
                                    "BreachDate": breach.get("BreachDate", "Unknown"),
                                    "PwnCount": breach.get("PwnCount", 0),
                                    "DataClasses": breach.get("DataClasses", [])
                                })
                            
                            # Generate damage assessment using RAVEN AI
                            breach_list_str = ", ".join(breach_names[:5])  # First 5 for prompt
                            if len(breach_names) > 5:
                                breach_list_str += f" and {len(breach_names) - 5} more"
                            
                            # Get AI-generated damage assessment
                            ai_prompt = f"""Generate a comprehensive damage assessment and mitigation plan for the email address {target} which has been found in the following data breaches: {breach_list_str}.
                            
                            Provide:
                            1. Immediate actions to take
                            2. Investigation steps
                            3. Long-term protection strategies
                            4. Security hardening recommendations
                            
                            Format as a professional security report with clear, actionable steps."""
                            
                            ai_response = get_reply(current_model, [{"role": "user", "content": ai_prompt}])
                            
                            response = f"""🚨 **CONFIRMED BREACH DETECTION** - {target}

🔒 **VERIFIED BY**: Have I Been Pwned (HIBP) API
⚠️ **BREACH COUNT**: {breach_count} confirmed breach(es)

**BREACH DETAILS:**
{chr(10).join([f"• {b['Name']} ({b['Title']}) - {b['BreachDate']} - {b['PwnCount']:,} accounts" for b in detailed_breaches])}

**DATA TYPES COMPROMISED:**
{', '.join([item for sublist in [b['DataClasses'] for b in detailed_breaches] for item in sublist][:10])}{'...' if len([item for sublist in [b['DataClasses'] for b in detailed_breaches] for item in sublist]) > 10 else ""}

**DAMAGE ASSESSMENT & MITIGATION PLAN:**

{ai_response}

**NOTE:** Data sourced from Have I Been Pwned API (https://haveibeenpwned.com/API/v3)
*Analysis conducted by RAVEN Breach Intelligence Module*"""
                        else:
                            response = f"""✅ **NO BREACHES DETECTED** - {target}

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
                    elif response_obj.status_code == 404:
                        response = f"""✅ **NO BREACHES DETECTED** - {target}

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
                    elif response_obj.status_code == 429:
                        response = f"""⚠️ **RATE LIMIT EXCEEDED** - {target}

🔒 **VERIFIED BY**: Have I Been Pwned (HIBP) API
📉 **STATUS**: Too many requests. Please wait a moment before trying again.

**RECOMMENDATIONS:**
1. Wait a few minutes before checking again
2. Consider upgrading your HIBP API plan for higher rate limits
3. Use cached results if available for recent checks

*Analysis conducted by RAVEN Breach Intelligence Module*"""
                    else:
                        response = f"❌ Error from HIBP API: HTTP {response_obj.status_code}\\n{response_obj.text}"
            except Exception as e:
                response = f"❌ Error conducting breach check: {str(e)}\\n\\nPlease check:\\n1. HIBP API key validity\\n2. Internet connection\\n3. Target format (email address)"

    # Add to conversation history
    conversation_history.append({"role": "user", "content": f"/leak {target}"})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(response, parse_mode='Markdown')

# MODULE B: VOICE COMMAND SUPPORT
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages by transcribing them and processing as text commands."""
    if not SPEECH_RECOGNITION_AVAILABLE:
        await update.message.reply_text("❌ Voice command support not available.\\nPlease install SpeechRecognition: `pip install SpeechRecognition`")
        return
    
    if not PYDUB_AVAILABLE:
        await update.message.reply_text("❌ Audio processing support not available.\\nPlease install pydub: `pip install pydub`")
        return
    
    # Show processing message
    processing_msg = await update.message.reply_text("🎤 Processing voice note...", parse_mode='Markdown')
    
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

async def handle_message_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process text (from voice or regular message) and get RAVEN's response."""
    await update.message.chat.send_action(action="typing")  # Show typing indicator
    
    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        response = get_reply(current_model, [{"role": "user", "content": text}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": text})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(response)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages."""
    user_message = update.message.text
    await update.message.reply_text("🤖 RAVEN is analyzing...", parse_mode='Markdown')

    # Capture stdout to prevent console spam
    f = io.StringIO()
    with redirect_stdout(f):
        response = get_reply(current_model, [{"role": "user", "content": user_message}])

    # Add to conversation history
    conversation_history.append({"role": "user", "content": user_message})
    conversation_history.append({"role": "assistant", "content": response})
    auto_save(conversation_history)

    await update.message.reply_text(response)

# MODULE C: AUTOMATED DAILY INTELLIGENCE BRIEFINGS
async def send_daily_briefing(context: ContextTypes.DEFAULT_TYPE):
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
                btc_binance = binance.fetch_ticker('BTC/USDT')
                btc_okx = okx.fetch_ticker('BTC/USDT')
                eth_binance = binance.fetch_ticker('ETH/USDT')
                eth_okx = okx.fetch_ticker('ETH/USDT')
                
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
            
            threat_intel = get_reply(current_model, [{"role": "user", "content": threat_prompt}])
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
            
            defense_tips = get_reply(current_model, [{"role": "user", "content": defense_prompt}])
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
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=briefing,
                parse_mode='Markdown'
            )
            
            logger.info(f"Daily briefing sent to admin chat {admin_chat_id}")
        else:
            logger.warning("No briefing components generated")
            
    except Exception as e:
        logger.error(f"Error sending daily briefing: {e}")

def setup_scheduler():
    """Set up the daily briefing scheduler."""
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
        
        # Add daily job
        scheduler.add_job(
            send_daily_briefing,
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

def main():
    """Start the bot."""
    # Get token from environment variable
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set!")
        return

    # Create the Application
    application = Application.builder().token(token).build()

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

    # Voice message handler
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Message handler for natural conversation
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Setup scheduler for daily briefings
    setup_scheduler()

    # Start the bot
    logger.info("Starting RAVEN Security Bot with Phase 3 features...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()