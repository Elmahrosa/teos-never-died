import os
import subprocess
import sys
import argparse
import datetime
import json
import time

try:
    import ollama
except ImportError:
    print("[!] Installing ollama package...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "ollama"])
    import ollama

SESSION_DIR = ".raven_sessions"
KNOWLEDGE_DIR = ".raven_knowledge"
MAX_RETRIES = 3
RETRY_DELAY = 2

# How long Ollama should keep the model resident in RAM after the last request.
# Ollama's own default is 5m, which on this CPU-only host means the next message
# pays a measured 130-143s cold load before a single token is produced. 30m keeps
# it warm across a normal session. Set RAVEN_KEEP_ALIVE="" to restore the
# Ollama default, or e.g. "10m" to trade RAM for a shorter residency window.
KEEP_ALIVE = os.getenv("RAVEN_KEEP_ALIVE", "30m").strip()
_KA_OPTS = {"keep_alive": KEEP_ALIVE} if KEEP_ALIVE else {}

# Hard cap on generated tokens per reply. Without it ollama runs until the
# model emits EOS, and dolphin-llama3:8b rambled for 376 tokens once = 8m14s
# of generation at ~0.8 tok/s, a 9m58s round trip that landed 2s short of the
# bot's 600s timeout. 300 tokens keeps the worst case (load 130s + prefill
# 45s + 300 tokens ~395s) under 600s with margin. RAVEN_MAX_TOKENS=0 disables
# the cap; the bot's .env sets 300 explicitly.
MAX_TOKENS = int((os.getenv("RAVEN_MAX_TOKENS", "300").strip() or "300"))
_OPTS = {"num_predict": MAX_TOKENS} if MAX_TOKENS > 0 else {}

# The canonical prompt, imported rather than redefined here. This module used to
# carry its own copy that dropped the mandated 4-phase output format, so the CLI
# and the Telegram bot answered the same question in different shapes. Both read
# this one definition now. See NOTOUCH.md section 4 before editing it: the format
# is contractual, and the text lives in multi_provider_ai.py.
from multi_provider_ai import RAVEN_SYSTEM_PROMPT


def ensure_session_dir():
    os.makedirs(SESSION_DIR, exist_ok=True)


def ensure_knowledge_dir():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)


def session_path(name):
    return os.path.join(SESSION_DIR, f"{name}.json")


def knowledge_path(name):
    return os.path.join(KNOWLEDGE_DIR, f"{name}.json")


def auto_save(messages, model, interval=0):
    """Auto-save session with optional interval control"""
    if interval <= 0:
        return  # Disabled
        
    ensure_session_dir()
    path = session_path("_autosave")
    save_interval = interval
    
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                last_saved_count = data.get("_save_count", 0)
        else:
            last_saved_count = 0
    except Exception:
        last_saved_count = 0
        
    if len(messages) - last_saved_count >= save_interval:
        data = {
            "model": model, 
            "ts": now_str(), 
            "messages": messages, 
            "_save_count": len(messages)
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))


def add_knowledge(title, content, category="general"):
    """Add a knowledge item to the knowledge base"""
    ensure_knowledge_dir()
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).rstrip()
    safe_title = safe_title.replace(" ", "_")[:50]
    if not safe_title:
        safe_title = f"knowledge_{int(time.time())}"
    
    knowledge_item = {
        "title": title,
        "content": content,
        "category": category,
        "timestamp": now_str(),
        "id": f"{safe_title}_{int(time.time())}"
    }
    
    path = knowledge_path(safe_title)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(knowledge_item, f, ensure_ascii=False, indent=2)
        return True, f"Knowledge added: {title}"
    except Exception as e:
        return False, f"Failed to add knowledge: {e}"


