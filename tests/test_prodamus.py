import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlencode, quote
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aioresponses import aioresponses

from config import Config
import prodamus
from prodamus import (
    ProdamusPayments, flatten_fields, parse_form_fields, sign_payload, sign_link_payload, verify_signature,
)
from webhook import create_webhook_app

SECRET = "test_secret_key"


@pytest.fixture
def payment_config():
    return Config(
        token="test-max-token", photo_path=Path("unused.png"),
        prodamus_domain="test-shop.payform.ru", prodamus_secret_key=SECRET,
        prodamus_return_url="https://max.ru/test_bot",
        prodamus_webhook_url="https://bot.example.com/prodamus/webhook",
    )


@pytest.fixture
def payments(payment_config):
    payments = ProdamusPayments(payment_config)
    # Webhook-тесты получают эхо исходящего payload вместо обращения к провайдеру.
    async def echo(data):
        return payments.domain + "?" + urlencode(flatten_fields(data), quote_via=quote)
    payments._request_payment_link = AsyncMock(side_effect=echo)
    return payments


@pytest.fixture
async def client(payments, credit_db):
    async with TestClient(TestServer(create_webhook_app(payments))) as client:
        yield client


async def notification(payments, credits=5, *, payment_id="provider-1", user_id=456, promo=False):
    link = await payments.create_payment_link(user_id, credits, promo=promo)
    fields = parse_form_fields(parse_qsl(urlsplit(link).query, keep_blank_values=True))
    return {
        "order_num": fields["order_id"], "order_id": payment_id,
        "sum": fields["products"][0]["price"], "currency": "rub",
        "domain": "test-shop.payform.ru", "payment_status": "success",
        "products": fields["products"], "customer_extra": fields["customer_extra"],
        "_param_user_id": str(user_id), "_param_credits": str(credits),
        "_param_package_id": fields["_param_package_id"],
    }


async def post_notification(client, data, *, encoding="form", signature=None):
    headers = {"Sign": signature or sign_payload(data, SECRET)}
    if encoding == "json":
        return await client.post("/prodamus/webhook", json=data, headers=headers)
    form = aiohttp.FormData(default_to_multipart=encoding == "multipart")
    for key, value in flatten_fields(data):
        form.add_field(key, value)
    return await client.post("/prodamus/webhook", data=form, headers=headers)


@pytest.mark.parametrize("data, expected", [
    (
        {"order_id": "12345", "sum": "1000.00", "customer_email": "client@example.com",
         "payment_status": "success"},
        "a0c2f61457c981288d7687765a069d5e8db5b6998706115fb0c0de0988620609",
    ),
    (
        {"order_id": "order-77", "sum": "2500.50",
         "products": [{"name": "Тестовый курс «Запуск»", "price": "2500.50", "quantity": "1"}],
         "urlNotification": "https://example.com/webhook/prodamus"},
        "ca54bade5070ec36f00dd966aea69bd84f5bdc89632b8f3d25c93f57340628c5",
    ),
])
def test_signature_matches_prodamus_reference_vectors(data, expected):
    assert sign_payload(data, SECRET) == expected
    assert verify_signature(data, expected.upper(), SECRET)
    assert not verify_signature(data, expected, "wrong-secret")


@pytest.mark.parametrize("credits,price", [(1, "99.00"), (3, "199.00"), (5, "299.00"), (15, "799.00")])
async def test_signed_link_persists_bound_order(payments, credit_db, credits, price):
    link = await payments.create_payment_link(456, credits)
    assert link.startswith("https://test-shop.payform.ru/?")
    data = parse_form_fields(parse_qsl(urlsplit(link).query))
    assert sign_link_payload({key: value for key, value in data.items() if key != "sign"}, SECRET) == data["sign"]
    assert data["do"] == "link"
    assert data["urlNotification"] == payments.webhook_url
    assert data["urlSuccess"] == data["urlReturn"] == payments.return_url
    assert data["products"] == [{"name": prodamus.PACKAGES[credits][1], "price": price, "quantity": "1"}]
    assert data["_param_package_id"] == f"package_{credits}"
    assert json.loads(data["customer_extra"]) == {"user_id": 456, "credits": credits, "package_id": f"package_{credits}"}
    order = await credit_db.get_payment_order(data["order_id"])
    assert order["user_id"] == 456 and order["credits"] == credits
    assert order["amount_kopecks"] == {1: 9900, 3: 19900, 5: 29900, 15: 79900}[credits]
    assert order["status"] == "pending"
    assert await credit_db.get_user(456) is None  # Ссылка ещё не означает оплату.


