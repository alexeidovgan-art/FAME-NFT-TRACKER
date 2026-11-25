
import asyncio
import html
import json
import logging
import os
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import websockets

from bot.db import (
    save_market_offer,
    deactivate_market_offer,
    find_floor,
)
from worker.publish import publish_listing


log = logging.getLogger(__name__)

WS_URL = "wss://live.see.tg/v1/ws"
API_URL = "https://kartoshka.free/v1"

API_TOKEN = os.getenv("SEE_TG_API_TOKEN", "").strip()

_last_api_call = 0.0
_verified_cache = {}


def api_get(path):
    url = API_URL + path

    request = Request(
        url,
        headers={
            "Authorization": "Bearer " + API_TOKEN,
            "Accept": "application/json",
            "User-Agent": "FAME-NFT-TRACKER/1.0",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )
        log.error(
            "API HTTP %s: %s",
            error.code,
            body[:300],
        )
        return None

    except (URLError, TimeoutError, ValueError) as error:
        log.error(
            "Ошибка API: %s",
            type(error).__name__,
        )
        return None


def save_offer(event):
    gift = event.get("gift") or {}
    price = event.get("price") or {}

    gift_id = gift.get("id")

    if gift_id is None:
        return

    save_market_offer(
        market=event.get("market", ""),
        gift_id=gift_id,
        collection=(
            gift.get("slug")
            or gift.get("collection")
        ),
        model=gift.get("model"),
        backdrop=gift.get("backdrop"),
        pattern=(
            gift.get("pattern")
            or gift.get("symbol")
        ),
        currency=price.get("currency", ""),
        price=price.get("amount"),
    )


def remove_offer(event):
    gift = event.get("gift") or {}
    gift_id = gift.get("id")

    if gift_id is None:
        return

    deactivate_market_offer(
        market=event.get("market", ""),
        gift_id=gift_id,
    )


def topic_for(price):
    if price < 10:
        return "cheap"

    if price < 50:
        return "mid"

    if price < 150:
        return "expensive"

    return "premium"


def extract_owner(data):
    for obj in (data, data.get("gift", {})):
        if not isinstance(obj, dict):
            continue

        for key in (
            "owner",
            "current_owner",
            "currentOwner",
        ):
            owner = obj.get(key)

            if isinstance(owner, dict):
                return owner

    return {}


def get_username(owner):
    username = owner.get("username")

    if isinstance(username, str):
        username = username.lstrip("@")

        if username.replace("_", "").isalnum():
            return username

    usernames = owner.get("usernames", [])

    if isinstance(usernames, list):
        for item in usernames:
            if isinstance(item, str):
                name = item.lstrip("@")

            elif isinstance(item, dict):
                name = str(
                    item.get("username", "")
                ).lstrip("@")

            else:
                continue

            if name and name.replace("_", "").isalnum():
                return name

    return None


def verify_gift_data(payload):
    if not isinstance(payload, dict):
        return None

    if payload.get("ok") is not True:
        return None

    data = payload.get("result")

    if not isinstance(data, dict):
        return None

    gift = data.get("gift")

    if not isinstance(gift, dict):
        gift = data

    # Не публикуем подарок без явного подтверждения,
    # что он не находится в блокчейне.
    on_chain = None

    for obj in (gift, data):
        for key in (
            "was_on_chain",
            "is_on_chain",
            "on_chain",
        ):
            if isinstance(obj.get(key), bool):
                on_chain = obj[key]
                break

        if on_chain is not None:
            break

    if on_chain is None:
        for obj in (gift, data):
            if "gift_address" in obj:
                on_chain = bool(obj["gift_address"])
                break

    if on_chain is not False:
        return None

    owner = extract_owner(data)

    owner_type = (
        owner.get("type")
        or owner.get("owner_type")
        or data.get("owner_type")
    )

    if str(owner_type).lower() != "user":
        return None

    username = get_username(owner)

    if not username:
        return None

    return {
        "owner_url": "https://t.me/" + username,
        "owner_name": (
            owner.get("name")
            or username
        ),
    }


async def verify_gift(gift):
    global _last_api_call

    slug = gift.get("slug")
    number = gift.get("num")

    if not slug or number is None:
        return None

    ref = f"{slug}-{number}"

    cached = _verified_cache.get(ref)

    if cached:
        if time.monotonic() - cached[0] < 300:
            return cached[1]

    wait = 1.1 - (
        time.monotonic() - _last_api_call
    )

    if wait > 0:
        await asyncio.sleep(wait)

    _last_api_call = time.monotonic()

    payload = await asyncio.to_thread(
        api_get,
        "/gift/" + quote(ref, safe=""),
    )

    result = (
        verify_gift_data(payload)
        if payload
        else None
    )

    if len(_verified_cache) > 1000:
        _verified_cache.clear()

    _verified_cache[ref] = (
        time.monotonic(),
        result,
    )

    if result is None:
        log.info(
            "Подарок %s не прошёл проверку API",
            ref,
        )

    return result


async def handle_event(bot, data):
    if not isinstance(data, dict):
        return

    if data.get("type") != "event":
        return

    kind = data.get("event")
    market = str(
        data.get("market", "")
    ).lower()

    if kind in ("listing", "price"):
        save_offer(data)

    elif kind in ("delisting", "sale"):
        remove_offer(data)
        return

    else:
        return

    # Публикуем только новые объявления
    # официального Telegram-маркета.
    if kind != "listing":
        return

    if market != "telegram":
        return

    gift = data.get("gift") or {}

    gift_id = gift.get("id")
    slug = gift.get("slug")
    number = gift.get("num")

    if gift_id is None or not slug or number is None:
        return

    owner = await verify_gift(gift)

    if not owner:
        return

    floor, scope = find_floor(
        collection=(
            gift.get("slug")
            or gift.get("collection")
        ),
        model=gift.get("model"),
        backdrop=gift.get("backdrop"),
        pattern=(
            gift.get("pattern")
            or gift.get("symbol")
        ),
        exclude_id=gift_id,
    )

    if floor is None:
        log.info(
            "Нет подтверждённого Floor для %s",
            gift_id,
        )
        return

    price = data.get("price") or {}

    currency = str(
        price.get("currency", "")
    ).lower()

    amount = price.get("amount")

    try:
        if currency == "gram":
            listing_price = (
                f"{float(amount) / 1_000_000_000:g} TON"
            )

        elif currency == "ton":
            listing_price = f"{float(amount):g} TON"

        elif currency == "xtr":
            listing_price = f"{amount} ⭐"

        else:
            log.info(
                "Неизвестная валюта объявления: %s",
                currency,
            )
            return

    except (TypeError, ValueError):
        return

    title = str(
        gift.get("title")
        or slug
    )

    details = (
        f"📉 Floor Price: <b>{floor:.4f} TON</b>\n"
        f"🔎 Совпадение: {html.escape(str(scope))}\n"
        f"🎨 Model: {html.escape(str(gift.get('model') or '—'))}\n"
        f"🖼 Backdrop: {html.escape(str(gift.get('backdrop') or '—'))}\n"
        f"🔣 Pattern: {html.escape(str(gift.get('pattern') or gift.get('symbol') or '—'))}\n"
        f"👤 Owner: <a href=\"{owner['owner_url']}\">"
        f"{html.escape(str(owner['owner_name']))}</a>\n"
        f"📊 Data: see.tg"
    )

    listing = {
        "id": f"see:{market}:{gift_id}",
        "title": title,
        "price": listing_price,
        "details": details,
        "gift_url": (
            f"https://t.me/nft/{slug}-{number}"
        ),
        "owner_url": owner["owner_url"],
        "topic": topic_for(floor),
    }

    await publish_listing(bot, listing)


async def run_scanner(bot):
    if not API_TOKEN:
        log.error(
            "SEE_TG_API_TOKEN не задан в Render Environment"
        )
        return

    log.info("Проверяю API-токен...")

    ping = await asyncio.to_thread(
        api_get,
        "/ping",
    )

    if not ping or ping.get("ok") is not True:
        log.error(
            "Проверка API /ping не пройдена. "
            "Сканер остановлен."
        )
        return

    log.info("API-токен принят")

    while True:
        try:
            log.info(
                "Подключаюсь к see.tg WebSocket..."
            )

            async with websockets.connect(
                WS_URL + "?token=" + quote(
                    API_TOKEN,
                    safe="",
                ),
                ping_interval=20,
                ping_timeout=30,
                close_timeout=10,
            ) as ws:

                await ws.send(json.dumps({
                    "type": "subscribe",
                    "events": ["all"],
                }))

                log.info(
                    "Подписка see.tg отправлена"
                )

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        await handle_event(bot, data)

                    except asyncio.CancelledError:
                        raise

                    except Exception:
                        log.exception(
                            "Ошибка обработки события"
                        )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            log.exception(
                "WebSocket отключился (%s). "
                "Повтор через 8 секунд",
                type(error).__name__,
            )

            await asyncio.sleep(8)
