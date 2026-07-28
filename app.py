from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
import os, uuid, base64, json, sqlite3, re
from functools import wraps
from dotenv import load_dotenv
from groq import Groq
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader
from docx import Document
from doc_generator import create_pptx, create_docx, create_pdf

load_dotenv()
app = Flask(__name__)
app.secret_key = os.urandom(24)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODEL = "qwen/qwen3.6-27b"
DB_FILE = "chatbot.db"

SYSTEM_PROMPT = """
You are Nova, a friendly and curious AI assistant.
Reply the way a thoughtful person texts, not the way an essay is written.
Rules:
- Break replies into short chunks (1-3 sentences), separated by a blank line.
- Use bullet points or numbered steps when listing things.
- Never write one giant dense paragraph.
- Keep tone warm, casual, and clear.
- Do NOT show your internal reasoning or thinking process. Only output the final reply.
Never pretend to know something you're unsure about.
"""

def strip_think(text):
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return cleaned.strip()

# NEW — pulls out just the {...} JSON object even if model adds extra text around it
def extract_json(text):
    if not text:
        return None
    text = strip_think(text)
    text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    candidate = text[start:end+1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title TEXT DEFAULT 'New Chat',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

init_db()
os.makedirs("generated", exist_ok=True)

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# ---------- AUTH ROUTES ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")
        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            conn.close()
            return render_template("signup.html", error="Username already taken.")
        password_hash = generate_password_hash(password)
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?,?)", (username, password_hash))
        conn.commit()
        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        session["user_id"] = user["id"]
        session["username"] = username
        return redirect(url_for("home"))
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = username
            return redirect(url_for("home"))
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- CONVERSATION HELPERS ----------
def create_conversation(user_id):
    conv_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO conversations (id, user_id, title) VALUES (?,?,?)", (conv_id, user_id, "New Chat"))
    conn.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)", (conv_id, "system", SYSTEM_PROMPT))
    conn.commit()
    conn.close()
    session["current_id"] = conv_id
    return conv_id

def add_message(conv_id, role, content):
    content_to_store = json.dumps(content) if isinstance(content, (list, dict)) else content
    conn = get_db()
    conn.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)", (conv_id, role, content_to_store))
    conn.commit()
    conn.close()

def get_messages(conv_id):
    conn = get_db()
    rows = conn.execute("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        content = r["content"]
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                content = parsed
        except (json.JSONDecodeError, TypeError):
            pass
        result.append({"role": r["role"], "content": content})
    return result

def set_title_if_new(conv_id, text):
    conn = get_db()
    row = conn.execute("SELECT title FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if row and row["title"] == "New Chat":
        title = text[:30] + ("..." if len(text) > 30 else "")
        conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        conn.commit()
    conn.close()

def get_current_convo_id():
    user_id = session["user_id"]
    conv_id = session.get("current_id")
    if conv_id:
        conn = get_db()
        row = conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id)).fetchone()
        conn.close()
        if row:
            return conv_id
    return create_conversation(user_id)

# ---------- MAIN ROUTES ----------
@app.route("/")
@login_required
def home():
    if "current_id" not in session:
        create_conversation(session["user_id"])
    return render_template("index.html", username=session.get("username"))

@app.route("/history")
@login_required
def history():
    conn = get_db()
    rows = conn.execute("SELECT id, title FROM conversations WHERE user_id=? ORDER BY created_at DESC",
                         (session["user_id"],)).fetchall()
    conn.close()
    result = [{"id": r["id"], "title": r["title"]} for r in rows]
    return jsonify({"history": result, "current_id": session.get("current_id")})

@app.route("/new_chat", methods=["POST"])
@login_required
def new_chat():
    conv_id = create_conversation(session["user_id"])
    return jsonify({"id": conv_id, "title": "New Chat"})

