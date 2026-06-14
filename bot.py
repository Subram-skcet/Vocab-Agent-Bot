import os
import datetime
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
import telebot
import requests

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

bot = telebot.TeleBot(BOT_TOKEN)

# =====================================================================
# 🌍 LIGHTWEIGHT WEB SERVER FOR CLOUD DEPLOYMENT (RENDER)
# =====================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Responds to Render's health checks and anti-sleep pings."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Vocabulary Bot is alive and polling!")

    def log_message(self, format, *args):
        """Overrides and suppresses standard console logging to keep logs clean."""
        return

def run_health_check_server():
    """Binds to the port provided by Render to keep the service healthy."""
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"🌍 Internal health server listening on port {port}...")
    server.serve_forever()
# =====================================================================

def add_to_notion(term: str, term_type: str):
    """Sends a structured POST request to the Notion API to insert a new row."""
    url = "https://api.notion.com/v1/pages"
    
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    today = datetime.date.today().isoformat()
    
    payload = {
        "parent": { "database_id": DATABASE_ID },
        "properties": {
            "Term": {
                "title": [
                    { "text": { "content": term } }
                ]
            },
            "Type": {
                "select": { "name": term_type }
            },
            "Review Stage": {
                "number": 0
            },
            "Count": {
                "number": 0
            },
            "Next Review Date": {
                "date": { "start": today }
            }
        }
    }
    
    response = requests.post(url, json=payload, headers=headers)
    return response.status_code == 200

def process_term(message, term_type):
    """Helper function to extract text and trigger Notion insertion."""
    term = message.text.split(' ', 1)[1].strip() if ' ' in message.text else ""
    
    if not term:
        bot.reply_to(message, f"❌ Please provide a value. Example: /{term_type.lower().replace(' ', '')} your_text")
        return

    bot.send_chat_action(message.chat.id, 'typing')
    success = add_to_notion(term, term_type)
    
    if success:
        bot.reply_to(message, f"✅ Added '{term}' as a {term_type} to your learning database!")
    else:
        bot.reply_to(message, "❌ Failed to connect to Notion. Check your API configurations or database schema column names.")

# Explicit Command Handlers
@bot.message_handler(commands=['word'])
def handle_word(message):
    process_term(message, "Word")

@bot.message_handler(commands=['idiom'])
def handle_idiom(message):
    process_term(message, "Idiom")

@bot.message_handler(commands=['phrasal'])
def handle_phrasal(message):
    process_term(message, "Phrasal Verb")

@bot.message_handler(commands=['phrase'])
def handle_phrase(message):
    process_term(message, "Phrase")

# Fallback: Treat any plain text message as a regular "Word"
@bot.message_handler(func=lambda message: True)
def handle_plain_text(message):
    term = message.text.strip()
    bot.send_chat_action(message.chat.id, 'typing')
    success = add_to_notion(term, "Word")
    if success:
        bot.reply_to(message, f"✅ Logged '{term}' (Defaulted to Word).")
    else:
        bot.reply_to(message, "❌ Something went wrong saving this word to Notion.")

if __name__ == "__main__":
    # 1. Start the web server in a daemon thread so it doesn't block the bot
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    # 2. Run the main loop for Telegram
    print("🚀 Vocabulary Telegram Bot is up and listening...")
    bot.infinity_polling()