@pytest.mark.parametrize("body", [
    " https://test-shop.payform.ru/payment/abc\n",
    '{"payment_link":"https://test-shop.payform.ru/payment/abc"}',
    '{"link":"https://test-shop.payform.ru/payment/abc"}',
])
async def test_link_api_returns_provider_url(payment_config, credit_db, body):
    import re
    payments = ProdamusPayments(payment_config)
    with aioresponses() as api:
        api.get(re.compile(r"https://test-shop\.payform\.ru/\?.*"), body=body)
        link = await payments.create_payment_link(456, 5)
        assert link == "https://test-shop.payform.ru/payment/abc"
        calls = [call for requests in api.requests.values() for call in requests]
        assert len(calls) == 1
        request_url = next(iter(api.requests))[1]
        fields = parse_form_fields(parse_qsl(request_url.query_string))
        assert fields["do"] == "link"
        assert fields["_param_user_id"] == "456"
        assert fields["_param_package_id"] == "package_5"
        clean = {key: value for key, value in fields.items() if key != "sign"}
        assert fields["sign"] == sign_link_payload(clean, SECRET)
        assert calls[0].kwargs["allow_redirects"] is True
        assert (await credit_db.get_payment_order(fields["order_id"]))["status"] == "pending"
    assert await credit_db.get_user(456) is None


async def test_link_api_follows_payform_redirect(payment_config, credit_db):
    import re
    with aioresponses() as api:
        api.get(re.compile(r"https://test-shop\.payform\.ru/\?.*"), status=302,
                headers={"Location": "https://a.payform.ru/create-link"})
        api.get("https://a.payform.ru/create-link", body="https://a.payform.ru/payment/abc")
        assert await ProdamusPayments(payment_config).create_payment_link(456, 5) == "https://a.payform.ru/payment/abc"


@pytest.mark.parametrize("body,status", [
    ("denied", 403), ("failure", 500), ("", 200),
    ('{"error":"invalid secret"}', 200), ('{"link":42}', 200),
    ("http://test-shop.payform.ru/payment/abc", 200),
])
async def test_link_api_errors_never_return_payment_link(payment_config, credit_db, body, status):
    import re
    with aioresponses() as api:
        api.get(re.compile(r"https://test-shop\.payform\.ru/\?.*"), body=body, status=status)
        with pytest.raises((ValueError, RuntimeError)):
            await ProdamusPayments(payment_config).create_payment_link(456, 5)
    assert await credit_db.get_user(456) is None


async def test_link_api_timeout_is_not_retried(payment_config, credit_db):
    import re
    with aioresponses() as api:
        api.get(re.compile(r"https://test-shop\.payform\.ru/\?.*"), exception=asyncio.TimeoutError())
        with pytest.raises(asyncio.TimeoutError):
            await ProdamusPayments(payment_config).create_payment_link(456, 5)
        assert sum(len(calls) for calls in api.requests.values()) == 1
    assert await credit_db.get_user(456) is None


async def test_existing_links_without_package_id_still_credit(client, payments, credit_db):
    data = await notification(payments)
    data.pop("_param_package_id")
    extra = json.loads(data["customer_extra"])
    extra.pop("package_id")
    data["customer_extra"] = json.dumps(extra)
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 5


def test_only_domain_and_secret_are_not_enough_for_payment_webhook(payment_config):
    from dataclasses import replace
    with pytest.raises(ValueError, match="PRODAMUS_RETURN_URL и PRODAMUS_WEBHOOK_URL"):
        ProdamusPayments(replace(payment_config, prodamus_return_url=None, prodamus_webhook_url=None))


@pytest.mark.parametrize("credits", [1, 3, 5, 15])
@pytest.mark.parametrize("encoding", ["form", "multipart", "json"])
async def test_webhook_credits_paid_order_once(client, payments, credit_db, credits, encoding):
    await credit_db.grant_free_trial(456)
    data = await notification(payments, credits)
    response = await post_notification(client, data, encoding=encoding)
    assert response.status == 200 and await response.text() == "OK"
    assert (await credit_db.get_user(456))["balance"] == credits + 1
    assert await credit_db.get_all_counters() == {f"buy_{credits}": 1}
    assert (await credit_db.get_user(456))["total_purchases"] == 1
    assert (await credit_db.get_global_stats())["total_revenue_credits"] == credits
    assert await credit_db.get_all_counters(admin_id=456) == {"gen_unclassified": 0}
    assert (await credit_db.get_all_counters(admin_id=999))[f"buy_{credits}"] == 1
    assert (await credit_db.get_user(456))["free_trial_used"] == 1
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "paid"
    await credit_db.init_db()  # Повтор после перезапуска и с новым attempt.
    data["attempt"] = "2"
    assert (await post_notification(client, data, encoding=encoding)).status == 200
    assert await credit_db.get_all_counters() == {f"buy_{credits}": 1}
    assert (await credit_db.get_user(456))["balance"] == credits + 1
    assert (await credit_db.get_user(456))["total_purchases"] == 1
    assert (await credit_db.get_global_stats())["total_revenue_credits"] == credits


