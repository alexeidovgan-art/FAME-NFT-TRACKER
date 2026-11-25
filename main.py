import os
from dotenv import load_dotenv
from telegram.ext import Application

load_dotenv()

from bot.handlers import register_handlers

def main():
    token = os.getenv("BOT_TOKEN")
    if not token or token.startswith("PASTE_"):
        raise RuntimeError("Укажи BOT_TOKEN в .env")
    app = Application.builder().token(token).build()
    register_handlers(app)
    print("FAME NFT TRACKER запущен")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
