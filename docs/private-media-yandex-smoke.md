# Security 1.1.3 — профиль Yandex и smoke владельца

Проверка источников: 28.11.2026 (Москва). База изменений: `f5dd8de787cd1b407df16f3072a2f792598ccd6a`, Draft PR №266. Production остаётся на `57744a0b600a9736cbaf1d00a2ccb6660869c903`. Здесь подготовлены код и инструкция; реальные bucket/VPS/keys не использовались. Команды ниже **не выполнялись** и не являются deployment.

## Документированные факты и границы доказательства

* [ACL](https://yandex.cloud/ru/docs/storage/concepts/acl): новые бакеты/объекты имеют пустой ACL; IAM каталога/облака наследуется независимо от видимых ACL grants. Поэтому пустой список не доказывает приватность.
* [XML ACL](https://yandex.cloud/ru/docs/storage/s3/api-ref/acl/xml-config): Owner.ID в GetBucketAcl относится к каталогу. Это не личный IAM ID и не allowlist runtime-получателей. Фактическая пустая строка из сообщения владельца учитывается как отдельная допустимая форма, а не как подтверждение каталога.
* [Conditions](https://yandex.cloud/ru/docs/storage/s3/api-ref/policy/conditions): документирован ключ `aws:securetransport`. Профиль нормализует регистр имени этого ключа; семантика Bool не меняется. Дубли после нормализации запрещены, даже с одинаковым значением.
* [Access overview](https://yandex.cloud/ru/docs/storage/security/overview): IAM, policy, public flags и ACL объектов взаимодействуют. Нельзя трактовать узкий Allow как полную отмену унаследованного IAM или расширяющего object ACL. Нужны административный аудит и фактические отрицательные пробы.
* [Public access](https://yandex.cloud/ru/docs/storage/security/public-access): чтение объектов, листинг и чтение конфигурации — отдельные настройки.

Успешные GetBucketAcl/GetBucketPolicy подтверждают доступность этих двух чтений. Они не подтверждают public flags, folder membership, все IAM bindings, CDN, object ACL и правильность привязки static key к субъекту.

## Контракт `yandex-v1`

Реализация: [yandex_media_profile.py](../yandex_media_profile.py). Это opt-in профиль, а не глобальный bypass. По умолчанию остаётся прежний `strict`; неверное имя профиля отвергается. Поддержан только HTTPS endpoint `https://storage.yandexcloud.net` (допустим завершающий `/`). Другие endpoint требуют отдельного review.

Допустимый ACL: dict с обязательными `Owner.ID` (строка) и `Grants` (пустой list). ID может быть пустым либо **точно** соответствовать заранее подтверждённому folder ID. Отсутствующие поля, null/неверные типы, чужой непустой folder ID, неожиданные поля и любые grants отвергаются, включая утверждённых субъектов и публичные группы. Необязательные стандартные DisplayName/ResponseMetadata не дают прав. При указанном HTTP status требуется 200.

Профиль требует ровно четыре семантически определённых правила. Sid — подпись, не основание доверия:

| Правило | Точный контракт |
|---|---|
| Object IO | Allow; только runtime CanonicalUser; ровно GetObject/PutObject; только `bucket/media/v1/*`; Bool TLS=true |
| Metadata | Allow; ровно два утверждённых CanonicalUser; ровно GetBucketAcl/ListBucket; только bucket; Bool TLS=true |
| Transport | Deny; Principal `*`; Action `*` или `s3:*`; bucket **и** bucket/*; Bool TLS=false |
| Legacy | Deny; ровно два известных старых CanonicalUser; Action `*` или `s3:*`; bucket **и** bucket/*; без условий |

Для Legacy Condition может отсутствовать, быть null или `{}`. Для остальных трёх правил отсутствие условия запрещено. Дополнительные правила, wildcard Allow, другие actions/resources/principals, NotPrincipal/NotAction, неизвестные условия и повторяющиеся JSON keys отвергаются. Bool принимает строки `true`/`false` либо JSON booleans. Неизвестные/неоднозначные формы не приводятся к «безопасным» значениям.

## Отдельная административная процедура — ДО подтверждения controls

Владелец выполняет в доверенном **административном** контуре, не runtime static key бота:

1. В Console открыть **именно новый** bucket; сверить имя, folder/cloud IDs и service account, которому принадлежит новый static key. Не выводить ключ/секрет. Folder ID записать отдельно от subject IDs. Непустой Owner.ID обязан совпадать с этим folder ID; пустой не подтверждает identity.
2. Проверить public access: object read=false, list=false, config read=false; нет website/CDN/анонимного origin. Через [Bucket.Get](https://yandex.cloud/ru/docs/storage/api-ref/Bucket/get) можно читать folderId/anonymousAccessFlags в административном контуре; нельзя подставлять API JSON-поля по догадке. Console — допустимая альтернатива. Эти права runtime не расширять.
3. Просмотреть прямые bucket IAM и унаследованные cloud/folder bindings, включая роли через группы. [Документированный read-only пример](https://yandex.cloud/ru/docs/storage/concepts/acl#inheritance): `yc resource-manager folder list-access-bindings --id <folder-id>`. Аналогично проверить cloud bindings в Console. Наличие viewer/editor/storage.* или публичных allUsers/allAuthenticatedUsers учитывать, даже когда Grants=[]; никаких remove/update команд здесь не предусмотрено.
4. Отдельно проверить оба legacy account, runtime account, оператора и группы, куда они входят. Не назначать runtime широкие cloud/folder роли ради прохождения preflight. Bucket-level `storage.uploader` + `storage.configViewer` не увеличиваются этим PR. IAM роль действует на bucket, не prefix; границу prefix нельзя считать доказанной одним Allow.
5. Просмотреть политику целиком: четыре правила, точные субъекты, оба Deny. Проверить ACL объектов (включая ранее загруженные synthetic), которые могут расширить доступ. Новый runtime-код и smoke не передают ACL/grant headers, используя пустые default ACL. Если ACL объектов/унаследованный доступ неясны — STOP, не подтверждать controls.
6. Зафиксировать вне Git/публичного чата: дату, оператора, bucket/folder/cloud, subject IDs и static-key ownership без секрета, результаты flags/IAM/groups/object ACL/CDN review и hash проверенной policy. Запись хранить с ограниченными правами. Повторять после изменений IAM/flags/policy/CDN и перед пилотом.

Только после этих пунктов оператор может поставить `PRIVATE_MEDIA_YANDEX_CONTROLS_VERIFIED=true` в **изолированном тестовом** окружении. Это явное подтверждение выполненной процедуры, не автоматическая проверка control plane и не бессрочная гарантия. `PRIVATE_MEDIA_STORAGE_VERIFIED` — другое подтверждение, после полного storage/MAX smoke; здесь оно остаётся false.

## Настройки тестового контура

Секреты вводить через защищённый environment-файл/secret manager, не CLI, не Git, не чат и не shell history. В этом документе только placeholders:

```dotenv
PRIVATE_MEDIA_ENABLED=false
PRIVATE_MEDIA_PRODUCTS=
PRIVATE_MEDIA_STORAGE_VERIFIED=false
PRIVATE_MEDIA_PROFILE=yandex-v1
PRIVATE_MEDIA_BUCKET=<new-private-bucket>
PRIVATE_MEDIA_YANDEX_FOLDER_ID=<verified-folder-id>
PRIVATE_MEDIA_RUNTIME_PRINCIPAL=<runtime-subject-id>
PRIVATE_MEDIA_PRINCIPALS=<runtime-subject-id>,<approved-metadata-subject-id>
PRIVATE_MEDIA_LEGACY_PRINCIPALS=<old-subject-a>,<old-subject-b>
PRIVATE_MEDIA_YANDEX_CONTROLS_VERIFIED=false
PRIVATE_MEDIA_ACCESS_KEY_ID=<dedicated-runtime-key>
PRIVATE_MEDIA_SECRET_ACCESS_KEY=<dedicated-runtime-secret>
```

Controls меняется на true только по процедуре выше. Runtime обязан входить в два разрешённых субъекта; legacy set состоит из двух других ID. Folder не добавляется в principal allowlist ради прохождения проверки. Не переносить старые ключи в runtime credentials.

## Повторная read-only проверка

На выбранном immutable release SHA, **вне production процесса**, после административного review и подготовки отдельного environment:

```bash
venv/bin/python - <<'PY'
from private_media_yandex_smoke import configuration
from private_media import storage_client
from yandex_media_profile import check_bucket
c=configuration()  # только явный private environment, без чтения .env бота
s=storage_client(c)
try:
    check_bucket(c,s)  # только GetBucketAcl/GetBucketPolicy
    print('Yandex profile: OK; no object operations performed')
except Exception:
    print('Yandex profile: FAIL; stop and review private configuration')
    raise SystemExit(1) from None
finally:
    s.close()
PY
```

Не печатать raw ACL/policy, exception/traceback, URL или credentials. Успех — не разрешение включить private products. Общий rollout и четыре provider-blocked продукта остаются запрещены.

## Synthetic PUT/HEAD/GET, anonymous и expiry

[private_media_yandex_smoke.py](../private_media_yandex_smoke.py) — отдельный owner-only скрипт. Он **не запускается ботом**, не читает .env бота, не обращается к business DB/registry/AI/MAX, не меняет флаги, ACL, policy или IAM. Нужны только явно экспортированные dedicated private настройки. Запуск требует `--execute`, точное подтверждение bucket и **новый** файл записи 0600 в каталоге оператора 0700:

```bash
venv/bin/python private_media_yandex_smoke.py \
  --execute --confirm-bucket '<new-private-bucket>' \
  --record /secure/operator/new-yandex-smoke-record.json \
  --ttl 10 --legacy-probes
```

Для `--legacy-probes` оператор отдельно и временно предоставляет credentials обоих старых accounts в **этом изолированном процессе**, проверив принадлежность ключей:

* `PRIVATE_MEDIA_SMOKE_LEGACY_1_PRINCIPAL`, `_1_ACCESS_KEY_ID`, `_1_SECRET_ACCESS_KEY`;
* `PRIVATE_MEDIA_SMOKE_LEGACY_2_PRINCIPAL`, `_2_ACCESS_KEY_ID`, `_2_SECRET_ACCESS_KEY`.

Предоставленные идентификаторы должны быть разными и входить в deny set; два доступных account покрывают его целиком, один — только свою часть. Эти ключи не переходят в конфигурацию runtime бота. Неверные/неполные пары credentials останавливают CLI до первого запроса. Отсутствие credentials одного account теперь оставляет его not tested, не блокируя имеющуюся пару другого. Для speechkit без static key использовать явный NO_STATIC_CREDENTIALS marker по [инструкции 1.1.3](private-media-metadata.md); не создавать ключи и не расширять права ради теста.

Проверяется последовательно:

1. Read-only профиль ACL/policy.
2. Один PUT небольшого фиксированного synthetic TXT по **новому UUID** под media/v1/smoke; до PUT записывается ключ и submitting. HEAD проверяет size/SHA; SDK GET проверяет bytes.
3. Anonymous GET и HEAD **этого существующего** объекта без query/credentials должны дать 403. 404, redirect и network error не принимаются как доказательство защиты.
4. Signed GET и отдельно signed HEAD должны дать 200. После TTL+3 секунд оба прежних URL должны дать 403. Свежие signed GET/HEAD теми же ключами затем обязаны дать 200: общая недоступность/отозванные credentials не подменяют проверку expiry.
5. Runtime PUT нового synthetic UUID **вне media/v1** обязан дать 403. Это фактическая проверка ограничения наследуемого IAM, не вывод из текста Allow. Если операция неожиданно разрешена, может появиться только новый synthetic файл; его ключ уже записан, проверка немедленно провалится.
6. Для **каждого** legacy account — HEAD/GET существующего synthetic объекта и PUT нового synthetic key внутри prefix должны дать 403. 404/timeout не считаются успехом.

Скрипт всегда использует HTTPS, verify=True, SigV4; anonymous/signed HTTP-клиент не использует environment proxies и не следует redirect. HTTP downgrade для тестирования Deny не выполняется. URL и API body не печатаются; signed URLs только в памяти. Сроки fetch внешних AI-провайдеров этим не подтверждаются.

Exit 0 — все пробы прошли; exit 2 — базовые пробы прошли, но два legacy account не проверялись (запуск без `--legacy-probes`), **пилот не разрешён**; exit 1 — STOP. Даже успешный smoke не доказывает безопасность account с правом изменения самой policy, будущих IAM изменений и всех внешних потребителей.

## Ошибки, неизвестный PUT и уборка

Нет автоматического retry PUT и автоматического Delete. Файл record хранит только технические bucket/key/state, без signed URLs и секретов. Он чувствителен, должен оставаться вне Git/веб-каталога. `submitting` после сбоя означает неизвестный исход; не повторять операцию слепо. Сначала authenticated HEAD именно сохранённого key в доверенном контуре и отдельное решение оператора. SDK automatic retries отключены.

При ошибочной policy могут сохраниться новый prefix TXT, новый out-of-prefix TXT либо новый legacy-probe TXT. Они не перезаписывают существующие файлы. После расследования оператор отдельно удаляет **только synthetic keys из record**, используя административный контур; runtime DeleteObject не выдаётся. Реальные фото/заказы/старый bucket не трогать.

После успешного storage smoke отдельно проверить synthetic MAX delivery, после чего обсуждать ограниченный пилот. Все product flags в поставляемом коде остаются off. Private animation/cartoon/ded_moroz/multstudio не разблокированы.

## Reader-compatible rollback

Формат refs и schema 111 не изменены. Но старый f5dd8de не понимает пустой ACL Yandex: он не является рабочим reader fallback для этого профиля. Для Yandex требуется текущий проверенный **Security 1.1.2 SHA** даже при PRIVATE_MEDIA_ENABLED=false/PRODUCTS пусто/READ_ONLY=true. Сохранить profile, expected folder, principals, административное подтверждение, bucket/keys, registry/anchor и согласованную business DB. Не восстанавливать старую DB, не делать bucket public. При неисправном reader остановить затронутую обработку и устранить проблему отдельно.


## Уже созданный synthetic TXT / Security 1.1.3

Не повторять PUT из-за ключа Metadata.Sha256. [Read-only resume и диагностика](private-media-metadata.md) принимают защищённый существующий record 1.1.2, не произвольный key/фото/файл. Входной record не меняется, новый diagnostic record создаётся 0600; все write probes пропускаются. Общий helper нормализует только регистр имени metadata key, не значение digest; GET metadata/length и bytes SHA также обязательны. Старый 337d485 не является рабочим reader fallback для фактического SDK Metadata.Sha256: сохранить проверенный 1.1.3 SHA и profile/identity/controls при отключении writes.

Два static ключа больше не требуются для независимых legacy probes. Частичный результат или отсутствие speechkit static key не подтверждает полный запрет доступа. Проверки с IAM-token только владельцем, только через уже разрешённую аутентификацию и без выдачи дополнительных прав.
