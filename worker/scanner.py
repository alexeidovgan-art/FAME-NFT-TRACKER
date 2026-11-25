
import asyncio
import html
import logging
import os
from urllib.parse import quote

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
)
from telethon.tl.types import PeerUser

from bot.db import conn, save_market_offer, find_floor
from worker.publish import publish_listing


log = logging.getLogger(__name__)

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
PAGE_LIMIT = 100


def topic_for(floor):
    if floor < 10:
        return "cheap"
    if floor < 50:
        return "mid"
    if floor < 150:
        return "expensive"
    return "premium"


def get_attributes(gift):
    result = {
        "model": None,
        "backdrop": None,
        "pattern": None,
    }

    for attribute in getattr(gift, "attributes", []) or []:
        kind = type(attribute).__name__.lower()
        name = getattr(attribute, "name", None)

        if not name:
            continue

        if "model" in kind:
            result["model"] = str(name)
        elif "backdrop" in kind:
            result["backdrop"] = str(name)
        elif "pattern" in kind:
            result["pattern"] = str(name)

    return result


def get_prices(gift):
    ton_price = None
    stars_price = None

    for amount in getattr(gift, "resell_amount", []) or []:
        kind = type(amount).__name__.lower()

        if "ton" in kind:
            raw = getattr(amount, "amount", None)

            if raw is not None and int(raw) > 0:
                ton_price = int(raw)

        elif "stars" in kind:
            whole = int(getattr(amount, "amount", 0) or 0)
            nanos = int(getattr(amount, "nanos", 0) or 0)

            value = whole + nanos / 1_000_000_000

            if value > 0:
                stars_price = value

    if ton_price is not None:
        return (
            f"{ton_price / 1_000_000_000:g} TON",
            ton_price,
        )

    if stars_price is not None:
        return f"{stars_price:g} ⭐", None

    return None, None


def is_offchain(gift):
    return not (
        getattr(gift, "gift_address", None)
        or getattr(gift, "owner_address", None)
    )


def get_public_owner(gift, users_by_id):
    owner_peer = getattr(gift, "owner_id", None)

    if not isinstance(owner_peer, PeerUser):
        return None

    user = users_by_id.get(owner_peer.user_id)

    if user is None:
        return None

    username = getattr(user, "username", None)

    if not username:
        return None

    username = str(username).lstrip("@")

    if not username or not username.replace("_", "").isalnum():
        return None

    full_name = " ".join(
        part
        for part in (
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        )
        if part
    ).strip()

    return {
        "owner_url": f"https://t.me/{username}",
        "owner_name": full_name or username,
    }


def replace_collection_snapshot(collection, gifts):
    with conn() as db:
        db.execute(
            """
            UPDATE market_offers
            SET active=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE market='telegram'
              AND collection=?
            """,
            (collection,),
        )

    for gift in gifts:
        if not is_offchain(gift):
            continue

        _, ton_nanos = get_prices(gift)

        if ton_nanos is None:
            continue

        slug = getattr(gift, "slug", None)

        if not slug:
            continue

        attrs = get_attributes(gift)

        save_market_offer(
            market="telegram",
            gift_id=str(slug),
            collection=collection,
            model=attrs["model"],
            backdrop=attrs["backdrop"],
            pattern=attrs["pattern"],
            currency="gram",
            price=ton_nanos,
        )


async def publish_collection(bot, collection, gifts, users_by_id):
    for gift in gifts:
        if not is_offchain(gift):
            continue

        slug = getattr(gift, "slug", None)

        if not slug:
            continue

        owner = get_public_owner(gift, users_by_id)

        if not owner:
            log.info(
                "Пропуск %s: владелец не подтверждён",
                slug,
            )
            continue

        attrs = get_attributes(gift)

        floor, scope = find_floor(
            collection=collection,
            model=attrs["model"],
            backdrop=attrs["backdrop"],
            pattern=attrs["pattern"],
            exclude_id=slug,
        )

        if floor is None:
            log.info("Нет floor для %s", slug)
            continue

        listing_price, _ = get_prices(gift)

        if not listing_price:
            continue

        title = str(
            getattr(gift, "title", None) or collection
        )

        model = attrs["model"] or "—"
        backdrop = attrs["backdrop"] or "—"
        pattern = attrs["pattern"] or "—"

        details = (
            f"📉 Floor Price: <b>{floor:.4f} TON</b>\n"
            f"🔎 Совпадение: {html.escape(str(scope or '—'))}\n"
            f"🎨 Model: {html.escape(model)}\n"
            f"🖼 Backdrop: {html.escape(backdrop)}\n"
            f"🔣 Pattern: {html.escape(pattern)}\n"
            f"👤 Owner: <a href=\"{owner['owner_url']}\">"
            f"{html.escape(owner['owner_name'])}</a>\n"
            f"📊 Источник: официальный маркет Telegram"
        )

        listing = {
            "id": f"tg:{slug}",
            "title": title,
            "price": listing_price,
            "details": details,
            "gift_url": (
                f"https://t.me/nft/{quote(str(slug), safe='-')}"
            ),
            "owner_url": owner["owner_url"],
            "topic": topic_for(floor),
        }

        try:
            await publish_listing(bot, listing)
        except Exception:
            log.exception(
                "Ошибка публикации подарка %s",
                slug,
            )


