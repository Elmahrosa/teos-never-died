import os, subprocess, sys, argparse, datetime, json, time

try:
    import ollama
except ImportError:
    print("[!] Installing ollama package...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "ollama"])
    import ollama

SESSION_DIR = ".raven_sessions"
MAX_RETRIES = 3
RETRY_DELAY = 2


def ensure_session_dir():
    os.makedirs(SESSION_DIR, exist_ok=True)


def session_path(name):
    return os.path.join(SESSION_DIR, f"{name}.json")


def auto_save(messages, model):
    ensure_session_dir()
    path = session_path("_autosave")
    data = {"model": model, "ts": now_str(), "messages": messages}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def get_reply(model, messages, stream):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if stream:
                response = ollama.chat(model=model, messages=messages, stream=True)
                print("\nRAVEN: ", end="", flush=True)
                reply = ""
                for chunk in response:
                    token = chunk["message"]["content"]
                    print(token, end="", flush=True)
                    reply += token
                print()
                return reply
            else:
                response = ollama.chat(model=model, messages=messages, stream=False)
                reply = response["message"]["content"]
                print(f"\nRAVEN: {reply}")
                return reply
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            err = str(e).lower()
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
        print(f"  {c:12s} {d}")


def cmd_clear(messages, _model):
    messages.clear()
    print("[*] History cleared.")


def cmd_history(messages, _model):
    if not messages:
        print("[*] No history.")
        return
    for m in messages:
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
    total = sum(len(m["content"].split()) for m in messages)
    user_msgs = sum(1 for m in messages if m["role"] == "user")
    asst_msgs = sum(1 for m in messages if m["role"] == "assistant")
    print(f"  Messages: {len(messages)} ({user_msgs} user, {asst_msgs} assistant)")
    print(f"  ~Words:   {total}")
    print(f"  ~Tokens:  ~{int(total * 1.3)} (estimated)")


def cmd_load(messages, model):
    ensure_session_dir()
    sessions = [f[:-5] for f in os.listdir(SESSION_DIR)
                if f.endswith(".json") and f != "_autosave.json"]
    if not sessions:
        print("[*] No saved sessions found.")
        return
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
            return
    except (ValueError, EOFError):
        print("[*] Cancelled.")
        return

    name = sessions[choice - 1]
    path = session_path(name)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    messages.clear()
    messages.extend(data.get("messages", []))
    loaded_model = data.get("model", model)
    print(f"[*] Loaded {name} ({len(messages)} messages, model: {loaded_model})")
    return loaded_model


COMMANDS = {
    "/help":    ("Show available commands",                 cmd_help),
    "/clear":   ("Clear conversation history",               cmd_clear),
    "/history": ("Show conversation history",                cmd_history),
    "/model":   ("Show current model",                       cmd_model),
    "/system":  ("Show active system prompt",                cmd_system),
    "/export":  ("Export history to markdown file",          cmd_export),
    "/tokens":  ("Show message count and estimated tokens",  cmd_tokens),
    "/save":    ("Save session to .raven_sessions/",         cmd_save),
    "/load":    ("List and load a saved session",            cmd_load),
}


def handle_command(cmd, messages, model):
    entry = COMMANDS.get(cmd)
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
    args = parser.parse_args()

    model = args.model
    messages = []

    saved_model, saved_msgs = auto_load()
    if saved_msgs:
        model = saved_model or model
        messages.extend(saved_msgs)
        print(f"[*] Restored session ({len(messages)} messages, model: {model})")

    print("=" * 55)
    print(f"  RAVEN — Zero Limits  [model: {model}]")
    print("  Type '/help' for commands, 'exit' to quit")
    print("=" * 55)

    while True:
        try:
            user_input = input("\n>> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                if not args.no_save:
                    auto_save(messages, model)
                print("RAVEN: Gone.")
                break
            if user_input.startswith("/"):
                result = handle_command(user_input, messages, model)
                if result and user_input == "/load":
                    model = result
                continue

            messages.append({"role": "user", "content": user_input})
            reply = get_reply(model, messages, stream=not args.no_stream)
            messages.append({"role": "assistant", "content": reply})

            if not args.no_save:
                auto_save(messages, model)

        except KeyboardInterrupt:
            print("\n\nRAVEN: Interrupted.")
            if not args.no_save:
                auto_save(messages, model)
            break
        except EOFError:
            print("\n[!] Input closed. Exiting.")
            if not args.no_save:
                auto_save(messages, model)
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