async def test_order_created_before_price_increase_still_credits(client, payments, credit_db, monkeypatch):
    current_amount, title = prodamus.PACKAGES[15]
    with monkeypatch.context() as old_catalog:
        old_catalog.setitem(prodamus.PACKAGES, 15, (current_amount - 10_000, title))
        data = await notification(payments, credits=15)

    assert prodamus.PACKAGES[15][0] == 79900
    assert (await post_notification(client, data)).status == 200
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 15
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "paid"


async def test_new_order_rejects_lower_old_price_even_with_valid_signature(client, payments, credit_db):
    data = await notification(payments, credits=15)
    lower_price = f"{(prodamus.PACKAGES[15][0] - 10_000) // 100}.00"
    data["sum"] = lower_price
    data["products"][0]["price"] = lower_price

    assert (await post_notification(client, data)).status == 400
    assert await credit_db.get_user(456) is None
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "pending"


async def test_concurrent_retries_cannot_double_credit(client, payments, credit_db):
    data = await notification(payments)
    responses = await asyncio.gather(*(post_notification(client, data) for _ in range(10)))
    assert all(response.status == 200 for response in responses)
    assert (await credit_db.get_user(456))["balance"] == 5


@pytest.mark.parametrize("signature", ["0" * 64, "bad", "X" * 64])
async def test_invalid_signature_does_not_credit(client, payments, credit_db, signature):
    data = await notification(payments)
    assert (await post_notification(client, data, signature=signature)).status == 403
    assert await credit_db.get_user(456) is None


async def test_tampered_notification_fails_signature(client, payments, credit_db):
    data = await notification(payments)
    signature = sign_payload(data, SECRET)
    data["sum"] = "799.00"
    assert (await post_notification(client, data, signature=signature)).status == 403
    assert await credit_db.get_user(456) is None


@pytest.mark.parametrize("field,value", [
    ("sum", "1.00"), ("sum", "NaN"), ("sum", "1e99999999"),
    ("currency", "usd"), ("order_num", "unknown"),
    ("_param_user_id", "999"), ("_param_credits", "15"),
    ("_param_package_id", "package_15"),
    ("customer_extra", '{"user_id":456,"credits":5,"package_id":"package_15"}'),
    ("domain", "other.payform.ru"), ("customer_extra", '{"user_id":999,"credits":5}'),
    ("products", [{"name": "15 генераций", "price": "299.00", "quantity": "1"}]),
    ("products", [{"name": "5 генераций", "price": "299.00", "quantity": "2"}]),
    ("products", [{"name": "5 генераций", "price": "1.00", "quantity": "1"}]),
])
async def test_signed_but_mismatched_payment_is_rejected(client, payments, credit_db, field, value):
    data = await notification(payments)
    data[field] = value
    assert (await post_notification(client, data)).status == 400
    assert await credit_db.get_user(456) is None


@pytest.mark.parametrize("status", ["pending", "order_canceled", "order_denied", "unknown"])
async def test_unpaid_status_is_acknowledged_without_credits(client, payments, credit_db, status):
    data = await notification(payments)
    data["payment_status"] = status
    assert (await post_notification(client, data)).status == 200
    assert await credit_db.get_user(456) is None


async def test_test_payment_is_acknowledged_without_real_credits(client, payments, credit_db):
    data = await notification(payments)
    data["is_test"] = "1"
    assert (await post_notification(client, data)).status == 200
    assert await credit_db.get_user(456) is None


async def test_provider_payment_cannot_pay_two_orders(client, payments, credit_db):
    first = await notification(payments)
    second = await notification(payments, user_id=789)
    assert (await post_notification(client, first)).status == 200
    assert (await post_notification(client, second)).status == 400
    assert (await credit_db.get_user(456))["balance"] == 5
    assert await credit_db.get_user(789) is None
    assert (await credit_db.get_payment_order(second["order_num"]))["status"] == "pending"


async def test_database_error_is_retried_not_acknowledged(client, payments, credit_db, monkeypatch):
    import webhook

    data = await notification(payments)
    monkeypatch.setattr(webhook, "add_credits", AsyncMock(side_effect=sqlite3.OperationalError("test")))
    assert (await post_notification(client, data)).status == 503
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "pending"
    monkeypatch.setattr(webhook, "add_credits", credit_db.add_credits)
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 5


