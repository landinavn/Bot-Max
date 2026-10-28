"""Постоянный баланс пользователей. Все изменения фиксируются в SQLite."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import aiosqlite

DATABASE_PATH = Path(__file__).resolve().parent / "database.sqlite3"
PROMO_SECONDS = 24 * 60 * 60


class PromoUnavailableError(ValueError):
    """Скидка истекла, ещё не открыта или уже использована."""


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

SOURCE_LABELS = {
    "vk_ads": "Реклама VK",
    "yandex_ads": "Реклама Яндекса",
    "yandex_newyear": "Yandex New Year",
    "unmarked": "Без метки",
    "legacy": "Источник неизвестен (до отслеживания)",
}


async def init_db() -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                free_trial_used BOOLEAN NOT NULL DEFAULT 0
                    CHECK (free_trial_used IN (0, 1)),
                total_generations INTEGER NOT NULL DEFAULT 0 CHECK (total_generations >= 0),
                total_purchases INTEGER NOT NULL DEFAULT 0 CHECK (total_purchases >= 0),
                first_start_source TEXT,
                first_started_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                days_active INTEGER NOT NULL DEFAULT 1 CHECK (days_active >= 1),
                last_active_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS payment_orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                credits INTEGER NOT NULL CHECK (credits > 0),
                amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks > 0),
                product_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
                provider_payment_id TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS shared_images (
                token TEXT PRIMARY KEY,
                image_url TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS audio_orders (
                order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                audio_format TEXT NOT NULL CHECK(audio_format IN ('fun', 'gift')),
                scenario TEXT NOT NULL, details TEXT NOT NULL, style TEXT,
                prompt TEXT NOT NULL, tags TEXT NOT NULL, title TEXT NOT NULL,
                amount_kopecks INTEGER NOT NULL CHECK(amount_kopecks > 0), product_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'paid')),
                provider_payment_id TEXT UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP, paid_at TEXT,
                submit_started INTEGER NOT NULL DEFAULT 0, job_id TEXT, tracks_json TEXT,
                running INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0, error TEXT
            )"""
        )
        async with db.execute('PRAGMA table_info(audio_orders)') as cursor:
            audio_columns = {row[1] for row in await cursor.fetchall()}
        for name, definition in {
            'lyrics': 'TEXT', 'preview_url': 'TEXT', 'payment_link': 'TEXT', 'preview_requested': 'INTEGER NOT NULL DEFAULT 0',
            'preview_delivered': 'INTEGER NOT NULL DEFAULT 0', 'free_access': 'INTEGER NOT NULL DEFAULT 0',
            'regeneration_used': 'INTEGER NOT NULL DEFAULT 0', 'previous_tracks_json': 'TEXT',
        }.items():
            if name not in audio_columns:
                await db.execute(f'ALTER TABLE audio_orders ADD COLUMN {name} {definition}')
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'global_counters'"
        ) as cursor:
            counters_exist = await cursor.fetchone() is not None
        await db.execute(
            """CREATE TABLE IF NOT EXISTS global_counters (
                event_name TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0)
            )"""
        )
        if not counters_exist:
            # Покупки восстановим из заказов один раз; тип старых генераций неизвестен.
            await db.execute(
                """INSERT INTO global_counters (event_name, count)
                   SELECT 'buy_' || credits, COUNT(*) FROM payment_orders
                   WHERE status = 'paid' AND credits IN (1, 3, 5, 15)
                   GROUP BY credits"""
            )
        async with db.execute("PRAGMA table_info(users)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        for column in ("total_generations", "total_purchases"):
            if column not in columns:
                await db.execute(
                    f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0 CHECK ({column} >= 0)"
                )
        if "total_purchases" not in columns:
            # История оплат уже существует; восстановить её только при миграции.
            await db.execute(
                """UPDATE users SET total_purchases = (
                    SELECT COUNT(*) FROM payment_orders
                    WHERE payment_orders.user_id = users.user_id AND status = 'paid'
                )"""
            )
        for column in ("first_start_source", "first_started_at"):
            if column not in columns:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
        if "first_start_source" not in columns:
            # Не приписывать старых пользователей новой рекламной кампании.
            # Их точная дата первого запуска до обновления неизвестна.
            await db.execute("UPDATE users SET first_start_source = 'legacy'")
        for column, definition in (
            ("is_active", "INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))"),
            ("days_active", "INTEGER NOT NULL DEFAULT 1 CHECK (days_active >= 1)"),
            ("last_active_at", "TEXT"),
        ):
            if column not in columns:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        if "created_at" not in columns:
            # SQLite запрещает ALTER ADD с динамическим DEFAULT для непустой таблицы.
            await db.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP")
            await db.execute("UPDATE users SET created_at = first_started_at")
        # Для мигрированных баз обеспечиваем тот же default при новых INSERT.
        await db.execute(
            """CREATE TRIGGER IF NOT EXISTS users_created_at_default
               AFTER INSERT ON users WHEN NEW.created_at IS NULL
               BEGIN
                   UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE user_id = NEW.user_id;
               END"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS user_activity_days (
                user_id INTEGER NOT NULL,
                activity_date TEXT NOT NULL,
                PRIMARY KEY (user_id, activity_date)
            )"""
        )
        # Существующие сроки сохраняем; TIMESTAMP в SQLite также хранит Unix-секунды.
        for column, definition in (("promo_expires_at", "TIMESTAMP"), ("promo_redeemed_at", "INTEGER"),
                                   ("song_promo_expires_at", "TIMESTAMP")):
            if column not in columns:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        if "yclid" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN yclid TEXT NOT NULL DEFAULT ''")
        if "promo_campaign_id" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN promo_campaign_id TEXT")
        async with db.execute("PRAGMA table_info(payment_orders)") as cursor:
            order_columns = {row[1] for row in await cursor.fetchall()}
        if "package_id" not in order_columns:
            await db.execute("ALTER TABLE payment_orders ADD COLUMN package_id TEXT")
            await db.execute("UPDATE payment_orders SET package_id = 'package_' || credits")
        if "promo_campaign_id" not in order_columns:
            await db.execute("ALTER TABLE payment_orders ADD COLUMN promo_campaign_id TEXT")
        await db.execute("DROP INDEX IF EXISTS one_promo_order_per_user")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_promo_order_per_campaign "
            "ON payment_orders(user_id, COALESCE(promo_campaign_id, 'tripwire')) "
            "WHERE package_id = 'promo_3'"
        )
        async with db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_counters'") as cursor:
            user_counters_exist = await cursor.fetchone() is not None
        await db.execute(
            """CREATE TABLE IF NOT EXISTS user_counters (
                user_id INTEGER NOT NULL,
                event_name TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
                PRIMARY KEY (user_id, event_name)
            )"""
        )
        if not user_counters_exist:
            # Заказы имеют владельца; старые глобальные счётчики генераций — нет.
            await db.execute(
                """INSERT INTO user_counters (user_id, event_name, count)
                   SELECT user_id, 'buy_' || credits, COUNT(*) FROM payment_orders
                   WHERE status = 'paid' AND credits IN (1, 3, 5, 15) GROUP BY user_id, credits"""
            )
            await db.execute(
                """INSERT INTO user_counters (user_id, event_name, count)
                   SELECT user_id, 'buy_3_promo', COUNT(*) FROM payment_orders
                   WHERE status = 'paid' AND package_id = 'promo_3' GROUP BY user_id"""
            )
        await db.commit()


