"""Асинхронный endpoint Prodamus; подтверждение только после коммита SQLite."""

import json
import logging
import sqlite3
from urllib.parse import parse_qsl

from aiohttp import web

from database import add_credits
from prodamus import ProdamusPayments, parse_form_fields, verify_signature

logger = logging.getLogger(__name__)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Дублирующееся поле JSON.")
        result[key] = value
    return result


def create_webhook_app(payments: ProdamusPayments) -> web.Application:
    app = web.Application(client_max_size=128 * 1024)

    async def payment_webhook(request: web.Request) -> web.Response:
        logger.info("HTTP POST Prodamus: уведомление об оплате.")
        if request.content_type not in {
            "application/x-www-form-urlencoded", "multipart/form-data", "application/json",
        }:
            return web.Response(status=415, text="Unsupported content type")
        try:
            if request.content_type == "application/json":
                data = json.loads(await request.text(), object_pairs_hook=_unique_object)
                if not isinstance(data, dict):
                    raise ValueError("Ожидается объект JSON.")
            elif request.content_type == "multipart/form-data":
                data = parse_form_fields((await request.post()).items())
            else:
                fields = parse_qsl(
                    await request.text(), keep_blank_values=True, strict_parsing=True,
                    errors="strict", max_num_fields=1000,
                )
                data = parse_form_fields(fields)
            if not verify_signature(data, request.headers.get("Sign", ""), payments.secret):
                logger.error("Prodamus: неверная подпись уведомления.")
                return web.Response(status=403, text="Invalid signature")
            if str(data.get("is_test", "0")) == "1":
                logger.info("Prodamus: тестовое уведомление, баланс не изменён.")
                return web.Response(text="OK")
            if data.get("payment_status") != "success":
                logger.info("Prodamus: уведомление без успешной оплаты пропущено.")
                return web.Response(text="OK")
            order, payment_id = await payments.validate_payment(data)
            # add_credits увеличивает total_purchases и buy_<credits> по проверенному
            # заказу в той же транзакции: повтор webhook не увеличивает счётчики.
            credited = await add_credits(
                order["user_id"], order["credits"],
                payment_order_id=order["order_id"], provider_payment_id=payment_id,
            )
            logger.info("Prodamus: %s.", "кредиты начислены" if credited else "повтор уведомления")
            return web.Response(text="OK")
        except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.IntegrityError):
            logger.error("Prodamus: уведомление не соответствует сохранённому заказу или формату.")
            return web.Response(status=400, text="Invalid payment data")
        except sqlite3.Error:
            logger.error("Prodamus: ошибка SQLite, уведомление необходимо повторить.")
            return web.Response(status=503, text="Please retry")

    app.router.add_post("/prodamus/webhook", payment_webhook)
    return app


async def start_webhook_server(
    payments: ProdamusPayments, host: str, port: int,
) -> web.AppRunner:
    runner = web.AppRunner(create_webhook_app(payments), access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("HTTP endpoint Prodamus запущен на порту %d.", port)
    return runner
