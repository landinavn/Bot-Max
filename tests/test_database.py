import asyncio

import pytest


async def test_trial_is_atomic_under_concurrent_starts(credit_db):
    grants = await asyncio.gather(*(credit_db.grant_free_trial(123) for _ in range(20)))
    assert sum(grants) == 1
    user = dict(await credit_db.get_user(123))
    assert user.pop("created_at") is not None
    assert user == {
        "user_id": 123, "balance": 1, "free_trial_used": 1,
        "total_generations": 0, "total_purchases": 0,
        "first_start_source": None, "first_started_at": None,
        "is_active": 1, "days_active": 1, "last_active_at": None,
        "promo_expires_at": None, "promo_redeemed_at": None, "promo_campaign_id": None,
        "song_promo_expires_at": None, "yclid": "",
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
    ("yandex_newyear", "yandex_newyear"), ("start=yandex_newyear_campaign", "yandex_newyear"),
    ("share_https://example.com/yandex_newyear.png", "unmarked"),
    (None, "unmarked"), ("", "unmarked"), ("  ", "unmarked"),
    ("unexpected-label", "unmarked"), ("<script>", "unmarked"),
])
async def test_first_start_records_source_and_event_date(credit_db, payload, source):
    assert await credit_db.record_first_start(123, payload, timestamp_ms=1792526400000)
    user = await credit_db.get_user(123)
    assert user["first_start_source"] == source
    assert user["first_started_at"] == "2026-10-20T20:00:00.000+00:00"
    assert user["balance"] == 0 and user["free_trial_used"] == 0


@pytest.mark.parametrize("first,later", [("vk_ads", "yandex_ads"), ("yandex_ads", None), (None, "vk_ads"), ("yandex_newyear", "vk_ads"), (None, "yandex_newyear"), ("vk_ads", "yandex_newyear")])
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
    assert [row["label"] for row in rows] == ["Реклама VK", "Реклама Яндекса", "Yandex New Year", "Без метки"]
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


async def test_global_counters_atomic_persistent_and_independent(credit_db):
    assert await credit_db.get_all_counters() == {}
    await asyncio.gather(*(credit_db.increment_counter("gen_ny") for _ in range(20)))
    await credit_db.increment_counter("gen_vision")
    await credit_db.init_db()
    assert await credit_db.get_all_counters() == {"gen_ny": 20, "gen_vision": 1}


@pytest.mark.parametrize("event", [None, "", "   ", 123])
async def test_global_counters_reject_empty_names(credit_db, event):
    with pytest.raises(ValueError):
        await credit_db.increment_counter(event)
    assert await credit_db.get_all_counters() == {}


async def test_package_counter_migration_backfills_paid_orders_once(credit_db):
    import aiosqlite
    for credits, amount in [(1, 9900), (5, 29900), (15, 79900), (15, 69900)]:
        order = f"order-{credits}-{amount}"
        await credit_db.create_payment_order(order, 456, credits, amount, "package")
        await credit_db.add_credits(456, credits, payment_order_id=order, provider_payment_id=order)
    await credit_db.create_payment_order("pending", 456, 1, 9900, "package")
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("DROP TABLE global_counters")
        await db.commit()
    await credit_db.init_db()
    assert await credit_db.get_all_counters() == {"buy_1": 1, "buy_5": 1, "buy_15": 2}
    await credit_db.increment_counter("gen_scrapbook")
    await credit_db.init_db()
    assert await credit_db.get_all_counters() == {"buy_1": 1, "buy_5": 1, "buy_15": 2, "gen_scrapbook": 1}


@pytest.mark.parametrize("admin", [True, False])
async def test_generation_counter_failure_rolls_back_user_and_balance(credit_db, monkeypatch, admin):
    from unittest.mock import AsyncMock
    import sqlite3
    await credit_db.add_credits(456, 2)
    with monkeypatch.context() as patch:
        patch.setattr(credit_db, "increment_counter", AsyncMock(side_effect=sqlite3.OperationalError("counter failed")))
        with pytest.raises(sqlite3.OperationalError):
            if admin:
                await credit_db.increment_generations(456, event_name="gen_vision")
            else:
                await credit_db.deduct_credit(456, count_generation=True, generation_event="gen_vision")
    user = await credit_db.get_user(456)
    assert user["balance"] == 2 and user["total_generations"] == 0
    assert await credit_db.get_all_counters() == {}


