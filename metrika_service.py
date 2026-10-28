"""Офлайн-конверсии Метрики: необязательная загрузка без влияния на выдачу картинки."""

import csv
import io
import json
import logging
import re
import time
from urllib.parse import unquote

import aiohttp

from config import Config
from database import get_user

logger = logging.getLogger(__name__)
BASE_URL = 'https://api-metrika.yandex.net'


def extract_yclid(payload) -> str | None:
    """Поддерживает query-параметры, URL-кодированный start и JSON payload."""
    for _ in range(3):
        if isinstance(payload, dict):
            value = payload.get('yclid')
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                value = str(value).strip()
                if re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
                    return value
            payload = payload.get('start') or payload.get('payload')
            continue
        if not isinstance(payload, str):
            return None
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
            continue
        match = re.search(r'(?:^|[?&;\s])yclid=([A-Za-z0-9_-]{1,256})(?=$|[&#;\s])', payload)
        if match:
            return match.group(1)
        decoded = unquote(payload)
        if decoded == payload:
            return None
        payload = decoded
    return None


async def send_offline_conversion(user_id: int, goal_id: str = 'bot_generation') -> None:
    """Один POST с таймаутом 5 секунд. Ошибки логируются, POST не повторяется."""
    token = None
    try:
        settings = Config.from_env()
        token = settings.yandex_metrika_oauth_token
        counter = settings.yandex_metrika_counter_id
        if not counter or not token:
            logger.warning('Метрика: не заданы YANDEX_METRIKA_COUNTER_ID или YANDEX_METRIKA_OAUTH_TOKEN; конверсия не отправлена.')
            return
        if not re.fullmatch(r'[0-9]+', counter) or int(counter) <= 0:
            logger.error('Ошибка Метрики: некорректный YANDEX_METRIKA_COUNTER_ID.')
            return
        user = await get_user(user_id)
        yclid = user['yclid'] if user else ''
        visitor_id = str(user['user_id'] if user else user_id)
        columns = ['Target', 'DateTime', 'UserId']
        values = [goal_id, int(time.time()) - 1, visitor_id]
        if yclid:
            columns.append('Yclid')
            values.append(yclid)
        csv_file = io.StringIO(newline='')
        writer = csv.writer(csv_file)
        writer.writerow(columns)
        writer.writerow(values)
        form = aiohttp.FormData()
        form.add_field('file', csv_file.getvalue().encode('utf-8'),
                       filename='conversions.csv', content_type='text/csv; charset=utf-8')
        # aiohttp сам задаёт multipart/form-data с правильным boundary.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(f'{BASE_URL}/management/v1/counter/{counter}/offline_conversions/upload',
                                    headers={'Authorization': f'OAuth {token}'}, data=form, allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    details = (await response.text()).replace(token, '[redacted]')[:2000]
                    logger.error('Ошибка Метрики: HTTP %s: %s', response.status, details)
                    return
        logger.info('Метрика: конверсия %s загружена.', goal_id)
    except Exception as exc:
        message = str(exc).replace(token, '[redacted]') if token else str(exc)
        logger.error('Ошибка Метрики: %s: %s', type(exc).__name__, message)