def _activity_date(timestamp: str) -> str:
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()


async def mark_user_inactive(user_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def update_user_activity(user_id: int, *, timestamp_ms: int | None = None) -> None:
    """Учесть уникальный московский день; входящее событие восстанавливает доступность."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Некорректный user_id.")
    moment = (datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
              if timestamp_ms is not None else datetime.now(timezone.utc))
    timestamp = moment.isoformat(timespec="milliseconds")
    activity_date = _activity_date(timestamp)
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, timestamp),
        )
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
        baseline = user["last_active_at"] or user["first_started_at"] or user["created_at"] or timestamp
        await db.execute(
            "INSERT OR IGNORE INTO user_activity_days VALUES (?, ?)", (user_id, _activity_date(baseline)),
        )
        inserted = await db.execute(
            "INSERT OR IGNORE INTO user_activity_days VALUES (?, ?)", (user_id, activity_date),
        )
        last_active = max(user["last_active_at"] or timestamp, timestamp)
        await db.execute(
            """UPDATE users SET is_active = 1, last_active_at = ?,
               days_active = days_active + ? WHERE user_id = ?""",
            (last_active, inserted.rowcount, user_id),
        )
        await db.commit()


async def get_advanced_stats(admin_id: int | None = None) -> dict[str, int]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) AS total_all,
               COALESCE(SUM(is_active = 1), 0) AS total_active,
               COALESCE(SUM(is_active = 0), 0) AS total_blocked,
               COALESCE(SUM(days_active > 1), 0) AS total_returned FROM users
               WHERE (? IS NULL OR user_id != ?)""", (admin_id, admin_id),
        ) as cursor:
            return dict(await cursor.fetchone())


