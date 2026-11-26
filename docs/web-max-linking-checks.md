# Проверка этапа 1.2Б — MAX linking

Исходная ветка `feature/web-auth-api`, коммит `ecf16597c71c3c2165bfc5145695b975a09ce9cd`; новая `feature/web-max-linking`. Все проверки в отдельных checkout и временных синтетических SQLite. Ни рабочая БД, ни её копия не открывались.

| Набор | Baseline | Новая ветка |
| --- | --- | --- |
| Исходные целевые тесты accounts/auth/API/SMTP | 227 passed | 227 passed |
| Новые OTP/linking/API/MAX тесты | — | 79 passed |
| Совместный целевой набор | 227 passed | 306 passed |
| Полный pytest | 3461 passed, 12 failed | 3540 passed, 12 failed |

Новых регрессий: **0**. Все 12 failure node IDs совпадают. **Полный прогон неуспешен** на обеих ветках; ошибки не скрывались, тесты не отключались. Ранее сообщённые 3438 passed / 6 failed приводятся как исторические данные, а не результат текущего baseline.

## Воспроизводимость и ограничения проверки

Python 3.12, отдельный venv с requirements-dev.txt + requirements-web-dev.txt. MAX_BOT_TOKEN заменён синтетическим. Внешний test harness блокирует подключения наружу, разрешая только loopback для локальных HTTP-тестов. Использованы fake email, SMTP mocks и синтетические MAX SDK events. SMTP/provider интеграции на реальных сервисах не проверялись и не запускались.

Команды pytest на каждой ветке (в выбранном checkout, с тем же внешним harness):

```text
python offline-test-runner.py -q tests/test_accounts.py tests/test_auth.py web_api/tests
python offline-test-runner.py -q tests/test_accounts.py tests/test_auth.py web_api/tests tests/test_account_linking.py tests/test_auth_browser_isolation.py tests/test_max_link_api.py tests/test_max_link_adapter.py
python offline-test-runner.py -q
```

Внешний harness — ограничение среды, а не изменение тестов приложения. В окружении с уже изолированной сетью допустим обычный `python -m pytest` с теми же путями. До полного запуска проверить, что конфигурация и все БД тестовые.

В существующем тесте миграции единственное изменение ожидания: последняя версия 4 вместо 3. Проверки прежних checksums и legacy-строк остались. OpenAPI export перегенерирован офлайн и сверяется прежним контрактным тестом. DDL/определение checksum v1 сверено с AST исходной ветки; файлы схем v2 и v3 побайтово совпадают.

## Одинаковые baseline failures

- `tests/test_cartoon_avatar_archive.py::test_restore_keeps_reviewed_words_and_never_generates_picture_or_music`
- `tests/test_custom_orders.py::test_custom_order_menu_position_and_retired_button`
- `tests/test_custom_orders.py::test_unpaid_legacy_long_state_cannot_create_a_new_invoice`
- `tests/test_ded_moroz_family.py::test_family_stats_separate_customer_interest_from_admin_tests`
- `tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[False-590]`
- `tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[False-990]`
- `tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[True-590]`
- `tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[True-990]`
- `tests/test_photo_payment_refresh.py::test_provider_error_never_returns_the_old_photo_link[590]`
- `tests/test_photo_payment_refresh.py::test_provider_error_never_returns_the_old_photo_link[990]`
- `tests/test_welcome_ads.py::test_exact_main_buttons_and_welcome_copy`
- `tests/test_welcome_ads.py::test_stats_shows_eight_labels_and_clicks`

Это старые проверки открыток/ручных заказов/статистики и фотосессий, а не сценарий привязки. Некоторые проверки фототарифов, например, ожидают 590/990 при возвращаемых исходной веткой 490/690. Их не исправляли в этом PR и не меняли действующие цены.

## Проверенные свойства

- 2 браузера, legacy-вызов, successful-login invalidation, активные caps, конкурентные запросы и общий бюджет OTP.
- 80-битный одноразовый код/HMAC/TTL, коллизии и лимиты после создания нового экземпляра сервиса.
- Регистрация MAX только существующим сценарием, trusted private events, callback actor вместо bot sender, запрет forged/group/bot events.
- Явные MAX + web подтверждения, неизменяемый MAX-кандидат, session/account isolation, CSRF/Origin/строгий JSON и body limit.
- Logout/revoke/idle/hard expiry/fresh auth, проверка после ожидания write-lock, конфликт в финальной транзакции.
- Конкурентные MAX claim, две финализации/две web-учётки, cancel/confirm race, rollback после вставки identity.
- Неизменность users, платежных строк и числа accounts; отсутствие новых начислений и секретов в логах/публичных ответах.
- Выключенные флаги без v4-запросов, явная миграция v1–v4, проверка сертификата/hostname при включённой MAX-привязке.

Открытые ограничения: UI не подключён, общий баланс/entitlements/слияние/отвязка не реализованы, для просроченной свежести нужен новый вход и новая операция, ручной код остаётся в истории MAX, до production требуется согласовать хранение аудита операций и проверенный TLS.
