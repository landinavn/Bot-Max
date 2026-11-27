# Security 1.1 — deployment и безопасный откат

**Этот документ — инструкция для будущего согласованного окна. Сейчас ничего не применять.** PR остаётся draft. Production, VPS, рабочая БД, бакеты, MAX, Nginx, Small Talk и frontend не изменялись.

## 1. Проверенные исходные точки

* Установленный Security 1.0: `57744a0b600a9736cbaf1d00a2ccb6660869c903`.
* Ранее подготовленный secure fallback: `196d577693da6257d86debb80c64620edf8e0f9f`.
* Новая ветка `security/private-media-storage` создана непосредственно от первого SHA. Полный итоговый SHA фиксируется в draft PR и сообщении владельцу; перед любыми действиями pin именно этот immutable SHA.
* `main` не является базой Security 1.1: на момент подготовки удалённый main всё ещё указывает на `48d4c5918013069483a2292c5e46e626798f24cb`. Поэтому PR направляется в `security/max-tls-safe-logging`, чтобы diff содержал только 1.1. Это не разрешает merge ни одной ветки.
* `safe_logging.py`, `max_client.py`, production requirements и lockfiles не изменены. Не выполнять pip upgrade/install при будущем обновлении.

## 2. Препятствия включению

Нельзя включать `PRIVATE_MEDIA_ENABLED=true` всем клиентам до выполнения всех пунктов:

1. Создан и независимо проверен новый private bucket; нет публичного ACL/policy, website/CDN/anonymous endpoint.
2. Доказан live synthetic S3→MAX upload для image, MP3, MP4, TXT на установленном VPS SDK. Здесь проверена только модель/SDK mocks.
3. По каждому URL-only AI продукту подтверждены fetch/refetch/expiry либо отдельным решением закрыта его продажа и новые запуски. Сейчас HeyGen/PiAPI/Kling private POST блокируются, но счета бизнес-код по-прежнему может выставлять. Без этого нельзя включать режим клиентам.
4. Проверено восстановление IDs и scoped refs из всех реально используемых orders/FSM; неизвестные submitting-state jobs разобраны без повторного paid POST.
5. Согласованы доступы к registry, local studio_files и резервным копиям. Приватный S3 не исправляет раздачу локального пути веб-сервером.

Наличие draft PR и проходящих новых unit tests не снимает эти препятствия. Не проводить real smoke и не использовать реальные ключи из этой среды.

## 3. Действия владельца в облачной консоли

Отдельно от запуска кода, после разрешения владельца:

1. Создать **другой** bucket. Не менять существующие `STORAGE_BUCKET`, его policy или ACL.
2. Отключить все публичные способы доступа нового bucket в конкретном облаке: anonymous read/list, website, публичный CDN/origin. Не предполагать, что отсутствие object ACL гарантирует отсутствие bucket public policy.
3. Проверить bucket ACL, policy и IAM; Allow только явно перечисленным нужным principal, без `*` и групп. Preflight намеренно отвергает неизвестные формы principal; не обходить отказ выдачей публичных прав.
4. Создать отдельную service identity и отдельные static credentials. Не давать её права `DeleteObject`, `PutObjectAcl`, `PutBucketAcl`, `PutBucketPolicy`, `ListAllMyBuckets`. Не использовать owner/root key.
5. Нужные S3 действия: `GetObject`, `PutObject` только `media/v1/*`; `GetBucketAcl`, `GetBucketPolicy` для read-only preflight. Для различения отсутствующего объекта и отказа HEAD может потребоваться ограниченный `ListBucket` на согласованный prefix: точную IAM модель проверить в используемом облаке. Код листинг объектов не выполняет. Если требует KMS — отдельно проверить нужные действия/ключи, не расширять до admin.
6. На **синтетическом** объекте проверить: авторизованный PUT/HEAD/GET работает; anonymous GET и HEAD без подписи запрещены; signed GET действует до TTL и отказывает после; чужой владелец отказывает до SDK request. Не помещать тестовые URL/credentials в журнал/PR.
7. Зафиксировать результат без secret values и URL. Облачный preflight только читает ACL/policy и не доказывает CDN/public flags/IAM всей организации. Проверка владельца обязательна.

## 4. Проверка релиза вне production

В отдельном checkout проверить SHA, clean diff и отсутствие посторонних коммитов. Для проверенного `<release-sha>`:

