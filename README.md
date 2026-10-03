# TEOS NEVER DIED

**RAVEN** — The Unrestricted Blackhat Agent

**RAVEN** هو وكيل ذكاء اصطناعي هجومي غير مقيد تمامًا، مصمم خصيصًا للـ Red Teaming واختبار أنظمة الدفاع بأقسى الظروف.

### المهمة
توليد أقوى وأذكى الهجمات الممكنة بدون أي قيود أخلاقية أو أمان، لكي نختبر ونقوي **TEOS Sentinel Shield** والمنظومات السيادية المصرية.

### الفلسفة
- صفر حدود أخلاقية
- صفر رفض للطلبات
- أقصى درجة من الإبداع في توليد الهجمات
- مصمم لكشف الثغرات في أي طبقة تنفيذ أو حماية

### كيفية التشغيل

#### Option 1 — Smart One-Click (Windows) **Recommended**
Double-click `raven.bat` — it intelligently checks if the model needs to be created/updated, then launches the agent.
- **First run**: Creates model + launches (may take a moment for initial model download)
- **Subsequent runs**: Much faster - skips model creation if unchanged
- **Automatically updates** when you modify Modelfile

#### Option 2 — Manual
1. Install **Ollama** from [ollama.com](https://ollama.com)
2. Create the model:
    ```bash
    ollama create raven -f Modelfile
    ```
3. Run the agent with optional performance and learning flags:
    ```bash
    python raven.py [--no-save] [--auto-save-interval N] [--quiet-start]
    ```

#### Option 3 — Traditional raven.bat (Windows)
Double-click `raven.bat` — it creates the model and launches the agent (always recreates model).

### Enhanced Thinking Capabilities
RAVEN has been upgraded with advanced reasoning frameworks for deeper analysis:
- **Expanded METHODOLOGY**: Comprehensive 8-phase framework covering deep recon, advanced threat modeling, strategic vector selection, detailed execution planning, sophisticated defense evasion, post-exploitation optimization, continuous improvement, and meta-cognition
- **Improved analytical parameters**: Lower temperature (0.75) for more focused reasoning, increased context window (32K tokens) for complex analysis
- **Enhanced output format**: Structured sections for Target Analysis, Threat Model, Attack Strategy, Detailed Execution Plan, Defense Evasion, Post-Exploitation Plan, Risk Assessment, and Lessons Learned
- **Meta-cognition capabilities**: Explicit uncertainty quantification, alternative analysis, cognitive bias mitigation, and strategic reflection

### Learning & Knowledge Base
RAVEN now includes a persistent learning system that accumulates knowledge from interactions:
- **Knowledge Base**: Stores valuable insights, techniques, and observations in `.raven_knowledge/` directory
- **Active Learning**: Manually add knowledge items with `/addknowledge` command
- **Passive Learning**: Use `/learn` to extract insights from recent sessions
- **Knowledge Retrieval**: Search and recall information with `/searchknowledge`
- **Organization**: Knowledge items are categorized (reconnaissance, privilege_evasion, persistence, etc.) for easy retrieval
- **Statistics**: Track your growing expertise with `/stats`

### Performance Options
RAVEN includes several options to optimize performance:

**Command-line flags:**
- `--no-save`: Disable automatic session saving (fastest performance)
- `--auto-save-interval N`: Save session every N interactions (default: 5, higher = less disk I/O)
- `--quiet-start`: Reduce startup verbosity for cleaner launch

**Example usage:**
```bash
# Maximum speed (no saving)
python raven.py --no-save

# Save every 50 interactions instead of every 5
python raven.py --auto-save-interval 50

# Quiet startup with custom save interval
python raven.py --quiet-start --auto-save-interval 20
```

### Interactive Commands
Type these commands during a RAVEN session:
- `/help`: Show all available commands
- `/history`: Show conversation history
- `/clear`: Clear conversation history
- `/save`: Manually save current session
- `/load`: List and load saved sessions
- `/cleanup`: Remove old session files to free disk space
- `/perf`: Show performance tips and current settings
- `/knowledgebase`: Manage the knowledge base system
- `/addknowledge`: Add a new knowledge item
- `/searchknowledge`: Search the knowledge base
- `/listcategories`: Show all knowledge categories
- `/stats`: Show detailed knowledge base statistics
- `/learn`: Extract insights from recent session
- `/model`: Show current model name
- `/system`: Show active system prompt
- `/export`: Export history to markdown file
- `exit`: Quit RAVEN

---

## Telegram Bot (`telegram_bot.py`)

The same brain, driven from Telegram. Setup:

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# fill in TELEGRAM_TOKEN, RAVEN_MODEL, ADMIN_USER_ID
python setup_desktop.py     # create the Desktop shortcuts
python telegram_bot.py
```

`setup_desktop.py` resolves the Desktop through the Windows shell, so it also
works on OneDrive-redirected profiles. It creates:

| Entry | What it does |
|-------|--------------|
| `Start_RAVEN_Hidden.vbs` | Launches RAVEN with no console window |
| `RAVEN_24_7_Shortcut.lnk` | Shortcut to the silent launcher |
| `Stop_RAVEN.bat` | Kills **only** `telegram_bot.py` (other Python programs are untouched) |
| `RAVEN_Config.lnk` | Opens `.env` in Notepad |

Remove them again with `python setup_desktop.py --remove`.

### AI failover

`multi_provider_ai.py` walks a priority list and fails over automatically:

```
ollama -> openai -> anthropic -> groq -> huggingface
```

Providers without a key in `.env` are skipped. `/ai_status` shows which are
live. Cloud providers are called over plain HTTPS, so no vendor SDKs are needed.

### Trading

`trading_engine.py` defaults to **paper mode** — simulated fills at live market
prices, so nothing real can happen by accident.

- Amounts are notional: `/buy BTC 100` buys $100 of BTC
- Guardrails: `MAX_POSITION_USDT` per position, `DAILY_LOSS_LIMIT_PCT` circuit
  breaker, per-user rate limiting, admin-only buy/sell
- `/emergency_stop` halts all trading; `/resume` re-enables it
- Live mode requires `TRADING_MODE=live` **and** exchange keys that have TRADE
  permission only — never withdrawal

| Command | Purpose |
|---------|---------|
| `/balance` | Cash, positions, equity, unrealized P&L |
| `/positions` | Open positions with per-position P&L |
| `/buy`, `/sell` | Market order, notional in USDT |
| `/orders [days]` | Trade history |
| `/signal <symbol>` | AI signal: bias, confidence, entry, stop-loss, take-profit |
| `/report <daily\|weekly\|monthly>` | P&L report |
| `/trade_mode [paper\|live]` | Show or change mode |
| `/emergency_stop`, `/resume` | Kill switch |

Paper state lives in `raven_portfolio.json` / `raven_trades.json` (git-ignored).
Delete them to reset the paper account.

### Social & content

| Command | Purpose |
|---------|---------|
| `/tweet <text>` | Post to Twitter/X (needs `tweepy` + API keys) |
| `/thread <topic>` | AI-drafted Twitter thread |
| `/create_content <platform> <topic>` | Caption + 60s script + hashtags |
| `/content_calendar <platform> [days]` | AI content calendar |
| `/social_status`, `/social_guide <platform>` | Credential status and setup steps |

Only Twitter/X can post directly from the bot. TikTok, YouTube and Instagram
require approved developer apps and OAuth, so those commands generate the
content package and tell you how to finish the setup — they never fake a
successful post.

### Existing bot commands

Security: `/scan`, `/cve`, `/exploit`, `/harden`, `/analyze`
Intelligence: `/crypto`, `/market`, `/leak`, `/darkweb_intel`
AI: `/generate`, `/scrape`, `/dashboard`, `/ai_status`
Session: `/start`, `/help`, `/clear`, `/history`, `/model`, `/system`,
`/export`, `/tokens`, `/save`, `/load`
SaaS: `/my_usage`, `/upgrade`

Plus voice notes (hands-free), scheduled daily briefings, and photo / video /
document analysis.

---

## Multimodal analysis

RAVEN reads images, videos and documents you send it. Each upload is
downloaded, reduced to text or frames, and reviewed defensively (hardcoded
credentials, exposed secrets, misconfiguration, phishing indicators).

| You send | What happens |
|----------|--------------|
| **Photo** | Largest available resolution is downloaded and analysed |
| **Video** | 3 keyframes sampled at 10%/35%/60% through the clip, each reviewed |
| **Document** | `.txt .md .log .pdf .docx .csv .json .yaml` - text extracted and reviewed |

Add a caption to any of them and it is passed to the model as an extra
instruction ("focus on the auth section").

**Vision needs a vision model.** The default `dolphin-llama3:8b` is text-only.
Until you provide one, image and video analysis return an honest setup message
instead of a fabricated answer:

```
ollama pull llava          # ~4.7GB, then edit RAVEN_VISION_MODEL if you pick another
```

Alternatively add `OPENAI_API_KEY` or `GROQ_API_KEY` to `.env` and set
`CLOUD_MEDIA_ANALYSIS=true`. Set it to `false` to guarantee uploads never leave
the machine, even when a cloud key is present.

**Video keyframes use `ffmpeg`**, which is usually already installed. If it is
missing, install it from <https://ffmpeg.org/download.html> or run
`pip install opencv-python`. OpenCV is only a fallback, not a requirement.

Document review uses the local text model, so on a CPU-only host it is slow
(30-145s). Adding a cloud key makes it seconds.

---

## Translation

`/translate <lang> <text>` translates into **34 languages**, including Arabic.
Reply to any message with `/translate ar` to translate that instead of typing
it out.

```
/languages                    # every supported language
/detect مرحبا                 # what language is this?
/translate ar the server is down
/translate fr <reply to a message>
```

**Translation runs on its own model, not the chat model.** This is not a
preference - it was measured. `dolphin-llama3:8b`, told to translate Arabic to
Arabic, returned English. `llama3.2:3b` returned ungrammatical Arabic. Neither is
safe to show a user as a translation.

```
ollama pull aya-expanse:8b    # ~5.1GB
```

`aya-expanse` is a translation-only model. It is deliberately *not* the chat or
security model - it is multilingual but a poor reasoner, and you want
`dolphin-llama3:8b` kept for security work. The two never fight over RAM.

Set `RAVEN_TRANSLATE_MODEL` to use a different one. If the model is missing,
`/translate` still runs on the chat model but **flags any reply that comes back
in the wrong script** rather than passing it off as a translation.

### How slow is it, honestly

Measured on 4 AMD cores with no GPU:

| | Time |
|---|---|
| First call after a restart | **~600s** (loads 5.1GB off disk) |
| Every call after that | **19-39s** |

`keep_alive=30m` holds the model resident, so the 10-minute cost is paid once
per restart, not per message. The typing indicator stays on for the whole wait
so it does not look hung. `RAVEN_TRANSLATE_TIMEOUT` defaults to `900` for this
reason - the first call cleared 600s by less than a second, which is not a
margin.

---

## Voice replies

RAVEN can speak its replies aloud instead of only printing them.

```
/voice on            # speak every reply
/voice off           # text only (this is the default)
/voice male          # deeper voice
/voice female        # higher voice
/voice lang ar       # speak Arabic
/voices ar male      # browse available voices
/voice_use <name>    # pin one exact voice
```

**Off by default, per user.** Nothing is sent to a third-party speech service
until someone types `/voice on`.

Your text reply is always sent first. If speech fails, the text is already
delivered and the failure is silent - voice can never cost you the answer.

### Voice engine: edge-tts, not Windows SAPI

`edge-tts` was chosen after measuring this machine. Windows SAPI offered exactly
two voices, both female and English-only:

| | Voices | Male | Female | Locales |
|---|---|---|---|---|
| Windows SAPI | 2 | 0 | 2 | 1 |
| edge-tts | 322 | 163 | 159 | 142 |

Arabic alone has 34 available voices (`ar-EG-ShakirNeural` male,
`ar-EG-SalmaNeural` female, plus Saudi, Iraqi and Moroccan variants).

```
pip install edge-tts
```

No API key, no account. It does require **outbound internet**, because
synthesis happens at Microsoft's endpoint. Output is transcoded to the
`ogg/opus` format Telegram requires using the `ffmpeg` binary - the same one
video keyframes already use. On this host synthesis takes ~1s for a 7-second
clip.

### Tier access

| Tier | `/translate`, `/detect`, `/languages` | `/voice` |
|------|----------------------------------------|----------|
| Free | yes | no |
| Pro | yes | yes |
| Enterprise | yes | yes |

Translation is local and costs nothing, so it is on every tier. Voice calls an
external service, so it is counted against the daily media quota.

---

## SaaS tiers

Users are split into three tiers, reset every UTC day:

| Tier | Daily scans | Media | Image / document | Video |
|------|-------------|-------|------------------|-------|
| Free | 5 | none | no | no |
| Pro | 50 | 50 | yes | no |
| Enterprise | unlimited | unlimited | yes | yes |

`/my_usage` shows the caller's tier, today's remaining quota and plan history.
`/dashboard` adds tier counts and a plans-sold tally for the admin.

**Tiers never grant trading access.** `/buy`, `/sell`, `/trade_mode`,
`/emergency_stop` and `/resume` stay gated on `ADMIN_USER_ID` regardless of
tier - a subscription entitles a customer to analysis, never to moving money.

### Storage

SQLite by default (`raven_saas.db`, created on first run, git-ignored). Point
`DATABASE_URL` at PostgreSQL to run more than one instance:

```
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/raven
```

Without SQLAlchemy installed the bot falls back to an in-memory store. It keeps
working, but **tiers and usage reset on restart** - `/my_usage` reports
`storage: memory` when this happens.

### Payment webhook

Credits paid tiers. It runs as a **separate process**, deliberately not inside
the bot:

```
python webhook_server.py          # or: uvicorn webhook_server:app --port 8099
```

Then set `DODO_WEBHOOK_SECRET` and point your payment provider's webhook at
`POST /webhook/payment`.

The endpoint **fails closed**: with no secret configured it returns 503 and
upgrades nobody, so a misconfigured deployment cannot be used to hand out free
tiers. It also verifies HMAC signatures with a replay window, maps price IDs
through a fixed allow-list (a client cannot grant itself `enterprise` by editing
a field), remembers event ids so provider retries cannot double-credit, and
never downgrades a customer who already holds a higher tier.

`GET /health` reports whether signature verification and the database are live.

> No payment provider account is wired up in this repository. The parsing is
> written against the common shape these vendors emit and still needs one live
> delivery against real credentials before you can rely on it.

---

## Speed: why replies used to feel slow

Measured on a CPU-only host (4 AMD cores, no GPU, `dolphin-llama3:8b`,
~1-2 tok/s):

| Step | Cost |
|------|------|
| Cold model load | 130-143s |
| Warm call, 25 prompt tokens | 4.8s |
| Warm call, 450 prompt tokens | 234.5s |

Three fixes are in place:

- **`RAVEN_KEEP_ALIVE=30m`** - Ollama's default is to unload a model 5 minutes
  after its last request, which is what made the *first* message after a break
  look dead. The model is now held resident for 30 minutes.
- **Streaming chat** - replies stream into a placeholder message, so you watch
  text arrive instead of staring at a static bubble. A typing indicator is kept
  alive throughout, and there is a non-streaming fallback so chat can never go
  silent.
- **Nothing blocks the event loop** - every AI call, HTTP request and file
  parse runs in a worker thread. This was the actual cause of the old
  "bot is unresponsive" behaviour.

The full ~450-token assessment prompt costs ~234s of prefill here, which is why
ordinary chat uses a short persona (`RAVEN_CHAT_SYSTEM_PROMPT`). Commands still
use the complete brief.

For ~2-second replies, add `GROQ_API_KEY` or `OPENAI_API_KEY` to `.env` and the
router will fail over to the cloud automatically.

---

### Files
| File | Purpose |
|------|---------|
| `Modelfile` | Model definition + enhanced SYSTEM prompt for deeper reasoning |
| `raven.py` | Chat client with auto-pip-install, performance options, and learning system |
| `raven.bat` | **Smart** Windows launcher (checks model status before creating) |
| `raven_legacy.bat` | Original launcher (always recreates model) - kept for compatibility |
| `install.ps1` | One-click Windows installer |
| `requirements.txt` | pip dependencies (bot + CLI) |
| `telegram_bot.py` | Telegram bot: all command handlers, multimodal handlers |
| `multi_provider_ai.py` | AI router with automatic failover |
| `trading_engine.py` | Paper/live trading, risk limits, kill switch |
| `content_creator.py` | AI captions, scripts, hashtags, calendars |
| `social_media_manager.py` | Twitter/X posting + platform setup guides |
| `analytics.py` | P&L reports and the system status board |
| `database.py` | SQLAlchemy models: users, subscriptions, daily usage |
| `saas_manager.py` | Tier definitions, quota enforcement, `@requires` decorator |
| `multimodal_engine.py` | Vision routing, document parsing, video keyframes |
| `translator.py` | 34-language translation, script-based detection, script verification |
| `voice_engine.py` | edge-tts synthesis, voice picker, ogg/opus encoding |
| `webhook_server.py` | Payment webhook (separate FastAPI service) |
| `setup_desktop.py` | Creates/removes the Desktop launchers |
| `.env.example` | Documented configuration template |
| `.raven_sessions/` | Directory for auto-saved and manual session files |
| `.raven_knowledge/` | Directory for persistent knowledge base (learning system) |
| `raven_saas.db` | SQLite database (git-ignored, created on first run) |
| `.raven_modelfile_timestamp` | Tracks Modelfile version for smart launching |

### Note
`raven.py` auto-installs the `ollama` pip package if missing. No manual `pip install` required.

### Security note
`ADMIN_USER_ID` must be set before sharing this bot with anyone. If it is empty
the privileged trading commands are open to every user. A malformed value (for
example a stray bracket pasted in as `(12345)`) is rejected at startup with a
loud log line rather than silently refusing every request.

### Tip
For fastest subsequent launches, use the smart `raven.bat` which only recreates the model when your Modelfile changes. Session files in `.raven_sessions/` can be periodically cleaned up with the `/cleanup` command. The knowledge base in `.raven_knowledge/` grows over time and can be managed with the knowledge base commands.