async def get_active_user_ids() -> list[int]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_active = 1 ORDER BY user_id") as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_users_by_activity(has_generated: bool = True) -> list[int]:
    condition = "total_generations > 0" if has_generated else "total_generations = 0"
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute(
            f"SELECT user_id FROM users WHERE is_active = 1 AND {condition} ORDER BY user_id"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_users_for_reminder() -> list[int]:
    """Активная неиспользованная скидка истекает строго в ближайшие три часа."""
    now = _now_ts()
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute(
            """SELECT user_id FROM users WHERE is_active = 1
               AND promo_expires_at > ? AND promo_expires_at < ?
               AND promo_redeemed_at IS NULL ORDER BY user_id""",
            (now, now + 3 * 60 * 60),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def start_broadcast_promo(user_id: int, campaign_id: str) -> None:
    """Новая акция рассылки; повтор в рамках той же кампании не продлевает срок."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute(
            """UPDATE users SET promo_expires_at = ?, promo_redeemed_at = NULL,
               promo_campaign_id = ? WHERE user_id = ? AND is_active = 1
               AND total_generations > 0 AND promo_campaign_id IS NOT ?""",
            (_now_ts() + PROMO_SECONDS, campaign_id, user_id, campaign_id),
        )
        await db.commit()


async def get_user(user_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()


async def add_credits(
    user_id: int, amount: int, *, payment_order_id: str | None = None,
    provider_payment_id: str | None = None,
) -> bool:
    """Начислить кредиты; оплаченный заказ фиксируется в той же транзакции.

    False означает повтор уже обработанного платежа. Ошибки откатывают
    и статус заказа, и баланс, чтобы уведомление можно было повторить.
    """
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("Количество кредитов должно быть положительным целым числом.")
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        if payment_order_id is not None:
            if not provider_payment_id:
                raise ValueError("Не указан номер платежа Prodamus.")
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?", (payment_order_id,)
            ) as cursor:
                order = await cursor.fetchone()
            if order is None or order["user_id"] != user_id or order["credits"] != amount:
                raise ValueError("Платёж не соответствует заказу.")
            if order["status"] == "paid":
                if order["provider_payment_id"] != provider_payment_id:
                    raise ValueError("Заказ уже оплачен другим платежом.")
                return False
            async with db.execute("SELECT 1 FROM audio_orders WHERE provider_payment_id = ?", (provider_payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError("Платёж уже использован для аудиозаказа.")
            await db.execute(
                """UPDATE payment_orders SET status = 'paid', provider_payment_id = ?,
                   paid_at = CURRENT_TIMESTAMP WHERE order_id = ?""",
                (provider_payment_id, payment_order_id),
            )
        await db.execute(
            """INSERT INTO users (user_id, balance) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance""",
            (user_id, amount),
        )
        if payment_order_id is not None:
            await increment_purchases(user_id, connection=db)
            if order["credits"] in (1, 3, 5, 15):
                await increment_counter(f"buy_{order['credits']}", user_id=user_id, connection=db)
            if order["package_id"] == "promo_3":
                await db.execute(
                    "UPDATE users SET promo_redeemed_at = ? WHERE user_id = ? AND promo_campaign_id IS ?",
                    (_now_ts(), user_id, order["promo_campaign_id"]),
                )
                await increment_counter("buy_3_promo", user_id=user_id, connection=db)
        await db.commit()
        return True


async def create_payment_order(
    order_id: str, user_id: int, credits: int, amount_kopecks: int, product_name: str,
    *, promo: bool = False,
) -> str:
    if promo and (credits != 3 or amount_kopecks != 14900):
        raise ValueError("Некорректный акционный пакет.")
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        campaign_id = None
        if promo:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT promo_campaign_id FROM users WHERE user_id = ? AND promo_expires_at > ? AND promo_redeemed_at IS NULL",
                (user_id, _now_ts()),
            ) as cursor:
                user = await cursor.fetchone()
                if user is None:
                    raise PromoUnavailableError("Скидка больше недоступна.")
                campaign_id = user[0]
            async with db.execute(
                "SELECT order_id FROM payment_orders WHERE user_id = ? AND package_id = 'promo_3' AND promo_campaign_id IS ?",
                (user_id, campaign_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                return existing[0]
        await db.execute(
            """INSERT INTO payment_orders
               (order_id, user_id, credits, amount_kopecks, product_name, package_id, promo_campaign_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_id, user_id, credits, amount_kopecks, product_name, "promo_3" if promo else f"package_{credits}", campaign_id),
        )
        await db.commit()
        return order_id


async def is_promo_active(user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ? AND promo_expires_at > ? AND promo_redeemed_at IS NULL",
            (user_id, _now_ts()),
        ) as cursor:
            return await cursor.fetchone() is not None


async def get_payment_order(order_id: str) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payment_orders WHERE order_id = ?", (order_id,)
        ) as cursor:
            return await cursor.fetchone()


