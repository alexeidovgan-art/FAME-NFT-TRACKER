import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes
from .db import init_db, set_worker, is_worker, get_listing, claim_listing

ADMIN_IDS={int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}

def card(row, hidden=False):
    if hidden:
        return f"🔒 <b>ЛОГ ЗАНЯТ</b>\\nЗанял: <code>{row['claimed_by']}</code>\\nИнформация скрыта."
    return (f"🎁 <b>{row['title']}</b>\\n💰 Цена: <b>{row['price']}</b>\\n"
            f"{row['details']}\\n<a href=\"{row['gift_url']}\">Открыть подарок</a>")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("FAME NFT TRACKER\\n/worfame — включить личные уведомления.\\n/adminfame — проверка админ-доступа.")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id or 0
    await update.effective_message.reply_text(
        f"Ваш Telegram ID: {user_id}\nID чата: {chat_id}\nID темы: {thread_id}\n"
        "Скопируйте нужные значения в настройки проекта."
    )

async def worfame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_worker(update.effective_user.id)
    await update.message.reply_text("Доступ воркера включён.")

async def adminfame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет административного доступа.")
        return
    await update.message.reply_text("Админ-доступ подтверждён. Источник листингов подключается отдельно.")

async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    listing_id=q.data.split(":",1)[1]
    row=get_listing(listing_id)
    if not row:
        await q.answer("Лот не найден.",show_alert=True); return
    if not is_worker(q.from_user.id):
        await q.answer("Сначала отправьте боту /worfame.",show_alert=True); return
    if not claim_listing(listing_id,q.from_user.id):
        await q.answer("Этот лог уже занят.",show_alert=True); return
    row=get_listing(listing_id)
    await q.edit_message_text(card(row,True),parse_mode="HTML")
    personal=card(row)
    buttons=[]
    if row["owner_url"]:
        buttons.append([InlineKeyboardButton("View owner",url=row["owner_url"])])
    await context.bot.send_message(q.from_user.id,personal,parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        disable_web_page_preview=True)

def register_handlers(app):
    init_db()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("id",get_id))
    app.add_handler(CommandHandler("worfame",worfame))
    app.add_handler(CommandHandler("adminfame",adminfame))
    app.add_handler(CallbackQueryHandler(claim,pattern=r"^claim:"))