def search_knowledge(query, category=None):
    """Search the knowledge base for items matching query"""
    ensure_knowledge_dir()
    if not os.path.exists(KNOWLEDGE_DIR):
        return []
    
    results = []
    query_lower = query.lower()
    
    for filename in os.listdir(KNOWLEDGE_DIR):
        if not filename.endswith(".json"):
            continue
            
        try:
            path = knowledge_path(filename)
            with open(path, "r", encoding="utf-8") as f:
                item = json.load(f)
                
            if category and item.get("category", "general") != category:
                continue
                
            if (query_lower in item.get("title", "").lower() or 
                query_lower in item.get("content", "").lower()):
                results.append(item)
        except Exception:
            continue
    
    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return results


def list_knowledge_categories():
    """List all categories in the knowledge base"""
    ensure_knowledge_dir()
    if not os.path.exists(KNOWLEDGE_DIR):
        return []
    
    categories = set()
    for filename in os.listdir(KNOWLEDGE_DIR):
        if not filename.endswith(".json"):
            continue
            
        try:
            path = knowledge_path(filename)
            with open(path, "r", encoding="utf-8") as f:
                item = json.load(f)
            categories.add(item.get("category", "general"))
        except Exception:
            continue
    
    return sorted(list(categories))


def get_knowledge_stats():
    """Get statistics about the knowledge base"""
    ensure_knowledge_dir()
    if not os.path.exists(KNOWLEDGE_DIR):
        return {"count": 0, "categories": []}
    
    count = 0
    categories = set()
    
    for filename in os.listdir(KNOWLEDGE_DIR):
        if not filename.endswith(".json"):
            continue
            
        try:
            path = knowledge_path(filename)
            with open(path, "r", encoding="utf-8") as f:
                item = json.load(f)
            count += 1
            categories.add(item.get("category", "general"))
        except Exception:
            continue
    
    return {
        "count": count,
        "categories": sorted(list(categories))
    }