async def scan_once(client, bot):
    catalog = await client(
        GetStarGiftsRequest(hash=0)
    )

    base_gifts = getattr(catalog, "gifts", []) or []

    # Сбрасываем старые предложения от прежнего сканера.
    with conn() as db:
        db.execute(
            """
            UPDATE market_offers
            SET active=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE market='telegram'
            """
        )

    scanned = 0
    scanned_types = 0

    for base in base_gifts:
        if not getattr(base, "availability_resale", None):
            continue

        collection = str(
            getattr(base, "title", None)
            or f"gift_{base.id}"
        )

        try:
            gifts = []
            users_by_id = {}
            offset = ""

            while True:
                response = await client(
                    GetResaleStarGiftsRequest(
                        sort_by_price=True,
                        gift_id=base.id,
                        offset=offset,
                        limit=PAGE_LIMIT,
                    )
                )

                gifts.extend(
                    getattr(response, "gifts", []) or []
                )

                for user in getattr(response, "users", []) or []:
                    users_by_id[user.id] = user

                next_offset = getattr(
                    response,
                    "next_offset",
                    None,
                )

                if not next_offset or next_offset == offset:
                    break

                offset = next_offset

            replace_collection_snapshot(
                collection,
                gifts,
            )

            await publish_collection(
                bot,
                collection,
                gifts,
                users_by_id,
            )

            scanned += len(gifts)
            scanned_types += 1

            await asyncio.sleep(0.25)

        except FloodWaitError:
            raise

        except Exception:
            log.exception(
                "Ошибка коллекции %s",
                collection,
            )

    log.info(
        "Сканирование завершено: коллекций %s, подарков %s",
        scanned_types,
        scanned,
    )


async def run_scanner(bot):
    session_string = os.getenv(
        "TG_SESSION", ""
    ).strip()

    api_id = os.getenv(
        "API_ID", ""
    ).strip()

    api_hash = os.getenv(
        "API_HASH", ""
    ).strip()

    if not session_string or not api_id or not api_hash:
        log.error(
            "Не заданы TG_SESSION, API_ID или API_HASH"
        )
        return

    client = None

    try:
        client = TelegramClient(
            StringSession(session_string),
            int(api_id),
            api_hash,
        )

        client.flood_sleep_threshold = 60

        await client.connect()

        if not await client.is_user_authorized():
            log.error(
                "TG_SESSION не авторизована. Сканер остановлен."
            )
            return

        me = await client.get_me()

        log.info(
            "Официальный Telegram-сканер подключён: %s",
            getattr(me, "username", None)
            or getattr(me, "id", "user"),
        )

        while True:
            started = asyncio.get_running_loop().time()

            try:
                await scan_once(client, bot)

            except FloodWaitError as error:
                wait_seconds = int(error.seconds) + 1

                log.warning(
                    "Telegram FloodWait: пауза %s секунд",
                    wait_seconds,
                )

                await asyncio.sleep(wait_seconds)

            except Exception:
                log.exception(
                    "Ошибка цикла сканирования"
                )

            elapsed = (
                asyncio.get_running_loop().time() - started
            )

            await asyncio.sleep(
                max(30, SCAN_INTERVAL - elapsed)
            )

    except asyncio.CancelledError:
        raise

    except Exception:
        log.exception(
            "Ошибка запуска официального сканера"
        )

    finally:
        if client and client.is_connected():
            await client.disconnect()
