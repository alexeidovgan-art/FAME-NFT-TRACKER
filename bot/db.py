
import os
import sqlite3
from contextlib import contextmanager


DB = os.getenv("DATABASE_PATH", "data.sqlite3")


# --------------------------------------------------
# ПОДКЛЮЧЕНИЕ К БАЗЕ
# --------------------------------------------------

def _prepare_path():
    folder = os.path.dirname(DB)

    if folder:
        os.makedirs(folder, exist_ok=True)


@contextmanager
def conn():
    _prepare_path()

    db = sqlite3.connect(
        DB,
        timeout=30,
    )

    db.row_factory = sqlite3.Row

    try:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")

        yield db

        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def _columns(db, table):
    return {
        row["name"]
        for row in db.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def _add_column(db, table, name, definition):
    if name not in _columns(db, table):
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
        )


# --------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ И МИГРАЦИЯ БАЗЫ
# --------------------------------------------------

def init_db():
    with conn() as db:

        db.execute("PRAGMA journal_mode=WAL")

        db.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price TEXT NOT NULL,
                details TEXT NOT NULL,
                gift_url TEXT NOT NULL,
                owner_url TEXT,
                topic TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                claimed_by INTEGER,
                message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS market_offers (
                market TEXT NOT NULL,
                gift_id TEXT NOT NULL,
                collection TEXT,
                model TEXT,
                backdrop TEXT,
                pattern TEXT,
                currency TEXT,
                price REAL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция таблицы listings.

        _add_column(
            db,
            "listings",
            "owner_url",
            "TEXT",
        )

        _add_column(
            db,
            "listings",
            "topic",
            "TEXT",
        )

        _add_column(
            db,
            "listings",
            "status",
            "TEXT NOT NULL DEFAULT 'open'",
        )

        _add_column(
            db,
            "listings",
            "claimed_by",
            "INTEGER",
        )

        _add_column(
            db,
            "listings",
            "message_id",
            "INTEGER",
        )

        _add_column(
            db,
            "listings",
            "created_at",
            "TEXT",
        )

        # Миграция таблицы market_offers.

        _add_column(
            db,
            "market_offers",
            "collection",
            "TEXT",
        )

        _add_column(
            db,
            "market_offers",
            "model",
            "TEXT",
        )

        _add_column(
            db,
            "market_offers",
            "backdrop",
            "TEXT",
        )

        _add_column(
            db,
            "market_offers",
            "pattern",
            "TEXT",
        )

        _add_column(
            db,
            "market_offers",
            "currency",
            "TEXT",
        )

        _add_column(
            db,
            "market_offers",
            "price",
            "REAL",
        )

        _add_column(
            db,
            "market_offers",
            "active",
            "INTEGER NOT NULL DEFAULT 1",
        )

        _add_column(
            db,
            "market_offers",
            "updated_at",
            "TEXT",
        )

        db.execute("""
            UPDATE market_offers
            SET updated_at=CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
        """)

        # Удаляем старые дубликаты предложений.

        db.execute("""
            DELETE FROM market_offers
            WHERE rowid NOT IN (
                SELECT MAX(rowid)
                FROM market_offers
                GROUP BY market, gift_id
            )
        """)

        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_market_gift_unique
            ON market_offers(market, gift_id)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_offers_floor
            ON market_offers (
                market,
                active,
                collection,
                model,
                backdrop,
                pattern
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_listings_status
            ON listings(status)
        """)

    print("База данных успешно инициализирована")


# --------------------------------------------------
# РАБОТНИКИ
# --------------------------------------------------

def set_worker(user_id):
    with conn() as db:

        db.execute("""
            INSERT INTO workers(user_id, enabled)
            VALUES (?, 1)

            ON CONFLICT(user_id)
            DO UPDATE SET enabled=1
        """, (
            int(user_id),
        ))


def is_worker(user_id):
    with conn() as db:

        row = db.execute("""
            SELECT enabled
            FROM workers
            WHERE user_id=?
        """, (
            int(user_id),
        )).fetchone()

        return bool(
            row and row["enabled"]
        )


# --------------------------------------------------
# ПОЛУЧЕНИЕ ЛОГА
# --------------------------------------------------

def get_listing(listing_id):
    with conn() as db:

        return db.execute("""
            SELECT *
            FROM listings
            WHERE id=?
        """, (
            str(listing_id),
        )).fetchone()


# --------------------------------------------------
# СОХРАНЕНИЕ И ОБНОВЛЕНИЕ ЛОГОВ
# --------------------------------------------------

def insert_listing(listing):
    listing_id = str(listing["id"])

    title = str(listing["title"])
    price = str(listing["price"])
    details = str(listing["details"])
    gift_url = str(listing["gift_url"])
    owner_url = listing.get("owner_url")
    topic = str(listing["topic"])

    with conn() as db:

        cur = db.execute("""
            INSERT OR IGNORE INTO listings (
                id,
                title,
                price,
                details,
                gift_url,
                owner_url,
                topic
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            listing_id,
            title,
            price,
            details,
            gift_url,
            owner_url,
            topic,
        ))

        # Новый подарок.
        if cur.rowcount == 1:
            return True

        # Подарок уже существует.
        old = db.execute("""
            SELECT
                status,
                message_id
            FROM listings
            WHERE id=?
        """, (
            listing_id,
        )).fetchone()

        if not old:
            return False

        # Не изменяем уже занятый лог.
        if old["status"] != "open":
            return False

        # Обновляем информацию существующего лота.
        # Если Telegram временно не передал owner_url,
        # сохраняем предыдущую известную ссылку.
        db.execute("""
            UPDATE listings
            SET
                title=?,
                price=?,
                details=?,
                gift_url=?,
                owner_url=COALESCE(?, owner_url),
                topic=?
            WHERE
                id=?
                AND status='open'
        """, (
            title,
            price,
            details,
            gift_url,
            owner_url,
            topic,
            listing_id,
        ))

        # Если сообщение ещё не отправлялось,
        # разрешаем публикацию.
        if old["message_id"] is None:
            return True

        # Если сообщение уже существует,
        # повторно его не публикуем.
        return False


# --------------------------------------------------
# УДАЛЕНИЕ НЕОПУБЛИКОВАННОГО ЛОГА
# --------------------------------------------------

def delete_unpublished_listing(listing_id):
    with conn() as db:

        db.execute("""
            DELETE FROM listings
            WHERE
                id=?
                AND status='open'
                AND message_id IS NULL
        """, (
            str(listing_id),
        ))


# --------------------------------------------------
# СОХРАНЕНИЕ ID СООБЩЕНИЯ
# --------------------------------------------------

def set_message_id(listing_id, message_id):
    with conn() as db:

        db.execute("""
            UPDATE listings
            SET message_id=?
            WHERE id=?
        """, (
            int(message_id),
            str(listing_id),
        ))


# --------------------------------------------------
# ЗАНЯТИЕ ЛОГА
# --------------------------------------------------

def claim_listing(listing_id, user_id):
    with conn() as db:

        cur = db.execute("""
            UPDATE listings
            SET
                status='claimed',
                claimed_by=?
            WHERE
                id=?
                AND status='open'
        """, (
            int(user_id),
            str(listing_id),
        ))

        return cur.rowcount == 1


# --------------------------------------------------
# ВОЗВРАТ ЛОГА ПРИ ОШИБКЕ
# --------------------------------------------------

def release_claim(listing_id, user_id):
    with conn() as db:

        cur = db.execute("""
            UPDATE listings
            SET
                status='open',
                claimed_by=NULL
            WHERE
                id=?
                AND status='claimed'
                AND claimed_by=?
        """, (
            str(listing_id),
            int(user_id),
        ))

        return cur.rowcount == 1


# --------------------------------------------------
# ОБНОВЛЕНИЕ СНИМКА ПРЕДЛОЖЕНИЙ TELEGRAM
# ЦЕНА ХРАНИТСЯ В TON
# --------------------------------------------------

def replace_collection_offers(collection, offers):
    offers = list(offers)

    with conn() as db:

        # Деактивируем предыдущие предложения коллекции.
        db.execute("""
            UPDATE market_offers
            SET
                active=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE
                market='telegram'
                AND collection=?
        """, (
            str(collection),
        ))

        for offer in offers:

            try:
                price = float(offer["price"])
            except (TypeError, ValueError, KeyError):
                continue

            if price <= 0:
                continue

            db.execute("""
                INSERT INTO market_offers (
                    market,
                    gift_id,
                    collection,
                    model,
                    backdrop,
                    pattern,
                    currency,
                    price,
                    active,
                    updated_at
                )
                VALUES (
                    'telegram',
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'ton',
                    ?,
                    1,
                    CURRENT_TIMESTAMP
                )

                ON CONFLICT(market, gift_id)
                DO UPDATE SET
                    collection=excluded.collection,
                    model=excluded.model,
                    backdrop=excluded.backdrop,
                    pattern=excluded.pattern,
                    currency='ton',
                    price=excluded.price,
                    active=1,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                str(offer["gift_id"]),
                str(collection),
                offer.get("model"),
                offer.get("backdrop"),
                offer.get("pattern"),
                price,
            ))


# --------------------------------------------------
# СОХРАНЕНИЕ ОДНОГО ПРЕДЛОЖЕНИЯ
# --------------------------------------------------

def save_market_offer(
    market,
    gift_id,
    collection,
    model,
    backdrop,
    pattern,
    currency,
    price,
):
    if str(market).lower() != "telegram":
        return

    currency = str(currency).lower()

    if currency not in ("ton", "gram"):
        return

    try:
        value = float(price)
    except (TypeError, ValueError):
        return

    # gram здесь означает нанотоны.
    if currency == "gram":
        value /= 1_000_000_000

    if value <= 0:
        return

    with conn() as db:

        db.execute("""
            INSERT INTO market_offers (
                market,
                gift_id,
                collection,
                model,
                backdrop,
                pattern,
                currency,
                price,
                active,
                updated_at
            )
            VALUES (
                'telegram',
                ?,
                ?,
                ?,
                ?,
                ?,
                'ton',
                ?,
                1,
                CURRENT_TIMESTAMP
            )

            ON CONFLICT(market, gift_id)
            DO UPDATE SET
                collection=excluded.collection,
                model=excluded.model,
                backdrop=excluded.backdrop,
                pattern=excluded.pattern,
                currency='ton',
                price=excluded.price,
                active=1,
                updated_at=CURRENT_TIMESTAMP
        """, (
            str(gift_id),
            str(collection),
            model,
            backdrop,
            pattern,
            value,
        ))


# --------------------------------------------------
# ПОИСК ФЛОРА
# --------------------------------------------------

def find_floor(
    collection,
    model,
    backdrop,
    pattern,
):
    search_levels = [

        (
            "model + backdrop + pattern",
            [
                ("collection", collection),
                ("model", model),
                ("backdrop", backdrop),
                ("pattern", pattern),
            ],
        ),

        (
            "model + backdrop",
            [
                ("collection", collection),
                ("model", model),
                ("backdrop", backdrop),
            ],
        ),

        (
            "model",
            [
                ("collection", collection),
                ("model", model),
            ],
        ),

        (
            "collection",
            [
                ("collection", collection),
            ],
        ),
    ]

    for label, conditions in search_levels:

        # Пропускаем уровень, если не хватает данных.
        if any(
            value is None or value == ""
            for _, value in conditions
        ):
            continue

        where = [
            "market='telegram'",
            "active=1",
            "currency='ton'",
            "price IS NOT NULL",
            "price > 0",
        ]

        values = []

        for field, value in conditions:

            where.append(f"{field}=?")
            values.append(value)

        sql = (
            "SELECT MIN(price) "
            "FROM market_offers "
            "WHERE "
            + " AND ".join(where)
        )

        with conn() as db:

            row = db.execute(
                sql,
                values,
            ).fetchone()

        if row and row[0] is not None:

            return (
                float(row[0]),
                label,
            )

    return None, None
