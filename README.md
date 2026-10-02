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

### Files
| File | Purpose |
|------|---------|
| `Modelfile` | Model definition + enhanced SYSTEM prompt for deeper reasoning |
| `raven.py` | Chat client with auto-pip-install, performance options, and learning system |
| `raven.bat` | **Smart** Windows launcher (checks model status before creating) |
| `raven_legacy.bat` | Original launcher (always recreates model) - kept for compatibility |
| `install.ps1` | One-click Windows installer |
| `requirements.txt` | pip dependency (`ollama`) |
| `.raven_sessions/` | Directory for auto-saved and manual session files |
| `.raven_knowledge/` | Directory for persistent knowledge base (learning system) |
| `.raven_modelfile_timestamp` | Tracks Modelfile version for smart launching |

### Note
`raven.py` auto-installs the `ollama` pip package if missing. No manual `pip install` required.

### Tip
For fastest subsequent launches, use the smart `raven.bat` which only recreates the model when your Modelfile changes. Session files in `.raven_sessions/` can be periodically cleaned up with the `/cleanup` command. The knowledge base in `.raven_knowledge/` grows over time and can be managed with the knowledge base commands.