import os
import sqlite3
import logging
from flask import Flask, request
import requests
import google.generativeai as genai

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s - %(message)s',
)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
FB_PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

model = None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
else:
    logger.warning("GEMINI_API_KEY is missing. AI responses will use fallback text.")

DB_NAME = "chat.db"

# ===== INIT DATABASE =====
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===== SAVE MESSAGE =====
def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()
    conn.close()

# ===== GET LAST 5 MESSAGES =====
def get_last_messages(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        SELECT role, content FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 5
    """, (user_id,))
    rows = c.fetchall()
    conn.close()

    # đảo ngược lại cho đúng thứ tự
    rows.reverse()

    history = ""
    for role, content in rows:
        history += f"{role}: {content}\n"

    return history

# ===== SEND MESSAGE TO FACEBOOK =====
def send_message(recipient_id, text):
    if not FB_PAGE_TOKEN:
        logger.error("FB_PAGE_TOKEN is missing. Cannot send message.")
        return

    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={FB_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
    except requests.RequestException:
        logger.exception("HTTP error while sending message recipient_id=%s", recipient_id)
        return

    if response.ok:
        logger.info("Sent message to recipient_id=%s", recipient_id)
    else:
        logger.error(
            "Failed to send message recipient_id=%s status=%s body=%s",
            recipient_id,
            response.status_code,
            response.text,
        )


def extract_message_events(data):
    events = []

    if not isinstance(data, dict):
        logger.warning("Webhook payload is not a dict")
        return events

    if data.get("field") == "messages" and isinstance(data.get("value"), dict):
        events.append(data["value"])

    sample = data.get("sample")
    if isinstance(sample, dict) and sample.get("field") == "messages" and isinstance(sample.get("value"), dict):
        events.append(sample["value"])

    for entry in data.get("entry", []):
        for msg in entry.get("messaging", []):
            events.append(msg)

    logger.info("Extracted %s message event(s)", len(events))

    return events


def get_user_text(message):
    text = (message.get("text") or "").strip()

    commands = message.get("commands") or []
    command_names = [
        command.get("name")
        for command in commands
        if isinstance(command, dict) and command.get("name")
    ]

    if command_names and text:
        text = f"{text}\nCommands: {', '.join(command_names)}"
    elif command_names:
        text = f"Commands: {', '.join(command_names)}"

    return text, command_names


def build_prompt(history, user_text):
    return f"""
Bạn là chatbot bán hàng.
Đây là lịch sử 5 tin nhắn gần nhất:
{history}

Đây là tin nhắn mới nhất của khách:
{user_text}

Trả lời khách chuyên nghiệp, ngắn gọn.
"""


def generate_bot_reply(prompt):
    fallback = "Xin lỗi, tôi chưa thể phản hồi ngay lúc này."

    if model is None:
        return fallback

    try:
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        return text or fallback
    except Exception:
        logger.exception("Failed to generate response from Gemini")
        return fallback

# ===== WEBHOOK VERIFY =====
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN and VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed"

# ===== WEBHOOK RECEIVE =====
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    logger.info("Received webhook request")

    if data is None:
        logger.warning("Received webhook with empty JSON body")
        return "OK", 200

    for msg in extract_message_events(data):
        sender_id = msg.get("sender", {}).get("id")
        if not sender_id:
            logger.warning("Skipping event without sender.id")
            continue

        message = msg.get("message", {})
        user_text, command_names = get_user_text(message)

        if not user_text:
            logger.info("Skipping sender_id=%s because no text or commands found", sender_id)
            continue

        logger.info(
            "Processing sender_id=%s text_len=%s commands=%s",
            sender_id,
            len(user_text),
            command_names,
        )

        # Lưu tin nhắn user
        save_message(sender_id, "user", user_text)

        # Lấy 5 tin gần nhất
        history = get_last_messages(sender_id)

        prompt = build_prompt(history, user_text)
        bot_reply = generate_bot_reply(prompt)
        logger.info("Generated bot reply for sender_id=%s reply_len=%s", sender_id, len(bot_reply))

        # Lưu tin bot
        save_message(sender_id, "bot", bot_reply)

        # Gửi lại Messenger
        send_message(sender_id, bot_reply)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
