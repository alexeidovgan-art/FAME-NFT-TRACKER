
import asyncio
import json
import logging
import os
import sqlite3

import websockets

from worker.publish import publish_listing

log = logging.getLogger(__name__)

WS_URL = "wss://live.see.tg/v1/ws"
DB = os.getenv("DATABASE_PATH", "data.sqlite3")

_logged_unknown_shapes = set()


def db():
    con = sqlite3.connect(DB, timeout=20)
    con.execute("""
        CREATE TABLE IF NOT EXISTS market_offers (
            market TEXT,
            gift_id TEXT,
            collection TEXT,
            model TEXT,
            backdrop TEXT,
            pattern TEXT,
            price_ton REAL,
            active INTEGER DEFAULT 1,
            PRIMARY KEY (market, gift_id)
        )
    """)
    return con


def ton_price(price):
    if not isinstance(price, dict):
        return None

    try:
        amount = float(price["amount"])
    except (KeyError, TypeError, ValueError):
        return None

    currency = str(price.get("currency", "")).lower()

    if currency == "gram":
        return amount / 1_000_000_000

    if currency == "ton":
        return amount

    return None


def save_offer(event):
    gift = event.get("gift") or {}
    market = str(event.get("market", "")).lower()
    gift_id = gift.get("id")

    if not market or gift_id is None:
        return

    con = db()
    try:
        con.execute("""
            INSERT INTO market_offers
            (market,gift_id,collection,model,backdrop,pattern,price_ton,active)
            VALUES(?,?,?,?,?,?,?,1)
            ON CONFLICT(market,gift_id) DO UPDATE SET
            price_ton=excluded.price_ton,
            active=1
        """, (
            market,
            str(gift_id),
            gift.get("slug") or gift.get("collection"),
            gift.get("model"),
            gift.get("backdrop"),
            gift.get("pattern") or gift.get("symbol"),
            ton_price(event.get("price"))
        ))
        con.commit()
    finally:
        con.close()


def remove_offer(event):
    gift = event.get("gift") or {}

    con = db()
    try:
        con.execute(
            "UPDATE market_offers SET active=0 WHERE market=? AND gift_id=?",
            (
                str(event.get("market", "")).lower(),
                str(gift.get("id"))
            )
        )
        con.commit()
    finally:
        con.close()


def find_floor(gift, exclude_id):
    collection = gift.get("slug") or gift.get("collection")
    model = gift.get("model")
    backdrop = gift.get("backdrop")
    pattern = gift.get("pattern") or gift.get("symbol")

    tiers = []

    if collection and model and backdrop and pattern:
        tiers.append(("model + backdrop + pattern", 4))

    if collection and model and backdrop:
        tiers.append(("model + backdrop", 3))

    if collection and model:
        tiers.append(("model", 2))

    if collection:
        tiers.append(("collection", 1))

    con = db()

    try:
        for label, count in tiers:
            conditions = [
                "active=1",
                "price_ton IS NOT NULL",
                "gift_id<>?"
            ]

            values = [str(exclude_id)]

            fields = [
                ("collection", collection),
                ("model", model),
                ("backdrop", backdrop),
                ("pattern", pattern)
            ]

            for field, value in fields[:count]:
                conditions.append(f"{field}=?")
                values.append(value)

            row = con.execute(
                "SELECT MIN(price_ton) FROM market_offers WHERE "
                + " AND ".join(conditions),
                values
            ).fetchone()

            if row and row[0] is not None:
                return row[0], label

        return None, None

    finally:
        con.close()


def topic_for(price):
    if price < 10:
        return "cheap"

    if price < 50:
        return "mid"

    if price < 150:
        return "expensive"

    return "premium"


def get_value(objects, names):
    for obj in objects:
        if not isinstance(obj, dict):
            continue

        for name in names:
            if name in obj:
                return obj[name], True

    return None, False


def is_confirmed_regular_user_gift(event):
    gift = event.get("gift") or {}

    objects = (gift, event)

    on_chain, has_on_chain = get_value(
        objects,
        ("was_on_chain", "is_on_chain", "on_chain")
    )

    owner_type, has_owner_type = get_value(
        objects,
        ("owner_type", "current_owner_type")
    )

    # Fail closed: publish only when both facts are explicitly confirmed.
    off_chain = has_on_chain and on_chain is False

    regular_user = (
        has_owner_type
        and str(owner_type).lower() == "user"
    )

    if not (off_chain and regular_user):
        shape = (
            tuple(sorted(gift.keys())),
            has_on_chain,
            repr(on_chain),
            has_owner_type,
            str(owner_type)
        )

        if shape not in _logged_unknown_shapes:
            _logged_unknown_shapes.add(shape)

            log.warning(
                "Лот пропущен: статус подарка не подтверждён. "
                "gift_keys=%s on_chain=%s owner_type=%s",
                sorted(gift.keys()),
                on_chain if has_on_chain else "missing",
                owner_type if has_owner_type else "missing"
            )

        return False

    return True


async def handle_event(bot, data):
    if data.get("type") != "event":
        return

    kind = data.get("event")
    market = str(data.get("market", "")).lower()

    if kind in ("listing", "price"):
        save_offer(data)

    elif kind in ("delisting", "sale"):
        remove_offer(data)
        return

    else:
        return

    # Only the built-in Telegram marketplace.
    if kind != "listing" or market != "telegram":
        return

    # Do not publish blockchain gifts or gifts with unknown status.
    if not is_confirmed_regular_user_gift(data):
        return

    gift = data.get("gift") or {}

    gift_id = gift.get("id")
    slug = gift.get("slug")
    number = gift.get("num")

    if gift_id is None or not slug or number is None:
        return

    floor, scope = find_floor(gift, gift_id)

    if floor is None:
        log.info("Нет подтверждённого флора для %s", gift_id)
        return

    price = data.get("price") or {}

    currency = str(price.get("currency", "")).lower()
    amount = price.get("amount", "?")

    if currency == "gram":
        listing_price = (
            f"{float(amount) / 1_000_000_000:g} TON"
        )

    elif currency == "ton":
        listing_price = f"{float(amount):g} TON"

    elif currency == "xtr":
        listing_price = f"{amount} ⭐"

    else:
        log.info("Неизвестная валюта листинга: %s", currency)
        return

    title = gift.get("title") or slug

    details = (
        f"📉 Floor Price: <b>{floor:.4f} TON</b>\n"
        f"🔎 Совпадение: {scope}\n"
        f"🎨 Model: {gift.get('model') or '—'}\n"
        f"🖼 Backdrop: {gift.get('backdrop') or '—'}\n"
        f"🔣 Pattern: "
        f"{gift.get('pattern') or gift.get('symbol') or '—'}\n"
        f"📊 Data: see.tg"
    )

    listing = {
        "id": f"see:{market}:{gift_id}",
        "title": title,
        "price": listing_price,
        "details": details,
        "gift_url": f"https://t.me/nft/{slug}-{number}",
        "topic": topic_for(floor)
    }

    await publish_listing(bot, listing)


async def run_scanner(bot):
    while True:
        try:
            log.info("Подключаюсь к see.tg...")

            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=30
            ) as ws:

                await ws.send(json.dumps({
                    "type": "subscribe",
                    "events": ["all"]
                }))

                log.info("Подписка see.tg отправлена")

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        await handle_event(bot, data)

                    except Exception:
                        log.exception("Ошибка обработки события")

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception(
                "Поток отключился. Повтор через 8 секунд"
            )
            await asyncio.sleep(8)