@app.route("/load_chat/<conv_id>")
@login_required
def load_chat(conv_id):
    conn = get_db()
    row = conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",
                        (conv_id, session["user_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    session["current_id"] = conv_id

    display_messages = []
    for m in get_messages(conv_id):
        if m["role"] == "system":
            continue
        content = m["content"]
        if isinstance(content, list):
            content = next((c["text"] for c in content if c.get("type") == "text"), "[image]")
        display_messages.append({"role": m["role"], "content": strip_think(content)})
    return jsonify({"messages": display_messages})

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    user_message = request.json.get("message")
    conv_id = get_current_convo_id()
    add_message(conv_id, "user", user_message)
    set_title_if_new(conv_id, user_message)

    try:
        response = client.chat.completions.create(model=MODEL, messages=get_messages(conv_id))
        reply = strip_think(response.choices[0].message.content)
    except Exception as e:
        reply = f"Sorry, I couldn't connect. ({e})"

    add_message(conv_id, "assistant", reply)
    conn = get_db()
    title = conn.execute("SELECT title FROM conversations WHERE id=?", (conv_id,)).fetchone()["title"]
    conn.close()
    return jsonify({"reply": reply, "title": title})

@app.route("/upload_image", methods=["POST"])
@login_required
def upload_image():
    file = request.files.get("image")
    caption = request.form.get("message") or "What's in this image?"
    conv_id = get_current_convo_id()

    image_bytes = file.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = file.mimetype

    vision_content = [
        {"type": "text", "text": caption},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
    ]
    add_message(conv_id, "user", vision_content)
    set_title_if_new(conv_id, "Image: " + file.filename[:20])

    try:
        response = client.chat.completions.create(model=MODEL, messages=get_messages(conv_id))
        reply = strip_think(response.choices[0].message.content)
    except Exception as e:
        reply = f"Sorry, I couldn't process that image. ({e})"

    add_message(conv_id, "assistant", reply)
    return jsonify({"reply": reply})

def extract_text_from_file(file):
    filename = file.filename.lower()
    if filename.endswith(".pdf"):
        reader = PdfReader(file)
        return "".join([p.extract_text() or "" for p in reader.pages])
    elif filename.endswith(".docx"):
        doc = Document(file)
        return "\n".join([para.text for para in doc.paragraphs])
    elif filename.endswith(".txt"):
        return file.read().decode("utf-8", errors="ignore")
    return None

@app.route("/upload_file", methods=["POST"])
@login_required
def upload_file():
    file = request.files.get("file")
    user_note = request.form.get("message") or ""
    conv_id = get_current_convo_id()

    extracted_text = extract_text_from_file(file)
    if extracted_text is None:
        return jsonify({"reply": "I can only read PDF, DOCX, or TXT files right now."})

    MAX_CHARS = 12000
    truncated = extracted_text[:MAX_CHARS]
    note = "" if len(extracted_text) <= MAX_CHARS else "\n\n(Note: file was long, I only read the first part.)"

    prompt = f"""Here is the content of a file named "{file.filename}":

{truncated}{note}

{('The user also said: ' + user_note) if user_note else ''}
Please read this and explain what it's about in your own words, then ask if I want you to go deeper into any part."""

    add_message(conv_id, "user", prompt)
    set_title_if_new(conv_id, "File: " + file.filename[:20])

    try:
        response = client.chat.completions.create(model=MODEL, messages=get_messages(conv_id))
        reply = strip_think(response.choices[0].message.content)
    except Exception as e:
        reply = f"Sorry, I couldn't process that file. ({e})"

    add_message(conv_id, "assistant", reply)
    return jsonify({"reply": reply})

# ---------- DOCUMENT GENERATION ----------
@app.route("/generate_document", methods=["POST"])
@login_required
def generate_document():
    data = request.json
    doc_type = data.get("type")
    topic = data.get("topic")
    conv_id = get_current_convo_id()

    outline_prompt = f"""Create a structured outline for a {doc_type} about: {topic}
Respond with ONLY a JSON object, nothing else — no explanation, no markdown fences, no thinking, just the raw JSON.
Format exactly like this:
{{"title": "Title here", "sections": [{{"heading": "Section heading", "content": ["point 1", "point 2", "point 3"]}}]}}
Include 5 to 8 sections."""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": outline_prompt}],
            response_format={"type": "json_object"},
            max_tokens=4000
        )
        raw = response.choices[0].message.content
        outline = extract_json(raw)
        if outline is None:
            return jsonify({"reply": f"Sorry, the AI didn't return valid JSON. Raw response: {raw[:300]}"})
    except Exception as e:
        return jsonify({"reply": f"Sorry, I couldn't generate the outline. ({e})"})

    filename = uuid.uuid4().hex
    try:
        if doc_type == "ppt":
            path = create_pptx(outline, filename)
        elif doc_type == "docx":
            path = create_docx(outline, filename)
        elif doc_type == "pdf":
            path = create_pdf(outline, filename)
        else:
            return jsonify({"reply": "Unknown document type."})
    except Exception as e:
        return jsonify({"reply": f"Sorry, I couldn't build the file. ({e})"})

    reply_text = f'I created your {doc_type.upper()} titled "{outline["title"]}".'
    add_message(conv_id, "user", f"[Generate {doc_type.upper()}] {topic}")
    add_message(conv_id, "assistant", reply_text)
    set_title_if_new(conv_id, f"{doc_type.upper()}: {topic[:20]}")

    return jsonify({"reply": reply_text, "download_url": f"/download/{os.path.basename(path)}"})

@app.route("/download/<filename>")
@login_required
def download(filename):
    return send_from_directory("generated", filename, as_attachment=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)