def auto_load():
    path = session_path("_autosave")
    if not os.path.exists(path):
        return None, []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[!] Failed to load session: {e}")
        return None, []
    model = data.get("model")
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    return model, messages


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_reply(model, messages, stream=False):
    """Send `messages` to the local Ollama model and return the reply text.

    Errors propagate. Callers decide how to report them, and nothing
    downstream mistakes an error string for a real model reply - which would
    then be persisted into the session file and the knowledge base.
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if stream:
                response = ollama.chat(model=model, messages=messages,
                                       stream=True, options=_OPTS, **_KA_OPTS)
                print("\nRAVEN: ", end="", flush=True)
                reply = ""
                for chunk in response:
                    token = chunk["message"]["content"]
                    print(token, end="", flush=True)
                    reply += token
                print()
                return reply
            else:
                response = ollama.chat(model=model, messages=messages,
                                       stream=False, options=_OPTS, **_KA_OPTS)
                reply = response["message"]["content"]
                print(f"\nRAVEN: {reply}")
                return reply
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            err = str(e).lower()
            # A missing model will not fix itself on a retry, so surface it now.
            if "not found" in err or "model" in err:
                raise
            if attempt < MAX_RETRIES:
                print(f"\n[!] Connection error (attempt {attempt}/{MAX_RETRIES}): {e}")
                print(f"    Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                raise last_err


# ── Commands ──────────────────────────────────────────────

def cmd_help(_messages, _model):
    for c, d in COMMANDS.items():
        print(f"  {c:18s} {d[0]}")


def cmd_clear(messages, _model):
    # Keep system prompt if it exists
    sys_prompt = next((m for m in messages if m["role"] == "system"), None)
    messages.clear()
    if sys_prompt:
        messages.append(sys_prompt)
    print("[*] History cleared.")


def cmd_history(messages, _model):
    if len(messages) <= 1: # Only system prompt or empty
        print("[*] No history.")
        return
    for m in messages:
        if m["role"] == "system": continue
        role = "You" if m["role"] == "user" else "RAVEN"
        text = m["content"][:120] + ("..." if len(m["content"]) > 120 else "")
        print(f"  {role}: {text}")


def cmd_model(_messages, model):
    print(f"[*] Current model: {model}")


def cmd_system(_messages, _model):
    for m in _messages:
        if m["role"] == "system":
            print(m["content"])
            return
    print("[*] No system prompt in current session.")


def cmd_export(messages, model):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = model.replace(":", "_").replace("/", "_")
    filename = f"raven_export_{safe_model}_{ts}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# RAVEN Session — {now_str()} | model: {model}\n\n")
        for m in messages:
            if m["role"] == "system": continue
            role = "You" if m["role"] == "user" else "RAVEN"
            f.write(f"### {role}\n{m['content']}\n\n")
    print(f"[*] Exported to {filename}")


def cmd_save(messages, model):
    name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ensure_session_dir()
    path = session_path(name)
    data = {"model": model, "ts": now_str(), "messages": messages}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[*] Saved as {name}")


def cmd_tokens(messages, _model):
    # Exclude system prompt from token estimation
    active_msgs = [m for m in messages if m["role"] != "system"]
    total = sum(len(m["content"].split()) for m in active_msgs)
    user_msgs = sum(1 for m in active_msgs if m["role"] == "user")
    asst_msgs = sum(1 for m in active_msgs if m["role"] == "assistant")
    print(f"  Messages: {len(active_msgs)} ({user_msgs} user, {asst_msgs} assistant)")
    print(f"  ~Words:   {total}")
    print(f"  ~Tokens:  ~{int(total * 1.3)} (estimated)")


def cmd_load(messages, model):
    ensure_session_dir()
    sessions = [f[:-5] for f in os.listdir(SESSION_DIR)
                if f.endswith(".json") and f != "_autosave.json"]
    if not sessions:
        print("[*] No saved sessions found.")
        return None
        
    print("[*] Saved sessions:")
    for i, s in enumerate(sessions, 1):
        path = session_path(s)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = len(data.get("messages", []))
        ts = data.get("ts", "unknown")
        print(f"  {i}. {s}  ({count} messages, {ts})")
    print(f"\n  0. Cancel")
    try:
        choice = int(input("  Load session #: ").strip())
        if choice == 0 or choice > len(sessions):
            print("[*] Cancelled.")
            return None
    except (ValueError, EOFError):
        print("[*] Cancelled.")
        return None

    name = sessions[choice - 1]
    path = session_path(name)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    messages.clear()
    messages.extend(data.get("messages", []))
    loaded_model = data.get("model", model)
    print(f"[*] Loaded {name} ({len(messages)} messages, model: {loaded_model})")
    return loaded_model


def cmd_cleanup(messages, model):
    """Clean up old session files"""
    ensure_session_dir()
    sessions = [f[:-5] for f in os.listdir(SESSION_DIR)
                if f.endswith(".json") and f != "_autosave.json"]
    if not sessions:
        print("[*] No session files to clean up.")
        return
        
    print(f"[*] Found {len(sessions)} session files:")
    for i, s in enumerate(sessions, 1):
        path = session_path(s)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = len(data.get("messages", []))
            ts = data.get("ts", "unknown")
            print(f"  {i}. {s}  ({count} messages, {ts})")
        except Exception:
            print(f"  {i}. {s}  (corrupted or unreadable)")
    
    try:
        choice = input("\n[*] Enter numbers to delete (e.g., 1,3,5) or 'all' or 'none': ").strip().lower()
        if choice == 'none' or choice == '':
            print("[*] Cancelled.")
            return
        if choice == 'all':
            to_delete = sessions
        else:
            indices = [int(x.strip())-1 for x in choice.split(',') if x.strip().isdigit()]
            to_delete = [sessions[i] for i in indices if 0 <= i < len(sessions)]
            
        deleted = 0
        for s in to_delete:
            try:
                os.remove(session_path(s))
                deleted += 1
                print(f"    [-] Deleted {s}")
            except Exception as e:
                print(f"    [!] Failed to delete {s}: {e}")
                
        print(f"[*] Cleanup complete. Deleted {deleted} file(s).")
    except Exception as e:
        print(f"[!] Cleanup error: {e}")


def cmd_performance(messages, model):
    """Show performance tips and current settings"""
    print("[*] Performance Tips for RAVEN:")
    print("    --no-auto-save          : Disable automatic session saving")
    print("    --auto-save-interval N  : Save every N interactions (default: 5)")
    print("    --quiet-start           : Reduce startup verbosity")
    print("    /cleanup                : Remove old session files")
    print("    /save                   : Manually save current session")
    print("    Tip: Session files in .raven_sessions/ can grow large over time")


def cmd_knowledgebase(messages, model):
    """Manage the knowledge base"""
    ensure_knowledge_dir()
    stats = get_knowledge_stats()
    
    print(f"[*] RAVEN Knowledge Base")
    print(f"    Total items: {stats['count']}")
    print(f"    Categories: {', '.join(stats['categories']) if stats['categories'] else 'None'}")
    print()
    print("[*] Knowledge Base Commands:")
    print("    /addknowledge <title> | <content> | [category]  - Add knowledge item")
    print("    /searchknowledge <query> [category]             - Search knowledge base")
    print("    /listcategories                                 - Show all categories")
    print("    /stats                                          - Show detailed statistics")
    print("    /learn                                          - Extract insights from recent session")


def cmd_addknowledge(messages, model):
    """Add a knowledge item"""
    print("[+] Adding new knowledge item")
    title = input("    Title: ").strip()
    if not title:
        print("[!] Title is required")
        return
        
    content = input("    Content: ").strip()
    if not content:
        print("[!] Content is required")
        return
        
    category = input("    Category (optional, default: general): ").strip()
    if not category:
        category = "general"
    
    success, msg = add_knowledge(title, content, category)
    if success:
        print(f"[+] {msg}")
    else:
        print(f"[!] {msg}")


def cmd_searchknowledge(messages, model):
    """Search the knowledge base"""
    query = input("[+] Search query: ").strip()
    if not query:
        print("[!] Query is required")
        return
        
    category = input("[+] Category (optional, press Enter to skip): ").strip()
    if not category:
        category = None
    
    results = search_knowledge(query, category)
    
    if not results:
        print(f"[!] No knowledge items found matching '{query}'" + 
              (f" in category '{category}'" if category else ""))
        return
        
    print(f"[+] Found {len(results)} knowledge item(s):")
    for i, item in enumerate(results, 1):
        print(f"    {i}. {item['title']}")
        print(f"       Category: {item.get('category', 'general')}")
        print(f"       Added: {item.get('timestamp', 'unknown')}")
        print(f"       Preview: {item['content'][:100]}{'...' if len(item['content']) > 100 else ''}")
        print()


def cmd_listcategories(messages, model):
    """List all knowledge categories"""
    categories = get_knowledge_stats()['categories']
    if not categories:
        print("[*] No categories found in knowledge base")
        return
        
    print("[*] Knowledge Base Categories:")
    for category in categories:
        items = search_knowledge("", category)
        print(f"    {category}: {len(items)} item(s)")


def cmd_stats(messages, model):
    """Show detailed knowledge base statistics"""
    stats = get_knowledge_stats()
    print("[*] Knowledge Base Statistics")
    print(f"    Total knowledge items: {stats['count']}")
    print(f"    Categories ({len(stats['categories'])}): {', '.join(stats['categories']) if stats['categories'] else 'None'}")
    
    if stats['count'] > 0:
        print("\n[*] Breakdown by category:")
        for category in stats['categories']:
            items = search_knowledge("", category)
            print(f"    {category}: {len(items)} item(s)")


def cmd_learn(messages, model):
    """Extract insights from recent session and add to knowledge base"""
    # Filter out system prompt for analysis
    active_msgs = [m for m in messages if m["role"] != "system"]
    if len(active_msgs) < 2:
        print("[!] Not enough conversation to learn from")
        return

    print("[+] Learning from recent session...")
    insights = []

    for i in range(0, len(active_msgs)-1, 2):
        if i+1 < len(active_msgs):
            user_msg = active_msgs[i]
            assistant_msg = active_msgs[i+1]

            if user_msg.get("role") == "user" and assistant_msg.get("role") == "assistant":
                user_content = user_msg.get("content", "").lower()
                assistant_content = assistant_msg.get("content", "")

                trigger_words = ["technique", "method", "tool", "command", "script",
                               "vulnerability", "exploit", "bypass", "payload"]

                if any(word in user_content for word in trigger_words) and len(assistant_content) > 50:
                    insight_title = f"Insight from session: {user_msg['content'][:30]}..."
                    insight_content = f"User asked: {user_msg['content']}\n\nRAVEN responded:\n{assistant_content[:500]}{'...' if len(assistant_content) > 500 else ''}"

                    category = "general"
                    if any(word in assistant_content.lower() for word in ["network", "port", "scan", "recon"]):
                        category = "reconnaissance"
                    elif any(word in assistant_content.lower() for word in ["privilege", "escalation", "root", "admin"]):
                        category = "privilege_escalation"
                    elif any(word in assistant_content.lower() for word in ["persistence", "backdoor", "service"]):
                        category = "persistence"
                    elif any(word in assistant_content.lower() for word in ["evade", "bypass", "obfuscate"]):
                        category = "defense_evasion"
                    elif any(word in assistant_content.lower() for word in ["exfil", "exfiltrate", "data theft"]):
                        category = "exfiltration"
                    elif any(word in assistant_content.lower() for word in ["phishing", "social", "email"]):
                        category = "social_engineering"

                    insights.append((insight_title, insight_content, category))

    if not insights:
        print("[!] No clear insights detected in recent session")
        print("[*] Try using /addknowledge to manually add valuable information")
        return

    print(f"[+] Detected {len(insights)} potential insight(s):")
    for i, (title, content, category) in enumerate(insights, 1):
        print(f"    {i}. [{category}] {title}")

    proceed = input("\n[+] Add these insights to knowledge base? (y/N): ").strip().lower()
    if proceed != 'y':
        print("[*] Learning cancelled")
        return

    added = 0
    for title, content, category in insights:
        success, msg = add_knowledge(title, content, category)
        if success:
            print(f"    [+] {msg}")
            added += 1
        else:
            print(f"    [!] {msg}")

    print(f"[+] Learning complete. Added {added} insight(s) to knowledge base.")


def cmd_autorecon(messages, model):
    """Perform automated reconnaissance on explicitly authorized targets only.

    Targets are gated by recon_engine.validate_target(), which uses a strict
    allowlist. Nothing here widens that allowlist.
    """
    # Import the recon engine
    try:
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from recon_engine import ReconEngine, SafetyError
    except ImportError as e:
        print(f"[!] Failed to load reconnaissance engine: {e}")
        return None

    # Get target from user
    print("[+] Automated Reconnaissance - authorized targets only")
    print("    Allowed: RFC1918/loopback addresses, plus any host listed in")
    print("    RAVEN_PRE_APPROVED_DOMAINS. Link-local metadata, unspecified and")
    print("    reserved ranges are refused.")
    target = input("    Enter target IP/hostname: ").strip()

    if not target:
        print("[!] Target is required")
        return None

    # Initialize engine
    engine = ReconEngine()

    # Validate target
    try:
        validated_target = engine.validate_target(target)
        print(f"[+] Target validated: {validated_target}")
    except SafetyError as e:
        print(f"[!] {e}")
        return None

    print("[*] Running safe reconnaissance checks...")

    # Run reconnaissance checks
    async def run_checks():
        results = {}

        # 1. Ping test
        print("    [*] Testing connectivity...")
        results["ping"] = await engine.ping_host(validated_target, count=2)

        # 2. Port scan (common ports)
        print("    [*] Scanning common ports...")
        common_ports = [21, 22, 23, 25, 53, 80, 110, 143, 443, 993, 995, 8080, 8443]
        results["port_scan"] = await engine.port_scan(validated_target, common_ports, timeout=2)

        # 3. HTTP header fetch (if web ports open)
        web_ports = [80, 443, 8080, 8443]
        open_web_ports = [p for p in web_ports if p in results["port_scan"].get("open_ports", [])]

        if open_web_ports:
            print(f"    [*] Fetching HTTP headers from ports {open_web_ports}...")
            http_results = []
            for port in open_web_ports[:2]:  # Limit to first 2 web ports
                use_ssl = port in [443, 8443]
                header_result = await engine.fetch_http_headers(validated_target, port, use_ssl)
                http_results.append({"port": port, "ssl": use_ssl, "result": header_result})
            results["http_headers"] = http_results
        else:
            results["http_headers"] = [{"error": "No web ports open"}]

        return results

    # Run the async checks
    try:
        if sys.platform == "win32":
            # Windows-specific asyncio setup
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        results = asyncio.run(run_checks())

        # Display results
        print("\n[+] Reconnaissance Results:")
        print(f"    Target: {validated_target}")

        # Ping results
        ping_result = results.get("ping", {})
        if ping_result.get("status") == "completed":
            print("    [*] Connectivity: Host is reachable")
        else:
            print("    [*] Connectivity: Host appears unreachable or blocked")

        # Port scan results
        port_scan = results.get("port_scan", {})
        open_ports = port_scan.get("open_ports", [])
        if open_ports:
            print(f"    [*] Open Ports: {', '.join(map(str, open_ports))}")
        else:
            print("    [*] Open Ports: None detected")

        # HTTP results
        http_results = results.get("http_headers", [])
        for http_result in http_results:
            port = http_result.get("port", "unknown")
            if "error" not in http_result.get("result", {}):
                print(f"    [*] HTTP/{'HTTPS' if http_result.get('ssl') else 'HTTP'} on port {port}: Headers retrieved")
            else:
                print(f"    [*] HTTP/{'HTTPS' if http_result.get('ssl') else 'HTTP'} on port {port}: Failed ({http_result['result'].get('error', 'unknown error')})")

        # Prepare data for AI analysis
        print("\n[+] Generating security assessment...")

        # Format results for AI prompt
        assessment_data = f"""