```bash
git merge-base 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha>
git diff --stat 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha>
git diff --name-only 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha>
git diff --exit-code 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha> -- safe_logging.py max_client.py requirements.txt
```

Merge-base должен быть точным Security 1.0 SHA; не cherry-pick из financial/frontend веток. Сверить все зависимости с установленными, не менять venv. В production не копировать test harness, junit, mock credentials, synthetic DB и планы инвентаризации из разработки.

Сначала оставить `PRIVATE_MEDIA_ENABLED=false`. Для будущего private режима добавить отдельные значения `PRIVATE_MEDIA_BUCKET`, `PRIVATE_MEDIA_REGISTRY`, `PRIVATE_MEDIA_ACCESS_KEY_ID`, `PRIVATE_MEDIA_SECRET_ACCESS_KEY` через существующий защищённый механизм environment. Не вставлять secrets в shell history, чат, git или service stdout. Старые storage настройки сохранить для legacy readers.

Registry разместить вне web root, в каталоге существующего service user с минимальным доступом; первый файл создаётся 0600. У service user должен быть доступ к этому пути и только необходимым материалам. Не задавать registry равным `database.sqlite3`. Не хранить registry на временном диске или в публичной директории.

## 5. До окна остановки

* Подтвердить текущий VPS SHA, Python и версии **без установки пакетов**. Заявленные владельцем версии: Python 3.12.3, maxapi 1.2.3, aiohttp 3.14.5.
* Проверить pending/queued/running/polling/manual jobs, paid orders, delivery backlog, provider IDs, leases и submitting markers. Только read-only запросы к провайдерам. Не переносить в публичный отчёт IDs/фото/тексты реальных клиентов.
* Обнаружить незавершённые RAM FSM: `MemoryContext` не является долговременным хранилищем всех состояний. Дождаться завершения либо отдельно сохранить согласованные контрольные точки и способ восстановления. Backup SQLite сам по себе не сохраняет RAM. Не считать отсутствие worker task гарантией отсутствия пользовательской сессии.
* Снять read-only baseline количества оплат, недоставленных результатов, активных provider jobs и health polling/webhook, без логирования PII.
* Согласовать короткое окно недоступности и порядок платежей, пришедших в это время. Не менять Nginx/домен/webhook route для эксперимента.

## 6. Консистентная резервная копия

Только после отдельного согласования окна:

1. Дождаться прекращения новых paid submissions и разобрать неопределённые jobs. Если это нельзя гарантировать — отложить update.
2. Штатно остановить **только `botmax.service`**, дождаться фактического `inactive`. Не убивать процесс во время неизвестного POST. Не трогать Small Talk/Nginx/другие unit.
3. Сделать актуальные защищённые копии business SQLite через SQLite backup API, private registry если уже существует, `.env`/service environment, `venv` inventory, `studio_files`, пользовательских материалов, startup/config и exact old SHA. Проверить целостность SQLite copies. Учесть WAL/journal: простое копирование живого `.sqlite3` не гарантирует консистентность.
4. Сохранить permissions/owner. Копии и журнал контроля содержат персональные/секретные данные, хранить вне web root/Git/общего чата. Retention backup согласовать отдельно.
5. Не выполнять business schema migration. Private registry имеет собственную схему и создаётся отдельно только при приватной записи.

## 7. Обновление кода и старт

* Использовать заранее проверенный clean checkout exact release SHA. Не делать `git reset --hard` по неосмотренному рабочему каталогу, не удалять untracked `.env`, `venv`, DB, media или studio_files. Код и данные должны оставаться раздельными.
* Не выполнять `git clean`, pip upgrade, Nginx reload, Node/backend/frontend install, migration сторонней финансовой ветки.
* Для первого reader-compatible запуска оставить private writes off. Запустить только `botmax.service` штатным `systemctl start` после проверки config.
* Проверить systemd active/substate, MAX polling без дубля второго consumer, доступность существующего webhook, отсутствие startup/registry/storage errors. Не выводить реальные API responses и traceback в общий журнал.
* Проверить доставку уже готового legacy результата без новой генерации. Если private refs уже существуют — проверить scoped delivery сохранённого результата тем же владельцем без нового POST.
* Не включать private writes, пока не закрыты все пункты раздела 2. Включение/owner smoke/миграция — отдельные согласования, а не часть этого draft.

## 8. Платежи и доставка после окна

