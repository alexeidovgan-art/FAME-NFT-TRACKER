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

from bot.db import (
    replace_collection_offers,
    find_floor,
)
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
    if floor < 10:
        return "cheap"

    if floor < 50:
        return "mid"

    if floor < 150:
        return "expensive"

    return "premium"


def is_temporary_error(error):
    name = type(error).__name__

    temporary_names = {
        "RpcMcgetFailError",
        "RpcCallFailError",
        "ServerError",
        "TimedOutError",
        "TimeoutError",
        "ConnectionError",
    }

    return (
        name in temporary_names
        or isinstance(
            error,
            (
                asyncio.TimeoutError,
                OSError,
                ConnectionError,
            ),
        )
    )


async def telegram_request(client, request, description):
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            return await client(request)

        except FloodWaitError as error:
            wait_seconds = int(error.seconds) + 1

            log.warning(
                "%s: Telegram FloodWait, ожидание %s сек.",
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
                    REQUEST_RETRIES,
                )
                raise

            delay = REQUEST_DELAYS[attempt - 1]

            log.warning(
                "%s: временная ошибка %s. "
                "Попытка %s/%s через %s сек.",
                description,
                type(error).__name__,
                attempt,
                REQUEST_RETRIES,
                delay,
            )

            await asyncio.sleep(delay)

    raise RuntimeError(
        f"{description}: превышено число попыток"
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
    """
    Возвращает:
    - отображаемую цену;
    - цену в нанограммах TON или None.

    StarsAmount не попадает в TON-floor.
    StarsTonAmount переводится из нанограммов в TON.
    """

    ton_nanos = None
    stars_price = None

    amounts = getattr(gift, "resell_amount", []) or []

    for amount in amounts:
        kind = type(amount).__name__.lower()

        if "ton" in kind:
            raw = getattr(amount, "amount", None)

            if raw is not None and int(raw) > 0:
                ton_nanos = int(raw)

        elif "stars" in kind:
            whole = int(
                getattr(amount, "amount", 0) or 0
            )

            nanos = int(
                getattr(amount, "nanos", 0) or 0
            )

            value = whole + nanos / 1_000_000_000

            if value > 0:
                stars_price = value

    if ton_nanos is not None:
        ton = ton_nanos / 1_000_000_000
        return f"{ton:g} TON", ton_nanos

    if stars_price is not None:
        return f"{stars_price:g} ⭐", None

    return None, None


def is_offchain(gift):
    """
    Не берём подарки, находящиеся в TON-блокчейне.

    host_id тоже не принимаем: Telegram указывает,
    что такой подарок размещён в профиле, но владельцем
    остаётся TON-кошелёк.
    """

    return not any((
        getattr(gift, "gift_address", None),
        getattr(gift, "owner_address", None),
        getattr(gift, "host_id", None),
    ))


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

    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
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

    # Обновляем всю коллекцию одной транзакцией.
    replace_collection_offers(
        collection,
        offers,
    )

    log.info(
        "Коллекция %s: в TON-floor сохранено %s предложений",
        collection,
        len(offers),
    )


async def publish_collection(
    bot,
    collection,
    gifts,
    users_by_id,
):
    published = 0

    skipped_blockchain = 0
    skipped_owner = 0
    skipped_floor = 0
    skipped_price = 0

    for gift in gifts:
        if not is_offchain(gift):
            skipped_blockchain += 1
            continue

        slug = getattr(gift, "slug", None)

        if not slug:
            continue

        owner = get_public_owner(
            gift,
            users_by_id,
        )

        if not owner:
            skipped_owner += 1
            continue

        attrs = get_attributes(gift)

        # Не исключаем сам подарок из floor.
        # Иначе единственное предложение коллекции
        # ошибочно получит floor=None.
        floor, scope = find_floor(
            collection=collection,
            model=attrs["model"],
            backdrop=attrs["backdrop"],
            pattern=attrs["pattern"],
        )

        if floor is None:
            skipped_floor += 1
            continue

        listing_price, _ = get_prices(gift)

        if not listing_price:
            skipped_price += 1
            continue

        title = str(
            getattr(gift, "title", None)
            or collection
        )

        model = attrs["model"] or "—"
        backdrop = attrs["backdrop"] or "—"
        pattern = attrs["pattern"] or "—"

        details = (
            f"📉 Floor Price: <b>{floor:.4f} TON</b>\n"
            f"🔎 Совпадение: "
            f"{html.escape(str(scope or '—'))}\n"
            f"🎨 Model: {html.escape(model)}\n"
            f"🖼 Backdrop: {html.escape(backdrop)}\n"
            f"🔣 Pattern: {html.escape(pattern)}\n"
            f"👤 Owner: "
            f'<a href="{html.escape(owner["owner_url"], quote=True)}">'
            f"{html.escape(owner['owner_name'])}</a>\n"
            f"📊 Источник: официальный маркет Telegram"
        )

        listing = {
            "id": f"tg:{slug}",
            "title": title,
            "price": listing_price,
            "details": details,
            "gift_url": f"https://t.me/nft/{slug}",
            "owner_url": owner["owner_url"],
            "topic": topic_for(floor),
        }

        try:
            result = await publish_listing(
                bot,
                listing,
            )

            if result:
                published += 1

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception(
                "Ошибка публикации подарка %s",
                slug,
            )

    log.info(
        "Коллекция %s: опубликовано=%s, "
        "пропущено blockchain=%s, "
        "без владельца=%s, "
        "без floor=%s, "
        "без цены=%s",
        collection,
        published,
        skipped_blockchain,
        skipped_owner,
        skipped_floor,
        skipped_price,
    )


async def scan_collection(client, bot, base):
    # ID базового подарка — стабильный ключ коллекции.
    # Название коллекции может повторяться.
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

        page_gifts = (
            getattr(response, "gifts", []) or []
        )

        gifts.extend(page_gifts)

        for user in getattr(response, "users", []) or []:
            users_by_id[user.id] = user

        next_offset = getattr(
            response,
            "next_offset",
            None,
        )

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
                f"Коллекция {collection}: Telegram вернул повторный offset"
            )

        offset = next_offset

        await asyncio.sleep(0.15)

    # Снимок меняется только после получения всех страниц.
    save_collection_snapshot(
        collection,
        gifts,
    )

    await publish_collection(
        bot,
        collection,
        gifts,
        users_by_id,
    )

    return len(gifts)


