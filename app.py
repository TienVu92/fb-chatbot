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
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash-latest")

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
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={FB_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    response = requests.post(url, json=payload)

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

# ===== WEBHOOK VERIFY =====
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed"

# ===== WEBHOOK RECEIVE =====
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
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
        user_text = (message.get("text") or "").strip()

        commands = message.get("commands") or []
        command_names = [
            command.get("name")
            for command in commands
            if isinstance(command, dict) and command.get("name")
        ]

        if command_names and user_text:
            user_text = f"{user_text}\nCommands: {', '.join(command_names)}"
        elif command_names:
            user_text = f"Commands: {', '.join(command_names)}"

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

        prompt = f"""
        Bạn là chatbot bán hàng.
        Đây là lịch sử 5 tin nhắn gần nhất:
        {history}

        Trả lời khách chuyên nghiệp, ngắn gọn.
        """

        response = model.generate_content(prompt)
        bot_reply = response.text or "Xin lỗi, tôi chưa thể phản hồi ngay lúc này."
        logger.info("Generated bot reply for sender_id=%s reply_len=%s", sender_id, len(bot_reply))

        # Lưu tin bot
        save_message(sender_id, "bot", bot_reply)

        # Gửi lại Messenger
        send_message(sender_id, bot_reply)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