async def test_database_rolls_back_paid_status_when_credit_update_fails(client, payments, credit_db):
    import aiosqlite

    data = await notification(payments)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute(
            """CREATE TRIGGER reject_test_credit BEFORE INSERT ON users
               BEGIN SELECT RAISE(ABORT, 'test failure'); END"""
        )
        await db.commit()
    assert (await post_notification(client, data)).status != 200
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "pending"
    assert await credit_db.get_user(456) is None
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("DROP TRIGGER reject_test_credit")
        await db.commit()
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 5


async def test_bot_closes_webhook_and_sessions_when_polling_fails(payment_config, monkeypatch):
    from unittest.mock import Mock

    import bot as bot_module
    import max_client

    client = Mock(close_session=AsyncMock())
    dispatcher = Mock(start_polling=AsyncMock(side_effect=RuntimeError("test")), shutdown=AsyncMock())
    runner = Mock(cleanup=AsyncMock())
    start_server = AsyncMock(return_value=runner)
    monkeypatch.setattr(bot_module.Config, "from_env", lambda: payment_config)
    monkeypatch.setattr(bot_module, "init_db", AsyncMock())
    monkeypatch.setattr(max_client, "Bot", Mock(return_value=client))
    monkeypatch.setattr(bot_module, "Dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(bot_module, "start_webhook_server", start_server)
    with pytest.raises(RuntimeError, match="test"):
        await bot_module.main()
    start_server.assert_awaited_once()
    dispatcher.start_polling.assert_awaited_once_with(client)
    runner.cleanup.assert_awaited_once()
    dispatcher.shutdown.assert_awaited_once()
    client.close_session.assert_awaited_once()


async def test_duplicate_form_keys_and_unsigned_json_are_rejected(client, payments, credit_db):
    data = await notification(payments)
    response = await client.post(
        "/prodamus/webhook", data="order_id=1&order_id=2",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sign": sign_payload(data, SECRET)},
    )
    assert response.status == 400
    assert (await client.post("/prodamus/webhook", json=data)).status == 403
    assert (await client.get("/prodamus/webhook")).status == 405
    assert await credit_db.get_user(456) is None


@pytest.mark.parametrize("domain", [
    "https://evil.com/payform.ru", "https://evil.com@demo.payform.ru",
    "https://demo.payform.ru:8080", "https://demo.payform.ru.evil.com",
    "https://evilpayform.ru", "http://demo.payform.ru",
])
def test_payment_domain_validation(payment_config, domain):
    from dataclasses import replace

    with pytest.raises(ValueError):
        ProdamusPayments(replace(payment_config, prodamus_domain=domain))


async def test_single_generation_payment_adds_exactly_one_credit(client, payments, credit_db):
    data = await notification(payments, credits=1)
    assert data["sum"] == "99.00"
    assert data["products"][0]["name"] == "1 генерация (Открытка)"
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 1


@pytest.mark.parametrize("field,value", [("sum", "1.00"), ("_param_credits", "15"), ("_param_package_id", "package_5")])
async def test_single_generation_payment_rejects_wrong_amount_or_package(client, payments, credit_db, field, value):
    data = await notification(payments, credits=1)
    data[field] = value
    response = await post_notification(client, data)
    assert response.status == 400
    assert await credit_db.get_user(456) is None
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "pending"


async def test_purchase_counter_failure_rolls_back_payment_and_balance(client, payments, credit_db, monkeypatch):
    data = await notification(payments, credits=1)
    await credit_db.grant_free_trial(456)
    with monkeypatch.context() as patch:
        patch.setattr(credit_db, "increment_purchases", AsyncMock(side_effect=sqlite3.OperationalError("counter failed")))
        assert (await post_notification(client, data)).status == 503
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_purchases"] == 0
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "pending"
    assert (await post_notification(client, data)).status == 200
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 2
    assert (await credit_db.get_user(456))["total_purchases"] == 1


@pytest.mark.parametrize("field,value", [("is_test", "1"), ("payment_status", "failed"), ("payment_status", "pending")])
async def test_non_paid_webhooks_do_not_count_as_purchases(client, payments, credit_db, field, value):
    data = await notification(payments, credits=1)
    data[field] = value
    assert (await post_notification(client, data)).status == 200
    stats = await credit_db.get_global_stats()
    assert stats["total_purchases"] == stats["total_paid_users"] == stats["total_revenue_credits"] == 0
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize("payload,source", [("vk_ads", "vk_ads"), ("yandex_ads", "yandex_ads"), (None, "unmarked")])
async def test_paid_webhook_remains_linked_to_first_source(client, payments, credit_db, payload, source):
    await credit_db.record_first_start(456, payload, timestamp_ms=1792526400000)
    data = await notification(payments, credits=1)
    assert (await post_notification(client, data)).status == 200
    assert (await post_notification(client, data)).status == 200
    await credit_db.record_first_start(456, "yandex_ads", timestamp_ms=1792612800000)
    rows = {row["source"]: row for row in await credit_db.get_source_stats()}
    assert rows[source]["new_users"] == rows[source]["buyers"] == 1
    assert rows[source]["revenue_kopecks"] == 9900
    assert sum(row["revenue_kopecks"] for row in rows.values()) == 9900
    assert (await credit_db.get_user(456))["total_purchases"] == 1


async def test_package_counter_failure_rolls_back_payment_and_retry_counts_once(client, payments, credit_db, monkeypatch):
    data = await notification(payments, credits=15)
    await credit_db.grant_free_trial(456)
    with monkeypatch.context() as patch:
        patch.setattr(credit_db, "increment_counter", AsyncMock(side_effect=sqlite3.OperationalError("counter failed")))
        assert (await post_notification(client, data)).status == 503
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_purchases"] == 0
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "pending"
    assert await credit_db.get_all_counters() == {}
    assert (await post_notification(client, data)).status == 200
    assert (await post_notification(client, data)).status == 200
    assert await credit_db.get_all_counters() == {"buy_15": 1}


async def test_discount_link_has_149_price_but_credits_three_and_is_single_use(client, payments, credit_db):
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    first = await notification(payments, 3, promo=True)
    second = await notification(payments, 3, promo=True)
    assert first["order_num"] == second["order_num"]
    assert first["sum"] == "149.00" and first["_param_package_id"] == "promo_3"
    assert (await post_notification(client, first)).status == 200
    assert (await post_notification(client, first)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 3
    assert (await credit_db.get_user(456))["total_purchases"] == 1
    with pytest.raises(credit_db.PromoUnavailableError):
        await payments.create_payment_link(456, 3, promo=True)


async def test_expired_promo_cannot_create_link_but_existing_paid_order_is_honoured(client, payments, credit_db, monkeypatch):
    clock = [1800000000]
    monkeypatch.setattr(credit_db, "_now_ts", lambda: clock[0])
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    data = await notification(payments, 3, promo=True)
    clock[0] += 86400
    payments._request_payment_link.reset_mock()
    with pytest.raises(credit_db.PromoUnavailableError):
        await payments.create_payment_link(456, 3, promo=True)
    payments._request_payment_link.assert_not_awaited()
    # Не отказывать в кредитах, если провайдер подтвердил оплату уже выданной ссылки.
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))["balance"] == 3


