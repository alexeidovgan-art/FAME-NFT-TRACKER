
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
from telegram.error import RetryAfter

from bot.db import replace_collection_offers, find_floor
from worker.publish import publish_listing


log = logging.getLogger(__name__)

PAGE_LIMIT = 100
MAX_PAGES = 1000
PUBLISH_EVERY_PAGES = 100
PUBLISH_DELAY_SECONDS = 3.2
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


def is_offchain(gift):
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


def extract_owner_id(owner):
    if owner is None:
        return None

    if isinstance(owner, PeerUser):
        return int(owner.user_id)

    user_id = getattr(owner, "user_id", None)
    if user_id is not None:
        return int(user_id)

    user_id = getattr(owner, "id", None)
    if user_id is not None:
        return int(user_id)

    if isinstance(owner, int):
        return owner

    return None


def get_owner(gift, users_by_id):
    peer = getattr(gift, "owner_id", None)
    owner_id = extract_owner_id(peer)

    if owner_id is None:
        return {
            "id": None,
            "name": (
                getattr(gift, "owner_name", None)
                or "Владелец не передан Telegram"
            ),
            "username": None,
            "url": None,
        }

    user = users_by_id.get(owner_id)

    username = (
        getattr(user, "username", None)
        if user else None
    )

    first_name = (
        getattr(user, "first_name", None)
        if user else None
    )
    last_name = (
        getattr(user, "last_name", None)
        if user else None
    )

    full_name = " ".join(
        part for part in (first_name, last_name) if part
    ).strip()

    gift_owner_name = getattr(gift, "owner_name", None)
    display_name = (
        full_name
        or gift_owner_name
        or (f"@{username}" if username else None)
        or f"Пользователь {owner_id}"
    )

    if username:
        username = str(username).lstrip("@")

        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            profile_url = f"https://t.me/{username}"
        else:
            profile_url = f"tg://user?id={owner_id}"
    else:
        profile_url = f"tg://user?id={owner_id}"

    return {
        "id": owner_id,
        "name": str(display_name),
        "username": username,
        "url": profile_url,
    }


def is_temporary_error(error):
    names = {
        "RpcMcgetFailError",
        "RpcCallFailError",
        "ServerError",
        "TimedOutError",
        "TimeoutError",
        "ConnectionError",
    }

    return (
        type(error).__name__ in names
        or isinstance(
            error,
            (asyncio.TimeoutError, OSError, ConnectionError),
        )
    )


