
import os
import logging
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
)

logger = logging.getLogger(__name__)

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}


def valid_url(value, allow_telegram=False):
    if not value:
        return False

    value = str(value).strip()
    parsed = urlparse(value)

    if parsed.scheme in ("https", "http") and parsed.netloc:
        return True

    if allow_telegram and parsed.scheme == "tg":
        return True

    return False


def card(row, hidden=False):
    if hidden:
        return (
            "🔒 <b>ЛОГ ЗАНЯТ</b>\n"
            "Информация скрыта."
        )

    return (
        f"🎁 <b>{escape(str(row['title']))}</b>\n"
        f"💰 Цена: <b>{escape(str(row['price']))}</b>\n"
        f"{escape(str(row['details']))}\n"
        f'<a href="{escape(str(row["gift_url"]), quote=True)}">'
        "Открыть подарок</a>"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "FAME NFT TRACKER\n"
        "/worfame — включить личные уведомления.\n"
        "/adminfame — проверка админ-доступа."
    )


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id or 0

    await update.effective_message.reply_text(
        f"Ваш Telegram ID: {user_id}\n"
        f"ID чата: {chat_id}\n"
        f"ID темы: {thread_id}\n"
        "Скопируйте нужные значения в настройки проекта."
    )


async def worfame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Сохраняем пользователя в базе.
    # Повторное включение не создаёт дубликат.
    set_worker(user_id)

    await update.message.reply_text(
        "✅ Личные уведомления включены.\n"
        "Повторно вводить /worfame не нужно."
    )


async def adminfame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет административного доступа.")
        return

    await update.message.reply_text(
        "Админ-доступ подтверждён."
    )


async def claim(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not q or not q.data:
        return

    listing_id = q.data.split(":", 1)[1]

    row = get_listing(listing_id)

    if not row:
        await q.answer("Лот не найден.", show_alert=True)
        return

    if not is_worker(q.from_user.id):
        await q.answer(
            "Сначала отправьте боту /worfame.",
            show_alert=True,
        )
        return

    # Проверяем обе ссылки до занятия лога.
    if not valid_url(row["gift_url"]):
        await q.answer(
            "У подарка нет действительной ссылки.",
            show_alert=True,
        )
        return

    if not valid_url(row["owner_url"], allow_telegram=True):
        await q.answer(
            "Не удалось подтвердить ссылку на профиль владельца. "
            "Лог не занят.",
            show_alert=True,
        )
        return

    # Атомарное занятие: только один пользователь сможет забрать лог.
    if not claim_listing(listing_id, q.from_user.id):
        await q.answer(
            "Этот лог уже занят.",
            show_alert=True,
        )
        return

    # Подтверждаем нажатие один раз.
    await q.answer("Лог закреплён за вами.")

    row = get_listing(listing_id)

    # Сразу скрываем данные для всех участников группы.
    try:
        await q.edit_message_text(
            text=card(row, hidden=True),
            parse_mode="HTML",
            reply_markup=None,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception(
            "Не удалось скрыть сообщение лога %s",
            listing_id,
        )

    # Ровно две кнопки в личном сообщении.
    keyboard = InlineKeyboardMarkup([
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

    try:
        await context.bot.send_message(
            chat_id=q.from_user.id,
            text=card(row),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        logger.info(
            "Лог %s занят пользователем %s",
            listing_id,
            q.from_user.id,
        )

    except TelegramError:
        logger.exception(
            "Не удалось отправить личное сообщение: "
            "лог %s, пользователь %s",
            listing_id,
            q.from_user.id,
        )


def register_handlers(app):
    init_db()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("worfame", worfame))
    app.add_handler(CommandHandler("adminfame", adminfame))

    app.add_handler(
        CallbackQueryHandler(
            claim,
            pattern=r"^claim:",
        )
    )