async def test_failed_charge_does_not_increment_generation_counter(credit_db):
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456)
    assert not await credit_db.deduct_credit(456, count_generation=True, generation_event="gen_vision")
    assert await credit_db.get_all_counters() == {}


def activity_timestamp(iso):
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


async def test_empty_advanced_stats(credit_db):
    assert await credit_db.get_advanced_stats() == {
        "total_all": 0, "total_active": 0, "total_blocked": 0, "total_returned": 0,
    }


async def test_unique_activity_days_are_concurrent_restart_safe_and_use_moscow(credit_db):
    first = activity_timestamp("2026-10-21T20:59:59+00:00")  # 23:59 Moscow
    second = first + 1000  # Midnight Moscow, still October 21 in UTC.
    await asyncio.gather(*(credit_db.update_user_activity(123, timestamp_ms=first) for _ in range(10)))
    user = await credit_db.get_user(123)
    assert user["days_active"] == 1 and user["is_active"] == 1
    created_at = user["created_at"]
    await asyncio.gather(*(credit_db.update_user_activity(123, timestamp_ms=second) for _ in range(10)))
    await credit_db.init_db()
    await credit_db.update_user_activity(123, timestamp_ms=first)  # Delayed duplicate from yesterday.
    user = await credit_db.get_user(123)
    assert user["days_active"] == 2 and user["created_at"] == created_at
    assert user["last_active_at"] == "2026-10-21T21:00:00.000+00:00"
    assert user["balance"] == 0 and user["total_generations"] == 0
    assert await credit_db.get_advanced_stats() == {
        "total_all": 1, "total_active": 1, "total_blocked": 0, "total_returned": 1,
    }


async def test_inactive_user_is_excluded_and_incoming_activity_reactivates(credit_db):
    first = activity_timestamp("2026-10-21T12:00:00+00:00")
    await credit_db.update_user_activity(123, timestamp_ms=first)
    await credit_db.update_user_activity(456, timestamp_ms=first)
    await credit_db.mark_user_inactive(123)
    await credit_db.mark_user_inactive(123)
    assert await credit_db.get_active_user_ids() == [456]
    assert (await credit_db.get_advanced_stats())["total_blocked"] == 1
    await credit_db.update_user_activity(123, timestamp_ms=first + 1000)
    assert (await credit_db.get_user(123))["days_active"] == 1
    assert await credit_db.get_active_user_ids() == [123, 456]
    assert (await credit_db.get_advanced_stats())["total_blocked"] == 0


async def test_activity_migration_preserves_history_and_sets_defaults_for_new_users(credit_db):
    import aiosqlite
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("DROP TABLE users")
        await db.execute('''CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0,
            free_trial_used INTEGER NOT NULL DEFAULT 0,
            first_started_at TEXT, first_start_source TEXT)''')
        await db.execute("INSERT INTO users VALUES (123, 7, 1, '2026-10-20T12:00:00.000+00:00', 'vk_ads')")
        await db.execute("INSERT INTO users VALUES (456, 5, 1, NULL, 'legacy')")
        await db.commit()
    await credit_db.init_db()
    user = await credit_db.get_user(123)
    assert user["created_at"] == user["first_started_at"]
    assert user["balance"] == 7 and user["free_trial_used"] == 1
    assert (await credit_db.get_user(456))["created_at"] is None
    await credit_db.update_user_activity(123, timestamp_ms=activity_timestamp("2026-10-22T12:00:00+00:00"))
    assert (await credit_db.get_user(123))["days_active"] == 2
    await credit_db.mark_user_inactive(123)
    await credit_db.init_db()
    user = await credit_db.get_user(123)
    assert user["days_active"] == 2 and user["is_active"] == 0 and user["balance"] == 7
    await credit_db.add_credits(789, 1)
    assert (await credit_db.get_user(789))["created_at"] is not None


async def test_tripwire_starts_with_first_free_generation_and_never_extends(credit_db, monkeypatch):
    clock = [1800000000]
    monkeypatch.setattr(credit_db, "_now_ts", lambda: clock[0])
    await credit_db.grant_free_trial(456)
    assert not await credit_db.is_promo_active(456)
    assert await credit_db.deduct_credit(456, count_generation=True, generation_event="gen_ny")
    expires = (await credit_db.get_user(456))["promo_expires_at"]
    assert expires == clock[0] + 86400
    assert await credit_db.is_promo_active(456)
    clock[0] += 100
    await credit_db.init_db()
    await credit_db.grant_free_trial(456)
    assert (await credit_db.get_user(456))["promo_expires_at"] == expires
    clock[0] = expires
    assert not await credit_db.is_promo_active(456)
    await credit_db.add_credits(456, 1)
    await credit_db.deduct_credit(456, count_generation=True)
    assert (await credit_db.get_user(456))["promo_expires_at"] == expires
    assert not await credit_db.is_promo_active(456)