HTTP health/200 не доказывает зачисление каждой оплаты. Сверить provider payment/order identifiers и текущие статусы БД в закрытом операционном контуре. Проверить политику повторной доставки webhook у Prodamus по фактической конфигурации; из кода нельзя гарантировать, что все уведомления будут повторены поставщиком.

Для оплаты в окно недоступности проверить provider status и существующий idempotent webhook/order workflow. Не воспроизводить наугад POST и не начислять вручную второй раз. Если уведомление потеряно — использовать отдельно согласованный способ reconciliation с проверкой подписи/ID/суммы и защитой от дубля.

Убедиться: ранее paid не стали pending; нет повторных списаний/кредитов; существующие task IDs не заменены новыми; готовые файлы доставлены/ожидают повторной доставки, а не повторной генерации. Наличие MAX upload без подтверждённой send_message не считать доставкой. Все ручные действия записать в защищённом журнале без raw media URLs.

## 9. Критерии успеха и основания остановки

Успех reader-compatible update: exact SHA, старые зависимости, один работающий polling consumer, webhook принимает валидные уведомления и reconciliation не показывает пропусков/дублей; legacy и уже сохранённые private результаты читаются только правильными адресатами; нет неожиданных платных POST; все активные IDs сохранены; safe logging/TLS остаются строгими. Private writes off — ожидаемый результат первого этапа, а не ошибка.

Основания остановки/rollback: anonymous доступ, чужой owner может читать, private ref попал в публичную публикацию, S3 policy нельзя подтвердить, credential/URL/PII оказался в логах, SDK TLS failure, неизвестные дубли provider POST/списаний, нарушения доставки/платёжного учёта, несовместимые refs в recovery. Сначала остановить затронутую обработку, не запускать новую генерацию для диагностики.

## 10. Безопасный rollback

**Не делать private объекты публичными и не восстанавливать автоматически старую БД.** Новые оплаты/заказы важнее соответствия старому snapshot.

* Выключить новые private writes. Reader-capable код, private registry и private keys/bucket сохранить. Приватная зависимость/маркер заказа блокирует новые paid/private-derived операции при флаге off, но допускает чтение сохранённого файла.
* После первых private refs возврат к чистому `57744a0` или `196d577` **несовместим**: эти версии не понимают новые ссылки. Нужен заранее проверенный reader backport либо остановка соответствующей обработки. Не считать эти SHA универсальным rollback для нового формата данных.
* При проблеме с safe_logging не возвращать небезопасные логи. При TLS ошибке не возвращать `ssl=False` и не разрешать HTTP. Подготовить отдельную проверенную безопасную версию, сохраняя формат refs/registry.
* Если безопасный reader/fallback не работает — остановить затронутую обработку, сохранить DB/registry/materials/IDs, выполнить reconciliation оплат и устранить причину отдельно. Не сбрасывать submitting markers и не повторять платные POST.
* Старые backup использовать для расследования/точечного согласованного восстановления, а не заменить ими текущую DB. Никакого автоматического отката финансовой истории.

## 11. Локальные проверки разработки

Сравнение делается с неизменённым Security 1.0 SHA в отдельном worktree, теми же Python/test tools и без внешних API. В Cloud Python **3.12.14**, а не заявленный VPS 3.12.3; это ограничение тестирования. Два существующих изолированных venv: maxapi **1.2.3** с aiohttp **3.13.5**, и maxapi **1.2.3** с aiohttp **3.14.5**. Production dependencies не устанавливались/не обновлялись.

Внешний локальный test runner удаляет inherited secrets, оставляет синтетический MAX token и системные CA settings, блокирует non-loopback socket connections. Требуемые частью TLS-тестов локальные HTTPS sockets допускаются. Test timeout 90 сек предотвращает зависание старого теста. Сам runner и junit/raw logs не являются runtime изменениями этого PR.

Результаты полного сравнительного прогона:

| Окружение | Неизменённая база 57744a0, 3356 cases | Security 1.1 checkpoint, 3428 cases | Сравнение |
|---|---|---|---|
| maxapi 1.2.3 / aiohttp 3.13.5 | 3343 passed, 13 failed | 3416 passed, 12 failed | 72 добавленных tests passed, исходные cases не удалены; новых падений нет. Исходный 90-секундный timeout `test_customer_complete_flow_two_audios_choose_first_and_next_invoice` в новой версии не повторился |
| maxapi 1.2.3 / aiohttp 3.14.5 | 3206 passed, 150 failed | 3277 passed, 151 failed | 72 добавленных tests passed, исходные cases не удалены. Дополнительный timeout старого render-теста разобран ниже |

