import asyncio

import pytest


async def test_trial_is_atomic_under_concurrent_starts(credit_db):
    grants = await asyncio.gather(*(credit_db.grant_free_trial(123) for _ in range(20)))
    assert sum(grants) == 1
    assert dict(await credit_db.get_user(123)) == {
        "user_id": 123, "balance": 1, "free_trial_used": 1,
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