@pytest.mark.parametrize("mutation", ["sum", "package"])
async def test_discount_webhook_rejects_price_and_package_tampering(client, payments, credit_db, mutation):
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    data = await notification(payments, 3, promo=True)
    if mutation == "sum":
        data["sum"] = "99.00"
    else:
        data["_param_package_id"] = "package_3"
    assert (await post_notification(client, data)).status == 400
    assert (await credit_db.get_user(456))["balance"] == 0
    assert (await credit_db.get_user(456))["promo_redeemed_at"] is None


async def audio_notification(payments, kind='fun', user_id=456, payment_id='audio-provider'):
    order_id = f'audio-test-{kind}-{user_id}'
    link = await payments.create_audio_payment_link(user_id, kind, 'boss' if kind == 'fun' else 'bday',
        'Аня, любит горы', 'pop', '[Verse]\nАня', 'pop', 'Для Ани', order_id=order_id)
    fields = parse_form_fields(parse_qsl(urlsplit(link).query, keep_blank_values=True))
    return {'order_num': fields['order_id'], 'order_id': payment_id, 'sum': fields['products'][0]['price'],
            'currency': 'rub', 'domain': 'test-shop.payform.ru', 'payment_status': 'success',
            'products': fields['products'], 'customer_extra': fields['customer_extra'],
            '_param_user_id': str(user_id), '_param_credits': '0', '_param_package_id': fields['_param_package_id']}


@pytest.mark.parametrize('kind,amount', [('fun', '199.00'), ('gift', '490.00')])
@pytest.mark.parametrize('encoding', ['form', 'json', 'multipart'])
async def test_audio_webhook_queues_paid_order_once_without_crediting_images(client, payments, credit_db, kind, amount, encoding):
    data = await audio_notification(payments, kind)
    assert data['sum'] == amount
    assert (await client.post('/prodamus/webhook', json=data, headers={'Sign': 'wrong'})).status == 403
    for _ in range(2):
        response = await post_notification(client, data, encoding=encoding)
        assert response.status == 200 and await response.text() == 'OK'
    order = await credit_db.get_audio_order(data['order_num'])
    assert order['status'] == 'paid' and order['details'] == 'Аня, любит горы'
    assert order['job_id'] is None
    user = await credit_db.get_user(456)
    assert user['balance'] == 0 and user['total_purchases'] == 1
    assert (await credit_db.get_all_counters())['buy_audio_' + kind] == 1


