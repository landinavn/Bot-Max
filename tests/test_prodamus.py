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


async def notification(payments, credits=5, *, payment_id="provider-1", user_id=456):
    link = await payments.create_payment_link(user_id, credits)
    fields = parse_form_fields(parse_qsl(urlsplit(link).query, keep_blank_values=True))
    return {
        "order_num": fields["order_id"], "order_id": payment_id,
        "sum": fields["products"][0]["price"], "currency": "rub",
        "domain": "test-shop.payform.ru", "payment_status": "success",
        "products": fields["products"], "customer_extra": fields["customer_extra"],
        "_param_user_id": str(user_id), "_param_credits": str(credits),
        "_param_package_id": f"package_{credits}",
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


@pytest.mark.parametrize("credits,price", [(5, "299.00"), (15, "699.00")])
async def test_signed_link_persists_bound_order(payments, credit_db, credits, price):
    link = await payments.create_payment_link(456, credits)
    assert link.startswith("https://test-shop.payform.ru/?")
    data = parse_form_fields(parse_qsl(urlsplit(link).query))
    assert sign_link_payload({key: value for key, value in data.items() if key != "sign"}, SECRET) == data["sign"]
    assert data["do"] == "link"
    assert data["urlNotification"] == payments.webhook_url
    assert data["urlSuccess"] == data["urlReturn"] == payments.return_url
    assert data["products"] == [{"name": f"{credits} генераций", "price": price, "quantity": "1"}]
    assert data["_param_package_id"] == f"package_{credits}"
    assert json.loads(data["customer_extra"]) == {"user_id": 456, "credits": credits, "package_id": f"package_{credits}"}
    order = await credit_db.get_payment_order(data["order_id"])
    assert order["user_id"] == 456 and order["credits"] == credits
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


@pytest.mark.parametrize("credits", [5, 15])
@pytest.mark.parametrize("encoding", ["form", "multipart", "json"])
async def test_webhook_credits_paid_order_once(client, payments, credit_db, credits, encoding):
    await credit_db.grant_free_trial(456)
    data = await notification(payments, credits)
    response = await post_notification(client, data, encoding=encoding)
    assert response.status == 200 and await response.text() == "OK"
    assert (await credit_db.get_user(456))["balance"] == credits + 1
    assert (await credit_db.get_user(456))["free_trial_used"] == 1
    assert (await credit_db.get_payment_order(data["order_num"]))["status"] == "paid"
    await credit_db.init_db()  # Повтор после перезапуска и с новым attempt.
    data["attempt"] = "2"
    assert (await post_notification(client, data, encoding=encoding)).status == 200
    assert (await credit_db.get_user(456))["balance"] == credits + 1


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
    data["sum"] = "699.00"
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
