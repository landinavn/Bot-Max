# Проверки этапа 1.3А

Дата: 26 ноября 2026. Только выдуманные данные, новые временные SQLite-базы.
Ни production, ни его backup не использовались. Deployment/merge не выполнялись.

## Основа и повтор baseline

- Репозиторий `landinavn/Bot-Max`.
- Основа `feature/web-max-linking`, `04b91be474e99922be7ced4c56f3ca22a38174b4`.
- Для baseline создан отдельный detached worktree того же коммита.
- Новая ветка `feature/web-entitlements`, зависимый draft PR в ветку основы.
- Frontend PR №257 не затрагивается.

| Прогон | Passed | Failed | Примечание |
|---|---:|---:|---|
| Исторический baseline до PR №261 | 3461 | 12 | Предоставлен ранее; не заменяется результатом новой ветки |
| Исторический PR №261 | 3540 | 12 | Предоставлен ранее |
| **Повтор baseline 04b91be, целевые** | **306** | **0** | 9.20 s |
| **Повтор baseline 04b91be, полный** | **3540** | **12** | 382.87 s, 4 warnings |
| Окончательные целевые, новая ветка | **493** | **0** | 17.75 s, 1 aiosqlite thread warning в прежнем auth test |
| Промежуточный полный новой ветки | 3720 | 12 | 398.27 s; после него добавлены 7 тестов и финальные проверки входных данных |

Результат окончательного полного прогона фиксируется ниже после его завершения.
Полный прогон с падениями **не считается успешным**.

## Команды и изоляция

Python 3.12, отдельный venv с `requirements-dev.txt` и `requirements-web-dev.txt`.
Команды запускались вне production, с `MAX_BOT_TOKEN=synthetic-test-token`.

```bash
MAX_BOT_TOKEN=synthetic-test-token \
  /workspace/work/max-linking-venv/bin/python \
  /workspace/work/auth_offline_test_runner.py -q \
  tests/test_entitlements.py tests/test_entitlements_legacy.py \
  tests/test_accounts.py tests/test_auth.py web_api/tests \
  tests/test_account_linking.py tests/test_auth_browser_isolation.py \
  tests/test_max_link_api.py tests/test_max_link_adapter.py

# Baseline targeted: тот же список без двух новых test_entitlements*.py.
# Полный прогон в каждом соответствующем worktree:
MAX_BOT_TOKEN=synthetic-test-token \
  /workspace/work/max-linking-venv/bin/python \
  /workspace/work/auth_offline_test_runner.py -q
```

`auth_offline_test_runner.py` — внешний harness окружения, **не часть приложения**:
запускает `pytest.main`, блокирует исходящие сетевые соединения, разрешает loopback
для SMTP/HTTP тестов; заменяет asyncio socket self-pipe на OS pipe для sandbox.
Одинаковый harness применён к baseline и новой ветке. Платёжные/provider/email/MAX
вызовы в тестах замоканы; новая финансовая библиотека вообще не содержит network
клиентов. Все DB paths тестов указывают на pytest tmp directories, без реальных БД.
В обычной среде тесты запускаются `python -m pytest` с теми же аргументами;
ограничение исходящей сети следует сохранить на уровне тестового окружения.

## Новые проверки: 187

- Grant одного и нескольких связанных видов права одной транзакцией.
- Повтор одного receipt с тем же/новым command key, смена owner/payload — Conflict.
- Уникальность purchase/order refs, запрет повторной передачи покупки другому owner.
- Все 7 видов единиц, разные products, order scopes, несколько lots, split allocation.
- Целые положительные количества; bool/float/строки/отрицательные/чрезмерные значения отклоняются.
- Allowlist metadata, запрет приватных payload/URL/email и malformed assertions.
- Два image продукта используют общий credit; остальные единицы не конвертируются.
- Expiry boundary; hold и settlement после expiry, release не оживляет истёкший lot.
- Reserve/commit/release/retry и изоляция owner; terminal transition запреты.
- Unknown POST удерживает reserve без timeout-release и повторного submitting.
- 12 конкурентных соединений за последнюю единицу; повтор commit в 8 соединениях.
- 8 отдельных процессов за последнюю единицу; 6 процессов commit/release одного
  резерва; 5 процессов повторяют один платёж с разными ключами. Единственный результат.