@pytest.mark.parametrize('field,value', [('sum', '99.00'), ('_param_user_id', '999'), ('_param_credits', '3'),
    ('_param_package_id', 'package_3'), ('currency', 'usd')])
async def test_audio_webhook_rejects_mismatched_signed_metadata(client, payments, credit_db, field, value):
    data = await audio_notification(payments)
    data[field] = value
    assert (await post_notification(client, data)).status == 400
    assert (await credit_db.get_audio_order(data['order_num']))['status'] == 'pending'


async def test_credit_package_199_is_not_an_audio_payment(client, payments, credit_db):
    data = await notification(payments, 3)
    assert data['sum'] == '199.00'
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_user(456))['balance'] == 3
    assert await credit_db.pending_audio_orders() == []


@pytest.mark.parametrize('field,value', [('is_test', '1'), ('payment_status', 'pending')])
async def test_audio_test_and_pending_notifications_do_not_queue(client, payments, credit_db, field, value):
    data = await audio_notification(payments)
    data[field] = value
    assert (await post_notification(client, data)).status == 200
    assert (await credit_db.get_audio_order(data['order_num']))['status'] == 'pending'
    assert await credit_db.pending_audio_orders() == []


@pytest.mark.parametrize('kind', ['fun', 'gift'])
async def test_audio_webhook_acknowledges_before_background_generation_finishes(payments, credit_db, kind):
    from types import SimpleNamespace
    from audio_orders import PaidAudioWorker
    entered, release = asyncio.Event(), asyncio.Event()
    result = {'status': 'SUCCESS', 'data': {'musics': [{'audioUrl': 'https://audio.example/song.mp3', 'title': 'Песня'}]}}
    async def wait(job):
        entered.set()
        await release.wait()
        return result
    submit = AsyncMock(return_value='paid-job')
    bot = SimpleNamespace(send_message=AsyncMock())
    worker = PaidAudioWorker(bot, generator=submit, waiter=wait, poll_interval=0.005)
    data = await audio_notification(payments, kind)
    async with TestClient(TestServer(create_webhook_app(payments, audio_worker=worker))) as client:
        try:
            response = await asyncio.wait_for(post_notification(client, data), timeout=2)
            assert response.status == 200
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not release.is_set()
            assert (await post_notification(client, data)).status == 200
            submit.assert_awaited_once()
        finally:
            release.set()
        async def delivered():
            while not (await credit_db.get_audio_order(data['order_num']))['delivered']:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(delivered(), timeout=2)
        assert 'https://audio.example/song.mp3' in bot.send_message.await_args.kwargs['text']
        assert (await credit_db.get_user(456))['balance'] == 0


