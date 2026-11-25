import logging
import os
from html import escape
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from .db import (
    init_db,
    set_worker,
    is_worker,
    get_listing,
    claim_listing,
    release_claim,
)


log = logging.getLogger(__name__)

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}


def valid_url(value):
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


def card(row, hidden=False):
    if hidden:
        return "🔒 <b>ЛОГ ЗАНЯТ</b>\nИнформация скрыта."

    return (
        f"🎁 <b>{escape(str(row['title']))}</b>\n"
        f"💰 Цена: <b>{escape(str(row['price']))}</b>\n"
        f"{row['details']}\n"
        f'<a href="{escape(str(row["gift_url"]), quote=True)}">'
        "Открыть подарок</a>"
    )


def claim_keyboard(listing_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Занять лог",
            callback_data=f"claim:{listing_id}",
        )
    ]])


def private_keyboard(row):
    # В личном сообщении всегда ровно две кнопки.
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Поставить подарок",
                url=row["gift_url"],
            )
        ],
        [
            InlineKeyboardButton(
                "View owner",
                url=row["owner_url"],
            )
        ],
    ])


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "FAME NFT TRACKER\n\n"
        "/worfame — включить личные уведомления.\n"
        "/id — показать ID пользователя и чата.\n"
        "/adminfame — проверить административный доступ."
    )


async def get_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    thread_id = (
        update.effective_message.message_thread_id or 0
    )

    await update.effective_message.reply_text(
        f"Ваш Telegram ID: {user_id}\n"
        f"ID чата: {chat_id}\n"
        f"ID темы: {thread_id}"
    )


async def worfame(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    set_worker(update.effective_user.id)

    await update.effective_message.reply_text(
        "✅ Личные уведомления включены.\n"
        "Теперь можно занимать логи."
    )


async def adminfame(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text(
            "Нет административного доступа."
        )
        return

    await update.effective_message.reply_text(
        "Админ-доступ подтверждён."
    )


async def claim(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not query.data:
        return

    if query.message is None:
        await query.answer(
            "Не удалось определить сообщение.",
            show_alert=True,
        )
        return

    listing_id = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    row = get_listing(listing_id)

    if not row:
        await query.answer(
            "Лот не найден.",
            show_alert=True,
        )
        return

    if not is_worker(user_id):
        await query.answer(
            "Сначала отправьте боту /start, затем /worfame.",
            show_alert=True,
        )
        return

    if not valid_url(row["gift_url"]):
        await query.answer(
            "Ссылка на подарок недействительна.",
            show_alert=True,
        )
        return

    if not valid_url(row["owner_url"]):
        await query.answer(
            "Нет подтверждённой ссылки владельца.",
            show_alert=True,
        )
        return

    # Атомарно закрепляем лог за первым нажавшим.
    if not claim_listing(listing_id, user_id):
        await query.answer(
            "Этот лог уже занят.",
            show_alert=True,
        )
        return

    await query.answer("Лог закреплён за вами.")

    row = get_listing(listing_id)

    # Сразу скрываем подробности в группе.
    try:
        await query.edit_message_text(
            text=card(row, hidden=True),
            parse_mode="HTML",
            reply_markup=None,
            disable_web_page_preview=True,
        )

        log.info(
            "Карточка %s скрыта после занятия пользователем %s",
            listing_id,
            user_id,
        )

    except TelegramError:
        log.exception(
            "Не удалось скрыть карточку %s",
            listing_id,
        )

    # Отправляем подробности в личные сообщения.
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=card(row),
            parse_mode="HTML",
            reply_markup=private_keyboard(row),
            disable_web_page_preview=True,
        )

        log.info(
            "Лог %s успешно отправлен пользователю %s",
            listing_id,
            user_id,
        )

    except TelegramError:
        log.exception(
            "Не удалось отправить лог %s пользователю %s",
            listing_id,
            user_id,
        )

        # Если личная отправка не удалась,
        # возвращаем лог в свободное состояние.
        release_claim(
            listing_id,
            user_id,
        )

        fresh_row = get_listing(listing_id)

        # Восстанавливаем исходную карточку.
        # message_thread_id здесь не передаём:
        # для edit_message_text он не нужен.
        try:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                text=card(fresh_row),
                parse_mode="HTML",
                reply_markup=claim_keyboard(listing_id),
                disable_web_page_preview=True,
            )

            log.info(
                "Карточка %s восстановлена после ошибки DM",
                listing_id,
            )

        except TelegramError:
            log.exception(
                "Не удалось восстановить карточку %s",
                listing_id,
            )

        # Сообщаем пользователю, почему лог не пришёл.
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=(
                    query.message.message_thread_id
                ),
                text=(
                    "Не удалось отправить лог в личные сообщения.\n"
                    "Откройте бота, отправьте /start и повторите занятие."
                ),
            )

        except TelegramError:
            log.exception(
                "Не удалось отправить сообщение об ошибке DM"
            )


def register_handlers(app):
    init_db()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("id", get_id)
    )

    app.add_handler(
        CommandHandler("worfame", worfame)
    )

    app.add_handler(
        CommandHandler("adminfame", adminfame)
    )

    app.add_handler(
        CallbackQueryHandler(
            claim,
            pattern=r"^claim:",
        )
    )
