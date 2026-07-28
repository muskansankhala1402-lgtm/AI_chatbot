import os
from dotenv import load_dotenv
from groq import Groq

# Load variables from .env into the environment
load_dotenv()

# Create the client using your key
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Send one simple message
response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[
        {"role": "user", "content": "Hello, who are you?"}
    ]
)

# Print just the reply text
print(response.choices[0].message.content)