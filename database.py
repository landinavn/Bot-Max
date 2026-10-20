"""Постоянный баланс пользователей. Все изменения фиксируются в SQLite."""

from pathlib import Path

import aiosqlite

DATABASE_PATH = Path(__file__).resolve().parent / "database.sqlite3"


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
                total_purchases INTEGER NOT NULL DEFAULT 0 CHECK (total_purchases >= 0)
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
        await db.commit()
        return True


async def create_payment_order(
    order_id: str, user_id: int, credits: int, amount_kopecks: int, product_name: str,
) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        await db.execute(
            """INSERT INTO payment_orders
               (order_id, user_id, credits, amount_kopecks, product_name)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, user_id, credits, amount_kopecks, product_name),
        )
        await db.commit()


async def get_payment_order(order_id: str) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payment_orders WHERE order_id = ?", (order_id,)
        ) as cursor:
            return await cursor.fetchone()


async def deduct_credit(user_id: int, *, count_generation: bool = False) -> bool:
    """Атомарно списать кредит и при необходимости учесть новую генерацию."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute(
            "UPDATE users SET balance = balance - 1 WHERE user_id = ? AND balance >= 1",
            (user_id,),
        )
        charged = cursor.rowcount == 1
        if charged and count_generation:
            await increment_generations(user_id, connection=db)
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


async def increment_generations(user_id: int, *, connection: aiosqlite.Connection | None = None) -> None:
    """Учесть новую генерацию; connection связывает счётчик со списанием кредита."""
    await _increment_counter(user_id, "total_generations", connection)


async def increment_purchases(user_id: int, *, connection: aiosqlite.Connection | None = None) -> None:
    """Учесть покупку в транзакции оплаты, без повторного начисления webhook."""
    await _increment_counter(user_id, "total_purchases", connection)


async def get_global_stats() -> dict[str, int]:
    """Снимок счётчиков и точного количества кредитов из оплаченных заказов."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) AS total_users,
                COALESCE(SUM(CASE WHEN total_purchases > 0 THEN 1 ELSE 0 END), 0) AS total_paid_users,
                COALESCE(SUM(total_generations), 0) AS total_generated,
                COALESCE(SUM(total_purchases), 0) AS total_purchases,
                (SELECT COALESCE(SUM(credits), 0) FROM payment_orders WHERE status = 'paid')
                    AS total_revenue_credits
                FROM users"""
        ) as cursor:
            return dict(await cursor.fetchone())