async def test_preview_worker_runs_without_payment_configuration_and_stops_cleanly(payment_config,monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import Mock
    import bot as bot_module
    import max_client
    settings=replace(payment_config,prodamus_domain=None,prodamus_secret_key=None,prodamus_return_url=None,prodamus_webhook_url=None)
    started,stopped=asyncio.Event(),asyncio.Event()
    async def worker_run():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def polling(client):
        await asyncio.wait_for(started.wait(),timeout=1)
        raise RuntimeError('stop test')
    client=Mock(close_session=AsyncMock())
    dispatcher=Mock(start_polling=AsyncMock(side_effect=polling),shutdown=AsyncMock())
    factory=Mock(return_value=SimpleNamespace(run=worker_run))
    monkeypatch.setattr(bot_module.Config,'from_env',lambda:settings)
    monkeypatch.setattr(bot_module,'init_db',AsyncMock())
    monkeypatch.setattr(max_client,'Bot',Mock(return_value=client))
    monkeypatch.setattr(bot_module,'Dispatcher',Mock(return_value=dispatcher))
    monkeypatch.setattr(bot_module,'PaidAudioWorker',factory)
    with pytest.raises(RuntimeError,match='stop test'):
        await bot_module.main()
    assert started.is_set() and stopped.is_set()
    client.close_session.assert_awaited_once()
    dispatcher.shutdown.assert_awaited_once()


async def test_discounted_gift_payment_validated_and_accounted_once_even_after_expiry(client, payments, credit_db, monkeypatch):
    now = 1800000000
    monkeypatch.setattr(credit_db, '_now_ts', lambda: now)
    await credit_db.set_song_promo(456)
    await credit_db.record_first_start(456, 'yandex_newyear')
    await credit_db.create_audio_order('audio-test-gift-456', 456, 'gift', 'bday', 'Аня, любит горы',
        'pop', '[Verse]\nАня', 'pop', 'Для Ани', song_discount=True)
    data = await audio_notification(payments, 'gift')
    assert data['sum'] == '392.00'
    assert data['products'][0]['name'] == 'Песня в подарок (Скидка 20%)'
    assert data['_param_package_id'] == 'audio_gift_first'
    wrong = dict(data, sum='490.00')
    assert (await post_notification(client, wrong)).status == 400
    now += 49 * 3600
    assert not await credit_db.check_song_promo(456)
    for _ in range(2):
        assert (await post_notification(client, data)).status == 200
    user = await credit_db.get_user(456)
    assert user['total_purchases'] == 1 and user['balance'] == 0
    assert (await credit_db.get_all_counters())['buy_audio_gift'] == 1
    assert (await credit_db.get_stats_by_source('yandex_newyear'))['total_revenue'] == 392


async def music_notification(payments, credit_db, tariff='direct', order_id='music_test', payment_id='music-provider-1'):
    await credit_db.create_musical_order(order_id,456,'https://cdn.example/card.jpg','https://cdn.example/full.mp3',tariff)
    order=await credit_db.get_musical_order(order_id)
    link=await payments.create_musical_payment_link(order_id=order_id,amount=order['amount_kopecks']//100,description=order['product_name'])
    fields=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    return {'order_num':order_id,'order_id':payment_id,'sum':fields['products'][0]['price'],
        'currency':'rub','payment_status':'success','domain':'test-shop.payform.ru',
        'products':fields['products'],'customer_extra':fields['customer_extra'],
        '_param_user_id':'456','_param_credits':'0','_param_package_id':fields['_param_package_id']}


@pytest.mark.parametrize('tariff,amount',[('direct','299.00'),('upsell','149.00')])
async def test_music_personal_checkout_signed_webhook_is_persistent_and_idempotent(client,payments,credit_db,tariff,amount):
    data=await music_notification(payments,credit_db,tariff)
    assert data['sum'] == amount and data['order_num'].startswith('music_')
    outgoing=payments._request_payment_link.await_args.args[0]
    unsigned={key:value for key,value in outgoing.items() if key != 'sign'}
    assert outgoing['sign'] == sign_link_payload(unsigned,SECRET)
    first=await post_notification(client,data); assert first.status == 200
    second=await post_notification(client,data); assert second.status == 200
    order=await credit_db.get_musical_order('music_test')
    assert order['status'] == 'paid' and order['audio_url'] == 'https://cdn.example/full.mp3'
    assert await credit_db.pending_musical_orders() == ['music_test']
    user=await credit_db.get_user(456)
    assert user['total_purchases'] == 1 and user['balance'] == 0
    assert (await credit_db.get_all_counters())['buy_music_'+tariff] == 1
    stats=await credit_db.get_stats_by_source('legacy')
    assert stats['total_revenue'] == int(amount.split('.')[0])


@pytest.mark.parametrize('tamper',['sum','user','product','package','signature'])
async def test_music_webhook_rejects_invalid_payment(client,payments,credit_db,tamper):
    data=await music_notification(payments,credit_db)
    if tamper=='sum': data['sum']='1.00'
    elif tamper=='user': data['_param_user_id']='999'
    elif tamper=='product': data['products'][0]['name']='Песня по фану'
    elif tamper=='package': data['_param_package_id']='package_3'
    result=await post_notification(client,data,signature='0'*64 if tamper=='signature' else None)
    assert result.status == (403 if tamper=='signature' else 400)
    assert (await credit_db.get_musical_order('music_test'))['status'] == 'pending'
    assert await credit_db.pending_musical_orders() == []


async def test_music_payment_id_cannot_be_reused_for_audio_or_credits(client,payments,credit_db):
    data=await music_notification(payments,credit_db)
    assert (await post_notification(client,data)).status==200
    credits=await notification(payments,payment_id=data['order_id'])
    assert (await post_notification(client,credits)).status==400
    await credit_db.create_audio_order('audio-collision',456,'fun','boss','Иван',None,'lyrics','pop','title')
    with pytest.raises(ValueError):
        await credit_db.mark_audio_paid('audio-collision',data['order_id'])


async def test_music_webhook_worker_renders_after_payment_and_reuses_result_for_duplicate(payments,credit_db):
    from types import SimpleNamespace
    from musical_orders import PaidMusicalWorker
    notified=asyncio.Event()
    async def send(**kwargs):
        assert kwargs['user_id']==456
        if kwargs.get('attachments'):
            notified.set()
        return object()
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=send))
    render=AsyncMock(return_value='https://cdn.example/paid.mp4')
    upload=AsyncMock(return_value=object())
    worker=PaidMusicalWorker(bot,renderer=render,uploader=upload,poll_interval=0.01)
    data=await music_notification(payments,credit_db)
    async with TestClient(TestServer(create_webhook_app(payments,musical_worker=worker))) as client:
        render.assert_not_awaited()
        response=await post_notification(client,data)
        assert response.status==200 and await response.text()=='OK'
        await asyncio.wait_for(notified.wait(),timeout=3)
        # Дожидаемся коммита delivered перед повтором уведомления.
        for attempt in range(30):
            if (await credit_db.get_musical_order('music_test'))['delivered']:
                break
            await asyncio.sleep(0.01)
        duplicate=await post_notification(client,data)
        assert duplicate.status==200
    render.assert_awaited_once_with('https://cdn.example/card.jpg','https://cdn.example/full.mp3')
    assert bot.send_message.await_count == 2
    assert (await credit_db.get_musical_order('music_test'))['delivered']==1