@pytest.mark.parametrize("paid", [False, True])
async def test_non_free_or_already_paid_user_does_not_start_tripwire(credit_db, paid):
    if paid:
        await credit_db.grant_free_trial(456)
        await credit_db.create_payment_order("paid", 456, 3, 19900, "3 генерации")
        await credit_db.add_credits(456, 3, payment_order_id="paid", provider_payment_id="provider")
        for _ in range(4):
            await credit_db.deduct_credit(456, count_generation=True)
    else:
        await credit_db.add_credits(456, 1)
        await credit_db.deduct_credit(456, count_generation=True)
    assert (await credit_db.get_user(456))["promo_expires_at"] is None


async def test_concurrent_promo_orders_reuse_one_bound_order(credit_db):
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    orders = await asyncio.gather(*(credit_db.create_payment_order(
        f"promo-{index}", 456, 3, 14900, "3 генерации", promo=True) for index in range(10)))
    assert len(set(orders)) == 1
    await credit_db.add_credits(456, 3, payment_order_id=orders[0], provider_payment_id="paid-promo")
    assert not await credit_db.add_credits(456, 3, payment_order_id=orders[0], provider_payment_id="paid-promo")
    user = await credit_db.get_user(456)
    assert user["balance"] == 3 and user["promo_redeemed_at"] is not None
    assert not await credit_db.is_promo_active(456)
    assert await credit_db.get_all_counters() == {"buy_3": 1, "buy_3_promo": 1}
    with pytest.raises(credit_db.PromoUnavailableError):
        await credit_db.create_payment_order("second-promo", 456, 3, 14900, "3 генерации", promo=True)


async def test_tripwire_migration_preserves_old_paid_orders_and_never_grants_retroactively(credit_db):
    import aiosqlite
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    await credit_db.create_payment_order("old-order", 456, 3, 19900, "3 генерации")
    await credit_db.add_credits(456, 3, payment_order_id="old-order", provider_payment_id="old-payment")
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("DROP INDEX one_promo_order_per_campaign")
        await db.execute("ALTER TABLE payment_orders DROP COLUMN package_id")
        await db.execute("ALTER TABLE users DROP COLUMN promo_expires_at")
        await db.execute("ALTER TABLE users DROP COLUMN promo_redeemed_at")
        await db.commit()
    await credit_db.init_db()
    user = await credit_db.get_user(456)
    assert user["balance"] == 3 and user["total_purchases"] == 1 and user["total_generations"] == 1
    assert user["promo_expires_at"] is None and user["promo_redeemed_at"] is None
    assert (await credit_db.get_payment_order("old-order"))["package_id"] == "package_3"
    await credit_db.deduct_credit(456, count_generation=True)
    assert (await credit_db.get_user(456))["promo_expires_at"] is None


async def test_rolled_back_free_generation_does_not_start_discount(credit_db, monkeypatch):
    from unittest.mock import AsyncMock
    import sqlite3
    await credit_db.grant_free_trial(456)
    monkeypatch.setattr(credit_db, "increment_generations", AsyncMock(side_effect=sqlite3.OperationalError("failed")))
    with pytest.raises(sqlite3.OperationalError):
        await credit_db.deduct_credit(456, count_generation=True)
    user = await credit_db.get_user(456)
    assert user["balance"] == 1 and user["promo_expires_at"] is None