async def deduct_credit(
    user_id: int, *, count_generation: bool = False, generation_event: str | None = None,
) -> bool:
    """Атомарно списать кредит и при необходимости учесть новую генерацию."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute(
            """UPDATE users SET balance = balance - 1,
               promo_expires_at = CASE WHEN ? AND free_trial_used = 1 AND total_generations = 0
                   AND total_purchases = 0 AND balance = 1 AND promo_expires_at IS NULL
                   THEN ? ELSE promo_expires_at END
               WHERE user_id = ? AND balance >= 1""",
            (count_generation, _now_ts() + PROMO_SECONDS, user_id),
        )
        charged = cursor.rowcount == 1
        if charged and count_generation:
            await increment_generations(user_id, connection=db, event_name=generation_event)
        await db.commit()
        return charged


async def mark_free_trial_used(user_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute(
            """INSERT INTO users (user_id, free_trial_used) VALUES (?, 1)
               ON CONFLICT(user_id) DO UPDATE SET free_trial_used = 1""",
            (user_id,),
        )
        await db.commit()


async def grant_free_trial(user_id: int) -> bool:
    """Создать пользователя и начислить пробный кредит ровно один раз.

    Начисление и отметка выполняются одной транзакцией: повторный /start,
    перезапуск и конкурентные запросы не дают дополнительного кредита.
    """
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        cursor = await db.execute(
            """UPDATE users SET balance = balance + 1, free_trial_used = 1
               WHERE user_id = ? AND free_trial_used = 0""",
            (user_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def _increment_counter(user_id: int, column: str, connection: aiosqlite.Connection | None) -> None:
    if column not in {"total_generations", "total_purchases"}:
        raise ValueError("Неизвестный счётчик статистики.")
    query = (
        f"INSERT INTO users (user_id, {column}) VALUES (?, 1) "
        f"ON CONFLICT(user_id) DO UPDATE SET {column} = {column} + 1"
    )
    if connection is not None:
        await connection.execute(query, (user_id,))
    else:
        async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
            await db.execute(query, (user_id,))
            await db.commit()


async def increment_counter(
    event_name: str, *, user_id: int | None = None, connection: aiosqlite.Connection | None = None,
) -> None:
    """Атомарно увеличить глобальный счётчик, при необходимости в общей транзакции."""
    if not isinstance(event_name, str) or not event_name.strip():
        raise ValueError("Название события не должно быть пустым.")
    query = (
        "INSERT INTO global_counters (event_name, count) VALUES (?, 1) "
        "ON CONFLICT(event_name) DO UPDATE SET count = count + 1"
    )
    if connection is not None:
        await connection.execute(query, (event_name,))
        if user_id is not None:
            await connection.execute(
                """INSERT INTO user_counters (user_id, event_name, count) VALUES (?, ?, 1)
                   ON CONFLICT(user_id, event_name) DO UPDATE SET count = count + 1""",
                (user_id, event_name),
            )
    else:
        async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
            await increment_counter(event_name, user_id=user_id, connection=db)
            await db.commit()


async def get_all_counters(admin_id: int | None = None) -> dict[str, int]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        if admin_id is None:
            async with db.execute("SELECT event_name, count FROM global_counters") as cursor:
                return dict(await cursor.fetchall())
        async with db.execute(
            "SELECT event_name, SUM(count) FROM user_counters WHERE user_id != ? GROUP BY event_name",
            (admin_id,),
        ) as cursor:
            counters = dict(await cursor.fetchall())
        async with db.execute(
            "SELECT COALESCE(SUM(total_generations), 0) FROM users WHERE user_id != ?", (admin_id,),
        ) as cursor:
            total_generations = (await cursor.fetchone())[0]
        classified = sum(count for event, count in counters.items() if event.startswith("gen_"))
        counters["gen_unclassified"] = max(0, total_generations - classified)
        return counters


async def increment_generations(
    user_id: int, *, connection: aiosqlite.Connection | None = None, event_name: str | None = None,
) -> None:
    """Учесть новую генерацию; connection связывает счётчик со списанием кредита."""
    if connection is None:
        async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
            await increment_generations(user_id, connection=db, event_name=event_name)
            await db.commit()
    else:
        await _increment_counter(user_id, "total_generations", connection)
        if event_name is not None:
            await increment_counter(event_name, user_id=user_id, connection=connection)


async def increment_purchases(user_id: int, *, connection: aiosqlite.Connection | None = None) -> None:
    """Учесть покупку в транзакции оплаты, без повторного начисления webhook."""
    await _increment_counter(user_id, "total_purchases", connection)


async def get_global_stats(admin_id: int | None = None) -> dict[str, int]:
    """Снимок счётчиков и точного количества кредитов из оплаченных заказов."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) AS total_users,
                COALESCE(SUM(CASE WHEN total_purchases > 0 THEN 1 ELSE 0 END), 0) AS total_paid_users,
                COALESCE(SUM(total_generations), 0) AS total_generated,
                COALESCE(SUM(total_purchases), 0) AS total_purchases,
                (SELECT COALESCE(SUM(credits), 0) FROM payment_orders WHERE status = 'paid'
                    AND (? IS NULL OR user_id != ?))
                    AS total_revenue_credits
                FROM users WHERE (? IS NULL OR user_id != ?)""", (admin_id,) * 4,
        ) as cursor:
            return dict(await cursor.fetchone())


async def record_first_start(user_id: int, payload: str | None = None, *, timestamp_ms: int | None = None) -> bool:
    """Атомарно сохранить источник и дату первого запуска, без перезаписи."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Некорректный user_id.")
    source = payload.strip() if isinstance(payload, str) else None
    if source and not source.startswith('share_'):
        campaign = unquote(source).split('?', 1)[0].split('&', 1)[0]
        if campaign in {'vk_ads', 'yandex_ads', 'yandex_newyear'}:
            source = campaign
    if source and "yandex_newyear" in source and not source.startswith("share_"):
        source = "yandex_newyear"
    if source not in {"vk_ads", "yandex_ads", "yandex_newyear"}:
        source = "unmarked"
    started_at = (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        if timestamp_ms is not None else datetime.now(timezone.utc)
    ).isoformat(timespec="milliseconds")
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute(
            """INSERT INTO users (user_id, first_start_source, first_started_at, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 first_start_source = excluded.first_start_source,
                 first_started_at = excluded.first_started_at,
                 created_at = excluded.created_at
               WHERE users.first_start_source IS NULL""",
            (user_id, source, started_at, started_at),
        )
        await db.commit()
        return cursor.rowcount == 1


