
import logging
import os
from html import escape
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from bot.db import (
    insert_listing,
    delete_unpublished_listing,
    set_message_id,
)

log = logging.getLogger(__name__)

TOPICS = {
    "cheap": int(os.getenv("TOPIC_CHEAP", "0")),
    "mid": int(os.getenv("TOPIC_MID", "0")),
    "expensive": int(os.getenv("TOPIC_EXPENSIVE", "0")),
    "premium": int(os.getenv("TOPIC_PREMIUM", "0")),
}

GROUP_ID = int(os.getenv("GROUP_ID", "0"))


def valid_http_url(value):
    if not value:
        return False

    try:
        parsed = urlparse(str(value))
        return (
            parsed.scheme in ("https", "http")
            and bool(parsed.netloc)
        )
    except ValueError:
        return False


async def publish_listing(bot, listing):
    required = (
        "id",
        "title",
        "price",
        "details",
        "gift_url",
        "topic",
    )

    if any(not listing.get(key) for key in required):
        raise ValueError("Не хватает обязательных полей листинга")

    topic = listing["topic"]

    if topic not in TOPICS:
        raise ValueError(f"Неизвестная тема: {topic}")

    if GROUP_ID == 0:
        raise ValueError("Не задан GROUP_ID")

    if not valid_http_url(listing["gift_url"]):
        raise ValueError("Некорректная ссылка на подарок")

    owner_url = listing.get("owner_url")

    if owner_url and not valid_http_url(owner_url):
        raise ValueError("Некорректная ссылка владельца")

    # Сначала создаём запись, чтобы не было дублей.
    inserted = insert_listing(listing)

    if not inserted:
        log.info("Повторный листинг пропущен: %s", listing["id"])
        return False

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Занять лог",
            callback_data=f"claim:{listing['id']}",
        )
    ]])

    # details — HTML-фрагмент, сформированный внутренним адаптером.
    text = (
        f"🎁 <b>{escape(str(listing['title']))}</b>\n"
        f"💰 Цена: <b>{escape(str(listing['price']))}</b>\n"
        f"{listing['details']}\n"
        f'<a href="{escape(str(listing["gift_url"]), quote=True)}">'
        "Открыть подарок</a>"
    )

    try:
        message = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=TOPICS[topic] or None,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        set_message_id(listing["id"], message.message_id)

        log.info(
            "Листинг опубликован: %s, message_id=%s",
            listing["id"],
            message.message_id,
        )

        return True

    except TelegramError:
        # Удаляем незавершённую запись, чтобы следующий повтор
        # события мог снова попробовать опубликовать листинг.
        delete_unpublished_listing(listing["id"])
        log.exception("Ошибка публикации листинга %s", listing["id"])
        raise