async def test_all_stats_exclude_admin_without_changing_recorded_activity(credit_db):
    for user_id in (111, 222, 999):
        await credit_db.record_first_start(user_id, "vk_ads", timestamp_ms=1792526400000)
    await credit_db.update_user_activity(999, timestamp_ms=1792699200000)
    await credit_db.mark_user_inactive(999)
    for _ in range(3):
        await credit_db.increment_generations(999, event_name="gen_halloween")
    for _ in range(2):
        await credit_db.increment_generations(111, event_name="gen_halloween")
    await credit_db.increment_generations(222, event_name="gen_ny")
    for order, user_id, credits, amount in [("admin-buy", 999, 5, 29900), ("user-buy", 111, 3, 19900)]:
        await credit_db.create_payment_order(order, user_id, credits, amount, "package")
        await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
    assert await credit_db.get_advanced_stats(admin_id=999) == {
        "total_all": 2, "total_active": 2, "total_blocked": 0, "total_returned": 0,
    }
    assert await credit_db.get_global_stats(admin_id=999) == {
        "total_users": 2, "total_paid_users": 1, "total_generated": 3,
        "total_purchases": 1, "total_revenue_credits": 3,
    }
    counters = await credit_db.get_all_counters(admin_id=999)
    assert counters == {"gen_halloween": 2, "gen_ny": 1, "buy_3": 1, "gen_unclassified": 0}
    source = next(row for row in await credit_db.get_source_stats(admin_id=999) if row["source"] == "vk_ads")
    assert source["total_users"] == 2 and source["new_users"] == 2
    assert source["users_with_generation"] == 2 and source["buyers"] == 1 and source["revenue_kopecks"] == 19900
    assert (await credit_db.get_user(999))["total_generations"] == 3
    assert (await credit_db.get_user(999))["total_purchases"] == 1
    assert (await credit_db.get_global_stats())["total_generated"] == 6
    assert (await credit_db.get_all_counters())["gen_halloween"] == 5
    # Фильтрация зависит от переданного id, а не от роли в момент записи события.
    assert (await credit_db.get_all_counters(admin_id=111))["gen_halloween"] == 3


async def test_excluding_absent_admin_does_not_subtract_a_user(credit_db):
    await credit_db.record_first_start(123)
    await credit_db.increment_generations(123, event_name="gen_vision")
    assert (await credit_db.get_global_stats(admin_id=999))["total_users"] == 1
    assert (await credit_db.get_advanced_stats(admin_id=999))["total_all"] == 1
    assert (await credit_db.get_all_counters(admin_id=999))["gen_vision"] == 1


async def test_user_counter_migration_backfills_payments_once_and_preserves_unclassified_generations(credit_db):
    import aiosqlite
    for user_id, credits, amount in [(123, 3, 19900), (999, 5, 29900)]:
        order = f"old-{user_id}"
        await credit_db.create_payment_order(order, user_id, credits, amount, "package")
        await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
    for _ in range(4):
        await credit_db.increment_generations(123, event_name="gen_ny")
    for _ in range(3):
        await credit_db.increment_generations(999, event_name="gen_ny")
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("DROP TABLE user_counters")
        await db.commit()
    await credit_db.init_db()
    assert await credit_db.get_all_counters(admin_id=999) == {"buy_3": 1, "gen_unclassified": 4}
    assert (await credit_db.get_global_stats(admin_id=999))["total_generated"] == 4
    await credit_db.increment_generations(123, event_name="gen_halloween")
    await credit_db.increment_generations(999, event_name="gen_halloween")
    await credit_db.init_db()
    assert await credit_db.get_all_counters(admin_id=999) == {"buy_3": 1, "gen_halloween": 1, "gen_unclassified": 4}
    assert (await credit_db.get_all_counters())["gen_ny"] == 7


async def test_user_counter_write_failure_rolls_back_global_counter_and_credit(credit_db):
    import aiosqlite
    await credit_db.add_credits(123, 2)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("CREATE TRIGGER reject_user_counter BEFORE INSERT ON user_counters BEGIN SELECT RAISE(ABORT, 'rejected'); END")
        await db.commit()
    with pytest.raises(Exception, match="rejected"):
        await credit_db.deduct_credit(123, count_generation=True, generation_event="gen_ny")
    assert (await credit_db.get_user(123))["balance"] == 2
    assert (await credit_db.get_user(123))["total_generations"] == 0
    assert await credit_db.get_all_counters() == {}


async def test_activity_segments_only_include_active_users(credit_db):
    for uid in (1, 2, 3, 4):
        await credit_db.grant_free_trial(uid)
    for uid in (1, 3):
        await credit_db.increment_generations(uid)
    for uid in (3, 4):
        await credit_db.mark_user_inactive(uid)
    assert await credit_db.get_users_by_activity(True) == [1]
    assert await credit_db.get_users_by_activity(False) == [2]


