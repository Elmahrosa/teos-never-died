import subprocess, sys

try:
    import ollama
except ImportError:
    print("[!] Installing ollama package...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "ollama"])
    import ollama

print("=" * 55)
print("  RAVEN \u2014 Zero Limits")
print("  Type 'exit' to quit")
print("=" * 55)

while True:
    try:
        user_input = input("\n>> ")
        if user_input.lower() in ["exit", "quit", "q"]:
            print("RAVEN: Gone.")
            break

        response = ollama.chat(model='raven', messages=[
            {'role': 'user', 'content': user_input}
        ])
        print("\n" + response['message']['content'])

    except KeyboardInterrupt:
        print("\n\nRAVEN: Interrupted.")
        break
    except Exception as e:
        print(f"[!] {e}")
        if "not found" in str(e).lower() or "model" in str(e).lower():
            print("[!] Run: ollama create raven -f Modelfile")
            break