async def save_yclid(user_id: int, yclid: str | None = None) -> None:
    """Последний переданный click ID; запуск без метки не стирает прежний.

    Сам user_id (первичный ключ users) служит запасным UserId для Метрики.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError('Некорректный user_id.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (user_id,))
        if isinstance(yclid, str) and yclid.strip():
            await db.execute('UPDATE users SET yclid = ? WHERE user_id = ?', (yclid.strip(), user_id))
        await db.commit()


async def get_source_stats(admin_id: int | None = None) -> list[dict]:
    """Воронка по первому источнику, выручка в копейках только по paid-заказам."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COALESCE(u.first_start_source, 'legacy') AS source,
                 COUNT(*) AS total_users,
                 SUM(CASE WHEN u.first_started_at IS NOT NULL THEN 1 ELSE 0 END) AS new_users,
                 SUM(CASE WHEN u.total_generations > 0 THEN 1 ELSE 0 END) AS users_with_generation,
                 SUM(CASE WHEN u.total_purchases > 0 THEN 1 ELSE 0 END) AS buyers,
                 COALESCE(SUM(p.revenue_kopecks), 0) AS revenue_kopecks
               FROM users u
               LEFT JOIN (
                 SELECT user_id, SUM(amount_kopecks) AS revenue_kopecks
                 FROM (
                   SELECT user_id, amount_kopecks FROM payment_orders WHERE status = 'paid'
                   UNION ALL
                   SELECT user_id, amount_kopecks FROM audio_orders WHERE status = 'paid'
                 ) GROUP BY user_id
               ) p ON p.user_id = u.user_id
               WHERE (? IS NULL OR u.user_id != ?)
               GROUP BY COALESCE(u.first_start_source, 'legacy')""", (admin_id, admin_id),
        ) as cursor:
            rows = {row["source"]: dict(row) for row in await cursor.fetchall()}
    result = []
    for source, label in SOURCE_LABELS.items():
        if source == "legacy" and source not in rows:
            continue
        row = rows.get(source, {
            "source": source, "total_users": 0, "new_users": 0,
            "users_with_generation": 0, "buyers": 0, "revenue_kopecks": 0,
        })
        result.append({**row, "label": label})
    return result


async def get_stats_by_source(source_name: str, admin_id: int | None = None) -> dict:
    """Статистика первого источника; total_revenue — подтверждённые оплаты в рублях.

    Источник хранится в существующей TEXT-колонке first_start_source.
    Агрегация заказов до соединения исключает повторный подсчёт пользователей.
    """
    rows = await get_source_stats(admin_id=admin_id)
    row = next((row for row in rows if row["source"] == source_name), None)
    return {
        "total_users": row["total_users"] if row else 0,
        "total_generated": row["users_with_generation"] if row else 0,
        "total_buyers": row["buyers"] if row else 0,
        "total_revenue": Decimal(row["revenue_kopecks"] if row else 0) / 100,
    }


async def register_shared_image(image_url: str) -> str:
    """Сохранить короткую ссылку на уже созданное изображение, независимо от FSM."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute(
            "INSERT INTO shared_images (token, image_url) VALUES (?, ?) ON CONFLICT(image_url) DO NOTHING",
            (token_urlsafe(16), image_url),
        )
        async with db.execute("SELECT token FROM shared_images WHERE image_url = ?", (image_url,)) as cursor:
            row = await cursor.fetchone()
        await db.commit()
        return row[0]


