
import os
import sqlite3
from contextlib import contextmanager


DB = os.getenv("DATABASE_PATH", "data.sqlite3")


def _prepare_path():
    folder = os.path.dirname(DB)
    if folder:
        os.makedirs(folder, exist_ok=True)


@contextmanager
def conn():
    _prepare_path()

    db = sqlite3.connect(DB, timeout=30)
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


def init_db():
    with conn() as db:
        db.execute("PRAGMA journal_mode=WAL")

        # Таблица листингов
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

        # Таблица пользователей, которые могут занимать логи
        db.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Таблица предложений официального Telegram-маркета
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
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (market, gift_id)
            )
        """)

        # МИГРАЦИЯ СТАРОЙ БАЗЫ
        # Проверяем существующие колонки, чтобы не создавать их повторно.
        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(market_offers)"
            ).fetchall()
        }

        if "currency" not in columns:
            db.execute("""
                ALTER TABLE market_offers
                ADD COLUMN currency TEXT
            """)

        if "price" not in columns:
            db.execute("""
                ALTER TABLE market_offers
                ADD COLUMN price REAL
            """)

        if "updated_at" not in columns:
            db.execute("""
                ALTER TABLE market_offers
                ADD COLUMN updated_at TEXT
            """)

        db.execute("""
            UPDATE market_offers
            SET updated_at=CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
        """)

        # Индекс для быстрого расчёта флора
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_offers_floor
            ON market_offers (
                market,
                active,
                collection,
                model,
                backdrop,
                pattern
            )
        """)

    print("База данных успешно инициализирована")


def set_worker(user_id):
    with conn() as db:
        db.execute("""
            INSERT INTO workers(user_id, enabled)
            VALUES (?, 1)
            ON CONFLICT(user_id)
            DO UPDATE SET enabled=1
        """, (int(user_id),))


def is_worker(user_id):
    with conn() as db:
        row = db.execute("""
            SELECT enabled
            FROM workers
            WHERE user_id=?
        """, (int(user_id),)).fetchone()

        return bool(row and row["enabled"])


def get_listing(listing_id):
    with conn() as db:
        return db.execute("""
            SELECT *
            FROM listings
            WHERE id=?
        """, (str(listing_id),)).fetchone()


def insert_listing(listing):
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
            str(listing["id"]),
            str(listing["title"]),
            str(listing["price"]),
            str(listing["details"]),
            str(listing["gift_url"]),
            listing.get("owner_url"),
            str(listing["topic"]),
        ))

        return cur.rowcount == 1


def delete_unpublished_listing(listing_id):
    with conn() as db:
        db.execute("""
            DELETE FROM listings
            WHERE id=?
              AND status='open'
              AND message_id IS NULL
        """, (str(listing_id),))


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


def claim_listing(listing_id, user_id):
    # Атомарное занятие лога.
    # Один листинг может получить только один пользователь.
    with conn() as db:
        cur = db.execute("""
            UPDATE listings
            SET status='claimed',
                claimed_by=?
            WHERE id=?
              AND status='open'
        """, (
            int(user_id),
            str(listing_id),
        ))

        return cur.rowcount == 1


def release_claim(listing_id, user_id):
    # Возвращаем лог в свободное состояние,
    # если отправка личного сообщения не удалась.
    with conn() as db:
        cur = db.execute("""
            UPDATE listings
            SET status='open',
                claimed_by=NULL
            WHERE id=?
              AND status='claimed'
              AND claimed_by=?
        """, (
            str(listing_id),
            int(user_id),
        ))

        return cur.rowcount == 1


def replace_collection_offers(collection, offers):
    """
    Обновляет все предложения одной коллекции
    в рамках одной транзакции.
    """

    offers = list(offers)

    with conn() as db:
        db.execute("""
            UPDATE market_offers
            SET active=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE market='telegram'
              AND collection=?
        """, (str(collection),))

        for offer in offers:
            price = float(offer["price"])

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
    # Совместимость с прежним интерфейсом.
    if str(market).lower() != "telegram":
        return

    if str(currency).lower() not in ("ton", "gram"):
        return

    try:
        value = float(price)
    except (TypeError, ValueError):
        return

    if str(currency).lower() == "gram":
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


def find_floor(
    collection,
    model,
    backdrop,
    pattern,
):
    """
    Ищет минимальную цену в TON.

    Текущее предложение также участвует в расчёте.
    """

    fields = [
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

    for label, conditions in fields:
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
            "WHERE " + " AND ".join(where)
        )

        with conn() as db:
            row = db.execute(sql, values).fetchone()

        if row and row[0] is not None:
            return float(row[0]), label

    return None, None
