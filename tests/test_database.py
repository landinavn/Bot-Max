import asyncio

import pytest


async def test_trial_is_atomic_under_concurrent_starts(credit_db):
    grants = await asyncio.gather(*(credit_db.grant_free_trial(123) for _ in range(20)))
    assert sum(grants) == 1
    assert dict(await credit_db.get_user(123)) == {
        "user_id": 123, "balance": 1, "free_trial_used": 1,
        "total_generations": 0, "total_purchases": 0,
    }


async def test_trial_survives_reinitialization_and_spending(credit_db):
    assert await credit_db.grant_free_trial(123)
    assert await credit_db.deduct_credit(123)
    await credit_db.init_db()
    assert not await credit_db.grant_free_trial(123)
    assert (await credit_db.get_user(123))["balance"] == 0


async def test_concurrent_deductions_never_go_negative(credit_db):
    await credit_db.add_credits(123, 5)
    charges = await asyncio.gather(*(credit_db.deduct_credit(123) for _ in range(20)))
    assert sum(charges) == 5
    assert (await credit_db.get_user(123))["balance"] == 0
    assert not await credit_db.deduct_credit(999)


async def test_marking_used_does_not_grant_or_reset_balance(credit_db):
    await credit_db.mark_free_trial_used(123)
    assert not await credit_db.grant_free_trial(123)
    await credit_db.add_credits(123, 5)
    await credit_db.mark_free_trial_used(123)
    assert (await credit_db.get_user(123))["balance"] == 5
    assert await credit_db.get_user(999) is None


@pytest.mark.parametrize("amount", [0, -1, 1.5, True])
async def test_invalid_credit_amount_does_not_create_user(credit_db, amount):
    with pytest.raises(ValueError):
        await credit_db.add_credits(123, amount)
    assert await credit_db.get_user(123) is None


async def test_empty_global_stats_are_zero(credit_db):
    assert await credit_db.get_global_stats() == {
        "total_users": 0, "total_paid_users": 0, "total_generated": 0,
        "total_purchases": 0, "total_revenue_credits": 0,
    }


async def test_atomic_counter_increments_create_users_without_free_credits(credit_db):
    await asyncio.gather(*(credit_db.increment_generations(123) for _ in range(12)))
    await asyncio.gather(*(credit_db.increment_purchases(456) for _ in range(7)))
    assert (await credit_db.get_user(123))["total_generations"] == 12
    assert (await credit_db.get_user(456))["total_purchases"] == 7
    assert (await credit_db.get_user(123))["balance"] == 0
    assert await credit_db.grant_free_trial(123)  # Counting is not a trial grant.
    await credit_db.init_db()
    assert (await credit_db.get_global_stats())["total_generated"] == 12


async def test_paid_and_manual_credits_have_distinct_stats(credit_db):
    await credit_db.grant_free_trial(123)
    await credit_db.add_credits(456, 100)  # A manual grant is not a purchase.
    for index, credits in enumerate((1, 5, 15)):
        user_id = 123 if index < 2 else 789
        order_id = f"order-{index}"
        await credit_db.create_payment_order(order_id, user_id, credits, 9900, "test")
        assert await credit_db.add_credits(user_id, credits, payment_order_id=order_id, provider_payment_id=order_id)
        assert not await credit_db.add_credits(user_id, credits, payment_order_id=order_id, provider_payment_id=order_id)
    await credit_db.create_payment_order("pending", 456, 15, 79900, "test")
    assert await credit_db.deduct_credit(123, count_generation=True)
    await credit_db.increment_generations(789)
    assert await credit_db.get_global_stats() == {
        "total_users": 3, "total_paid_users": 2, "total_generated": 2,
        "total_purchases": 3, "total_revenue_credits": 21,
    }


async def test_generation_count_and_balance_roll_back_together(credit_db, monkeypatch):
    from unittest.mock import AsyncMock
    import sqlite3
    await credit_db.add_credits(123, 1)
    with monkeypatch.context() as patch:
        patch.setattr(credit_db, "increment_generations", AsyncMock(side_effect=sqlite3.OperationalError("counter failed")))
        with pytest.raises(sqlite3.OperationalError):
            await credit_db.deduct_credit(123, count_generation=True)
    assert (await credit_db.get_user(123))["balance"] == 1
    assert (await credit_db.get_user(123))["total_generations"] == 0
    assert await credit_db.deduct_credit(123, count_generation=True)
    assert not await credit_db.deduct_credit(123, count_generation=True)
    assert (await credit_db.get_user(123))["total_generations"] == 1


async def test_legacy_migration_preserves_balances_and_backfills_only_paid_purchases(credit_db):
    import aiosqlite
    await credit_db.grant_free_trial(123)
    for index, credits in enumerate((1, 15)):
        await credit_db.create_payment_order(f"old-{index}", 123, credits, 9900, "test")
        await credit_db.add_credits(123, credits, payment_order_id=f"old-{index}", provider_payment_id=f"pay-{index}")
    await credit_db.create_payment_order("pending", 123, 5, 29900, "test")
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("ALTER TABLE users DROP COLUMN total_generations")
        await db.execute("ALTER TABLE users DROP COLUMN total_purchases")
        await db.commit()
    await credit_db.init_db()
    user = await credit_db.get_user(123)
    assert user["balance"] == 17 and user["free_trial_used"] == 1
    assert user["total_generations"] == 0 and user["total_purchases"] == 2
    await credit_db.increment_generations(123)
    await credit_db.increment_purchases(123)
    await credit_db.init_db()
    user = await credit_db.get_user(123)
    assert user["total_generations"] == 1 and user["total_purchases"] == 3
    assert (await credit_db.get_global_stats())["total_revenue_credits"] == 16


async def test_partial_migration_keeps_existing_counter(credit_db):
    import aiosqlite
    await credit_db.increment_generations(123)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("ALTER TABLE users DROP COLUMN total_purchases")
        await db.commit()
    await credit_db.init_db()
    assert (await credit_db.get_user(123))["total_generations"] == 1
    assert (await credit_db.get_user(123))["total_purchases"] == 0