async def telegram_request(client, request, description):
    attempt = 0

    while True:
        attempt += 1

        try:
            return await client(request)

        except FloodWaitError as error:
            wait_seconds = int(error.seconds) + 1
            log.warning(
                "%s: Telegram FloodWait, ждём %s сек.",
                description,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

        except asyncio.CancelledError:
            raise

        except Exception as error:
            if not is_temporary_error(error):
                raise

            if attempt >= REQUEST_RETRIES:
                log.exception(
                    "%s: запрос не выполнен после %s попыток",
                    description,
                    attempt,
                )
                raise

            delay = REQUEST_DELAYS[attempt - 1]
            log.warning(
                "%s: %s, повтор через %s сек. (%s/%s)",
                description,
                type(error).__name__,
                delay,
                attempt,
                REQUEST_RETRIES,
            )
            await asyncio.sleep(delay)


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

        # ВАЖНО: база хранит цену именно в TON,
        # а Telegram отдаёт её в нанотонах.
        ton_price = ton_nanos / 1_000_000_000

        offers.append({
            "gift_id": str(slug),
            "price": ton_price,
            "model": attrs["model"],
            "backdrop": attrs["backdrop"],
            "pattern": attrs["pattern"],
        })

    replace_collection_offers(str(collection), offers)

    log.info(
        "FLOOR-БАЗА: коллекция=%s, TON-предложений=%s",
        collection,
        len(offers),
    )


async def publish_collection(
    bot,
    collection,
    gifts,
    users_by_id,
    batch_number,
):
    published = 0
    skipped_blockchain = 0
    failed = 0
    duplicates = 0
    owner_ids_found = 0
    owner_ids_missing = 0

    log.info(
        "НАЧАЛО ПУБЛИКАЦИИ: коллекция=%s, партия=%s, подарков=%s",
        collection,
        batch_number,
        len(gifts),
    )

    for gift in gifts:
        if not is_offchain(gift):
            skipped_blockchain += 1
            continue

        slug = (
            getattr(gift, "slug", None)
            or getattr(gift, "id", None)
        )

        if not slug:
            slug = (
                f"{collection}-"
                f"{getattr(gift, 'num', 'unknown')}"
            )

        slug = str(slug)
        owner = get_owner(gift, users_by_id)
        attrs = get_attributes(gift)

        if owner["id"] is not None:
            owner_ids_found += 1
        else:
            owner_ids_missing += 1
            log.warning(
                "Telegram не передал owner_id: подарок=%s, slug=%s",
                getattr(gift, "title", "—"),
                slug,
            )

        # find_floor возвращает TON, не нанотоны.
        floor, scope = find_floor(
            str(collection),
            attrs["model"],
            attrs["backdrop"],
            attrs["pattern"],
        )

        price, _ = get_prices(gift)

        title = str(
            getattr(gift, "title", None)
            or f"Подарок {slug}"
        )

        floor_text = (
            f"{floor:g} TON"
            if floor is not None
            else "Не определён"
        )

        if owner["url"]:
            owner_line = (
                f'👤 Owner: <a href="{html.escape(owner["url"], quote=True)}">'
                f'{html.escape(owner["name"])}</a>'
            )
        else:
            owner_line = (
                f"👤 Owner: {html.escape(owner['name'])}\n"
                "⚠️ Telegram не передал ID владельца"
            )

        details = (
            f"📉 Floor Price: <b>{html.escape(floor_text)}</b>\n"
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
            "owner_url": owner["url"],
            "topic": topic_for(floor),
        }

        sent = False

        for attempt in range(1, 4):
            try:
                result = await publish_listing(bot, listing)

                if result:
                    published += 1
                    sent = True
                    await asyncio.sleep(PUBLISH_DELAY_SECONDS)
                else:
                    duplicates += 1

                break

            except RetryAfter as error:
                retry_value = error.retry_after

                if hasattr(retry_value, "total_seconds"):
                    wait_seconds = retry_value.total_seconds()
                else:
                    wait_seconds = float(retry_value)

                wait_seconds += 1

                log.warning(
                    "Ограничение Telegram: подарок=%s, ожидание=%.1f сек.",
                    slug,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)

            except asyncio.CancelledError:
                raise

            except Exception:
                failed += 1
                log.exception(
                    "Ошибка публикации подарка %s",
                    slug,
                )
                break

        if not sent and failed == 0 and attempt >= 3:
            failed += 1
            log.error(
                "Не удалось опубликовать подарок %s",
                slug,
            )

    log.info(
        "ИТОГ ПАРТИИ: коллекция=%s, партия=%s, "
        "получено=%s, опубликовано=%s, дубликатов=%s, "
        "blockchain=%s, ошибок=%s, owner_id найдено=%s, "
        "owner_id отсутствует=%s",
        collection,
        batch_number,
        len(gifts),
        published,
        duplicates,
        skipped_blockchain,
        failed,
        owner_ids_found,
        owner_ids_missing,
    )


async def scan_collection(client, bot, base):
    collection = str(base.id)

    all_gifts = []
    batch_gifts = []
    users_by_id = {}

    offset = ""
    page = 0
    batch_number = 0

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

        all_gifts.extend(page_gifts)
        batch_gifts.extend(page_gifts)

        for user in getattr(response, "users", []) or []:
            users_by_id[int(user.id)] = user

        next_offset = getattr(response, "next_offset", None)

        log.info(
            "Коллекция %s: страница %s, получено %s подарков, "
            "пользователей в кеше=%s",
            collection,
            page,
            len(page_gifts),
            len(users_by_id),
        )

        if page % PUBLISH_EVERY_PAGES == 0:
            batch_number += 1

            log.info(
                "ПАРТИЯ ГОТОВА: коллекция=%s, страницы=%s-%s, "
                "подарков=%s",
                collection,
                page - PUBLISH_EVERY_PAGES + 1,
                page,
                len(batch_gifts),
            )

            save_collection_snapshot(collection, all_gifts)

            await publish_collection(
                bot,
                collection,
                batch_gifts,
                users_by_id,
                batch_number,
            )

            batch_gifts.clear()

        if not next_offset:
            break

        if next_offset == offset:
            raise RuntimeError(
                f"Коллекция {collection}: Telegram вернул повторный offset"
            )

        offset = next_offset
        await asyncio.sleep(0.15)

    if batch_gifts:
        batch_number += 1

        log.info(
            "ПОСЛЕДНЯЯ ПАРТИЯ: коллекция=%s, подарков=%s",
            collection,
            len(batch_gifts),
        )

        save_collection_snapshot(collection, all_gifts)

        await publish_collection(
            bot,
            collection,
            batch_gifts,
            users_by_id,
            batch_number,
        )

    log.info(
        "ИТОГ КОЛЛЕКЦИИ %s: подарков=%s, партий=%s",
        collection,
        len(all_gifts),
        batch_number,
    )

    return len(all_gifts)


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
        "Каталог Telegram: типов=%s, resale=%s",
        len(base_gifts),
        len(resale_gifts),
    )

    scanned = 0
    successful = 0
    failed = 0

    for base in resale_gifts:
        try:
            scanned += await scan_collection(client, bot, base)
            successful += 1

        except asyncio.CancelledError:
            raise

        except FloodWaitError:
            raise

        except Exception:
            failed += 1
            log.exception(
                "Ошибка коллекции %s",
                base.id,
            )

        await asyncio.sleep(0.2)

    log.info(
        "СКАНИРОВАНИЕ ЗАВЕРШЕНО: коллекций=%s, "
        "ошибок=%s, подарков=%s",
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
        "Сканер настроен: интервал=%s сек., партия=%s страниц",
        SCAN_INTERVAL,
        PUBLISH_EVERY_PAGES,
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
                    raise ConnectionError(
                        "Соединение Telethon потеряно"
                    )

                started = asyncio.get_running_loop().time()

                try:
                    await scan_once(client, bot)

                except asyncio.CancelledError:
                    raise

                except FloodWaitError as error:
                    wait_seconds = int(error.seconds) + 1
                    log.warning(
                        "FloodWait: %s секунд",
                        wait_seconds,
                    )
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
