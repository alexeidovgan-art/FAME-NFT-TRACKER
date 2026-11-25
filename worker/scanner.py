
import asyncio
import html
import logging
import os
import re

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
)
from telethon.tl.types import PeerUser

from bot.db import replace_collection_offers, find_floor
from worker.publish import publish_listing


log = logging.getLogger(__name__)

PAGE_LIMIT = 100
MAX_PAGES = 1000
REQUEST_RETRIES = 5
REQUEST_DELAYS = (2, 4, 8, 16, 30)


def get_scan_interval():
    try:
        return max(60, int(os.getenv("SCAN_INTERVAL", "300")))
    except (TypeError, ValueError):
        return 300


SCAN_INTERVAL = get_scan_interval()


def topic_for(floor):
    if floor is None:
        return "cheap"
    if floor < 10:
        return "cheap"
    if floor < 50:
        return "mid"
    if floor < 150:
        return "expensive"
    return "premium"


def is_temporary_error(error):
    name = type(error).__name__
    return (
        name in {
            "RpcMcgetFailError",
            "RpcCallFailError",
            "ServerError",
            "TimedOutError",
            "TimeoutError",
            "ConnectionError",
        }
        or isinstance(error, (asyncio.TimeoutError, OSError, ConnectionError))
    )


async def telegram_request(client, request, description):
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            return await client(request)

        except FloodWaitError as error:
            wait_seconds = int(error.seconds) + 1
            log.warning("%s: FloodWait %s сек.", description, wait_seconds)
            await asyncio.sleep(wait_seconds)

        except asyncio.CancelledError:
            raise

        except Exception as error:
            if not is_temporary_error(error):
                raise

            if attempt >= REQUEST_RETRIES:
                log.exception("%s: запрос не выполнен", description)
                raise

            delay = REQUEST_DELAYS[attempt - 1]
            log.warning(
                "%s: ошибка %s, попытка %s/%s через %s сек.",
                description,
                type(error).__name__,
                attempt,
                REQUEST_RETRIES,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"{description}: превышено число попыток")


def is_offchain(gift):
    """
    Единственная причина пропуска:
    Telegram вернул блокчейн-адрес самого подарка
    или адрес владельца.
    host_id отдельно не фильтруем.
    """
    return not bool(
        getattr(gift, "gift_address", None)
        or getattr(gift, "owner_address", None)
    )


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
    ton_nanos = None
    stars_price = None

    for amount in getattr(gift, "resell_amount", []) or []:
        kind = type(amount).__name__.lower()

        if "ton" in kind:
            raw = getattr(amount, "amount", None)
            if raw is not None and int(raw) > 0:
                ton_nanos = int(raw)

        elif "stars" in kind:
            whole = int(getattr(amount, "amount", 0) or 0)
            nanos = int(getattr(amount, "nanos", 0) or 0)
            value = whole + nanos / 1_000_000_000

            if value > 0:
                stars_price = value

    if ton_nanos is not None:
        return (
            f"{ton_nanos / 1_000_000_000:g} TON",
            ton_nanos,
        )

    if stars_price is not None:
        return f"{stars_price:g} ⭐", None

    return "Цена не определена", None


def get_owner(gift, users_by_id):
    peer = getattr(gift, "owner_id", None)

    if not isinstance(peer, PeerUser):
        return {
            "name": "Неизвестно",
            "url": None,
        }

    owner_id = peer.user_id
    user = users_by_id.get(owner_id)

    username = getattr(user, "username", None) if user else None

    full_name = " ".join(
        part for part in (
            getattr(user, "first_name", None) if user else None,
            getattr(user, "last_name", None) if user else None,
        ) if part
    ).strip()

    if username:
        username = str(username).lstrip("@")

        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return {
                "name": full_name or username,
                "url": f"https://t.me/{username}",
            }

    return {
        "name": full_name or f"Пользователь {owner_id}",
        "url": f"tg://user?id={owner_id}",
    }


