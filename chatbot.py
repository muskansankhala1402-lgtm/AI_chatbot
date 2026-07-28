import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# System prompt — give your bot a real personality here
messages = [
    {"role": "system", "content": """
    You are Nova, a friendly and curious AI assistant.
    You explain things clearly and keep a warm, casual tone.
    You never pretend to know something you're unsure about.
    """}
]

while True:
    user_input = input("You: ")
    if user_input.lower() in ["bye", "exit", "quit"]:
        print("Goodbye!")
        break

    messages.append({"role": "user", "content": user_input})

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages
        )
    except Exception as e:
        print(f"Sorry, I couldn't connect. Try again. ({e})")
        continue

    reply = response.choices[0].message.content
    print(f"Assistant: {reply}")

    messages.append({"role": "assistant", "content": reply})