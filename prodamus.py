"""Ссылки через API Prodamus Payform (do=link) и проверка HMAC webhook.

Протокол: https://github.com/PRODAMUS/integration-expert/tree/main/
prodamus-integration-expert/references (payform-protocol.md, hmac.md).
"""

import hashlib
import hmac
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import aiohttp
from yarl import URL

from config import Config
from database import get_photo_order, set_photo_payment_link, PHOTO_TARIFFS, get_cartoon_order, update_cartoon_order, CARTOON_TARIFFS, get_musical_order, update_musical_order, MUSICAL_TARIFFS, create_payment_order, get_payment_order, create_audio_order, get_audio_order, update_audio_order, SONG_PROMO_PRODUCT

PACKAGES = {
    1: (9900, "1 генерация (Открытка)"),
    3: (19900, "3 генерации"),
    5: (29900, "5 генераций"),
    15: (79900, "15 генераций"),
}
logger = logging.getLogger(__name__)

SIGN_FIELDS = {"signature", "sign", "_payform_sign"}


def _prepare(value):
    if isinstance(value, dict):
        return {key: _prepare(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_prepare(item) for item in value]
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def sign_payload(data: dict, secret: str) -> str:
    """PHP json_encode(JSON_UNESCAPED_UNICODE): строки, сортировка, escaped slash."""
    canonical = json.dumps(_prepare(data), ensure_ascii=False, separators=(",", ":"))
    canonical = canonical.replace("/", "\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def sign_link_payload(data: dict, secret: str) -> str:
    """Исходящий sign для do=link: отсортированный query RFC3986, не webhook HMAC."""
    query = urlencode(flatten_fields(_prepare(data)), quote_via=quote)
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def verify_signature(data: dict, signature: str, secret: str) -> bool:
    if not secret or not re.fullmatch(r"[a-fA-F0-9]{64}", signature):
        return False
    clean = {key: value for key, value in data.items() if key not in SIGN_FIELDS}
    return hmac.compare_digest(sign_payload(clean, secret), signature.lower())


def flatten_fields(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    result = []
    items = enumerate(data) if isinstance(data, list) else data.items()
    for key, value in items:
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, (dict, list)):
            result.extend(flatten_fields(value, name))
        else:
            result.append((name, str(value)))
    return result


def parse_form_fields(fields) -> dict:
    """Восстановить PHP-массивы products[0][name], не теряя дубликаты ключей."""
    root = {}
    for name, value in fields:
        if not isinstance(value, str):
            raise ValueError("Файлы в уведомлении не поддерживаются.")
        match = re.fullmatch(r"([^\[\]]+)((?:\[[^\[\]]+\])*)", name)
        if not match:
            raise ValueError("Некорректное имя поля.")
        keys = [match[1], *re.findall(r"\[([^\[\]]+)\]", match[2])]
        if len(keys) > 8:
            raise ValueError("Слишком большая вложенность.")
        node = root
        for key in keys[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError("Конфликт полей.")
        if keys[-1] in node:
            raise ValueError("Дублирующееся поле.")
        node[keys[-1]] = value

    def convert(node):
        if not isinstance(node, dict):
            return node
        if node and set(node) == {str(i) for i in range(len(node))}:
            return [convert(node[str(i)]) for i in range(len(node))]
        return {key: convert(value) for key, value in node.items()}

    return {key: convert(value) for key, value in root.items()}


def _https_url(value: str | None) -> str:
    parsed = urlsplit(value or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Для Prodamus задайте корректные публичные HTTPS URL.")
    return value


def _kopecks(value) -> int:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Некорректная сумма.")
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", str(value)):
        raise ValueError("Некорректная сумма.")
    try:
        number = Decimal(value)
        if not number.is_finite() or number <= 0 or number * 100 != (number * 100).to_integral_value():
            raise ValueError("Некорректная сумма.")
        return int(number * 100)
    except InvalidOperation as exc:
        raise ValueError("Некорректная сумма.") from exc


class ProdamusPayments:
    def __init__(self, config: Config):
        if not config.prodamus_domain or not config.prodamus_secret_key:
            raise ValueError("Задайте PRODAMUS_DOMAIN и PRODAMUS_SECRET_KEY.")
        domain = config.prodamus_domain.strip()
        if "://" not in domain:
            domain = "https://" + domain
        self.domain = _https_url(domain).rstrip("/") + "/"
        parsed = urlsplit(self.domain)
        if parsed.path != "/" or parsed.query or parsed.port not in (None, 443) or not any(
            parsed.hostname.endswith("." + suffix)
            for suffix in ("payform.ru", "prodamus.ru", "prodamus.tech")
        ):
            raise ValueError("PRODAMUS_DOMAIN должен быть доменом платёжной формы Prodamus.")
        if not config.prodamus_return_url or not config.prodamus_webhook_url:
            raise ValueError("Для приёма оплаты задайте PRODAMUS_RETURN_URL и PRODAMUS_WEBHOOK_URL.")
        self.return_url = _https_url(config.prodamus_return_url)
        self.webhook_url = _https_url(config.prodamus_webhook_url)
        webhook = urlsplit(self.webhook_url)
        if webhook.path != "/prodamus/webhook" or webhook.query:
            raise ValueError("PRODAMUS_WEBHOOK_URL должен оканчиваться на /prodamus/webhook.")
        self.secret = config.prodamus_secret_key

    async def create_payment_link(self, user_id: int, credits: int | None = None, *, promo: bool = False,
                                  order_id: str | None = None, amount: int | None = None,
                                  description: str | None = None) -> str:
        if order_id is not None:
            if credits is not None or promo:
                raise ValueError('Нельзя смешивать мульт-заказ и пакет кредитов.')
            if order_id.startswith('photo_'):
                return await self._create_photo_payment_link(user_id, order_id, amount, description)
            return await self._create_cartoon_payment_link(user_id, order_id, amount, description)
        if amount is not None or description is not None:
            raise ValueError('Для персональной оплаты нужен номер заказа.')
        if isinstance(credits, bool) or credits not in PACKAGES or isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Некорректный пользователь или пакет.")
        amount, title = PACKAGES[credits]
        if promo:
            if credits != 3:
                raise ValueError("Скидка доступна только для трёх генераций.")
            amount = 14900
        order_id = f"max-{user_id}-{uuid4().hex}"
        # Создать заказ/проверить срок скидки до обращения к платёжному сервису.
        order_id = await create_payment_order(order_id, user_id, credits, amount, title, promo=promo)
        package_id = "promo_3" if promo else f"package_{credits}"
        data = {
            "do": "link", "order_id": order_id,
            "products": [{"name": title, "price": f"{amount // 100}.00", "quantity": "1"}],
            "customer_extra": json.dumps(
                {"user_id": user_id, "credits": credits, "package_id": package_id},
                separators=(",", ":"),
            ),
            "_param_user_id": str(user_id), "_param_credits": str(credits),
            "_param_package_id": package_id,
            "urlReturn": self.return_url, "urlSuccess": self.return_url,
            "urlNotification": self.webhook_url, "payments_limit": "1",
        }
        data["sign"] = sign_link_payload(data, self.secret)
        # Заказ должен существовать ДО передачи ссылки покупателю.
        return await self._request_payment_link(data)

    async def _request_payment_link(self, data: dict) -> str:
        query = urlencode(flatten_fields(data), quote_via=quote)
        # Не повторяем запрос автоматически: Prodamus мог создать ссылку до таймаута.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True,
        ) as session:
            async with session.get(
                URL(self.domain + "?" + query, encoded=True), allow_redirects=True,
            ) as response:
                if response.status != 200:
                    raise RuntimeError("Prodamus не подтвердил создание платёжной ссылки.")
                body = (await response.text()).strip()
        if body.startswith("{"):
            result = json.loads(body)
            body = result.get("payment_link") or result.get("link")
        if not isinstance(body, str):
            raise ValueError("Prodamus не вернул платёжную ссылку.")
        link = _https_url(body.strip())
        product = data['products'][0]
        description = product['name']
        amount = format(Decimal(product['price']).normalize(), 'f')
        order_id = data['order_id']
        logger.info(f"Создан счет: {description}, сумма: {amount} ₽, order_id: {order_id}")
        return link

    async def create_audio_payment_link(
        self, user_id: int, audio_format: str, scenario: str, details: str, style: str | None,
        prompt: str, tags: str, title: str, *, order_id: str,
    ) -> str:
        if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id <= 0:
            raise ValueError('Некорректный пользователь аудиозаказа.')
        existing = await get_audio_order(order_id)
        if existing is None:
            await create_audio_order(order_id, user_id, audio_format, scenario, details, style, prompt, tags, title)
        order = await get_audio_order(order_id)
        identity = ('user_id','audio_format','scenario','details','style','prompt','tags','title')
        if tuple(order[key] for key in identity) != (user_id,audio_format,scenario,details,style,prompt,tags,title):
            raise ValueError('Нельзя изменить сохранённый аудиозаказ.')
        if order['status'] != 'pending':
            raise ValueError('Аудиозаказ уже оплачен.')
        expected = {'fun': (19900,'Песня по фану'), 'gift': (49000,'Песня в подарок')}.get(audio_format)
        if expected != (order['amount_kopecks'],order['product_name']):
            raise ValueError('Для новой ссылки выберите действующий аудиотариф.')
        if order['payment_link']:
            return order['payment_link']
        package_id = order['package_id']
        data = {
            'do': 'link', 'order_id': order_id,
            'products': [{'name': order['product_name'], 'price': f"{order['amount_kopecks'] // 100}.00", 'quantity': '1'}],
            'customer_extra': json.dumps({'user_id': user_id, 'credits': 0, 'package_id': package_id}, separators=(',', ':')),
            '_param_user_id': str(user_id), '_param_credits': '0', '_param_package_id': package_id,
            'urlReturn': self.return_url, 'urlSuccess': self.return_url, 'urlNotification': self.webhook_url,
            'payments_limit': '1',
        }
        data['sign'] = sign_link_payload(data, self.secret)
        link = await self._request_payment_link(data)
        await update_audio_order(order_id, payment_link=link)
        return link

    async def _create_photo_payment_link(self, user_id, order_id, amount, description) -> str:
        order = await get_photo_order(order_id)
        if (isinstance(amount, bool) or not isinstance(amount, int) or not order
                or order['status'] != 'pending' or order['user_id'] != user_id
                or order['amount_kopecks'] != amount * 100 or order['product_name'] != description):
            raise ValueError('Параметры оплаты не совпадают с фотозаказом.')
        if order['payment_link']:
            return order['payment_link']
        if PHOTO_TARIFFS.get(order['tariff']) != (order['amount_kopecks'],order['product_name']):
            raise ValueError('Для новой ссылки выберите действующий тариф.')
        data = {
            'do': 'link', 'order_id': order_id,
            'products': [{'name': description, 'price': f'{amount}.00', 'quantity': '1'}],
            'customer_extra': json.dumps({'user_id': user_id, 'credits': 0, 'package_id': order['package_id']}, separators=(',', ':')),
            '_param_user_id': str(user_id), '_param_credits': '0', '_param_package_id': order['package_id'],
            'urlReturn': self.return_url, 'urlSuccess': self.return_url, 'urlNotification': self.webhook_url,
            'payments_limit': '1',
        }
        data['sign'] = sign_link_payload(data, self.secret)
        link = await self._request_payment_link(data)
        await set_photo_payment_link(order_id, link)
        return link

    async def _create_cartoon_payment_link(self, user_id, order_id, amount, description) -> str:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError('Некорректная сумма мульт-заказа.')
        order = await get_cartoon_order(order_id)
        if (not order or order['status'] != 'pending' or order['user_id'] != user_id
                or order['amount_kopecks'] != amount * 100 or order['product_name'] != description):
            raise ValueError('Параметры оплаты не совпадают с мульт-заказом.')
        if order['payment_link']:
            return order['payment_link']
        if CARTOON_TARIFFS.get(order['tariff']) != (order['amount_kopecks'],order['product_name']):
            raise ValueError('Для новой ссылки выберите действующий тариф.')
        data = {
            'do': 'link', 'order_id': order_id,
            'products': [{'name': description, 'price': f'{amount}.00', 'quantity': '1'}],
            'customer_extra': json.dumps({'user_id': user_id, 'credits': 0, 'package_id': order['package_id']}, separators=(',', ':')),
            '_param_user_id': str(user_id), '_param_credits': '0', '_param_package_id': order['package_id'],
            'urlReturn': self.return_url, 'urlSuccess': self.return_url, 'urlNotification': self.webhook_url,
            'payments_limit': '1',
        }
        data['sign'] = sign_link_payload(data, self.secret)
        link = await self._request_payment_link(data)
        await update_cartoon_order(order_id, payment_link=link)
        return link

    async def create_musical_payment_link(self, *, order_id: str, amount: int, description: str) -> str:
        order = await get_musical_order(order_id)
        if isinstance(amount,bool) or not isinstance(amount,int) or not order or order['status'] != 'pending' or order['amount_kopecks'] != amount * 100 or order['product_name'] != description:
            raise ValueError('Параметры оплаты не совпадают с музыкальным заказом.')
        if order['payment_link']:
            return order['payment_link']
        if MUSICAL_TARIFFS.get(order['tariff']) != (order['amount_kopecks'],order['product_name']):
            raise ValueError('Для новой ссылки выберите действующий музыкальный тариф.')
        data = {
            'do': 'link', 'order_id': order_id,
            'products': [{'name': description, 'price': f'{amount}.00', 'quantity': '1'}],
            'customer_extra': json.dumps({'user_id': order['user_id'], 'credits': 0, 'package_id': order['package_id']}, separators=(',', ':')),
            '_param_user_id': str(order['user_id']), '_param_credits': '0', '_param_package_id': order['package_id'],
            'urlReturn': self.return_url, 'urlSuccess': self.return_url, 'urlNotification': self.webhook_url,
            'payments_limit': '1',
        }
        data['sign'] = sign_link_payload(data, self.secret)
        link = await self._request_payment_link(data)
        await update_musical_order(order_id, payment_link=link)
        return link

    async def validate_payment(self, data: dict):
        order_id = data.get("order_num")
        payment_id = data.get("order_id")
        if not isinstance(order_id, str) or not isinstance(payment_id, str) or not payment_id or len(payment_id) > 200:
            raise ValueError("Нет номера заказа или платежа.")
        # A saved family identifies the product; identical prices are never dispatch keys.
        candidates = [await get_payment_order(order_id), await get_audio_order(order_id)]
        for prefix, getter in (('photo_',get_photo_order),('cartoon_',get_cartoon_order),('music_',get_musical_order)):
            if order_id.startswith(prefix):
                candidates.append(await getter(order_id))
        orders = [dict(candidate) for candidate in candidates if candidate is not None]
        if len(orders) != 1:
            raise ValueError("Заказ не найден или его тип неоднозначен.")
        order = orders[0]
        if str(data.get("currency", "rub")).lower() != "rub":
            raise ValueError("Валюта не соответствует заказу.")
        if _kopecks(data.get("sum", data.get("amount"))) != order["amount_kopecks"]:
            raise ValueError("Сумма не соответствует заказу.")
        if "domain" in data:
            incoming_domain = str(data["domain"]).removeprefix("https://").rstrip("/").lower()
            if incoming_domain != urlsplit(self.domain).hostname:
                raise ValueError("Платёжная форма не соответствует заказу.")
        products = data.get("products")
        if not isinstance(products, list) or len(products) != 1 or not isinstance(products[0], dict):
            raise ValueError("Некорректный состав заказа.")
        product = products[0]
        if (
            unescape(str(product.get("name", ""))) != order["product_name"]
            or _kopecks(product.get("price")) != order["amount_kopecks"]
            or str(product.get("quantity")) not in ("1", "1.0", "1.00")
        ):
            raise ValueError("Товар не соответствует заказу.")
        if "_param_user_id" in data and str(data["_param_user_id"]) != str(order["user_id"]):
            raise ValueError("Пользователь не соответствует заказу.")
        if "_param_credits" in data and str(data["_param_credits"]) != str(order["credits"]):
            raise ValueError("Пакет не соответствует заказу.")
        package_id = order["package_id"] or f"package_{order['credits']}"
        if "_param_package_id" in data and data["_param_package_id"] != package_id:
            raise ValueError("Идентификатор пакета не соответствует заказу.")
        if "customer_extra" in data:
            extra = json.loads(data["customer_extra"])
            if not isinstance(extra, dict) or extra.get("user_id") != order["user_id"] or extra.get("credits") != order["credits"]:
                raise ValueError("Метаданные покупателя не соответствуют заказу.")
            if "package_id" in extra and extra["package_id"] != package_id:
                raise ValueError("Метаданные пакета не соответствуют заказу.")
        # Сумма уже сверена с сохранённым заказом выше: смена прайса не должна
        # блокировать оплату ссылки, выданной до изменения цены.
        if order.get('order_type') == 'audio':
            expected = {'audio_fun': (19900, 'Песня по фану'), 'audio_gift': (49000, 'Песня в подарок'),
                        'audio_gift_first': (39200, SONG_PROMO_PRODUCT)}
            if expected.get(order['package_id']) != (order['amount_kopecks'], order['product_name']):
                raise ValueError('Неизвестный аудиотариф.')
        elif order.get('order_type') == 'photo':
            if PHOTO_TARIFFS.get(order['tariff']) != (order['amount_kopecks'], order['product_name']):
                raise ValueError('Неизвестный фототариф.')
        elif order.get('order_type') == 'cartoon':
            # Сумма уже сверена с сохранённым заказом: ранее выданные счета
            # остаются действительными после изменения прайса.
            tariff = CARTOON_TARIFFS.get(order['tariff'])
            # Сохраняем совместимость с уже выданными счетами короткого формата.
            names = {tariff[1]} if tariff else set()
            if order['tariff'] == 'short':
                names.add('Мульт-открытка (10-15 сек)')
            if order['product_name'] not in names:
                raise ValueError('Неизвестный мульт-тариф.')
        elif order.get('order_type') == 'musical':
            if MUSICAL_TARIFFS.get(order['tariff']) != (order['amount_kopecks'], order['product_name']):
                raise ValueError('Неизвестный музыкальный тариф.')
        elif order["credits"] not in PACKAGES or PACKAGES[order["credits"]][1] != order["product_name"]:
            raise ValueError("Неизвестный пакет.")
        return order, payment_id
