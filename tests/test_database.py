import asyncio

import pytest


async def test_trial_is_atomic_under_concurrent_starts(credit_db):
    grants = await asyncio.gather(*(credit_db.grant_free_trial(123) for _ in range(20)))
    assert sum(grants) == 1
    assert dict(await credit_db.get_user(123)) == {
        "user_id": 123, "balance": 1, "free_trial_used": 1,
        "total_generations": 0, "total_purchases": 0,
        "first_start_source": None, "first_started_at": None,
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


@pytest.mark.parametrize("payload,source", [
    ("vk_ads", "vk_ads"), ("yandex_ads", "yandex_ads"),
    (None, "unmarked"), ("", "unmarked"), ("  ", "unmarked"),
    ("unexpected-label", "unmarked"), ("<script>", "unmarked"),
])
async def test_first_start_records_source_and_event_date(credit_db, payload, source):
    assert await credit_db.record_first_start(123, payload, timestamp_ms=1792526400000)
    user = await credit_db.get_user(123)
    assert user["first_start_source"] == source
    assert user["first_started_at"] == "2026-10-20T20:00:00.000+00:00"
    assert user["balance"] == 0 and user["free_trial_used"] == 0


@pytest.mark.parametrize("first,later", [("vk_ads", "yandex_ads"), ("yandex_ads", None), (None, "vk_ads")])
async def test_first_source_and_date_are_not_overwritten(credit_db, first, later):
    await credit_db.record_first_start(123, first, timestamp_ms=1792526400000)
    original = dict(await credit_db.get_user(123))
    assert not await credit_db.record_first_start(123, later, timestamp_ms=1792612800000)
    await credit_db.init_db()
    assert dict(await credit_db.get_user(123)) == original


async def test_concurrent_first_starts_have_one_winner(credit_db):
    sources = ["vk_ads", "yandex_ads", None] * 4
    winners = await asyncio.gather(*(
        credit_db.record_first_start(123, source, timestamp_ms=1792526400000 + i * 1000)
        for i, source in enumerate(sources)
    ))
    assert sum(winners) == 1
    winner = winners.index(True)
    from datetime import datetime
    user = await credit_db.get_user(123)
    assert user["first_start_source"] == (sources[winner] or "unmarked")
    assert int(datetime.fromisoformat(user["first_started_at"]).timestamp() * 1000) == 1792526400000 + winner * 1000


async def test_source_migration_keeps_old_users_out_of_new_ad_cohorts(credit_db):
    import aiosqlite
    await credit_db.grant_free_trial(123)
    await credit_db.increment_generations(123)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("ALTER TABLE users DROP COLUMN first_start_source")
        await db.execute("ALTER TABLE users DROP COLUMN first_started_at")
        await db.commit()
    await credit_db.init_db()
    assert not await credit_db.record_first_start(123, "vk_ads", timestamp_ms=1792526400000)
    await credit_db.init_db()
    user = await credit_db.get_user(123)
    assert user["first_start_source"] == "legacy" and user["first_started_at"] is None
    assert user["balance"] == user["total_generations"] == 1
    rows = {row["source"]: row for row in await credit_db.get_source_stats()}
    assert rows["legacy"]["total_users"] == 1
    assert rows["legacy"]["new_users"] == 0
    assert rows["legacy"]["users_with_generation"] == 1
    assert rows["vk_ads"]["new_users"] == 0


async def test_source_funnel_counts_users_once_and_sums_only_paid_amounts(credit_db):
    await credit_db.record_first_start(101, "vk_ads")
    await credit_db.record_first_start(102, "vk_ads")
    await credit_db.record_first_start(201, "yandex_ads")
    await credit_db.record_first_start(301)
    await credit_db.increment_generations(101)
    await credit_db.increment_generations(101)
    await credit_db.increment_generations(201)
    for order, user_id, credits, amount in [
        ("vk-one", 101, 1, 9900), ("vk-five", 101, 5, 29900), ("ya-fifteen", 201, 15, 79900),
    ]:
        await credit_db.create_payment_order(order, user_id, credits, amount, "test")
        assert await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
        assert not await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
    await credit_db.create_payment_order("not-paid", 102, 15, 79900, "test")
    await credit_db.add_credits(301, 100)  # Manual credits are not ad revenue.
    await credit_db.record_first_start(101, "yandex_ads")  # Later ads must not move revenue.
    rows = {row["source"]: row for row in await credit_db.get_source_stats()}
    assert {key: rows["vk_ads"][key] for key in ("new_users", "users_with_generation", "buyers", "revenue_kopecks")} == {
        "new_users": 2, "users_with_generation": 1, "buyers": 1, "revenue_kopecks": 39800,
    }
    assert rows["yandex_ads"]["new_users"] == rows["yandex_ads"]["users_with_generation"] == rows["yandex_ads"]["buyers"] == 1
    assert rows["yandex_ads"]["revenue_kopecks"] == 79900
    assert rows["unmarked"]["new_users"] == 1 and rows["unmarked"]["revenue_kopecks"] == 0


async def test_empty_source_stats_include_zero_rows_for_all_ad_labels(credit_db):
    rows = await credit_db.get_source_stats()
    assert [row["label"] for row in rows] == ["Реклама VK", "Реклама Яндекса", "Без метки"]
    assert all(row["new_users"] == row["buyers"] == row["revenue_kopecks"] == 0 for row in rows)


async def test_share_tokens_are_persistent_and_do_not_create_users(credit_db):
    import re
    url = "https://storage.yandexcloud.net/bucket/vision-boards/example.png"
    tokens = await asyncio.gather(*(credit_db.register_shared_image(url) for _ in range(8)))
    assert len(set(tokens)) == 1
    assert re.fullmatch(r"[A-Za-z0-9_-]+", tokens[0])
    assert len("share_" + tokens[0]) <= 128
    await credit_db.init_db()
    assert await credit_db.get_shared_image(tokens[0]) == url
    assert await credit_db.get_shared_image(url) == url  # Registered legacy share_<URL>.
    assert await credit_db.get_shared_image("https://127.0.0.1/private") is None
    assert (await credit_db.get_global_stats())["total_users"] == 0
