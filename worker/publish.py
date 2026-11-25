
import logging
import os
from html import escape
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db import (
    insert_listing,
    delete_unpublished_listing,
    set_message_id,
)


log = logging.getLogger(__name__)


def read_int_env(name, default=0):
    value = os.getenv(name, str(default)).strip()

    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Переменная {name} должна содержать целое число"
        )


GROUP_ID = read_int_env("GROUP_ID")

TOPICS = {
    "cheap": read_int_env("TOPIC_CHEAP"),
    "mid": read_int_env("TOPIC_MID"),
    "expensive": read_int_env("TOPIC_EXPENSIVE"),
    "premium": read_int_env("TOPIC_PREMIUM"),
}


def valid_http_url(value):
    if not value:
        return False

    try:
        parsed = urlparse(str(value))

        if parsed.scheme in ("https", "http"):
            return bool(parsed.netloc)

        if parsed.scheme == "tg" and parsed.netloc == "user":
            return bool(parsed.query)

        return False

    except ValueError:
        return False


def validate_config():
    if GROUP_ID == 0:
        raise RuntimeError("Не задан GROUP_ID")

    for name, topic_id in TOPICS.items():
        if topic_id <= 0:
            raise RuntimeError(
                f"Не задан ID топика для категории {name}"
            )

    log.info(
        "Настройки публикации: GROUP_ID=%s, "
        "Дешёвые=%s, Средние=%s, Дорогие=%s, Премиум=%s",
        GROUP_ID,
        TOPICS["cheap"],
        TOPICS["mid"],
        TOPICS["expensive"],
        TOPICS["premium"],
    )


def make_keyboard(listing_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Занять лог",
            callback_data=f"claim:{listing_id}",
        )
    ]])


def make_text(listing):
    return (
        f"🎁 <b>{escape(str(listing['title']))}</b>\n"
        f"💰 Цена: <b>{escape(str(listing['price']))}</b>\n"
        f"{listing['details']}\n"
        f'<a href="{escape(str(listing["gift_url"]), quote=True)}">'
        "Открыть подарок</a>"
    )


async def publish_listing(bot, listing):
    required = (
        "id",
        "title",
        "price",
        "details",
        "gift_url",
        "topic",
    )

    missing = [
        key for key in required
        if not listing.get(key)
    ]

    if missing:
        raise ValueError(
            "Не хватает полей: " + ", ".join(missing)
        )

    topic = listing["topic"]

    if topic not in TOPICS:
        raise ValueError(f"Неизвестная категория: {topic}")

    if GROUP_ID == 0:
        raise RuntimeError("GROUP_ID равен 0")

    if not valid_http_url(listing["gift_url"]):
        raise ValueError("Некорректная ссылка на подарок")

    owner_url = listing.get("owner_url")

    if owner_url and not valid_http_url(owner_url):
        raise ValueError("Некорректная ссылка владельца")

    inserted = insert_listing(listing)

    if not inserted:
        log.info(
            "Листинг уже опубликован или занят: %s",
            listing["id"],
        )
        return False

    try:
        message = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=TOPICS[topic],
            text=make_text(listing),
            parse_mode="HTML",
            reply_markup=make_keyboard(listing["id"]),
            disable_web_page_preview=True,
        )

        set_message_id(
            listing["id"],
            message.message_id,
        )

        log.info(
            "УСПЕШНАЯ ПУБЛИКАЦИЯ: id=%s, категория=%s, "
            "топик=%s, message_id=%s",
            listing["id"],
            topic,
            TOPICS[topic],
            message.message_id,
        )

        return True

    except Exception:
        delete_unpublished_listing(listing["id"])

        log.exception(
            "НЕ УДАЛОСЬ ОТПРАВИТЬ ЛИСТИНГ: id=%s, группа=%s, топик=%s",
            listing["id"],
            GROUP_ID,
            TOPICS[topic],
        )

        raise


validate_config()
