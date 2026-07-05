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

#### Option 1 — One-Click (Windows)
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
python raven.py
```

#### Option 2 — Manual
1. Install **Ollama** from [ollama.com](https://ollama.com)
2. Create the model:
   ```bash
   ollama create raven -f Modelfile
   ```
3. Run the agent:
   ```bash
   python raven.py
   ```

#### Option 3 — raven.bat (Windows)
Double-click `raven.bat` — it creates the model and launches the agent.

> **Note:** `raven.py` auto-installs the `ollama` pip package if missing. No manual `pip install` needed.

---

### Files
| File | Purpose |
|------|---------|
| `Modelfile` | Model definition + unrestricted SYSTEM prompt |
| `raven.py` | Chat client with auto-pip-install |
| `raven.bat` | Windows launcher (create + run) |
| `install.ps1` | One-click Windows installer |
| `requirements.txt` | pip dependency (`ollama`)