- SQLite busy; rollback grant/reserve/commit/release и asyncio cancellation.
- Append-only SQL triggers, allocation capacity и cross-account guards, schema tampering.
- Compensation привязана к commit, bounded и идемпотентна; исходное списание неизменно;
  unlinked compensation через обычный grant запрещена.
- Legacy проверяется только после verified MAX mapping, без создания аккаунтов/trial.
- Все payment tables, pending/неизвестные/неполные/неденежные состояния.
- Фотосессия 3/5/7 по tariff ID, saved vs delivered, несовместимые completed состояния.
- Song included regeneration/unknown submit, musical/cartoon completion/manual states.
- Animation queue/creating/processing/ready/delivered/manual, материализация bonus counter.
- Single/family Ded Moroz, 13 состояний, claim owner/payment mismatch.
- Синтетическая actual-DDL база через существующие legacy initializers + настоящий
  `promo1111.claim_animation` без внешних вызовов.
- Dry-run receipt duplicates (между legacy и ledger), blocked/manual transfer plan,
  отсутствие сложения balances, отсутствие приватных полей, byte-for-byte read-only.
- Неизвестные schemas/types/malformed timestamp; миграция не меняет product tables.
- Миграции v1→v5, сохранность ранних versions/checksums, downgrade запрещён.
- Import/construction без config, DB I/O, migrations и сетевых обращений.

Из прежних тестов изменены только ожидания версии: конечная версия теперь v5,
проверка последовательности расширена до v5. Старые тестовые случаи не удалены.

## Контрольные суммы миграций

Сравнены с повторно прочитанным registry исходного коммита:

```text
v1 b42298a6271e1efd250cdb735fc1af3cc1da161555db2299b428801fb66c9981
v2 93141c516b6d3da0c536e275643c32c3f211b60d62c92df3b54b47e06f248bc6
v3 aa2c4cd303a9f272f03018703d1f1e8ae5355f1876dbb6fbd69a436fdec375e0
v4 b228de646ac91fc332c09c49e016bd94a40047710f160dab701b78c2aa0d86c1
v5 5fcc25d37990943a894d257b05daa2be022e31394679fb50a71eb26da2d0a521
```

v1–v4 совпадают. DDL этих версий не менялся. В registry добавлена только v5.

## Повторённые 12 падений baseline

```text
tests/test_cartoon_avatar_archive.py::test_restore_keeps_reviewed_words_and_never_generates_picture_or_music
tests/test_custom_orders.py::test_custom_order_menu_position_and_retired_button
tests/test_custom_orders.py::test_unpaid_legacy_long_state_cannot_create_a_new_invoice
tests/test_ded_moroz_family.py::test_family_stats_separate_customer_interest_from_admin_tests
tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[False-590]
tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[False-990]
tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[True-590]
tests/test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout[True-990]
tests/test_photo_payment_refresh.py::test_provider_error_never_returns_the_old_photo_link[590]
tests/test_photo_payment_refresh.py::test_provider_error_never_returns_the_old_photo_link[990]
tests/test_welcome_ads.py::test_exact_main_buttons_and_welcome_copy
tests/test_welcome_ads.py::test_stats_shows_eight_labels_and_clicks
```

Они относятся к прежним avatar restore, меню/историческим checkout, family stats,
photo-link refresh и welcome/stats ожиданиям. Product code и эти падающие tests
в этом PR не исправлялись, ожидания не ослаблялись. Точные node IDs сравниваются
между повторным baseline и окончательным прогоном. Нестабильные aiosqlite worker
warnings `Event loop is closed` также фиксируются, а не скрываются фильтром.

## Diff и границы

Изменяются только новый `entitlements/`, его два тестовых файла, два документа,
registry миграций и два теста с ожиданиями версии. Ни webhook/платежи/тарифы/акции,
ни handlers/очереди/бот/TLS/config, ни frontend/web routes не меняются.
`git diff --check` и Python compileall — без ошибок.

Состав прав, операции, таблицы, dry-run пример, спорные бизнес-правила, стратегия
cutover/rollback и отдельный TLS compatibility plan: [web-entitlements.md](web-entitlements.md).
До owner review остановиться: **не deploy, не merge, не применять реальные миграции**.
