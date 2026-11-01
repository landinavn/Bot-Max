"""Постоянный баланс пользователей. Все изменения фиксируются в SQLite."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import aiosqlite

DATABASE_PATH = Path(__file__).resolve().parent / "database.sqlite3"
PROMO_SECONDS = 24 * 60 * 60
logger = logging.getLogger(__name__)


class PromoUnavailableError(ValueError):
    """Скидка истекла, ещё не открыта или уже использована."""


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

SOURCE_LABELS = {
    "vk_ads": "Реклама VK",
    "vk_posts": "Посты ВК",
    "dzen_posts": "Посты Дзена",
    "youtube": "YouTube",
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
            """CREATE TABLE IF NOT EXISTS photo_orders (
                order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tariff INTEGER NOT NULL,
                amount_kopecks INTEGER NOT NULL, product_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')),
                provider_payment_id TEXT UNIQUE, payment_link TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, paid_at TEXT
            )"""
        )
        async with db.execute('PRAGMA table_info(photo_orders)') as cursor:
            photo_columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in {
            'generation_status': "TEXT NOT NULL DEFAULT 'awaiting_payment'",
            'style': 'TEXT', 'template': 'TEXT',
            'user_notified': 'INTEGER NOT NULL DEFAULT 0',
            'admin_notified': 'INTEGER NOT NULL DEFAULT 0',
            'next_attempt_at': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if column not in photo_columns:
                await db.execute(f'ALTER TABLE photo_orders ADD COLUMN {column} {definition}')
        await db.execute("UPDATE photo_orders SET generation_status='queued' "
                         "WHERE status='paid' AND generation_status='awaiting_payment'")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS cartoon_orders (
                order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                theme TEXT NOT NULL, tariff TEXT NOT NULL,
                amount_kopecks INTEGER NOT NULL, product_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','completed','manual')),
                provider_payment_id TEXT UNIQUE, payment_link TEXT,
                photo_notified INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                result_url TEXT, fsm_data TEXT, fsm_state TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, paid_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS musical_orders (
                order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                image_url TEXT NOT NULL, audio_url TEXT NOT NULL, tariff TEXT NOT NULL,
                amount_kopecks INTEGER NOT NULL, product_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', provider_payment_id TEXT UNIQUE,
                payment_link TEXT, result_url TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                running INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, paid_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS pending_videos (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                avatar_url TEXT NOT NULL,
                song_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                result_url TEXT,
                mode TEXT NOT NULL DEFAULT '',
                render_kind TEXT NOT NULL DEFAULT 'kling'
            )"""
        )
        async with db.execute('PRAGMA table_info(pending_videos)') as cursor:
            pending_columns = {row[1] for row in await cursor.fetchall()}
        if 'result_url' not in pending_columns:
            await db.execute('ALTER TABLE pending_videos ADD COLUMN result_url TEXT')
        if 'mode' not in pending_columns:
            await db.execute("ALTER TABLE pending_videos ADD COLUMN mode TEXT NOT NULL DEFAULT ''")
        if 'render_kind' not in pending_columns:
            await db.execute("ALTER TABLE pending_videos ADD COLUMN render_kind TEXT NOT NULL DEFAULT 'kling'")
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
        await _init_animation_orders(db)
        await _init_repeat_promos(db)
        await _backfill_sale_counters(db, force=not counters_exist or not user_counters_exist)
        await db.commit()


async def save_pending_video(user_id: int, avatar_url: str, song_url: str, *, mode: str = '', render_kind: str = 'kling') -> int:
    """Сохранить данные до ответа клиенту; повторное нажатие не дублирует pending-заказ."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError('Некорректный user_id видео.')
    if not isinstance(avatar_url, str) or not avatar_url.startswith('https://'):
        raise ValueError('Не задана HTTPS-ссылка на аватар.')
    if not isinstance(song_url, str):
        raise ValueError('Некорректная ссылка на песню.')
    if not isinstance(mode, str):
        raise ValueError('Некорректный стиль видео.')
    if render_kind not in {'kling', 'musical'}:
        raise ValueError('Некорректный способ создания видео.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute(
            "SELECT id FROM pending_videos WHERE user_id=? AND avatar_url=? AND song_url=? AND mode=? AND render_kind=? AND status='pending' ORDER BY id LIMIT 1",
            (user_id, avatar_url, song_url, mode, render_kind),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            return existing[0]
        cursor = await db.execute('INSERT INTO pending_videos(user_id, avatar_url, song_url, mode, render_kind) VALUES (?, ?, ?, ?, ?)',
                                  (user_id, avatar_url, song_url, mode, render_kind))
        await db.commit()
        return cursor.lastrowid


async def get_all_pending_videos() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pending_videos WHERE status='pending' ORDER BY id") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def save_pending_video_result(video_id: int, result_url: str) -> None:
    """Кеш результата: сбой отправки MAX не требует ещё одной платной генерации."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("UPDATE pending_videos SET result_url=? WHERE id=? AND status='pending'", (result_url, video_id))
        await db.commit()