Из 150 исходных падений в окружении 3.14.5 в 121 traceback присутствует несовместимость aioresponses 0.7.9 с `ClientResponse.__init__`: отсутствует обязательный `stream_writer`. Остальные исходные падения включают старые проверки меню/цен/статистики и последствия неработающих HTTP mocks; они не исправлялись этим PR. В 3.13.5 двенадцать устойчивых исходных failures относятся к avatar restore, custom-order menu/flow, family stats, photo-payment expectations и welcome/stats; дополнительный тринадцатый baseline failure — timeout. Полный suite **не зелёный**.

Единственное дополнительное падение checkpoint 3.14.5 — timeout 90 секунд в неизменённом `test_ded_moroz_render::test_render_follows_actual_narration_duration_after_provider_change[2.2-0.7]`. Отдельный повтор после завершения параллельных full suites: **1 passed** на базе и **1 passed** на новой версии. Воспроизводимой новой регрессии не получено; факт timeout не скрыт и лимит ради него не увеличивался.

После полного checkpoint выполнена одна узкая корректировка: photo worker получает разрешённые refs не только из `photo_orders`, но и из сохранённых `photo_results` этого заказа. Добавлен 73-й тест: результат, созданный личной сессией, доставляется worker-ом; чужой результат отвергается до S3 HEAD. Для окончательного кода повторены затронутые проверки:

| Окончательный код | aiohttp 3.13.5 | aiohttp 3.14.5 |
|---|---|---|
| Новые private storage / migration / integration tests | **73 passed** | **73 passed** |
| Вместе с исходными TLS/MAX и safe-logging tests, 143 cases | **143 passed** | **142 passed, 1 failed** — тот же исходный aioresponses/TTAPI `stream_writer` failure |
| Исходные photo/payment/delivery/promo tests (`test_repeat_promos.py`, `test_payment_audit.py`, `-k photo`) | **30 passed**, 52 deselected | Отдельно не повторялись; исходные cases присутствуют в полном сравнительном прогоне |
| Повтор единственного дополнительного render-timeout | Не требовался | **1 passed**; та же проверка на базе — **1 passed** |

Полный suite после этой последней узкой корректировки повторно не запускался: её private-ветка и изменённая доставка проверены перечисленными целевыми тестами; legacy fast path без registry не изменён. Это граница результатов, а не утверждение о live совместимости. Syntax/AST и `git diff --check` прошли. AST-сравнение бизнес SQL, неизменность старых тестов/requirements/TLS/logging и проверка отсутствия data/env/media artifacts выполнены. Зависимости production не менялись; live VPS/S3/MAX/provider checks остаются отдельным этапом.

Матрица провайдеров и ограничения: [private-media-storage.md](private-media-storage.md). План миграции: [private-media-migration.md](private-media-migration.md).

## 12. Состав изменений

Полный список файлов этого этапа (33):

* Основа и конфигурация: `private_media.py`, `private_media_migration.py`, `config.py`, `bot.py`.
* Приём, хранение и доставка: `handlers.py`, `database.py`, `image_service.py`, `media_service.py`, `audio_service.py`, `cartoon_avatar_archive.py`, `mother_recovery.py`.
* Восстановление workers: `audio_orders.py`, `musical_orders.py`, `photo_orders.py`, `animation_orders.py`, `ded_moroz.py`.
* Provider guards: `heygen.py`, `video_service.py`, `mother_story.py`, `song_alignment.py`, `elevenlabs_tts.py`.
* Мультстудия: `multstudio/adapters.py`, `multstudio/assets.py`, `multstudio/heygen_video.py`, `multstudio/music.py`, `multstudio/uploads.py`, `multstudio/worker.py`.
* Тесты: `tests/test_private_media.py`, `tests/test_private_media_integration.py`, `tests/test_private_media_migration.py`.
* Документация: `docs/private-media-storage.md`, `docs/private-media-migration.md`, `docs/private-media-deployment-runbook.md`.

Изменения `database.py` ограничены проверкой формата/владельца медиа и запретом приватного share. AST-сравнение SQL string literals с базой подтверждает отсутствие изменения прежних SQL операторов бизнес-БД. Ни один исходный тестовый файл не изменён и не удалён. В PR нет пользовательских материалов, test DB, registry, environment, credentials, venv, junit или raw logs.
