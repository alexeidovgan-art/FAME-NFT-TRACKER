"""Adapter point for a permitted, documented public listing feed."""
import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from bot.db import conn

TOPICS={
 "cheap":int(os.getenv("TOPIC_CHEAP","0")),
 "mid":int(os.getenv("TOPIC_MID","0")),
 "expensive":int(os.getenv("TOPIC_EXPENSIVE","0")),
 "premium":int(os.getenv("TOPIC_PREMIUM","0"))
}
GROUP_ID=int(os.getenv("GROUP_ID","0"))

async def publish_listing(bot, listing):
    required=("id","title","price","details","gift_url","topic")
    if any(not listing.get(k) for k in required):
        raise ValueError("Не хватает обязательных полей")
    if listing["topic"] not in TOPICS:
        raise ValueError("Неизвестный ценовой сегмент")
    with conn() as db:
        cur=db.execute("""INSERT OR IGNORE INTO listings
          (id,title,price,details,gift_url,owner_url,topic)
          VALUES(?,?,?,?,?,?,?)""",
          (listing["id"],listing["title"],listing["price"],listing["details"],
           listing["gift_url"],listing.get("owner_url"),listing["topic"]))
        if cur.rowcount != 1:
            return False
    keyboard=InlineKeyboardMarkup([[
        InlineKeyboardButton("Занять лог",callback_data=f"claim:{listing['id']}")
    ]])
    msg=await bot.send_message(
      chat_id=GROUP_ID,
      message_thread_id=TOPICS[listing["topic"]] or None,
      text=f"🎁 <b>{listing['title']}</b>\\n💰 Цена: <b>{listing['price']}</b>\\n{listing['details']}\\n<a href=\"{listing['gift_url']}\">Открыть подарок</a>",
      parse_mode="HTML",reply_markup=keyboard,disable_web_page_preview=True)
    with conn() as db:
        db.execute("UPDATE listings SET message_id=? WHERE id=?",(msg.message_id,listing["id"]))
    return True