async def test_campaign_promo_renewal_and_delayed_payment(credit_db, monkeypatch):
    clock = [1800000000]
    monkeypatch.setattr(credit_db, "_now_ts", lambda: clock[0])
    await credit_db.increment_generations(1)
    await credit_db.start_broadcast_promo(1, "campaign-a")
    expiry = (await credit_db.get_user(1))["promo_expires_at"]
    assert expiry == clock[0] + 86400
    old_order = await credit_db.create_payment_order("a", 1, 3, 14900, "3 генерации", promo=True)
    clock[0] += 100
    await credit_db.start_broadcast_promo(1, "campaign-a")
    assert (await credit_db.get_user(1))["promo_expires_at"] == expiry
    await credit_db.start_broadcast_promo(1, "campaign-b")
    new_order = await credit_db.create_payment_order("b", 1, 3, 14900, "3 генерации", promo=True)
    assert new_order != old_order
    assert await credit_db.add_credits(1, 3, payment_order_id=old_order, provider_payment_id="old")
    assert await credit_db.is_promo_active(1)  # Старый платёж не погашает новую акцию.
    assert await credit_db.add_credits(1, 3, payment_order_id=new_order, provider_payment_id="new")
    assert not await credit_db.is_promo_active(1)
    assert not await credit_db.add_credits(1, 3, payment_order_id=new_order, provider_payment_id="new")
    assert (await credit_db.get_user(1))["balance"] == 6
    await credit_db.start_broadcast_promo(1, "campaign-c")
    assert await credit_db.is_promo_active(1)
    clock[0] += 86400
    assert not await credit_db.is_promo_active(1)
    with pytest.raises(credit_db.PromoUnavailableError):
        await credit_db.create_payment_order("c", 1, 3, 14900, "3 генерации", promo=True)
    await credit_db.init_db()
    assert (await credit_db.get_payment_order("a"))["promo_campaign_id"] == "campaign-a"


async def test_broadcast_campaign_migrates_original_promo_order(credit_db):
    import aiosqlite
    await credit_db.grant_free_trial(1)
    await credit_db.deduct_credit(1, count_generation=True)
    await credit_db.create_payment_order("legacy", 1, 3, 14900, "3 генерации", promo=True)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("DROP INDEX one_promo_order_per_campaign")
        await db.execute("ALTER TABLE users DROP COLUMN promo_campaign_id")
        await db.execute("ALTER TABLE payment_orders DROP COLUMN promo_campaign_id")
        await db.execute("CREATE UNIQUE INDEX one_promo_order_per_user ON payment_orders(user_id) WHERE package_id = 'promo_3'")
        await db.commit()
    await credit_db.init_db()
    assert await credit_db.create_payment_order("reuse", 1, 3, 14900, "3 генерации", promo=True) == "legacy"
    await credit_db.start_broadcast_promo(1, "new")
    assert await credit_db.create_payment_order("new-order", 1, 3, 14900, "3 генерации", promo=True) == "new-order"


async def test_reminder_recipients_strict_window_and_available_promo(credit_db, monkeypatch):
    import aiosqlite
    now = 1800000000
    monkeypatch.setattr(credit_db, "_now_ts", lambda: now)
    cases = [
        (1, None, 1, None), (2, now - 1, 1, None), (3, now, 1, None),
        (4, now + 1, 1, None), (5, now + 10800 - 1, 1, None),
        (6, now + 10800, 1, None), (7, now + 10801, 1, None),
        (8, now + 7200, 0, None), (9, now + 7200, 1, now - 10),
    ]
    for uid, *_ in cases:
        await credit_db.grant_free_trial(uid)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.executemany(
            "UPDATE users SET promo_expires_at = ?, is_active = ?, promo_redeemed_at = ? WHERE user_id = ?",
            [(expiry, active, redeemed, uid) for uid, expiry, active, redeemed in cases],
        )
        await db.commit()
    assert await credit_db.get_users_for_reminder() == [4, 5]
    assert (await credit_db.get_user(4))["promo_expires_at"] == now + 1
    monkeypatch.setattr(credit_db, "_now_ts", lambda: now + 1)
    assert await credit_db.get_users_for_reminder() == [5, 6]


async def test_reminder_migration_preserves_promo_deadline(credit_db):
    await credit_db.increment_generations(1)
    await credit_db.start_broadcast_promo(1, "campaign")
    before = dict(await credit_db.get_user(1))
    await credit_db.init_db()
    assert dict(await credit_db.get_user(1)) == before