async def mark_video_completed(video_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("UPDATE pending_videos SET status='completed' WHERE id=? AND status='pending'", (video_id,))
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
            async with db.execute('SELECT 1 FROM musical_orders WHERE provider_payment_id=?', (provider_payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован для музыкальной открытки.')
            async with db.execute('SELECT 1 FROM cartoon_orders WHERE provider_payment_id=?', (provider_payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован для мульт-открытки.')
            async with db.execute('SELECT 1 FROM photo_orders WHERE provider_payment_id=?', (provider_payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован для фотосессии.')
            async with db.execute('SELECT 1 FROM animation_orders WHERE provider_payment_id=?', (provider_payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован для оживления фото.')
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
            await _record_sale_counters(db, 'credit', dict(order))
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
        if campaign in {'vk_ads', 'yandex_ads', 'yandex_newyear', 'vk_posts', 'dzen_posts', 'youtube'}:
            source = campaign
    if source and "yandex_newyear" in source and not source.startswith("share_"):
        source = "yandex_newyear"
    if source not in {"vk_ads", "yandex_ads", "yandex_newyear", "vk_posts", "dzen_posts", "youtube"}:
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
                   UNION ALL
                   SELECT user_id, amount_kopecks FROM musical_orders WHERE status = 'paid'
                   UNION ALL
                   SELECT user_id, amount_kopecks FROM cartoon_orders WHERE status IN ('paid','completed','manual')
                   UNION ALL
                   SELECT user_id, amount_kopecks FROM photo_orders WHERE status = 'paid'
                   UNION ALL
                   SELECT user_id, amount_kopecks FROM animation_orders WHERE status = 'paid' AND is_admin=0
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
    # SQL GROUP BY already guarantees uniqueness; keep unknown source names too.
    for source in sorted(rows.keys() - SOURCE_LABELS.keys()):
        result.append({**rows[source], "label": source})
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
    """Новые подарочные счета — 490 ₽; старые счета неизменны, для новой цены отдельный заказ.

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
            amount = 49000
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
                    (target_id, amount, 'Песня в подарок', order_id))
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
        raise PromoUnavailableError('Скидка FIRST больше не действует. Песня в подарок стоит 490 ₽.')
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
        async with db.execute('SELECT 1 FROM musical_orders WHERE provider_payment_id=?', (payment_id,)) as cursor:
            if await cursor.fetchone():
                raise ValueError('Платёж уже использован для музыкальной открытки.')
        async with db.execute('SELECT 1 FROM cartoon_orders WHERE provider_payment_id=?', (payment_id,)) as cursor:
            if await cursor.fetchone():
                raise ValueError('Платёж уже использован для мульт-открытки.')
        async with db.execute('SELECT 1 FROM photo_orders WHERE provider_payment_id=?', (payment_id,)) as cursor:
            if await cursor.fetchone():
                raise ValueError('Платёж уже использован для фотосессии.')
        async with db.execute('SELECT 1 FROM animation_orders WHERE provider_payment_id=?', (payment_id,)) as cursor:
            if await cursor.fetchone():
                raise ValueError('Платёж уже использован для оживления фото.')
        await db.execute("UPDATE audio_orders SET status='paid', provider_payment_id=?, paid_at=CURRENT_TIMESTAMP WHERE order_id=?", (payment_id,order_id))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (order['user_id'],))
        await increment_purchases(order['user_id'], connection=db)
        await increment_counter('buy_audio_' + order['audio_format'], user_id=order['user_id'], connection=db)
        await _record_sale_counters(db, 'audio', dict(order))
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


async def claim_audio_order(order_id: str, *, inline_preview: bool = False) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute("""UPDATE audio_orders SET running=1,
            preview_requested=CASE WHEN ? THEN 0 ELSE preview_requested END
            WHERE order_id=? AND ((status='paid' OR free_access=1) AND delivered=0 OR
            (audio_format='gift' AND status='pending' AND free_access=0 AND (preview_requested=1 OR ?)
             AND preview_delivered=0)) AND failed=0 AND running=0 AND (next_attempt_at <= ? OR ?)""",
            (inline_preview, order_id, inline_preview, _now_ts(), inline_preview))
        await db.commit()
        return cursor.rowcount == 1


async def find_gift_preview_order(user_id: int, scenario: str, details: str, style: str | None):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("SELECT order_id FROM audio_orders WHERE user_id=? AND audio_format='gift' AND scenario=? AND details=? AND style IS ? AND status='pending' AND failed=0 ORDER BY created_at DESC LIMIT 1", (user_id, scenario, details, style)) as cursor:
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


MUSICAL_TARIFFS = {
    'direct': (29900, 'Музыкальная открытка'),
    'upsell': (14900, 'Апгрейд открытки с песней'),
}


async def create_musical_order(order_id: str, user_id: int, image_url: str, audio_url: str, tariff: str):
    from urllib.parse import urlsplit
    if not isinstance(order_id,str) or not order_id.startswith(('music_','promo_music_card_')) or len(order_id)>200:
        raise ValueError('Некорректный номер музыкального заказа.')
    if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0 or tariff not in MUSICAL_TARIFFS:
        raise ValueError('Некорректный покупатель или музыкальный тариф.')
    for url in (image_url,audio_url):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Музыкальному заказу нужны HTTPS-ссылки на картинку и полную песню.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        result=await _create_repeat_order(db,'musical_orders',order_id,
            {'user_id':user_id,'image_url':image_url,'audio_url':audio_url,'tariff':tariff},
            *MUSICAL_TARIFFS[tariff],category='music_card' if tariff=='direct' else None)
        await db.commit()
        return result


async def get_musical_order(order_id: str):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM musical_orders WHERE order_id=?', (order_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    order = dict(row)
    order.update(order_type='musical', credits=0, package_id='music_' + order['tariff'])
    return order


async def update_musical_order(order_id: str, **fields):
    allowed = {'payment_link','result_url','delivered','running','attempts','failed','next_attempt_at','error','promo_due'}
    if not fields or not set(fields) <= allowed:
        raise ValueError('Некорректные поля музыкального заказа.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE musical_orders SET ' + ','.join(key+'=?' for key in fields) + ' WHERE order_id=?', (*fields.values(),order_id))
        await db.commit()


async def mark_musical_paid(order_id: str, payment_id: str) -> bool:
    if not payment_id:
        raise ValueError('Не указан платёж.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM musical_orders WHERE order_id=?', (order_id,)) as cursor:
            order = await cursor.fetchone()
        if order is None:
            raise ValueError('Музыкальный заказ не найден.')
        if order['status'] == 'paid':
            if order['provider_payment_id'] != payment_id:
                raise ValueError('Заказ уже оплачен другим платежом.')
            return False
        for table in ('payment_orders', 'audio_orders', 'cartoon_orders', 'photo_orders', 'animation_orders'):
            async with db.execute(f'SELECT 1 FROM {table} WHERE provider_payment_id=?', (payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован другим заказом.')
        await db.execute("UPDATE musical_orders SET status='paid', provider_payment_id=?, paid_at=CURRENT_TIMESTAMP WHERE order_id=?", (payment_id,order_id))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (order['user_id'],))
        await increment_purchases(order['user_id'], connection=db)
        await _record_sale_counters(db, 'musical', dict(order))
        await _consume_repeat_promo(db, dict(order), 'music_card')
        await db.commit()
    return True


async def claim_musical_order(order_id: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute("UPDATE musical_orders SET running=1 WHERE order_id=? AND status='paid' AND delivered=0 AND failed=0 AND running=0 AND next_attempt_at<=?", (order_id,_now_ts()))
        await db.commit()
        return cursor.rowcount == 1


async def pending_musical_orders():
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("SELECT order_id FROM musical_orders WHERE status='paid' AND delivered=0 AND failed=0 AND running=0 AND next_attempt_at<=? ORDER BY created_at LIMIT 2", (_now_ts(),)) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def reset_musical_workers():
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE musical_orders SET running=0 WHERE running=1')
        await db.commit()



CARTOON_TARIFFS = {
    'short': (59000, 'Мульт-открытка'),
    'long': (129000, 'Мульт-поздравление (до 30 сек)'),
}
CARTOON_ORDER_THEMES = frozenset({
    'theme_ny', 'theme_bday', 'theme_bday_kid', 'theme_valentine',
    'theme_feb23', 'theme_mar8', 'theme_mother', 'theme_father',
})


async def create_cartoon_order(order_id: str, user_id: int, theme: str, tariff: str):
    if (not isinstance(order_id,str) or not order_id.startswith(('cartoon_','promo_cartoon_')) or len(order_id)>200
            or isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0
            or theme not in CARTOON_ORDER_THEMES or tariff not in CARTOON_TARIFFS):
        raise ValueError('Некорректный мульт-заказ.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        result=await _create_repeat_order(db,'cartoon_orders',order_id,
            {'user_id':user_id,'theme':theme,'tariff':tariff},*CARTOON_TARIFFS[tariff],category='cartoon')
        await db.commit()
        return result


async def get_cartoon_order(order_id: str):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM cartoon_orders WHERE order_id=?', (order_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    order = dict(row)
    order.update(order_type='cartoon', credits=0, package_id='cartoon_' + order['tariff'])
    return order


async def update_cartoon_order(order_id: str, **fields):
    if not fields or not set(fields) <= {'payment_link','photo_notified','next_attempt_at','result_url','fsm_data','fsm_state'}:
        raise ValueError('Некорректные поля мульт-заказа.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE cartoon_orders SET ' + ','.join(key+'=?' for key in fields) + ' WHERE order_id=?', (*fields.values(),order_id))
        await db.commit()


async def mark_cartoon_paid(order_id: str, payment_id: str) -> bool:
    if not payment_id:
        raise ValueError('Не указан платёж.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM cartoon_orders WHERE order_id=?', (order_id,)) as cursor:
            order = await cursor.fetchone()
        if order is None:
            raise ValueError('Мульт-заказ не найден.')
        if order['status'] != 'pending':
            if order['provider_payment_id'] != payment_id:
                raise ValueError('Заказ уже оплачен другим платежом.')
            return False
        for table in ('payment_orders', 'audio_orders', 'musical_orders', 'photo_orders', 'animation_orders'):
            async with db.execute(f'SELECT 1 FROM {table} WHERE provider_payment_id=?', (payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован другим заказом.')
        await db.execute("UPDATE cartoon_orders SET status='paid', provider_payment_id=?, paid_at=CURRENT_TIMESTAMP WHERE order_id=?", (payment_id,order_id))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (order['user_id'],))
        await increment_purchases(order['user_id'], connection=db)
        await increment_counter('buy_cartoon_' + order['tariff'], user_id=order['user_id'], connection=db)
        await _record_sale_counters(db, 'cartoon', dict(order))
        await _consume_repeat_promo(db, dict(order), 'cartoon')
        await db.commit()
    return True


async def pending_cartoon_notifications():
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM cartoon_orders WHERE status='paid' AND photo_notified=0 AND next_attempt_at<=? ORDER BY created_at LIMIT 10", (_now_ts(),)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def latest_paid_cartoon(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        async with db.execute("SELECT order_id FROM cartoon_orders WHERE user_id=? AND status='paid' ORDER BY paid_at DESC, rowid DESC LIMIT 1", (user_id,)) as cursor:
            row = await cursor.fetchone()
    return await get_cartoon_order(row[0]) if row else None


async def finish_cartoon_order(order_id: str, user_id: int, *, manual=False):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute("UPDATE cartoon_orders SET status=?,promo_due=? WHERE order_id=? AND user_id=? AND status='paid'", ('manual' if manual else 'completed',0 if manual else 1,order_id,user_id))
        await db.commit()


PHOTO_TARIFFS = {
    590: (59000, 'Фотосессия Попробовать'),
    990: (99000, 'Фотосессия Стандарт'),
    1490: (149000, 'Фотосессия Премиум'),
}


def _base_sale_counter_keys(kind: str, order: dict) -> tuple[str, ...]:
    """Classify by validated order family and tariff; equal prices are not products."""
    if kind == 'credit':
        credits = order['credits']
        keys = {1: ('buy_credit_99',), 3: ('buy_credit_199',),
                5: ('buy_credit_299',), 15: ('buy_credit_799',)}.get(credits, ())
        return keys + (('buy_credit_149',) if credits == 3 and order['package_id'] == 'promo_3' else ())
    if kind == 'audio':
        return ('buy_song_199' if order['audio_format'] == 'fun' else 'buy_song_490',)
    if kind == 'musical':
        return (('buy_music_direct',) if order['tariff'] == 'direct'
                else ('buy_music_upsell', 'buy_music_card_149'))
    if kind == 'cartoon':
        return ('buy_cartoon_590' if order['tariff'] == 'short' else 'buy_cartoon_1290',)
    if kind == 'photo':
        return (f"buy_photo_{order['tariff']}",)
    if kind == 'animation':
        return (f"buy_animation_{order['tariff']}",)
    raise ValueError('Неизвестный тип продажи.')


async def _record_sale_counters(db, kind: str, order: dict) -> None:
    for key in sale_counter_keys(kind, order):
        await increment_counter(key, user_id=order['user_id'], connection=db)


async def _backfill_sale_counters(db, *, force=False) -> None:
    """One-time migration from confirmed orders, within init_db's transaction."""
    await db.execute('CREATE TABLE IF NOT EXISTS stats_migrations (name TEXT PRIMARY KEY)')
    async with db.execute("SELECT 1 FROM stats_migrations WHERE name='sales_v2'") as cursor:
        if await cursor.fetchone() and not force:
            return
    totals = {}
    for kind, table, fields, status in (
        ('credit', 'payment_orders', 'credits,package_id', "status='paid'"),
        ('audio', 'audio_orders', 'audio_format', "status='paid'"),
        ('musical', 'musical_orders', 'tariff,promo_token', "status='paid'"),
        ('cartoon', 'cartoon_orders', 'tariff,promo_token', "status IN ('paid','completed','manual')"),
        ('photo', 'photo_orders', 'tariff,promo_token', "status='paid'"),
        ('animation', 'animation_orders', 'tariff', "status='paid' AND is_admin=0"),
    ):
        async with db.execute(f'SELECT user_id,{fields},COUNT(*) FROM {table} WHERE {status} GROUP BY user_id,{fields}') as cursor:
            names = [column[0] for column in cursor.description]
            for row in await cursor.fetchall():
                order = dict(zip(names, row))
                for key in sale_counter_keys(kind, order):
                    pair = (order['user_id'], key)
                    totals[pair] = totals.get(pair, 0) + row[-1]
    global_totals = {}
    for (user_id, key), count in totals.items():
        await db.execute('INSERT INTO user_counters(user_id,event_name,count) VALUES (?,?,?) '
                         'ON CONFLICT(user_id,event_name) DO NOTHING', (user_id, key, count))
        global_totals[key] = global_totals.get(key, 0) + count
    for key, count in global_totals.items():
        await db.execute('INSERT INTO global_counters(event_name,count) VALUES (?,?) '
                         'ON CONFLICT(event_name) DO NOTHING', (key, count))
    await db.execute("INSERT INTO stats_migrations(name) VALUES ('sales_v2') ON CONFLICT(name) DO NOTHING")


async def create_photo_order(order_id: str, user_id: int, tariff: int = 590):
    if (not isinstance(order_id,str) or not order_id.startswith(('photo_','promo_photo_')) or len(order_id)>200
            or isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0
            or isinstance(tariff,bool) or tariff not in PHOTO_TARIFFS):
        raise ValueError('Некорректный фотозаказ.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        result=await _create_repeat_order(db,'photo_orders',order_id,
            {'user_id':user_id,'tariff':tariff},*PHOTO_TARIFFS[tariff],category='photo')
        await db.commit()
        return result


async def get_photo_order(order_id: str):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM photo_orders WHERE order_id=?', (order_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    order = dict(row)
    order.update(order_type='photo', credits=0, package_id=f"photo_{order['tariff']}")
    return order


async def set_photo_payment_link(order_id: str, link: str):
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE photo_orders SET payment_link=? WHERE order_id=?', (link,order_id))
        await db.commit()


async def mark_photo_paid(order_id: str, payment_id: str) -> bool:
    if not payment_id:
        raise ValueError('Не указан платёж.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM photo_orders WHERE order_id=?', (order_id,)) as cursor:
            order = await cursor.fetchone()
        if order is None:
            raise ValueError('Фотозаказ не найден.')
        if order['status'] == 'paid':
            if order['provider_payment_id'] != payment_id:
                raise ValueError('Заказ уже оплачен другим платежом.')
            return False
        for table in ('payment_orders', 'audio_orders', 'musical_orders', 'cartoon_orders', 'animation_orders'):
            async with db.execute(f'SELECT 1 FROM {table} WHERE provider_payment_id=?', (payment_id,)) as cursor:
                if await cursor.fetchone():
                    raise ValueError('Платёж уже использован другим заказом.')
        await db.execute("UPDATE photo_orders SET status='paid',generation_status='queued',provider_payment_id=?,paid_at=CURRENT_TIMESTAMP WHERE order_id=?",
                         (payment_id,order_id))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (order['user_id'],))
        await increment_purchases(order['user_id'], connection=db)
        await _record_sale_counters(db, 'photo', dict(order))
        await _consume_repeat_promo(db, dict(order), 'photo')
        await db.commit()
    return True


async def update_photo_order(order_id: str, **fields):
    if not fields or not set(fields) <= {'style','template','generation_status','user_notified','admin_notified','next_attempt_at','reference_url','promo_due'}:
        raise ValueError('Некорректные поля фотозаказа.')
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE photo_orders SET '+','.join(key+'=?' for key in fields)+' WHERE order_id=?',
                         (*fields.values(),order_id))
        await db.commit()


async def get_pending_photo_jobs(*, notifications=False, notify_admin=False) -> list[dict]:
    """Paid photoshoots await fulfillment; no payment is acknowledged into a dead end."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM photo_orders WHERE status='paid' AND generation_status='queued'"
        params = ()
        if notifications:
            query += " AND (user_notified=0 OR (? AND admin_notified=0)) AND next_attempt_at <= ?"
            params = (int(notify_admin), _now_ts())
        async with db.execute(query + ' ORDER BY paid_at,order_id', params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


REPEAT_CATEGORIES = {'music_card','cartoon','photo'}
PHOTO_PACKAGE_SIZES = {590:3,990:5,1490:7}
REPEAT_SUFFIX = ' (скидка 20%)'


class PromoReservedError(ValueError):
    def __init__(self,order_id):
        self.order_id=order_id
        super().__init__('Скидка уже закреплена за неоплаченным заказом.')


def _promo_input(user_id,category):
    if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0 or category not in REPEAT_CATEGORIES:
        raise ValueError('Некорректный пользователь или категория промо.')


def repeat_discount(amount_kopecks: int) -> int:
    return amount_kopecks * 80 // 10000 * 100


async def _init_repeat_promos(db):
    await db.execute("""CREATE TABLE IF NOT EXISTS active_promos(
        user_id INTEGER NOT NULL,category TEXT NOT NULL,expires_at TIMESTAMP NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,token TEXT NOT NULL,reserved_order_id TEXT,
        PRIMARY KEY(user_id,category))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS promo_redemptions(
        token TEXT PRIMARY KEY,order_id TEXT NOT NULL,user_id INTEGER NOT NULL,category TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS promo_offers(
        delivery_key TEXT PRIMARY KEY,user_id INTEGER NOT NULL,category TEXT NOT NULL,
        expires_at TIMESTAMP NOT NULL,token TEXT NOT NULL,notified INTEGER NOT NULL DEFAULT 0,
        lease_until INTEGER NOT NULL DEFAULT 0)""")
    async with db.execute('PRAGMA table_info(promo_offers)') as cursor:
        offer_columns={row[1] for row in await cursor.fetchall()}
    if 'lease_until' not in offer_columns:
        await db.execute('ALTER TABLE promo_offers ADD COLUMN lease_until INTEGER NOT NULL DEFAULT 0')
    await db.execute("""CREATE TABLE IF NOT EXISTS photo_results(
        order_id TEXT NOT NULL,slot INTEGER NOT NULL,image_url TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(order_id,slot))""")
    for table in ('musical_orders','cartoon_orders','photo_orders'):
        async with db.execute(f'PRAGMA table_info({table})') as cursor:
            columns={row[1] for row in await cursor.fetchall()}
        fields={'promo_token':'TEXT','promo_expires_at':'TIMESTAMP','promo_base_amount_kopecks':'INTEGER',
                'promo_base_product_name':'TEXT','promo_due':'INTEGER NOT NULL DEFAULT 0'}
        if table=='photo_orders':
            fields['reference_url']='TEXT'
        for column,definition in fields.items():
            if column not in columns:
                await db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


async def set_promo(user_id,category,hours=24,*,connection=None):
    _promo_input(user_id,category)
    if isinstance(hours,bool) or not isinstance(hours,(int,float)) or not 0<hours<87600:
        raise ValueError('Некорректная длительность промо.')
    if connection is None:
        async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
            await db.execute('BEGIN IMMEDIATE')
            result=await set_promo(user_id,category,hours,connection=db)
            await db.commit()
            return result
    expires_at=_now_ts()+int(hours*3600)
    await connection.execute("""INSERT INTO active_promos(user_id,category,expires_at,token)
        VALUES (?,?,?,?) ON CONFLICT(user_id,category) DO UPDATE SET expires_at=excluded.expires_at,
        token=excluded.token,reserved_order_id=NULL,created_at=CURRENT_TIMESTAMP""",
        (user_id,category,expires_at,token_urlsafe(24)))
    logger.info(f"Промо 20% выдано: user={user_id}, category={category}, expires={expires_at}")
    return expires_at


async def _active_promo_row(db,user_id,category):
    db.row_factory=aiosqlite.Row
    await db.execute('DELETE FROM active_promos WHERE user_id=? AND category=? AND expires_at<=?',
                     (user_id,category,_now_ts()))
    async with db.execute('SELECT * FROM active_promos WHERE user_id=? AND category=?',(user_id,category)) as cursor:
        row=await cursor.fetchone()
    return dict(row) if row else None


async def get_active_promo(user_id,category):
    _promo_input(user_id,category)
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        row=await _active_promo_row(db,user_id,category)
        await db.commit()
        return row['expires_at'] if row else None


async def clear_promo(user_id,category,*,connection=None,token=None):
    _promo_input(user_id,category)
    if connection is None:
        async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
            await clear_promo(user_id,category,connection=db,token=token)
            await db.commit()
        return
    query='DELETE FROM active_promos WHERE user_id=? AND category=?'
    params=(user_id,category)
    if token is not None:
        query+=' AND token=?'
        params+= (token,)
    await connection.execute(query,params)


async def quote_repeat_price(user_id,category,base_amount,base_name):
    if await get_active_promo(user_id,category) is not None:
        return repeat_discount(base_amount),base_name+REPEAT_SUFFIX
    return base_amount,base_name


async def get_promo_reservation(user_id,category):
    _promo_input(user_id,category)
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        row=await _active_promo_row(db,user_id,category)
        await db.commit()
        return row['reserved_order_id'] if row else None


async def _create_repeat_order(db,table,order_id,identity,base_amount,base_name,*,category):
    db.row_factory=aiosqlite.Row
    async with db.execute(f'SELECT * FROM {table} WHERE order_id=?',(order_id,)) as cursor:
        existing=await cursor.fetchone()
    if existing:
        if any(existing[key]!=value for key,value in identity.items()):
            raise ValueError('Номер заказа уже связан с другими данными.')
        return order_id
    promo=await _active_promo_row(db,identity['user_id'],category) if category else None
    if promo and promo['reserved_order_id']:
        raise PromoReservedError(promo['reserved_order_id'])
    if not promo and order_id.startswith('promo_'):
        raise PromoUnavailableError('Срок действия скидки истёк.')
    amount,name=base_amount,base_name
    values={'order_id':order_id,**identity,'amount_kopecks':amount,'product_name':name}
    if promo:
        order_id=f"promo_{category}_{identity['user_id']}_{_now_ts()}_{token_urlsafe(6)}"
        values.update(order_id=order_id,amount_kopecks=repeat_discount(base_amount),product_name=base_name+REPEAT_SUFFIX,
                      promo_token=promo['token'],promo_expires_at=promo['expires_at'],
                      promo_base_amount_kopecks=base_amount,promo_base_product_name=base_name)
        await db.execute('UPDATE active_promos SET reserved_order_id=? WHERE user_id=? AND category=?',
                         (order_id,identity['user_id'],category))
        logger.info(f"Применена скидка 20%: user={identity['user_id']}, category={category}, amount={values['amount_kopecks']//100}")
    columns=','.join(values)
    await db.execute(f"INSERT INTO {table}({columns}) VALUES ({','.join('?' for _ in values)})",tuple(values.values()))
    return order_id


def repeat_order_value(order,category,base_value,*,creation=False):
    """Validate persisted discount snapshots; issued invoices survive expiry."""
    if not order.get('promo_token'):
        if order['order_id'].startswith('promo_'):
            raise ValueError('У скидочного заказа нет сохранённого промо.')
        return base_value
    if not order['order_id'].startswith(f'promo_{category}_{order["user_id"]}_'):
        raise ValueError('Категория скидочного заказа не соответствует продукту.')
    if category=='music_card' and order['tariff']!='direct':
        raise ValueError('Скидка музыкальной открытки не применяется к апгрейду.')
    base=(order['promo_base_amount_kopecks'],order['promo_base_product_name'])
    if not isinstance(base[0],int) or base[0]<=0 or not isinstance(base[1],str):
        raise ValueError('Некорректные данные скидочного заказа.')
    if creation and base!=base_value:
        raise ValueError('Для нового счёта нужен действующий тариф.')
    return repeat_discount(base[0]),base[1]+REPEAT_SUFFIX


async def validate_repeat_invoice(order,category):
    if not order.get('promo_token'):
        return
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        promo=await _active_promo_row(db,order['user_id'],category)
        await db.commit()
    if not promo or promo['token']!=order['promo_token'] or promo['reserved_order_id']!=order['order_id']:
        raise PromoUnavailableError('Срок действия скидки истёк или она уже использована.')


def repeat_sale_counter(kind,order):
    if not order.get('promo_token'):
        return None
    if kind=='musical':
        return 'promo_buy_music_card'
    if kind=='cartoon':
        return 'promo_buy_cartoon_'+('590' if order['tariff']=='short' else '1290')
    if kind=='photo':
        return f"promo_buy_photo_{order['tariff']}"
    return None


def sale_counter_keys(kind,order):
    keys=_base_sale_counter_keys(kind,order)
    key=repeat_sale_counter(kind,order)
    return keys+((key,) if key else ())


async def _consume_repeat_promo(db,order,category):
    token=order.get('promo_token')
    if not token:
        return
    await db.execute("""INSERT INTO promo_redemptions(token,order_id,user_id,category) VALUES (?,?,?,?)
        ON CONFLICT(token) DO NOTHING""",(token,order['order_id'],order['user_id'],category))
    async with db.execute('SELECT order_id FROM promo_redemptions WHERE token=?',(token,)) as cursor:
        used=await cursor.fetchone()
    if used[0]!=order['order_id']:
        raise ValueError('Скидка уже использована в другом заказе.')
    await clear_promo(order['user_id'],category,connection=db,token=token)


async def grant_repeat_offer(user_id,category,delivery_key):
    _promo_input(user_id,category)
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        db.row_factory=aiosqlite.Row
        if category=='photo':
            if not delivery_key.startswith('photo:'):
                raise ValueError('Не указан фотопакет для промо.')
            order_id=delivery_key.removeprefix('photo:')
            async with db.execute("SELECT tariff FROM photo_orders WHERE order_id=? AND user_id=? AND status='paid'",(order_id,user_id)) as cursor:
                order=await cursor.fetchone()
            async with db.execute('SELECT COUNT(*) FROM photo_results WHERE order_id=? AND delivered=1',(order_id,)) as cursor:
                delivered=(await cursor.fetchone())[0]
            if not order or delivered!=PHOTO_PACKAGE_SIZES[order['tariff']]:
                await db.commit()
                return False
        async with db.execute('SELECT * FROM promo_offers WHERE delivery_key=?',(delivery_key,)) as cursor:
            existing=await cursor.fetchone()
        if existing:
            if existing['user_id']!=user_id or existing['category']!=category:
                raise ValueError('Результат уже связан с другим промо.')
        else:
            expires=await set_promo(user_id,category,connection=db)
            promo=await _active_promo_row(db,user_id,category)
            await db.execute('INSERT INTO promo_offers(delivery_key,user_id,category,expires_at,token) VALUES (?,?,?,?,?)',
                             (delivery_key,user_id,category,expires,promo['token']))
        await db.commit()
        return True


async def pending_repeat_offers():
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("""SELECT o.* FROM promo_offers o JOIN active_promos p
            ON p.user_id=o.user_id AND p.category=o.category AND p.token=o.token
            WHERE o.notified=0 AND p.expires_at>? AND o.lease_until<=?""",(_now_ts(),_now_ts())) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def mark_repeat_offer_sent(delivery_key):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('UPDATE promo_offers SET notified=1,lease_until=0 WHERE delivery_key=?',(delivery_key,))
        await db.commit()


async def repeat_offers_due(admin_id=None):
    result=[]
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        for table,category,prefix,condition in (
            ('musical_orders','music_card','music',"status='paid' AND delivered=1"),
            ('cartoon_orders','cartoon','cartoon',"status='completed'"),
            ('photo_orders','photo','photo',"status='paid' AND generation_status='completed'")):
            async with db.execute(f"""SELECT user_id,order_id FROM {table}
                WHERE promo_due=1 AND {condition} AND (? IS NULL OR user_id!=?)
                AND NOT EXISTS(SELECT 1 FROM promo_offers WHERE delivery_key=?||':'||{table}.order_id)""",
                (admin_id,admin_id,prefix)) as cursor:
                result.extend((row[0],category,prefix+':'+row[1]) for row in await cursor.fetchall())
    return result


async def get_photo_results(order_id):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM photo_results WHERE order_id=? ORDER BY slot',(order_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_photo_package_balance(order_id):
    order=await get_photo_order(order_id)
    if not order or order['status']!='paid':
        raise ValueError('Оплаченный фотопакет не найден.')
    return PHOTO_PACKAGE_SIZES[order['tariff']]-len(await get_photo_results(order_id))


async def save_photo_result(order_id,slot,image_url):
    if isinstance(slot,bool) or not isinstance(slot,int) or not isinstance(image_url,str):
        raise ValueError('Некорректный кадр фотосессии.')
    parsed=urlsplit(image_url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Кадру фотосессии нужна HTTPS-ссылка.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute("SELECT tariff FROM photo_orders WHERE order_id=? AND status='paid'",(order_id,)) as cursor:
            order=await cursor.fetchone()
        if not order or not 1<=slot<=PHOTO_PACKAGE_SIZES[order[0]]:
            raise ValueError('В фотопакете нет такого кадра.')
        async with db.execute('SELECT image_url,delivered FROM photo_results WHERE order_id=? AND slot=?',(order_id,slot)) as cursor:
            old=await cursor.fetchone()
        if old and old[0]!=image_url:
            if old[1]:
                raise ValueError('Кадр уже отправлен; повторно списывать фото нельзя.')
            await db.execute('UPDATE photo_results SET image_url=? WHERE order_id=? AND slot=? AND delivered=0',
                             (image_url,order_id,slot))
        await db.execute('INSERT INTO photo_results(order_id,slot,image_url) VALUES (?,?,?) ON CONFLICT(order_id,slot) DO NOTHING',
                         (order_id,slot,image_url))
        await db.commit()


async def mark_photo_result_delivered(order_id,slot):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute("SELECT tariff FROM photo_orders WHERE order_id=? AND status='paid'",(order_id,)) as cursor:
            order=await cursor.fetchone()
        if not order:
            raise ValueError('Оплаченный фотопакет не найден.')
        changed=await db.execute('UPDATE photo_results SET delivered=1 WHERE order_id=? AND slot=?',(order_id,slot))
        if not changed.rowcount:
            raise ValueError('Кадр не был сохранён.')
        async with db.execute('SELECT COUNT(*) FROM photo_results WHERE order_id=? AND delivered=1',(order_id,)) as cursor:
            count=(await cursor.fetchone())[0]
        complete=count==PHOTO_PACKAGE_SIZES[order[0]]
        if complete:
            await db.execute("UPDATE photo_orders SET generation_status='completed',promo_due=1 WHERE order_id=?",(order_id,))
        await db.commit()
        return complete


async def claim_repeat_offer(delivery_key):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        now=_now_ts()
        changed=await db.execute("""UPDATE promo_offers SET lease_until=?
            WHERE delivery_key=? AND notified=0 AND lease_until<=?
            AND EXISTS(SELECT 1 FROM active_promos p WHERE p.user_id=promo_offers.user_id
                       AND p.category=promo_offers.category AND p.token=promo_offers.token AND p.expires_at>?)""",
            (now+300,delivery_key,now,now))
        await db.commit()
        return changed.rowcount==1


async def release_repeat_offer(delivery_key):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('UPDATE promo_offers SET lease_until=0 WHERE delivery_key=? AND notified=0',(delivery_key,))
        await db.commit()


# Independent prepaid balance: one unit is one five-second animated photograph.
ANIMATION_TARIFFS = {
    'intro': (19900, 1, 'Оживление фотографии — первый запуск'),
    'one': (24900, 1, 'Оживление 1 фотографии'),
    'three': (59900, 3, 'Оживление 3 фотографий'),
    'five': (89900, 5, 'Оживление 5 фотографий'),
}


async def _init_animation_orders(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS animation_orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tariff TEXT NOT NULL,
        amount_kopecks INTEGER NOT NULL, quantity INTEGER NOT NULL, product_name TEXT NOT NULL,
        package_id TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 0,
        order_type TEXT NOT NULL DEFAULT 'animation', status TEXT NOT NULL DEFAULT 'pending',
        provider_payment_id TEXT UNIQUE, payment_link TEXT, notified INTEGER NOT NULL DEFAULT 0,
        is_admin INTEGER NOT NULL DEFAULT 0, notify_next_attempt_at INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        paid_at TIMESTAMP)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS animation_jobs (
        job_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, user_id INTEGER NOT NULL,
        slot INTEGER NOT NULL, image_url TEXT NOT NULL, source_message_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', task_id TEXT, video_url TEXT,
        running INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0, error TEXT,
        UNIQUE(order_id,slot), UNIQUE(user_id,source_message_id))''')


async def animation_first_price(user_id):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        async with db.execute("SELECT 1 FROM animation_orders WHERE user_id=? AND status='paid' AND is_admin=0 LIMIT 1",(user_id,)) as cursor:
            return 249 if await cursor.fetchone() else 199


async def create_animation_order(user_id,package,*,admin=False):
    if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0 or package not in ('one','three','five'):
        raise ValueError('Неверный пакет оживления фото.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        if not admin:
            async with db.execute("SELECT * FROM animation_orders WHERE user_id=? AND status='pending' AND tariff IN (?,?) ORDER BY created_at LIMIT 1",
                                  (user_id,package,'intro' if package=='one' else package)) as cursor:
                row=await cursor.fetchone()
            if row:
                return dict(row)
        tariff=package
        if package=='one' and not admin:
            async with db.execute("SELECT 1 FROM animation_orders WHERE user_id=? AND is_admin=0 AND status='paid' LIMIT 1",(user_id,)) as cursor:
                if not await cursor.fetchone(): tariff='intro'
        amount,quantity,title=ANIMATION_TARIFFS[tariff]
        key=f'animate_{user_id}_{token_urlsafe(12)}'
        await db.execute('''INSERT INTO animation_orders(order_id,user_id,tariff,amount_kopecks,quantity,product_name,package_id,status,is_admin)
            VALUES (?,?,?,?,?,?,?,?,?)''',(key,user_id,tariff,amount,quantity,title,'animation_'+tariff,'paid' if admin else 'pending',int(admin)))
        await db.commit()
    return await get_animation_order(key)


async def get_animation_order(order_id):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM animation_orders WHERE order_id=?',(order_id,)) as cursor:
            row=await cursor.fetchone()
            return dict(row) if row else None


async def update_animation_order(order_id,**fields):
    if not fields or set(fields)-{'payment_link','notified','notify_next_attempt_at'}: raise ValueError('Неверное поле заказа.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('UPDATE animation_orders SET '+','.join(key+'=?' for key in fields)+' WHERE order_id=?',(*fields.values(),order_id))
        await db.commit()


async def mark_animation_paid(order_id,payment_id):
    if not payment_id: raise ValueError('Не указан платёж.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM animation_orders WHERE order_id=?',(order_id,)) as cursor:
            order=await cursor.fetchone()
        if not order or order['is_admin']: raise ValueError('Заказ не найден.')
        if order['status']=='paid':
            if order['provider_payment_id']!=payment_id: raise ValueError('Другой платёж.')
            return False
        for table in ('payment_orders','audio_orders','musical_orders','cartoon_orders','photo_orders','animation_orders'):
            async with db.execute(f'SELECT 1 FROM {table} WHERE provider_payment_id=?',(payment_id,)) as cursor:
                if await cursor.fetchone(): raise ValueError('Платёж уже использован.')
        await db.execute("UPDATE animation_orders SET status='paid',provider_payment_id=?,paid_at=CURRENT_TIMESTAMP WHERE order_id=?",(payment_id,order_id))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING',(order['user_id'],))
        await increment_purchases(order['user_id'],connection=db)
        await _record_sale_counters(db,'animation',dict(order))
        await db.commit()
        return True


async def animation_orders_for_user(user_id):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('''SELECT o.*,o.quantity-COUNT(j.job_id) AS remaining
            FROM animation_orders o LEFT JOIN animation_jobs j ON j.order_id=o.order_id
            WHERE o.user_id=? AND o.status='paid' GROUP BY o.order_id ORDER BY o.created_at''',(user_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def queue_animation_photo(user_id,order_id,image_url,message_id):
    url=urlsplit(image_url)
    if url.scheme!='https' or not url.hostname or url.username or url.password or not message_id:
        raise ValueError('Нужна HTTPS-ссылка на фото.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM animation_orders WHERE order_id=? AND user_id=? AND status=\'paid\'',(order_id,user_id)) as cursor:
            order=await cursor.fetchone()
        if not order: raise ValueError('Оплаченный пакет не найден.')
        async with db.execute('SELECT job_id FROM animation_jobs WHERE user_id=? AND source_message_id=?',(user_id,message_id)) as cursor:
            existing=await cursor.fetchone()
        if existing: return existing[0]
        async with db.execute('SELECT COUNT(*) FROM animation_jobs WHERE order_id=?',(order_id,)) as cursor:
            count=(await cursor.fetchone())[0]
        if count>=order['quantity']: raise ValueError('Все фотографии пакета уже отправлены.')
        key='animation_job_'+token_urlsafe(12)
        await db.execute('INSERT INTO animation_jobs(job_id,order_id,user_id,slot,image_url,source_message_id) VALUES (?,?,?,?,?,?)',
                         (key,order_id,user_id,count+1,image_url,message_id))
        await db.commit()
        return key


async def get_animation_job(job_id):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM animation_jobs WHERE job_id=?',(job_id,)) as cursor:
            row=await cursor.fetchone()
            return dict(row) if row else None


async def update_animation_job(job_id,**fields):
    if not fields or set(fields)-{'status','task_id','video_url','running','attempts','next_attempt_at','error'}:
        raise ValueError('Неверное поле задачи.')
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        if fields.get('video_url'):
            async with db.execute('SELECT user_id,video_url FROM animation_jobs WHERE job_id=?',(job_id,)) as cursor:
                old=await cursor.fetchone()
            if old and not old[1]: await increment_counter('gen_animation',user_id=old[0],connection=db)
        await db.execute('UPDATE animation_jobs SET '+','.join(key+'=?' for key in fields)+' WHERE job_id=?',(*fields.values(),job_id))
        await db.commit()


async def animation_work_items(*,reset=False):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        if reset:
            await db.execute('UPDATE animation_jobs SET running=0')
            await db.commit()
        async with db.execute("SELECT order_id FROM animation_orders WHERE status='paid' AND notified=0 AND notify_next_attempt_at<=? LIMIT 10",(_now_ts(),)) as cursor:
            notices=[row[0] for row in await cursor.fetchall()]
        async with db.execute("SELECT job_id FROM animation_jobs WHERE status IN ('queued','creating','processing','ready') AND running=0 AND next_attempt_at<=? LIMIT 2",(_now_ts(),)) as cursor:
            return notices,[row[0] for row in await cursor.fetchall()]


async def claim_animation_job(job_id):
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        cursor=await db.execute("UPDATE animation_jobs SET running=1 WHERE job_id=? AND running=0 AND status IN ('queued','creating','processing','ready') AND next_attempt_at<=?",(job_id,_now_ts()))
        await db.commit()
        return cursor.rowcount==1


async def get_photo_template_stats(template, *, admin_id=None):
    """Use paid orders and durable frame slots; selection after payment is attributed too."""
    async with aiosqlite.connect(DATABASE_PATH,timeout=30) as db:
        async with db.execute('''SELECT COUNT(*),COALESCE(SUM(amount_kopecks),0),
            COALESCE(SUM(CASE WHEN generation_status='completed' THEN 1 ELSE 0 END),0)
            FROM photo_orders WHERE status='paid' AND template=? AND (? IS NULL OR user_id!=?)''',
            (template,admin_id,admin_id)) as cursor:
            paid,revenue,completed=await cursor.fetchone()
        async with db.execute('''SELECT COUNT(*) FROM photo_results r JOIN photo_orders o ON o.order_id=r.order_id
            WHERE o.status='paid' AND o.template=? AND (? IS NULL OR o.user_id!=?)''',
            (template,admin_id,admin_id)) as cursor:
            generated=(await cursor.fetchone())[0]
    return {'paid_packages':paid,'revenue_kopecks':revenue,'generated_photos':generated,'completed_packages':completed}