def save_collection_snapshot(collection, gifts):
    offers = []

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

        offers.append({
            "gift_id": str(slug),
            "price": ton_nanos,
            "model": attrs["model"],
            "backdrop": attrs["backdrop"],
            "pattern": attrs["pattern"],
        })

    replace_collection_offers(collection, offers)

    log.info(
        "Коллекция %s: предложений для TON-floor=%s",
        collection,
        len(offers),
    )


async def publish_collection(bot, collection, gifts, users_by_id):
    published = 0
    skipped_blockchain = 0
    failed = 0

    for gift in gifts:
        # Никаких других фильтров исключения лота.
        if not is_offchain(gift):
            skipped_blockchain += 1
            continue

        slug = (
            getattr(gift, "slug", None)
            or getattr(gift, "id", None)
        )

        if not slug:
            slug = f"{collection}-{getattr(gift, 'num', 'unknown')}"

        slug = str(slug)

        owner = get_owner(gift, users_by_id)
        attrs = get_attributes(gift)

        floor, scope = find_floor(
            collection=collection,
            model=attrs["model"],
            backdrop=attrs["backdrop"],
            pattern=attrs["pattern"],
        )

        price, _ = get_prices(gift)

        title = str(
            getattr(gift, "title", None)
            or getattr(gift, "name", None)
            or f"Подарок {slug}"
        )

        floor_text = (
            f"{floor:.4f} TON"
            if floor is not None
            else "Не определён"
        )

        owner_url = owner["url"]

        if owner_url:
            owner_line = (
                f'👤 Owner: <a href="{html.escape(owner_url, quote=True)}">'
                f'{html.escape(owner["name"])}</a>'
            )
        else:
            owner_line = (
                f'👤 Owner: {html.escape(owner["name"])} '
                '(ссылка не получена Telegram)'
            )

        details = (
            f"📉 Floor Price: <b>{floor_text}</b>\n"
            f"🔎 Совпадение: {html.escape(str(scope or '—'))}\n"
            f"🎨 Model: {html.escape(str(attrs['model'] or 'Неизвестно'))}\n"
            f"🖼 Backdrop: {html.escape(str(attrs['backdrop'] or 'Неизвестно'))}\n"
            f"🔣 Pattern: {html.escape(str(attrs['pattern'] or 'Неизвестно'))}\n"
            f"{owner_line}\n"
            f"📊 Источник: официальный маркет Telegram"
        )

        listing = {
            "id": f"tg:{slug}",
            "title": title,
            "price": price,
            "details": details,
            "gift_url": f"https://t.me/nft/{slug}",
            "owner_url": owner_url or f"https://t.me/nft/{slug}",
            "topic": topic_for(floor),
        }

        try:
            result = await publish_listing(bot, listing)

            if result:
                published += 1

        except asyncio.CancelledError:
            raise

        except Exception:
            failed += 1
            log.exception("Ошибка публикации подарка %s", slug)

    log.info(
        "ИТОГ КОЛЛЕКЦИИ %s: получено=%s, опубликовано=%s, "
        "пропущено blockchain=%s, ошибок отправки=%s",
        collection,
        len(gifts),
        published,
        skipped_blockchain,
        failed,
    )


async def scan_collection(client, bot, base):
    collection = str(base.id)

    gifts = []
    users_by_id = {}

    offset = ""
    page = 0

    while True:
        page += 1

        if page > MAX_PAGES:
            raise RuntimeError(
                f"Коллекция {collection}: превышен лимит страниц"
            )

        request = GetResaleStarGiftsRequest(
            sort_by_price=True,
            gift_id=base.id,
            offset=offset,
            limit=PAGE_LIMIT,
        )

        response = await telegram_request(
            client,
            request,
            f"Коллекция {collection}, страница {page}",
        )

        page_gifts = getattr(response, "gifts", []) or []
        gifts.extend(page_gifts)

        for user in getattr(response, "users", []) or []:
            users_by_id[user.id] = user

        next_offset = getattr(response, "next_offset", None)

        log.info(
            "Коллекция %s: страница %s, получено %s подарков",
            collection,
            page,
            len(page_gifts),
        )

        if not next_offset:
            break

        if next_offset == offset:
            raise RuntimeError(
                f"Коллекция {collection}: повторный offset"
            )

        offset = next_offset
        await asyncio.sleep(0.15)

    # Снимок обновляется после полного получения всех страниц.
    save_collection_snapshot(collection, gifts)

    await publish_collection(
        bot,
        collection,
        gifts,
        users_by_id,
    )

    return len(gifts)