@pytest.mark.parametrize('tariff,amount', [('short',890),('long',1690)])
async def test_cartoon_personal_link_validated_webhook_and_duplicate_are_idempotent(payments, client, credit_db, tariff, amount):
    db=credit_db
    order_id='cartoon_456_theme_feb23_123'
    await db.create_cartoon_order(order_id,456,'theme_feb23',tariff)
    order=await db.get_cartoon_order(order_id)
    link=await payments.create_payment_link(456,order_id=order_id,amount=amount,description=order['product_name'])
    assert await payments.create_payment_link(456,order_id=order_id,amount=amount,description=order['product_name'])==link
    payments._request_payment_link.assert_awaited_once()
    fields=parse_form_fields(parse_qsl(urlsplit(link).query))
    assert fields['order_id']==order_id and fields['_param_credits']=='0'
    assert fields['_param_package_id']=='cartoon_'+tariff
    assert fields['products']==[{'name':order['product_name'],'price':f'{amount}.00','quantity':'1'}]
    assert sign_link_payload({k:v for k,v in fields.items() if k!='sign'},SECRET)==fields['sign']
    data={'order_num':order_id,'order_id':'cartoon-provider-1','payment_status':'success','sum':str(amount),
          'currency':'rub','products':fields['products'],'customer_extra':fields['customer_extra']}
    for _ in range(2):
        response=await post_notification(client,data)
        assert response.status==200 and await response.text()=='OK'
    assert (await db.get_cartoon_order(order_id))['status']=='paid'
    assert (await db.get_user(456))['balance']==0
    assert (await db.get_user(456))['total_purchases']==1
    assert (await db.get_all_counters())['buy_cartoon_'+tariff]==1
    assert len(await db.pending_cartoon_notifications())==1


@pytest.mark.parametrize('change', ['sum','product','user','signature','test','failed'])
async def test_cartoon_invalid_or_unsuccessful_webhook_never_unlocks_photo(payments, client, credit_db, change):
    await credit_db.create_cartoon_order('cartoon_456_theme_ny_1',456,'theme_ny','short')
    order=await credit_db.get_cartoon_order('cartoon_456_theme_ny_1')
    data={'order_num':order['order_id'],'order_id':'provider-1','payment_status':'success','sum':'890',
          'products':[{'name':order['product_name'],'price':'890.00','quantity':'1'}]}
    if change=='sum': data['sum']='1'
    if change=='product': data['products'][0]['name']='Другой товар'
    if change=='user': data['_param_user_id']='999'
    if change=='test': data['is_test']='1'
    if change=='failed': data['payment_status']='failed'
    response=await post_notification(client,data,signature='bad' if change=='signature' else None)
    assert response.status==(403 if change=='signature' else 200 if change in ('test','failed') else 400)
    assert (await credit_db.get_cartoon_order(order['order_id']))['status']=='pending'
    assert await credit_db.pending_cartoon_notifications()==[]


async def test_paid_cartoon_counts_as_source_buyer_and_revenue(credit_db):
    await credit_db.record_first_start(456,'yandex_newyear')
    await credit_db.create_cartoon_order('cartoon_456_theme_ny_1',456,'theme_ny','long')
    await credit_db.mark_cartoon_paid('cartoon_456_theme_ny_1','pay-source')
    stats=await credit_db.get_stats_by_source('yandex_newyear')
    assert stats['total_buyers']==1 and stats['total_revenue']==1690
