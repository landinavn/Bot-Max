# Security 1.1.1 — private media, provider compatibility и staged rollout

Дата проверки документации: **28 ноября 2026, Москва**. Продолжение PR №266, ветка `security/private-media-storage`, исходный SHA `e6ef3dce87e0f67e2c7fb280f5e9901fabaccd4b`. Установленная база Security 1.0: `57744a0b600a9736cbaf1d00a2ccb6660869c903`. Точный проверенный итоговый SHA и результаты полного сравнения публикуются в описании [PR №266](https://github.com/landinavn/Bot-Max/pull/266).

**Не deployment и не разрешение на общий rollout.** Рабочие API, VPS, данные пользователей и реальные бакеты не использовались. Все режимы выключены по умолчанию. Legacy временно остаётся работоспособным; это не завершённая защита исторических персональных данных.

## Что изменено относительно Security 1.1

* Десять продуктов выбираются независимо; решение `private/legacy` сохраняется в media registry, а указатель операции и режим — в FSM. Изменение флагов не переписывает начатые операции.
* Проверки доступности добавлены до новых счетов, списания кредитов/бонусов и прав повторной генерации. Уже оплаченные legacy-заказы сохраняют legacy, а не переоцениваются по новым флагам.
* Runtime principal должен точно совпадать с утверждённым allowlist. Неизвестные principal/Allow rules, wildcard, AWS principal вместо Yandex CanonicalUser и неподтверждённые условия отвергаются.
* Ограниченная песня требует права исходного заказа. Ссылка из другого оплаченного заказа сама по себе не даёт это право. Recipient grants для таких песен запрещены.
* S3 HEAD/GET/PUT вынесены из SQLite write transactions. PUT имеет атомарный claim и сохраняет неопределённый исход без слепого повтора.
* Поток S3 читается по 64 KiB. Крупная доставка использует временный файл 0600 и file-based путь MAX SDK; одновременно одна доставка. Размеры ограничены по типу.
* Реализован отдельный аварийный reader без генераций, оплаты, публикации и signer. Потерянный registry нельзя автоматически пересоздать после появления `.required` marker.

## Модель хранения и доступа

[private_media.py](../private_media.py) хранит объект в отдельном бакете под случайным `media/v1/<uuid>.<ext>`, без public-read ACL. Бизнес-поля могут содержать стабильный `private-media:v1:<uuid>`; это **не URL и не право доступа**. Object key, owner/order, SHA-256, MIME, размер и state находятся в отдельном локальном SQLite. Подписанные URL туда не записываются.

`Owner('max', id)` и `Owner('account', id)` — разные пространства. Связывания веб-аккаунтов здесь нет. Пользовательская личность берётся из authenticated MAX SDK `event.get_ids()`. Приложение не десериализует Access/roles/entitlements из callback или JSON пользователя. `Access` — внутренний Python API доверенного сервера, не защита от выполнения произвольного кода на VPS.

Service scope восстанавливается по владельцу сохранённого заказа либо JOIN задания с проектом. Service references берутся из сохранённых строк/snapshot и `photo_results` именно данного заказа. Чужой owner запрещён независимо от списка refs. Для song `owner_access=order` нужны:

* пользователю — свежая paid/free assertion исходного заказа из существующей бизнес-БД;
* сервису того же заказа — его конкретный scope;
* другому сервисному заказу — **и** сохранённый ref, **и** свежая assertion исходного оплаченного/free заказа.

Таким образом новая оплата музыкальной открытки не открывает неоплаченную исходную песню. Legitimate reuse ранее оплаченной песни остаётся возможным. Grant не обходят это правило: для restricted song они вообще не выдаются. Admin readonly export допускает только настроенный `admin_user_id` и причины support/recovery/export. Admin не создаёт owner grants от имени клиента.

MAX middleware работает и при выключенных новых записях. В группах выбранный private-сценарий не запускается; private upload требует явного персонального адресата. Callback скачивания и аварийный reader отправляют файл в личный диалог владельцу, admin export — администратору. Legacy-события в группах при флаге off не отключаются целиком.

## Независимые продукты и сохранённая политика

[media_policy.py](../media_policy.py): `PRIVATE_MEDIA_PRODUCTS` — список через запятую из таблицы ниже. Пустой список — все off. Старый `PRIVATE_MEDIA_ENABLED=true` оставлен как совместимый глобальный переключатель, но для staged rollout **не использовать**.

`media_policies` в registry связывает `(owner_namespace, owner_id, operation_id)` с продуктом/режимом неизменно. Долговременный order/job имеет собственную запись. FSM хранит `media_operation_id`, `media_product`, `media_storage`; эти поля не являются доказательством доступа, policy повторно проверяется по registry и доверенному owner. Вложенные операции наследуют root policy: музыкальная открытка не меняет режим в середине генерации песни/изображения.

Для pre-existing заказа без policy применяется legacy. Исключение — сохранённые private refs/предыдущий private marker: они требуют private; смешивание с legacy запрещено. Задание Деда Мороза наследует invoice policy; мультстудия фиксирует policy при новой оценке/подтверждении, а worker восстанавливает её по `studio:<job_id>`. Read-only recovery не создаёт повторный платный POST.

| Продукт | Private readiness в этом PR | Legacy при private off |
|---|---|---|
| `postcards` | Условный synthetic pilot: файл OpenAI → private S3 → MAX | Сохранён |
| `vision_board` | Условный synthetic pilot, тот же файловый путь | Сохранён |
| `photosession` | Условный pilot: source/result refs, delivery по photo_results | Сохранён |
| `song` | Условный pilot: text TTAPI, архив MP3, paid-origin gate | Сохранён |
| `musical_card` | Условный pilot: scoped image/audio → локальный ffmpeg → private MP4; проверен synthetic paid flow | Сохранён |
| `custom_order` | Условный pilot: private приём фото и readonly admin export; ручная услуга не превращается в автоматическую генерацию | Сохранён |
| `animation` | **BLOCKED**: PiAPI/Kling fetch/refetch contract не подтверждён | Сохранён |
| `cartoon` | **BLOCKED**: HeyGen URL path/asset publication и provider smoke не допущены | Сохранён |
| `ded_moroz` | **BLOCKED** как полный продукт: TTS сам по себе не доказывает готовность Avatar/Video | Сохранён, включая старые оплаченные заказы |
| `multstudio` | **BLOCKED** как полный продукт: несколько видео-провайдеров; отдельный image/TTS успех не даёт общего допуска | Сохранён |

«Условный pilot» не означает production readiness: нужны закрытый бакет, IAM/allowlist, owner smoke и `PRIVATE_MEDIA_STORAGE_VERIFIED=true`. Значение false по умолчанию блокирует новые private счета/генерации. Это подтверждение оператора, не криптографическое доказательство будущей доступности облака. `PRIVATE_MEDIA_PILOT_USER_ID` должен совпадать с настроенным admin ID; только этот MAX owner получает новый private режим, остальные — legacy. Пилот проводить на отдельном тестовом административном аккаунте и синтетических материалах.

## Матрица официальной документации провайдеров

Только primary sources, проверены 28.11.2026. Никаких реальных API-запросов, генераций или загрузок этим исследованием не выполнено. Отсутствие численного окна в проверенных страницах означает «не установлено», а не «не существует нигде».

| Интеграция / фактические endpoints | Direct file / provider ID | Signed HTTPS и timing | Решение |
|---|---|---|---|
| HeyGen Avatar: `POST /v3/avatars`, look/group GET | [Assets overview](https://developers.heygen.com/docs/assets) и [Create Avatar](https://developers.heygen.com/docs/reference/create-avatar) подтверждают asset input. [Upload](https://developers.heygen.com/docs/upload-assets): multipart `POST /v3/assets`, возвращает asset_id **и публичный URL**. Image limit simple upload 32 MB | Signed input разрешён на условиях URL input; численная верхняя граница queue/fetch/refetch не установлена | Предпочтительный будущий путь — file→asset_id с сохранением ID и контролем неизвестного исхода upload. Сейчас не подключён: публичная provider-copy и жизненный цикл требуют отдельного допуска/smoke. Avatar-creation может быть платной; не использовать для бесплатной проверки |
| HeyGen Video: `POST /v3/videos`, `GET /v3/videos/{id}`; image/silent/scene variants в текущем коде | [Audio to Video](https://developers.heygen.com/docs/audio-to-video) подтверждает `audio_asset_id`, а `image` принимает asset_id. [Create Video](https://developers.heygen.com/docs/reference/create-video) — точная v3 reference. Существующие URL payload не заменялись на новые model endpoints | [URL requirements](https://developers.heygen.com/docs/assets): HTTPS, прямой файл, без extra headers/redirects, действителен до fetch. Максимальное время fetch/refetch не указано | Private mutations остаются заблокированы. GET существующего video ID допустим; истёкший output URL получать повторным GET, не новой генерацией |
| HeyGen TTS: `POST /v3/models/audio/tts` | [TTS guide](https://developers.heygen.com/docs/voices/heygen-voice-speech): текст/voice/model, синхронный audio_url; есть отдельный stream endpoint. Текущий код использует completed MP3 path, stream API не вводился | S3 input URL не нужен. SLA получения/срок output URL не установлен здесь | Локальный MP3/manifest сохраняются до публикации; неизвестный paid POST не повторяется. Сам TTS не разблокирует полный ded_moroz |
| PiAPI/Kling I2V: `POST /api/v1/task`, task_type video_generation, version 3.0; GET того же task | [Create Task](https://piapi.ai/docs/kling-api/create-task) подтверждает image_url. Не переносить свойства Kling 4.0/omni на используемую 3.0. [File upload](https://piapi.ai/docs/tools/file-upload): JSON base64 до 10 MB, provider возвращает URL, не asset ID | Ephemeral upload удаляется через 24 часа; это **срок хранения**, не maximum queue/fetch/refetch. Численная гарантия получения входного URL не установлена | **BLOCKED private**. Не увеличивать presign TTL до произвольных суток и не считать единичный успех гарантией |
| Kling с аудио: task_type avatar, image_url + local_dubbing_url; также отдельный lip_sync с origin_task_id | [Avatar API](https://piapi.ai/docs/kling-api/kling-avatar-api) — фактическая схема. [Lipsync](https://piapi.ai/docs/kling-api/create-task) — другая операция; native enable_audio не означает приём произвольной песни | Direct provider asset ID для этого пути не подтверждён; максимальное refetch окно не установлено. HYA/PAYG service_mode private/public — способ обслуживания, не ACL наших файлов | **BLOCKED private**, оба входа защищены до POST; existing GET polling сохранён |
| TTAPI/Suno: `POST /suno/v1/music`, `GET /suno/v2/fetch` | [Generate](https://docs.ttapi.io/api/en/suno/suno_music) подтверждает custom lyrics/tags/title и chirp-v6; [Fetch V2](https://docs.ttapi.io/api/en/suno/suno_fetch_v2) возвращает musics/audioUrl. Файл на вход этого endpoint не требуется | Input S3 TTL не нужен. Срок доступности output audioUrl не установлен по этим страницам | Supported song pilot: немедленно архивировать MP3, сохранить job ID. Download/expiry error — повторный fetch существующего job, не новый music POST. Upload/cover APIs не используются и не подменяют этот контракт |

[HeyGen asset lifetime](https://developers.heygen.com/docs/assets): ID повторно используется до удаления, а исходные assets не удаляются автоматически вместе с готовым avatar/video. Это не гарантия приватности URL или согласие на бессрочное хранение. 24 часа direct-upload URL относятся к входящему PUT в HeyGen, не к нашей signed GET ссылке. Не смешивать эти сроки.

Другие текущие пути: OpenAI Images получает multipart bytes, vision — data URI из авторизованных bytes, Whisper — локальный audio sample. Yandex SpeechKit и имеющийся ElevenLabs adapter принимают текст и возвращают audio bytes. Они не требуют signed S3 input. Отдельного работающего MiniMax клиента в этом checkout не обнаружено; упоминание сервиса/модели у посредника не доказывает прямой вызов MiniMax.

## Где находятся медиа и кто читает

| Категория | Source/owner/хранение | Получатели и восстановление |
|---|---|---|
| Принятые фото пользователя/семьи | MAX payload → owned source ref; FSM хранит ref. Не все незапрошенные вложения архивируются | OpenAI по сценарию, MAX при доставке, scoped support. MAX сохраняет свою копию независимо |
| Фото детей | Владелец verified Ded Moroz order; refs и локальный нормализованный PNG | Локальный renderer, owner/admin export; полный private продукт пока закрыт |
| Открытки/карты желаний | image_service: private media/v1 вместо legacy vision-boards | Оригинал и preview — разные immutable objects; чтение SDK с owner check |
| Фотосессии | Source в order/FSM; photo_results хранит private ref | Worker получает именно результаты заказа; сохранённый кадр доставляется без новой генерации |
| Песни/preview | Full tracks restricted-to-order; preview — отдельный объект | TTAPI text, MAX MP3; job ID и tracks сохраняются, права исходного заказа проверяются заново |
| Музыкальные подарки | Image/audio refs, локальный ffmpeg, retained MP4 | Root musical policy наследуется по всем стадиям, доставка не публикует S3 URL |
| Мультфильмы/студия/Дед Мороз | Existing FSM/job IDs, Assets local paths/SHA; private outputs поддерживаются reader-ом | Новые неподтверждённые private POST блокируются; legacy paid jobs не пересоздаются из-за смены флагов |
| Custom/support | Owned uploads, readonly admin export | Персональный MAX диалог, без автоматического consent/public share |
| shared_images | Старые tokens/URL сохраняются | Private/signed refs не регистрируются; исторический public доступ этим PR не отзывается |
| Local/temp/backup | TempDirectory, studio_files, registry/journal/anchor | Новые private temp files 0600/dirs 0700; crash cleanup, OS copies и backup retention не гарантируются |

Основные файлы: [media_policy.py](../media_policy.py), [media_registry.py](../media_registry.py), [private_media.py](../private_media.py), [private_media_emergency.py](../private_media_emergency.py), [handlers.py](../handlers.py), [database.py](../database.py), [prodamus.py](../prodamus.py), workers и [multstudio/assets.py](../multstudio/assets.py). Тарифы, webhook HMAC и финансовые SQL не переписаны; добавлены только технические guards перед соответствующими действиями.

## Долговечность и реестр

Registry schema version 111; version 0 от Security 1.1 допускается только при известной совместимой структуре. Неизвестная версия/таблица/повреждение — отказ, а не обнуление. Файл и SQLite sidecars должны принадлежать service UID и не иметь group/other прав. Symlink и доступные другим registry/journal отвергаются. `.required` marker (0600) сохраняется при первой записи: отсутствие самой DB при marker блокирует пересоздание и новые операции. Удаление DB и marker вместе или утрата всего диска не обнаруживаются магически: нужны backup и внешняя проверка.

PUT: metadata prepared → короткий CAS submitting commit → SDK HEAD/PUT/HEAD вне write transaction → ready. Неизвестный PUT не повторяется автоматически; принятый объект восстанавливается HEAD/SHA без повторной записи. Изменённый content/order для той же операции отвергается. Чтение подтверждает размер и SHA. Никаких анонимных HEAD или fallback на public storage.

Runtime S3 использует отдельные credentials, HTTPS, SigV4, verify=True и отключённые автоматические SDK retries. Security 1.0 TLS/safe_logging не ослаблены. Новый код не пишет URL/API body/traceback в журнал; repr policy скрывает owner/operation. Registry/планы сами содержат чувствительные metadata и не должны раздаваться Nginx.

## Память и ограничения VPS

* Input/output limits приложения: image 8 MiB, audio 16 MiB, video 32 MiB, document/octet-stream 8 MiB. Это наши консервативные пределы, не обещание полного покрытия лимитов MAX/провайдеров.
* S3 GET читается блоками до 64 KiB в эксклюзивный файл 0600. Для крупных медиа MAX получает InputMedia(path); маленький buffer оставлен для SDK совместимости и image preparation.
* Установленный maxapi 1.2.3 `BaseConnection.upload_file` **сам читает файл целиком**. Поэтому file-based путь не является end-to-end streaming. Один transfer slot и 32 MiB cap ограничивают этот SDK buffer; 200 MiB больше не используется как универсальный безопасный буфер.
* Синтетический тест трёх одновременных 32 MiB доставок с реальным SDK upload_file и mocked HTTP: max active=1, Python tracemalloc peak около **32,4 MiB**. Это не общий RSS сервиса и не профилирование production VPS. Pillow/ffmpeg/native memory, OS cache и остальные задачи требуют отдельного owner monitoring на 1 GiB/1 vCPU.
* SDK network calls выполняются через to_thread; write transactions содержат только metadata SQL. Синтетический конкурентный writer во время HEAD/GET/PUT подтверждает отсутствие SQLite write lock на сети.

## Временные ссылки и добровольное предоставление

Signer выдаёт HTTPS GET после owner/order checks, по умолчанию 300 секунд, максимум 900. Это **наше access policy**, не retention и не подтверждённое provider fetch окно. Signed URL — bearer credential; не хранить в БД/логах. Получатель с grant повторно проверяется после HEAD; уже выданный URL после revoke может действовать до TTL.

Recipient grant — только явное действие владельца для конкретного получателя, не более 24 часов; UI массовой публикации не добавлен. Для restricted songs grant запрещён. Генерация не означает согласие на рекламу, публикацию, обучение моделей или использование фото третьих лиц/детей.

## Что остаётся неизвестным / блокирует запуск

Live Yandex ACL/IAM/CDN/public flags, реальная media delivery через MAX, общий RSS VPS, договорные retention/страны обработки сторонних сервисов, queue/refetch upper bounds HeyGen/Kling, privacy implications provider assets и максимальные сроки output URLs не подтверждены по исходникам. Не давать этим параметрам выдуманные значения. Юридические согласия, retention/deletion и закрытие исторических URL — отдельные этапы.

Дальше: [миграция](private-media-migration.md), [точный Yandex setup, smoke и rollback runbook](private-media-deployment-runbook.md).