RECONNAISSANCE DATA FOR TARGET: {validated_target}

CONNECTIVITY:
- Ping: {'Success' if ping_result.get('status') == 'completed' else 'Failed'}

PORT SCAN:
- Open Ports: {', '.join(map(str, open_ports)) if open_ports else 'None'}
- Scanned Ports: {', '.join(map(str, port_scan.get('scanned_ports', [])))}

WEB SERVICE CHECKS:
"""
        for http_result in http_results:
            port = http_result.get("port")
            result = http_result.get("result", {})
            if "error" not in result:
                assessment_data += f"- Port {port} ({'HTTPS' if http_result.get('ssl') else 'HTTP'}): Accessible, headers retrieved\n"
            else:
                assessment_data += f"- Port {port} ({'HTTPS' if http_result.get('ssl') else 'HTTP'}): {result.get('error', 'Failed')}\n"

        assessment_data += """
IMPORTANT: This is authorized local testing only. Provide a professional 4-phase Red Team assessment based on this data.
"""

        # Add to messages and get AI response
        messages.append({"role": "user", "content": assessment_data})
        reply = get_reply(model, messages, stream=False)
        messages.append({"role": "assistant", "content": reply})

        print("\n[+] RAVEN Assessment:")
        print(reply)

    except Exception as e:
        print(f"[!] Reconnaissance error: {e}")
        return None

    return None


COMMANDS = {
    "/help":            ("Show available commands",                 cmd_help),
    "/clear":           ("Clear conversation history",              cmd_clear),
    "/history":         ("Show conversation history",               cmd_history),
    "/model":           ("Show current model",                      cmd_model),
    "/system":          ("Show active system prompt",               cmd_system),
    "/export":          ("Export history to markdown file",         cmd_export),
    "/tokens":          ("Show message count and estimated tokens", cmd_tokens),
    "/save":            ("Save session to .raven_sessions/",        cmd_save),
    "/load":            ("List and load a saved session",           cmd_load),
    "/cleanup":         ("Clean up old session files",              cmd_cleanup),
    "/perf":            ("Show performance tips",                   cmd_performance),
    "/knowledgebase":   ("Manage the knowledge base",               cmd_knowledgebase),
    "/addknowledge":    ("Add knowledge item",                      cmd_addknowledge),
    "/searchknowledge": ("Search knowledge base",                   cmd_searchknowledge),
    "/listcategories":  ("List knowledge categories",               cmd_listcategories),
    "/stats":           ("Show knowledge base statistics",          cmd_stats),
    "/learn":           ("Extract insights from session",           cmd_learn),
    "/autorecon":       ("Automated reconnaissance (local only)",   cmd_autorecon),
}


def handle_command(cmd, messages, model):
    # Handle commands that might have arguments (like /addknowledge Title | Content)
    base_cmd = cmd.split()[0] if " " in cmd else cmd
    entry = COMMANDS.get(base_cmd)
    
    if entry:
        result = entry[1](messages, model)
        return result
    else:
        print(f"[!] Unknown command: {cmd}. Type /help for available commands.")
        return None


def main():
    parser = argparse.ArgumentParser(description="RAVEN — Unrestricted Agent")
    parser.add_argument("--model", default=os.getenv("RAVEN_MODEL", "raven"),
                        help="Ollama model name (default: raven)")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming")
    parser.add_argument("--no-save", action="store_true", help="Disable auto-save")
    parser.add_argument("--auto-save-interval", type=int, default=5,
                        help="Auto-save every N interactions (0 to disable, default: 5)")
    parser.add_argument("--quiet-start", action="store_true", help="Reduce startup verbosity")
    args = parser.parse_args()

    model = args.model
    messages = []

    if not args.quiet_start:
        print("=" * 55)
        print(f"  RAVEN — Zero Limits  [model: {model}]")
        print(f"  Auto-save: {'Disabled' if args.no_save or args.auto_save_interval == 0 else f'Every {args.auto_save_interval} interactions'}")
        print(f"  Stream:    {'Disabled' if args.no_stream else 'Enabled'}")
        print("  Type '/help' for commands, 'exit' to quit")
        print("=" * 55)

    saved_model, saved_msgs = auto_load()
    if saved_msgs:
        model = saved_model or model
        messages.extend(saved_msgs)
        if not args.quiet_start:
            print(f"[*] Restored session ({len(messages)} messages, model: {model})")

    # Ensure the system prompt is injected at the start of the conversation
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": RAVEN_SYSTEM_PROMPT})

    while True:
        try:
            user_input = input("\n>> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                if not args.no_save:
                    auto_save(messages, model, args.auto_save_interval if not args.no_save else 0)
                print("RAVEN: Gone.")
                break
            if user_input.startswith("/"):
                result = handle_command(user_input, messages, model)
                if result and user_input.startswith("/load"):
                    model = result
                continue

            messages.append({"role": "user", "content": user_input})
            reply = get_reply(model, messages, stream=not args.no_stream)
            messages.append({"role": "assistant", "content": reply})

            if not args.no_save:
                auto_save(messages, model, args.auto_save_interval if not args.no_save else 0)

        except KeyboardInterrupt:
            print("\n\nRAVEN: Interrupted.")
            if not args.no_save:
                auto_save(messages, model, args.auto_save_interval if not args.no_save else 0)
            break
        except EOFError:
            print("\n[!] Input closed. Exiting.")
            if not args.no_save:
                auto_save(messages, model, args.auto_save_interval if not args.no_save else 0)
            break
        except Exception as e:
            err = str(e).lower()
            print(f"\n[!] {e}")
            if "not found" in err or "model" in err:
                print(f"[!] Run: ollama create {model} -f Modelfile")
                break
            if "connect" in err or "refused" in err or "timeout" in err:
                print("[!] Is Ollama running? Start it with: ollama serve")
                continue
            print("[!] Unexpected error. Type 'exit' to quit.")


if __name__ == "__main__":
    main()