async def test_campaign_funnel_excludes_admin_pending_and_duplicate_payments(credit_db):
    from decimal import Decimal
    for user_id, source in [(101, 'yandex_newyear'), (102, 'yandex_newyear'),
                            (103, 'yandex_newyear'), (999, 'yandex_newyear'), (201, 'yandex_ads')]:
        await credit_db.record_first_start(user_id, source)
    for user_id in (101, 101, 102, 999, 201):
        await credit_db.increment_generations(user_id)
    for order, user_id, credits, amount in [
        ('first', 101, 1, 9900), ('second', 101, 5, 29900),
        ('admin', 999, 15, 79900), ('other', 201, 1, 9900),
    ]:
        await credit_db.create_payment_order(order, user_id, credits, amount, 'test')
        assert await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
        assert not await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
    await credit_db.create_payment_order('pending', 102, 15, 79900, 'test')
    await credit_db.record_first_start(101, 'vk_ads')
    assert await credit_db.get_stats_by_source('yandex_newyear', admin_id=999) == {
        'total_users': 3, 'total_generated': 2, 'total_buyers': 1,
        'total_revenue': Decimal('398.00'),
    }
    await credit_db.init_db()
    assert (await credit_db.get_stats_by_source('yandex_newyear'))['total_revenue'] == Decimal('1197.00')


async def test_empty_campaign_stats_are_zero(credit_db):
    assert await credit_db.get_stats_by_source('yandex_newyear') == {
        'total_users': 0, 'total_generated': 0, 'total_buyers': 0, 'total_revenue': 0,
    }


async def test_song_promo_migration_expiration_and_first_gift_purchase(credit_db, monkeypatch):
    now = 1800000000
    monkeypatch.setattr(credit_db, '_now_ts', lambda: now)
    assert not await credit_db.check_song_promo(456)
    await credit_db.set_song_promo(456)
    assert (await credit_db.get_user(456))['song_promo_expires_at'] == now + 48 * 3600
    await credit_db.init_db()
    assert await credit_db.check_song_promo(456)
    now += 48 * 3600
    assert not await credit_db.check_song_promo(456)
    await credit_db.set_song_promo(456, hours=1)
    assert await credit_db.check_song_promo(456)
    await credit_db.create_audio_order('first-gift', 456, 'gift', 'bday', 'Аня', None, 'lyrics', 'pop', 'title', song_discount=True)
    assert (await credit_db.get_audio_order('first-gift'))['amount_kopecks'] == 39200
    await credit_db.mark_audio_paid('first-gift', 'provider-first')
    await credit_db.set_song_promo(456)
    assert not await credit_db.check_song_promo(456)
    with pytest.raises(credit_db.PromoUnavailableError):
        await credit_db.create_audio_order('second-gift', 456, 'gift', 'bday', 'Аня', None, 'lyrics', 'pop', 'title', song_discount=True)


async def test_song_promo_cannot_discount_fun_or_unqualified_user(credit_db):
    for kind, error in [('gift', credit_db.PromoUnavailableError), ('fun', ValueError)]:
        with pytest.raises(error):
            await credit_db.create_audio_order('invalid', 456, kind, 'bday', 'Аня', None, 'lyrics', 'pop', 'title', song_discount=True)
    assert await credit_db.get_audio_order('invalid') is None


async def test_yclid_migration_last_click_and_user_id_fallback(credit_db):
    await credit_db.save_yclid(456)
    user = await credit_db.get_user(456)
    assert user['user_id'] == 456 and user['yclid'] == ''
    await credit_db.save_yclid(456, '111111')
    await credit_db.init_db()
    assert (await credit_db.get_user(456))['yclid'] == '111111'
    await credit_db.save_yclid(456, '222222')
    await credit_db.save_yclid(456, None)
    await credit_db.save_yclid(456, '')
    assert (await credit_db.get_user(456))['yclid'] == '222222'
    assert (await credit_db.get_user(456))['balance'] == 0


@pytest.mark.parametrize('payload,source', [
    ('vk_ads?yclid=1111', 'vk_ads'), ('yandex_ads&yclid=1111', 'yandex_ads'),
    ('yandex_newyear?yclid=1111', 'yandex_newyear'), ('vk_ads%3Fyclid%3D1111', 'vk_ads'),
])
async def test_yclid_parameter_does_not_hide_first_campaign(credit_db, payload, source):
    await credit_db.record_first_start(456, payload)
    await credit_db.save_yclid(456, '1111')
    await credit_db.record_first_start(456, 'yandex_newyear?yclid=2222')
    await credit_db.save_yclid(456, '2222')
    user = await credit_db.get_user(456)
    assert user['first_start_source'] == source and user['yclid'] == '2222'