async def scan_once(client, bot):
    log.info("========== НАЧАЛО СКАНИРОВАНИЯ ==========")

    catalog = await telegram_request(
        client,
        GetStarGiftsRequest(hash=0),
        "Каталог Telegram",
    )

    base_gifts = getattr(catalog, "gifts", []) or []

    resale_gifts = [
        gift for gift in base_gifts
        if getattr(gift, "availability_resale", None)
    ]

    log.info(
        "Каталог Telegram получен: всего типов=%s, resale=%s",
        len(base_gifts),
        len(resale_gifts),
    )

    if not resale_gifts:
        log.warning("В каталоге не найдено типов подарков resale")
        return

    scanned = 0
    successful = 0
    failed = 0

    for base in resale_gifts:
        try:
            count = await scan_collection(client, bot, base)
            scanned += count
            successful += 1

        except asyncio.CancelledError:
            raise

        except FloodWaitError:
            raise

        except Exception:
            failed += 1
            log.exception("Ошибка коллекции %s", base.id)

        await asyncio.sleep(0.2)

    log.info(
        "СКАНИРОВАНИЕ ЗАВЕРШЕНО: коллекций=%s, ошибок=%s, подарков=%s",
        successful,
        failed,
        scanned,
    )


async def run_scanner(bot):
    session_string = os.getenv("TG_SESSION", "").strip()
    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()

    if not session_string or not api_id or not api_hash:
        log.error(
            "Сканер не запущен: проверь TG_SESSION, API_ID и API_HASH"
        )
        return

    try:
        api_id = int(api_id)
    except ValueError:
        log.error("API_ID должен быть числом")
        return

    log.info(
        "Сканер настроен. Интервал: %s секунд",
        SCAN_INTERVAL,
    )

    while True:
        client = None

        try:
            client = TelegramClient(
                StringSession(session_string),
                api_id,
                api_hash,
            )

            client.flood_sleep_threshold = 60

            log.info("Подключение к официальному Telegram API...")
            await client.connect()

            if not await client.is_user_authorized():
                log.error("TG_SESSION не авторизована")
                return

            me = await client.get_me()

            log.info(
                "Официальный Telegram-сканер подключён: %s",
                getattr(me, "username", None) or me.id,
            )

            while True:
                if not client.is_connected():
                    raise ConnectionError("Соединение Telethon потеряно")

                started = asyncio.get_running_loop().time()

                try:
                    await scan_once(client, bot)

                except asyncio.CancelledError:
                    raise

                except FloodWaitError as error:
                    wait_seconds = int(error.seconds) + 1
                    log.warning("FloodWait: %s секунд", wait_seconds)
                    await asyncio.sleep(wait_seconds)
                    continue

                except Exception:
                    log.exception("Ошибка цикла сканирования")
                    await asyncio.sleep(20)
                    continue

                elapsed = (
                    asyncio.get_running_loop().time() - started
                )

                wait_time = max(30, SCAN_INTERVAL - elapsed)

                log.info(
                    "Следующий цикл через %.0f секунд",
                    wait_time,
                )

                await asyncio.sleep(wait_time)

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception("Сканер отключился")

        finally:
            if client and client.is_connected():
                await client.disconnect()

        log.warning("Переподключение через 15 секунд")
        await asyncio.sleep(15)