async def get_shared_image(key: str) -> str | None:
    # Принимаются только зарегистрированные результаты. Внешний URL из payload
    # не скачивается произвольно; поддержан и старый формат share_<image_url>.
    if not isinstance(key, str) or not 1 <= len(key) <= 2048:
        return None
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute(
            "SELECT image_url FROM shared_images WHERE token = ? OR image_url = ?", (key, key)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


SONG_PROMO_PRODUCT = 'Песня в подарок (Скидка 20%)'


async def set_song_promo(user_id: int, hours=48) -> None:
    if hours <= 0:
        raise ValueError('Срок акции должен быть положительным.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (user_id,))
        await db.execute('UPDATE users SET song_promo_expires_at = ? WHERE user_id = ?',
                         (_now_ts() + int(hours * 3600), user_id))
        await db.commit()


async def check_song_promo(user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("""SELECT 1 FROM users WHERE user_id = ? AND song_promo_expires_at > ?
            AND NOT EXISTS (SELECT 1 FROM audio_orders WHERE user_id = ? AND audio_format = 'gift' AND status = 'paid')""",
                              (user_id, _now_ts(), user_id)) as cursor:
            return await cursor.fetchone() is not None


async def latest_gift_preview(user_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("""SELECT order_id FROM audio_orders
            WHERE user_id = ? AND audio_format = 'gift' AND status = 'pending' AND free_access = 0
              AND preview_url IS NOT NULL AND failed = 0
            ORDER BY created_at DESC, rowid DESC LIMIT 1""", (user_id,)) as cursor:
            row = await cursor.fetchone()
    return await get_audio_order(row[0]) if row else None


async def song_checkout_order(order_id: str) -> dict:
    """Проверить FIRST заново; выданные счета неизменны, для новой цены отдельный заказ.

    Готовые треки/превью переносятся без повторного обращения к TTAPI.
    """
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM audio_orders WHERE order_id = ?', (order_id,)) as cursor:
            row = await cursor.fetchone()
        if not row or row['audio_format'] != 'gift':
            raise ValueError('Подарочный заказ не найден.')
        order = dict(row)
        target_id = order_id
        if order['status'] != 'paid' and not order['free_access']:
            async with db.execute("""SELECT 1 FROM users WHERE user_id = ? AND song_promo_expires_at > ?
                AND NOT EXISTS (SELECT 1 FROM audio_orders WHERE user_id = ? AND audio_format = 'gift' AND status = 'paid')""",
                                  (order['user_id'], _now_ts(), order['user_id'])) as cursor:
                discount = await cursor.fetchone() is not None
            amount = 39200 if discount else 49000
            if order['amount_kopecks'] != amount:
                if not order['preview_url'] or not order['tracks_json']:
                    raise ValueError('Сначала необходимо создать превью.')
                target_id = order_id.split('-songprice-')[0] + '-songprice-' + str(amount)
                await db.execute("""INSERT INTO audio_orders
                    (order_id,user_id,audio_format,scenario,details,style,prompt,tags,title,amount_kopecks,product_name,
                     lyrics,tracks_json,preview_url,job_id,submit_started,preview_delivered)
                    SELECT ?,user_id,audio_format,scenario,details,style,prompt,tags,title,?,?,
                           lyrics,tracks_json,preview_url,job_id,submit_started,1
                    FROM audio_orders WHERE order_id = ?
                    ON CONFLICT(order_id) DO NOTHING""",
                    (target_id, amount, SONG_PROMO_PRODUCT if discount else 'Песня в подарок', order_id))
        await db.commit()
    return await get_audio_order(target_id)


async def create_audio_order(order_id: str, user_id: int, audio_format: str, scenario: str,
                             details: str, style: str | None, prompt: str, tags: str, title: str,
                             *, song_discount: bool = False) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0 or audio_format not in {'fun', 'gift'}:
        raise ValueError('Некорректный пользователь или аудиоформат.')
    if not scenario or not details.strip() or not prompt or len(prompt) > 5000 or len(tags) > 1000 or len(title) > 80:
        raise ValueError('Некорректные детали песни.')
    amount, product = (19900, 'Песня по фану') if audio_format == 'fun' else (49000, 'Песня в подарок')
    if song_discount:
        if audio_format != 'gift':
            raise ValueError('FIRST действует только на подарочную песню.')
        existing = await get_audio_order(order_id)
        if existing is None and not await check_song_promo(user_id):
            raise PromoUnavailableError('Скидка FIRST недоступна.')
        amount, product = 39200, SONG_PROMO_PRODUCT
    values = (order_id, user_id, audio_format, scenario, details, style, prompt, tags, title, amount, product)
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('''INSERT INTO audio_orders
            (order_id,user_id,audio_format,scenario,details,style,prompt,tags,title,amount_kopecks,product_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO NOTHING''', values)
        await db.commit()
    order = await get_audio_order(order_id)
    keys = ('order_id','user_id','audio_format','scenario','details','style','prompt','tags','title','amount_kopecks','product_name')
    if tuple(order[key] for key in keys) != values:
        raise ValueError('Нельзя изменить сохранённый аудиозаказ.')
    return order_id


async def get_audio_order(order_id: str) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM audio_orders WHERE order_id = ?', (order_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    order = dict(row)
    order.update(credits=0, package_id=('audio_gift_first' if order['amount_kopecks'] == 39200 else 'audio_' + order['audio_format']), order_type='audio')
    return order


async def mark_audio_paid(order_id: str, payment_id: str) -> bool:
    if not payment_id:
        raise ValueError('Не указан платёж.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM audio_orders WHERE order_id = ?', (order_id,)) as cursor:
            order = await cursor.fetchone()
        if order is None:
            raise ValueError('Аудиозаказ не найден.')
        if order['status'] == 'paid':
            if order['provider_payment_id'] != payment_id:
                raise ValueError('Заказ уже оплачен другим платежом.')
            return False
        async with db.execute('SELECT 1 FROM payment_orders WHERE provider_payment_id = ?', (payment_id,)) as cursor:
            if await cursor.fetchone():
                raise ValueError('Платёж уже использован для пакета кредитов.')
        await db.execute("UPDATE audio_orders SET status='paid', provider_payment_id=?, paid_at=CURRENT_TIMESTAMP WHERE order_id=?", (payment_id,order_id))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (order['user_id'],))
        await increment_purchases(order['user_id'], connection=db)
        await increment_counter('buy_audio_' + order['audio_format'], user_id=order['user_id'], connection=db)
        await db.commit()
    return True


async def update_audio_order(order_id: str, **fields) -> None:
    allowed = {'submit_started','job_id','tracks_json','running','delivered','failed','next_attempt_at','error',
               'lyrics','preview_url','preview_requested','preview_delivered','payment_link'}
    if not fields or not set(fields) <= allowed:
        raise ValueError('Некорректные поля аудиозаказа.')
    assignments = ','.join(key + '=?' for key in fields)
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute(f'UPDATE audio_orders SET {assignments} WHERE order_id=?', (*fields.values(), order_id))
        await db.commit()


async def reset_audio_workers() -> None:
    # Один экземпляр botmax; после перезапуска продолжаем только сохранённые задачи.
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE audio_orders SET running=0 WHERE running=1')
        await db.commit()


async def pending_audio_orders() -> list[str]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("SELECT order_id FROM audio_orders WHERE ((status='paid' OR free_access=1) AND delivered=0 OR (audio_format='gift' AND status='pending' AND free_access=0 AND preview_requested=1 AND preview_delivered=0)) AND failed=0 AND running=0 AND next_attempt_at <= ? ORDER BY created_at LIMIT 4", (_now_ts(),)) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def claim_audio_order(order_id: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute("UPDATE audio_orders SET running=1 WHERE order_id=? AND ((status='paid' OR free_access=1) AND delivered=0 OR (audio_format='gift' AND status='pending' AND free_access=0 AND preview_requested=1 AND preview_delivered=0)) AND failed=0 AND running=0 AND next_attempt_at <= ?", (order_id, _now_ts()))
        await db.commit()
        return cursor.rowcount == 1


async def find_gift_preview_order(user_id: int, scenario: str, details: str, style: str | None):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("SELECT order_id FROM audio_orders WHERE user_id=? AND audio_format='gift' AND scenario=? AND details=? AND style IS ? AND status='pending' AND preview_requested=1 AND failed=0 ORDER BY created_at DESC LIMIT 1", (user_id, scenario, details, style)) as cursor:
            row = await cursor.fetchone()
    return await get_audio_order(row[0]) if row else None


async def grant_audio_admin_access(order_id: str, user_id: int) -> bool:
    # Авторизацию ADMIN_USER_ID выполняет обработчик перед вызовом этой функции.
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute('UPDATE audio_orders SET free_access=1 WHERE order_id=? AND user_id=?', (order_id, user_id))
        await db.commit()
        return cursor.rowcount == 1


async def request_audio_regeneration(order_id: str, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute("""UPDATE audio_orders SET regeneration_used=1,
            previous_tracks_json=tracks_json, tracks_json=NULL, job_id=NULL, submit_started=0,
            delivered=0, failed=0, error=NULL, next_attempt_at=0
            WHERE order_id=? AND user_id=? AND (status='paid' OR free_access=1)
            AND regeneration_used=0 AND delivered=1 AND running=0 AND tracks_json IS NOT NULL""", (order_id,user_id))
        await db.commit()
        return cursor.rowcount == 1
