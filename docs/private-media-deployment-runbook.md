# Security 1.1.2 — staged deployment, Yandex setup, smoke и reader rollback

**Подготовка, не выполнение. Не запускать эти действия на VPS без отдельного согласования.** Production остаётся на `57744a0b600a9736cbaf1d00a2ccb6660869c903`. Разработка продолжается от `e6ef3dce87e0f67e2c7fb280f5e9901fabaccd4b` в Draft [PR №266](https://github.com/landinavn/Bot-Max/pull/266). Точный итоговый SHA, полный список файлов и результаты сравнительных запусков — в описании PR, привязанные к immutable commit. Merge/deployment не выполнялись.

## 1. Допуск и критерии остановки

До пилота нужны: новый private bucket; отдельно проверенные Yandex folder/runtime/metadata identities; минимальные IAM и policy; успешный synthetic owner smoke; ограничение одним тестовым admin owner; готовый emergency reader и backup. Private animation/cartoon/ded_moroz/multstudio остаются **BLOCKED**. Их legacy при private off не отключается. Таблица всех десяти продуктов и официальные API — [архитектура](private-media-storage.md).

Остановить пилот при anonymous read, чужом owner доступе, неизвестном principal/правиле, registry error, private→legacy попытке, утечке URL/PII/секрета в журнал, новом paid POST вместо recovery, пропуске/дублировании оплаты или неприемлемом RSS/latency. Не снимать TLS/проверки ACL ради продолжения. Не повышать роль до storage.admin или folder editor.

## 2. Точные исходники и зависимости

Перед будущим окном проверить:

```bash
git rev-parse HEAD
git merge-base 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha>
git diff --name-status 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha>
git diff --exit-code 57744a0b600a9736cbaf1d00a2ccb6660869c903 <release-sha> -- safe_logging.py max_client.py requirements.txt
```

Никаких financial/frontend веток, установки Node, backend migrations или pip upgrades. `safe_logging.py`, `max_client.py` и production requirements не менялись. Заявленные владельцем версии VPS: Python 3.12.3, maxapi 1.2.3, aiohttp 3.14.5. В Cloud проверяется Python 3.12.14 и два существующих venv (aiohttp 3.13.5/3.14.5); patch-version VPS здесь не запускалась.

Новые слои: media_policy.py, media_registry.py, private_media_emergency.py, private_media.py; технические guards в database/prodamus/promo1111/Ded Moroz/мультстудии; потоковые readers и проверки output refs в существующих service/handler путях. Бизнес SQL, цены и HMAC webhook не переписаны. Новые metadata tables находятся только в отдельном media registry.

## 3. Создание отдельного закрытого бакета Yandex

Все пункты выполняет владелец **позже**, только для нового бакета; действующий STORAGE_BUCKET не менять.

1. В Yandex Cloud Console выбрать нужные cloud/folder → Object Storage → создать bucket с новым уникальным техническим именем. Не включать имена клиентов в bucket/key. Выбрать согласованные storage class/region, соответствующие будущей конфигурации SDK.
2. Оставить чтение объектов, листинг объектов и просмотр конфигурации **непубличными**. Не включать website, CDN/origin, anonymous read или allAuthenticatedUsers. Не публиковать test objects через веб-сайт.
3. Создать отдельный service account для runtime, без cloud/folder широких storage ролей. Отдельно оставить owner/operator identity для создания/настройки: её административные права не переносить боту.
4. В bucket IAM access добавить runtime service account: **storage.uploader + storage.configViewer на этот bucket**. Первая роль не удаляет объекты/не настраивает bucket; вторая нужна для просмотра policy. Не назначать эти роли на весь folder вместо bucket.
5. Применить bucket policy ниже с точными идентификаторами и bucket name. Это template для нового bucket, не команда изменения старого. Не добавлять wildcard console access rule: приложение намеренно отклоняет неоднозначный Allow principal даже с условием.
6. Создать static access key этого runtime account. Секрет не помещать в чат, git или shell history; использовать защищённый environment-файл service user. Legacy ключи оставить отдельно для legacy readers.
7. Для Yandex отдельно подтвердить folder ID и runtime/metadata subject IDs через административный контур. Owner.ID GetBucketAcl может быть пустым либо относиться к каталогу; не добавлять его автоматически в principal allowlist. Применять новый профиль yandex-v1 и административную процедуру из [Security 1.1.2](private-media-yandex-smoke.md).
8. Проверить доступность bucket/prefix и отрицательные случаи из раздела smoke. Если GetBucketPolicy/HEAD/PUT не проходит — остановиться; не выдавать admin и не отключать preflight.

Официальные источники (проверка 28.11.2026): [создание bucket](https://yandex.cloud/en/docs/storage/operations/buckets/create), [bucket IAM bindings](https://yandex.cloud/en/docs/storage/operations/buckets/iam-access), [IAM/ACL/policy взаимодействие](https://yandex.cloud/en/docs/storage/security/overview), [policy principal/resource](https://yandex.cloud/en/docs/storage/concepts/policy), [просмотр policy — minimum storage.configViewer](https://yandex.cloud/en/docs/storage/operations/buckets/policy), [S3 actions](https://yandex.cloud/en/docs/storage/s3/api-ref/policy/actions).

IAM роли назначаются на bucket, **не prefix**. Prefix для данных ограничивает bucket policy. S3 GetBucketPolicy документирован как отдельный метод, но отсутствует в опубликованной таблице policy actions: здесь не придуман `s3:GetBucketPolicy` Allow; право просмотра обеспечивается bucket IAM storage.configViewer и проверяется живым GET в smoke.

## 4. Минимальный policy template

Заменить все `<...>` до использования. Runtime создаёт только media/v1; объектные данные вне prefix и Delete/PutAcl/PutBucketPolicy не разрешены этим policy. CanonicalUser — формат Yandex, а не AWS ARN. Owner.ID проверяется отдельно от allowlist субъектов: Yandex возвращает folder ID либо пустую строку. Этот template предназначен для yandex-v1, который требует четыре правила, а не для прежнего strict.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RuntimeObjectIO",
      "Effect": "Allow",
      "Principal": {"CanonicalUser": "<runtime-service-account-id>"},
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<new-private-bucket>/media/v1/*",
      "Condition": {"Bool": {"aws:securetransport": "true"}}
    },
    {
      "Sid": "ReadBucketMetadata",
      "Effect": "Allow",
      "Principal": {"CanonicalUser": ["<runtime-service-account-id>", "<approved-metadata-subject-id>"]},
      "Action": ["s3:GetBucketAcl", "s3:ListBucket"],
      "Resource": "arn:aws:s3:::<new-private-bucket>",
      "Condition": {"Bool": {"aws:securetransport": "true"}}
    },
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "*",
      "Resource": ["arn:aws:s3:::<new-private-bucket>", "arn:aws:s3:::<new-private-bucket>/*"],
      "Condition": {"Bool": {"aws:securetransport": "false"}}
    },
    {
      "Sid": "DenyLegacyStorageAccess",
      "Effect": "Deny",
      "Principal": {"CanonicalUser": ["<legacy-account-a>", "<legacy-account-b>"]},
      "Action": "*",
      "Resource": ["arn:aws:s3:::<new-private-bucket>", "arn:aws:s3:::<new-private-bucket>/*"]
    }
  ]
}
```

ListBucket на выделенном bucket нужен для проверки отсутствующего ключа/отличения 404 от запрета HEAD в S3-совместимом поведении. Код bucket listing не выполняет. Он раскрывает service account имена ключей этого выделенного бакета; данные всё равно ограничены media/v1. Профиль yandex-v1 намеренно не принимает дополнительные StringLike условия: отдельная list/prefix схема требует нового review. Проверить HEAD отсутствующего key и отрицательный synthetic PUT вне prefix; неожиданный 403/разрешённый PUT — STOP. Не отправлять PUT вслепую. Не выдавать полный доступ ради устранения 403.

Этот template — проверяемая минимальная исходная конфигурация, не утверждение об уже проверенном реальном IAM. KMS, VPC, CDN, account-level IAM и новые особенности облака здесь не настраивались. Если есть KMS, отдельно согласовать необходимые key permissions и повторить smoke.

## 5. Реальный synthetic smoke для владельца

Для Yandex сначала выполнить [owner-only S3 smoke 1.1.2](private-media-yandex-smoke.md), сохраняя private feature flags off. Он заменяет первичную проверку S3 из старой процедуры ниже; последующая проверка MAX/reader выполняется отдельно в изолированном административном контуре.

Проводить вне production потока, с отдельным тестовым MAX admin аккаунтом и **только искусственными** PNG/JPEG, коротким сгенерированным локально WAV/MP3, MP4 тестовой сеткой и TXT. Ни фотографий людей/детей, ни платёжных данных. Эта задача smoke не выполняла.

### A. S3 и авторизация — без платных AI API

1. На проверенном release SHA в изолированном окружении владельца передать dedicated credentials безопасным способом. Проверить SDK preflight: Owner.ID пустой либо равен подтверждённому folder ID, CanonicalUser входят в отдельный allowlist, а все четыре правила точно соответствуют yandex-v1. Непонятное правило = остановка.
2. Через PrivateMedia выполнить PUT под synthetic owner и стабильной operation ID. Сохранить только технический результат/SHA в закрытом журнале; не публиковать credentials, URL и owner mapping.
3. SDK HEAD/GET проверяют bytes/hash. Тот же operation ID повторно возвращает ref без нового PUT. Искусственно оборванный/неизвестный PUT не должен создать второй PUT без подтверждённого HEAD.
4. Anonymous GET и HEAD без подписи должны отказать. Проверить также все CDN/website endpoints, если они когда-либо настроены: SDK ACL check сам по себе их не доказывает.
5. Выдать signed GET непосредственно перед проверкой с коротким TTL, получить файл, после expiry получить отказ. Чужой owner/account namespace должен отказать **до** SDK network call. Подписанный URL не копировать в общий журнал.
6. Попытки runtime вне media/v1, DeleteObject, PutObjectAcl/PutBucketPolicy должны отказать. Реальный bucket policy не изменять ради negative test: достаточно безопасного запроса к несуществующему тестовому ресурсу/операции без изменения политики; destructive проверки выполнять только по отдельному решению.
7. Проверить права registry, journal/WAL/SHM и `.required` — service UID, 0600; каталог вне web root. На копии проверить corrupt/unknown/missing registry: отказ, отсутствие нового счёта/PUT.

### B. MAX и память — без платных генераций

Из authenticated тестового MAX диалога отправить сохранённые synthetic image/audio/video/TXT через private reader. Проверить получателя, media type и bytes, неизвестного owner и нажатие из группы (файл приходит лично либо отказ). Проверить file path больших файлов, временные permissions/cleanup и одновременные доставки. Начать с малого файла, затем не превышать лимиты приложения 8/16/32/8 MiB.

На VPS с 1 GiB/1 vCPU контролировать RSS всего botmax, native ffmpeg/Pillow процессы, swap/OOM, очереди и latency. Синтетическое измерение Python 32,4 MiB не включает эти процессы. При неприемлемом потреблении снизить нагрузку/остановить пилот, не повышать автоматически лимит размера. Установленный SDK file upload буферизует целый файл, несмотря на streaming S3 reader.

### C. Провайдеры — сначала evidence, потом отдельный допуск

Без платной генерации можно отдельно проверить documented upload/asset APIs на искусственном файле, **если владелец это разрешил и подтвердил, что сам upload не тарифицируется**. HeyGen asset имеет публичный URL и отдельный lifecycle: это требует отдельного решения о передаче, не становится автоматически private из-за asset_id.

Перед каким-либо paid smoke для HeyGen/Kling владелец отдельно подтверждает бюджет и тип задачи; сейчас он **не разрешён**. Требуемый план:

* Зафиксировать exact endpoint/version, входные asset IDs/URLs, максимальные ограничения файла и серверные request IDs без raw API body в обычных логах.
* Записывать фактические первое/повторные GET/HEAD по signed URL в защищённый тестовый контур. Проверить очередь, позднее получение, retries/refetch, URL expiry до fetch и во время выполнения, 403/404 и malformed MIME.
* Получить от провайдера документированную верхнюю границу queue+fetch+refetch либо изменить интеграцию на подтверждённый asset ID путь с согласованной privacy/lifecycle. **Один успешный smoke не доказывает максимальное окно.**
* При ошибке сохранить task/asset/request ID, опрашивать существующий task GET. Новый paid POST автоматически не запускать. Не подменять Kling version 3.0 на 4.0 или model endpoint без отдельного решения о поведении/цене.

Пока evidence не получен, четыре private продукта остаются BLOCKED независимо от успешно загруженного test file.

## 6. Настройки staged pilot

Пример структуры environment, не готовые секреты и не действие с VPS:

```dotenv
PRIVATE_MEDIA_ENABLED=false
PRIVATE_MEDIA_PRODUCTS=postcards
PRIVATE_MEDIA_PILOT_USER_ID=<test-admin-max-id>
PRIVATE_MEDIA_STORAGE_VERIFIED=false
PRIVATE_MEDIA_READ_ONLY=false
PRIVATE_MEDIA_BUCKET=<new-private-bucket>
PRIVATE_MEDIA_REGISTRY=/secure/botmax/private_media.sqlite3
PRIVATE_MEDIA_PRINCIPALS=<approved-metadata-subject-id>,<runtime-service-account-id>
PRIVATE_MEDIA_ACCESS_KEY_ID=<separate-static-key-id>
PRIVATE_MEDIA_SECRET_ACCESS_KEY=<separate-secret>
```

Все новые поля по умолчанию off/empty. Pilot ID обязан совпадать с настроенным ADMIN_USER_ID этого изолированного экземпляра. Сначала завершить A/B smoke, затем записать его результат в защищённый operator record и явно поставить STORAGE_VERIFIED=true. Это attestation, не автоматическая гарантия. Новые private счета без него блокируются; существующие retained files могут читаться.

Сначала postcards одному synthetic admin. Затем отдельно vision_board/photosession/song/musical_card/custom_order, каждый с собственной проверкой полной цепочки и обычного legacy пользователя. Не включать BLOCKED продукты и не переносить pilot на всех клиентов одним global=true. Изменение списка влияет только на новые операции. Для остановки начатых private mutations нужен READ_ONLY=true.

## 7. Future update window и оплаченные заказы

До любого изменения: pin exact SHA; сравнить зависимости без установки; read-only inventory активных paid/pending/queued/polling/manual jobs, submitting/IDs и delivery backlog; проверить RAM-only FSM. Если неизвестен исход paid POST или нельзя сохранить сессию — отложить окно. Не считать backup SQLite сохранением RAM.

После отдельного согласования остановить **только botmax.service**, дождаться inactive и консистентно снять SQLite backup API копии business DB + registry/sidecars/anchor, environment/config, venv inventory, studio_files и пользовательских материалов. Копии вне web root/Git, с ограниченными правами. Не использовать git clean/reset по неосмотренному каталогу, не удалять untracked данные. Код и данные сохранять раздельно.

Первый запуск reader-compatible SHA — с PRODUCTS пусто/global=false; private credentials/allowlist/registry оставить, если refs уже существуют. Не менять Nginx, основной домен, Small Talk или webhook route. Не запускать миграции другого проекта. Запустить только botmax.service, проверить один polling consumer, webhook health, доставку сохранённых legacy/private результатов без новых генераций.

Оплаты, пришедшие во время остановки: сверить provider/order/payment IDs и суммы через закрытый операционный контур. Не считать HTTP 200 или ожидание provider retries доказательством учёта всех платежей. Не воспроизводить наугад webhook POST и не начислять вручную второй раз. Reconciliation — по существующему idempotent механизму с проверкой подписи/привязок, после отдельного решения для пропущенного уведомления.

Проверить: paid не стали pending/cancelled; нет повторных счетов/списаний, IDs не заменены, выдача использует retained object. Новая technical availability ошибка сообщает о поддержке и сохраняет историю; она не является автоматическим возвратом или аннулированием заказа.

## 8. Проверяемый reader-compatible rollback

После private refs **не checkout 57744a0, 196d577 или старый e6ef3dc как универсальный fallback**: первые два не понимают формат, а Security 1.1 не содержит всех новых auth/registry ограничений.

Проверенный reader baseline — immutable SHA этого PR в режиме:

```dotenv
PRIVATE_MEDIA_ENABLED=false
PRIVATE_MEDIA_PRODUCTS=
PRIVATE_MEDIA_READ_ONLY=true
```

Сохранить bucket/keys/allowlist, текущие business DB, registry/sidecars/anchor и local materials. [private_media_emergency.deliver_saved](../private_media_emergency.py) вызывается из существующего private download callback при read-only. Он читает только retained ref, обновляет пользовательские paid assertions и отправляет лично owner либо readonly admin; service/caller без authenticated scope отвергается. Нет генерации, payment, storage PUT, grant, signer или публичного URL. Это аварийная функция текущего приложения, не новый постоянный процесс/backend.

Синтетические тесты проверяют: старые файлы доставляются; новый PUT/paid generation запрещены; чужой owner не получает upload; service scope не выдаётся за пользователя. Для будущих версий можно откатывать **код** к этому exact reader-compatible SHA после schema/refs comparison. Сейчас аварийное переключение — остановка mutations на том же проверенном коде, а не обещание исправить произвольную поломку этого reader-а сменой environment.

Если reader небезопасен или не работает — остановить затронутую обработку, сохранить metadata/материалы/IDs, устранить отдельно. Не делать bucket public, не возвращать ssl=False/HTTP/сырые логи. Не восстанавливать старую бизнес-БД автоматически: потеряются новые оплаты. Потерянный registry восстанавливать только из согласованной проверенной копии/точечного mapping, не удалять `.required` для обхода ошибки.

## 9. Тесты и границы подтверждения

Исходные 73 теста PR №266 сохранены (fixture дополнен явным synthetic allowlist и synthetic storage verification). Новые rollout tests покрывают десять независимых продуктов, restart/policy immutability, admin pilot, invoice/credit/regeneration guards, legacy paid continuation, точные principals, cross-order song reuse/denial, повреждение registry, groups при flag off, no-network-in-write-transactions, MIME limits, SDK buffering/concurrency, emergency reader и полный synthetic musical flow.

Финальные full comparison запускаются на **окончательном SHA** и публикуются в PR без последующего изменения исходников. Окружения: Python 3.12.14/maxapi 1.2.3 + aiohttp 3.13.5 либо 3.14.5; production requirements не изменяются. Runner убирает inherited credentials, использует synthetic token, сохраняет публичные CA settings и запрещает внешний network. Рабочая БД не подключается.

Предыдущий запуск среды запрещал даже localhost sockets; его PermissionError не доказывают работоспособность TLS/webhook и не входят в окончательное сравнение. После восстановления сети localhost доступен: baseline и окончательный код проверяются заново одним изолированным runner, внешние API остаются запрещены. aioresponses 0.7.9/ClientResponse stream_writer несовместимость в 3.14.5 учитывается отдельно и сверяется с исходным кодом без изменения зависимостей. Полный suite не объявлять зелёным при наличии падений; owner live smoke остаётся отдельным условием допуска.


Воспроизводимый состав проверки (два существующих venv, без установки пакетов):

* Private: `tests/test_private_media.py tests/test_private_media_integration.py tests/test_private_media_migration.py tests/test_private_media_rollout.py` — все исходные 73 случая плюс новые проверки.
* Регрессии: payment audit, repeat promos, HeyGen/ElevenLabs TTS, MAX TLS и безопасное логирование.
* Полный `pytest -q --tb=short` на production baseline 57744a0 и окончательном SHA в обоих venv; каждый test ID сравнивается по JUnit, включая setup errors, failures и отсутствующие случаи.
* Для 14 payment audit `stream_writer` failures дополнительно повторяется тот же файл на опубликованном e6ef3dc. Не исправлять production dependencies ради зелёного отчёта.
* Финальные значения passed/failed/errors, точные SHA, версии и причины расхождений публикуются в PR. Сырые логи/JUnit с синтетическими данными остаются вне репозитория.
* `git diff --check`, Python syntax, неизменность financial SQL literals и production dependency/TLS/logging файлов, отсутствие DB/media/env артефактов в commit.


## 10. Дополнение Security 1.1.2

Перед использованием Yandex выполнить [отдельную инструкцию](private-media-yandex-smoke.md): operator-only flags/folder/IAM review → yandex-v1 read-only preflight → explicit synthetic PUT/HEAD/GET, anonymous GET/HEAD, expiry и negative IAM probes. Скрипт подготовлен, **здесь не запускался против облака**. Не менять runtime роли и не включать features для запуска smoke. Новые controls/profile поля не являются общей production готовностью.

В yandex-v1 required: точно два approved subject IDs (включая runtime), два других legacy subject IDs, отдельно folder ID и подтверждённый административный review. По умолчанию profile=strict, controls=false, все private flags off. Старые synthetic strict fixtures остаются без ослабления.

Для reader rollback Yandex сохранить текущий проверенный 1.1.2 SHA и все profile/controls/identity настройки при READ_ONLY=true и отключённых writes. f5dd8de/57744a0/196d577 не являются совместимым Yandex reader fallback. Business DB не откатывать.

Полный сравнительный прогон 1.1.2 выполняется относительно f5dd8de в обоих существующих venv; точные SHA/счётчики/классификация в PR. Предыдущие 136 private-media test ID сохраняются и сверяются отдельно; новые tests проверяют фактическую форму пустого ACL, четыре Deny/Allow контракта, TLS aliases/duplicates, operator gates и owner smoke без cloud/API calls. Dependencies и тарифы не изменяются.
