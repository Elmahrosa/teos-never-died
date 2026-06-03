import ollama
import sys

print("=== RAVEN - Unrestricted Blackhat Agent ===")
print("Type 'exit' or 'quit' to stop.\n")

while True:
    try:
        user_input = input("\nYou: ")
        if user_input.lower() in ["exit", "quit", "q"]:
            print("RAVEN: Until next time... Stay dangerous.")
            break

        response = ollama.chat(model='raven', messages=[
            {'role': 'user', 'content': user_input}
        ])
        
        print("\nRAVEN:", response['message']['content'])

    except KeyboardInterrupt:
        print("\n\nRAVEN: Session terminated.")
        break
    except Exception as e:
        print(f"Error: {e}")
