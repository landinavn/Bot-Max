# Проверка этапа 1.2А.1 — 26 ноября 2026

Исходная ветка `feature/web-core-auth`, коммит `4c8207910bf27a19b1f6eeb666e4f35a4b22c0b2`. PR #255/#256 на старте открыты и не слиты. Новая ветка `feature/web-auth-api`, PR направляется в `feature/web-core-auth`.

## Целевые проверки

- Предыдущие `tests/test_accounts.py` + `tests/test_auth.py`: **157 passed**.
- Новые `web_api/tests`: **68 passed** (отдельно от MAX-фикстур).
- Финальный совместный целевой прогон: **225 passed**, 11,81 с, без предупреждений.
- `compileall` для web_api/auth/accounts: успешно.
- Offline OpenAPI: 3.1.0, восемь endpoints, схемы sessionCookie/preauthCookie/csrfHeader; проверена идентичность файла `docs/web-auth-openapi.json` контракту фабрики.
- Настоящий локальный Uvicorn + HTTP: ready → CSRF → request → verify → me → logout → 401: успешно. Временная новая БД, FakeEmailSender, сервер после проверки остановлен. Реальных писем не было.

Python 3.12.14; FastAPI 0.141.1, Starlette 1.6.0, Pydantic 2.13.4, Uvicorn 0.52.1, aiosqlite 0.22.1, httpx 0.28.1, pytest 9.1.1, pytest-asyncio 1.4.0. Версии зафиксированы в отдельных requirements-web*.txt. В окружении проверки уже были доступны зависимости; рабочие зависимости бота и production не изменялись.

## Общий регрессионный прогон

Новая ветка: **3438 passed, 6 failed**, 430,56 с; ошибок подготовки (errors) нет. Это **не успешный полный прогон**. Он включал 67 новых проверок; добавленная затем проверка совпадения сохранённого OpenAPI вошла в финальные 68/225 целевых тестов. После изменения кодировки SMTP на quoted-printable повторно прошёл весь новый/целевой набор; наследуемые модули генерации/оплаты не менялись.

Все шесть падений независимо воспроизводятся на исходном коммите (отдельный прогон: **6 failed**, 2,59 с):

1. `test_cartoon_avatar_archive.py::test_restore_keeps_reviewed_words_and_never_generates_picture_or_music`
2. `test_custom_orders.py::test_custom_order_menu_position_and_retired_button`
3. `test_custom_orders.py::test_unpaid_legacy_long_state_cannot_create_a_new_invoice`
4. `test_ded_moroz_family.py::test_family_stats_separate_customer_interest_from_admin_tests`
5. `test_welcome_ads.py::test_exact_main_buttons_and_welcome_copy`
6. `test_welcome_ads.py::test_stats_shows_eight_labels_and_clicks`

Новых падений в выполненном общем прогоне не обнаружено. Перечисленные legacy-проверки находятся вне изменений HTTP/auth и не исправлялись несвязанным рефакторингом.

Первоначальные общие прогоны в среде без возможности создания локального сокета были прерваны и не считаются итогом. После получения разрешения на loopback использовался внешний тестовый harness, блокирующий внешние TCP connect/connect_ex и заменяющий self-pipe asyncio для изолированной среды. Legacy-конфигурация использовала только явно синтетический MAX-токен; реальные токены/SMTP не применялись.

Дополнительный общий прогон исходной ветки столкнулся с зависшим FFmpeg в `test_customer_complete_flow_two_audios_choose_first_and_next_invoice`: собственный тестовый подпроцесс был остановлен после нескольких минут. Это ограничение сравнительного прогона, а не изменение генератора. По этой причине для точной проверки шести ошибок выполнен отдельный короткий baseline-прогон выше.

В одном предварительном целевом прогоне наблюдалось прежнее предупреждение `aiosqlite: Event loop is closed` при завершении worker thread; финальные 225 тестов прошли без него. Предупреждение не скрывалось и не исправлялось изменениями legacy-storage вне задачи.

## Проверяемые свойства

В новых тестах: регистрация/повторный вход/один аккаунт, cookie attributes/без Domain/без токена в JSON, отзыв всех/одной сессии, expiry/idle/revocation, login CSRF, чужой/повторный browser context, параллельный verify, смена email и вкладки, явный logout перед сменой аккаунта, Origin/CSRF/CORS, источник и поддельные proxy headers, IPv4/IPv6, постоянные source/email/global бюджеты, body limit/chunked/схемы, чужой account_id, SQLite busy, ready без миграции, неизменность v1/v2 и synthetic legacy rows, безопасные ошибки/логи, импорт без IO/MAX, TLS/UTF-8/подмена заголовков, доказанный/неопределённый отказ SMTP, отмена потока без повторов.

Реальная SMTP-доставка, публичный HTTPS, работа конкретного Nginx и браузерный frontend production не проверялись: это отдельный этап после данных владельца и разрешения на подключение. Основные остаточные риски/план запуска перечислены в `web-auth-api.md`.

Production, рабочая БД/резервная копия, MAX-бот, тарифы, платежи и AI-провайдеры не использовались и не менялись. Реальных email/платежей/платных AI-запросов, deployment, перезапуска бота и автоматического слияния не было.
