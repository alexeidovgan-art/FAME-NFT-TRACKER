
import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from telegram.ext import Application

load_dotenv()

from bot.handlers import register_handlers
from worker.scanner import run_scanner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"FAME NFT TRACKER is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(
        target=server.serve_forever,
        daemon=True
    ).start()
    print(f"Health server listening on port {port}")


async def post_init(app):
    app.bot_data["scanner_task"] = asyncio.create_task(
        run_scanner(app.bot)
    )
    print("NFT scanner запущен")


async def post_shutdown(app):
    task = app.bot_data.get("scanner_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def main():
    token = os.getenv("BOT_TOKEN")

    if not token or token.startswith("PASTE_"):
        raise RuntimeError("Укажи BOT_TOKEN в Environment Variables")

    start_health_server()

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    register_handlers(app)

    print("FAME NFT TRACKER запущен")
    app.run_polling(
        allowed_updates=["message", "callback_query"]
    )


if __name__ == "__main__":
    main()