async def scan_once(client, bot):
    log.info("Начинается новый цикл сканирования")

    catalog = await telegram_request(
        client,
        GetStarGiftsRequest(hash=0),
        "Каталог Telegram",
    )

    base_gifts = (
        getattr(catalog, "gifts", []) or []
    )

    resale_gifts = [
        gift
        for gift in base_gifts
        if getattr(gift, "availability_resale", None)
    ]

    log.info(
        "Каталог Telegram получен: всего типов=%s, "
        "доступно для перепродажи=%s",
        len(base_gifts),
        len(resale_gifts),
    )

    if not resale_gifts:
        log.warning(
            "В каталоге не найдено типов подарков с resale"
        )
        return

    scanned_gifts = 0
    successful_collections = 0
    failed_collections = 0

    for base in resale_gifts:
        collection = str(base.id)

        try:
            count = await scan_collection(
                client,
                bot,
                base,
            )

            scanned_gifts += count
            successful_collections += 1

        except asyncio.CancelledError:
            raise

        except FloodWaitError:
            raise

        except Exception:
            failed_collections += 1

            log.exception(
                "Ошибка коллекции %s. "
                "Предыдущий снимок оставлен без изменений.",
                collection,
            )

        await asyncio.sleep(0.2)

    log.info(
        "Цикл сканирования завершён: "
        "успешных коллекций=%s, "
        "ошибок=%s, "
        "получено подарков=%s",
        successful_collections,
        failed_collections,
        scanned_gifts,
    )


async def run_scanner(bot):
    session_string = os.getenv(
        "TG_SESSION",
        "",
    ).strip()

    api_id = os.getenv(
        "API_ID",
        "",
    ).strip()

    api_hash = os.getenv(
        "API_HASH",
        "",
    ).strip()

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
        "Сканер настроен. Интервал между циклами: %s секунд",
        SCAN_INTERVAL,
    )

    # Внешний цикл восстанавливает соединение,
    # если Telegram отключил клиента или произошла ошибка.
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
                log.error(
                    "TG_SESSION не авторизована. "
                    "Сканер остановлен."
                )
                return

            me = await client.get_me()

            log.info(
                "Официальный Telegram-сканер подключён: %s",
                getattr(me, "username", None)
                or getattr(me, "id", "user"),
            )

            while True:
                if not client.is_connected():
                    raise ConnectionError(
                        "Соединение Telethon потеряно"
                    )

                started = asyncio.get_running_loop().time()

                try:
                    await scan_once(
                        client,
                        bot,
                    )

                except asyncio.CancelledError:
                    raise

                except FloodWaitError as error:
                    wait_seconds = int(error.seconds) + 1

                    log.warning(
                        "Telegram FloodWait: ожидание %s секунд",
                        wait_seconds,
                    )

                    await asyncio.sleep(wait_seconds)
                    continue

                except Exception:
                    log.exception(
                        "Ошибка цикла сканирования. "
                        "Следующая попытка будет автоматически."
                    )

                    await asyncio.sleep(20)
                    continue

                elapsed = (
                    asyncio.get_running_loop().time()
                    - started
                )

                wait_time = max(
                    30,
                    SCAN_INTERVAL - elapsed,
                )

                log.info(
                    "Следующий цикл через %.0f секунд",
                    wait_time,
                )

                await asyncio.sleep(wait_time)

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception(
                "Сканер отключился. Будет выполнено переподключение."
            )

        finally:
            if client and client.is_connected():
                await client.disconnect()

        log.warning(
            "Повторное подключение к Telegram через 15 секунд"
        )

        await asyncio.sleep(15)
