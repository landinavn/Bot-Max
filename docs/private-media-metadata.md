# Security 1.1.3 — SHA256 metadata и диагностика существующего smoke

База: `337d485ceaf8787c3c505be831921d44cea49b71`; Draft PR №266. Production остаётся на `57744a0b600a9736cbaf1d00a2ccb6660869c903`. Дата документации: 28.11.2026, Москва. Действия ниже подготовлены для владельца; разработчик не обращался к VPS, реальным bucket/keys или клиентским материалам.

## Факты, сообщённые владельцем

Владелец подтвердил успешный реальный read-only preflight 1.1.2, административную проверку public flags/IAM/website/CDN/key ownership, PUT/HEAD/GET synthetic TXT, anonymous 403 и активные signed GET/HEAD 200. Эти сведения не перепроверялись нами. Остановку вызвал SDK ключ `Metadata.Sha256` вместо `Metadata.sha256`; значение SHA256 и фактические bytes совпали. Подтверждение всех legacy способов авторизации, expiry, out-of-prefix и полного product pilot из этих фактов не следует.

## Общий строгий helper

[private_media_integrity.py](../private_media_integrity.py) извлекает digest по одному ключу, имя которого после нормализации регистра равно sha256. Поддерживаются sha256/Sha256/SHA256/смешанный регистр. Значение **не** lower/strip/исправляется: требуется ровно 64 hex символа, затем точное сравнение с ожидаемым digest. Верхний регистр hex в значении не преобразуется ради совпадения. Пробелы, переносы, неверные типы/формат, отсутствующий digest и несколько совпавших ключей (даже с одинаковым значением) запрещены. ETag не используется вместо SHA256.

Проверка ContentLength сохраняется в HEAD и GET. GET дополнительно проверяет длину и SHA256 фактически прочитанных bytes. Неудачная проверка закрывает body и удаляет только новый незавершённый temp output; не удаляет S3 объект, сохранённый ref или заказ. HTTP response body/metadata/digest не попадают в пользовательскую ошибку.

Общий helper используется в `_verify_head`: PUT verification, recovery, stat/presign/read. GET проверяет metadata перед чтением. Private destination migration проходит тот же `PrivateMedia.put`; legacy source без SHA metadata не объявляется повреждённым только из-за исторического формата — его bytes/length проверяются существующим copy, затем новый private digest проверяется общим helper. Историческая миграция здесь не запускалась. `multstudio` local published_sha256 — другое поле локального manifest, не S3 Metadata, его формат не менялся.

Unknown PUT не повторяется: submitting marker сохраняется; existing HEAD с корректным digest допускает recovery без нового PUT, неверные/неоднозначные metadata — STOP. При HEAD 404 после неизвестного PUT нельзя считать запись точно не выполненной и повторять её автоматически.

## Безопасная поэтапная диагностика

[private_media_yandex_smoke.py](../private_media_yandex_smoke.py) сохраняет version=2 record с полями stage/stages/objects/legacy/result. В stdout — только фиксированные этапы, статусы и безопасные коды:

1. preflight — ACL/policy.
2. put.
3. head_integrity.
4. get_integrity.
5. anonymous_get_head.
6. signed_get_head.
7. signed_url_expiry.
8. fresh_signed_access.
9. out_of_prefix_put.
10. legacy_probes — отдельно по каждому account.

Примеры кодов: metadata_missing, metadata_duplicate_sha256, metadata_invalid_sha256, size_mismatch, sha256_mismatch, content_sha256_mismatch, network_timeout, access_not_denied. Неизвестное исключение становится probe_failed, а не его исходным текстом. Нет credentials, signed URL, raw response body, traceback или произвольных exception messages. Record 0600 содержит технические bucket/key; держать его вне Git/веб-каталогов в директории оператора 0700.

## Использовать уже созданный объект — без нового PUT

Не удалять и не переписывать существующий record 1.1.2. На отдельно проверенном SHA и **вне production процесса** экспортировать только dedicated private environment по [инструкции профиля](private-media-yandex-smoke.md). Скрипт не читает .env бота. Controls подтверждать только после административного review; product flags и STORAGE_VERIFIED остаются false.

Новая команда владельца (подставить настоящие локальные пути и имя нового private bucket; секреты не передавать в аргументах):

```bash
venv/bin/python private_media_yandex_smoke.py \
  --confirm-bucket '<new-private-bucket>' \
  --resume-record /secure/operator/existing-112-smoke-record.json \
  --record /secure/operator/NEW-113-diagnostic.json \
  --ttl 10 --legacy-probes
```

**Без `--execute`**: вместе с resume он запрещён. `--record` — новый output (exclusive 0600), `--resume-record` — существующий input, открываемый только O_RDONLY/O_NOFOLLOW. Проверяются UID, права, regular file, размер до 64 KiB, уникальные JSON keys, bucket, формат и synthetic namespace. Поддерживается старый record 1.1.2 и новый version=2.

Выбирается ровно один основной `media/v1/smoke/<uuid>.txt`, а не произвольная пользовательская ссылка/файл. Ожидаемые bytes — встроенный фиксированный synthetic TXT из 1.1.2, не содержимое внешнего файла. HEAD должен подтвердить его размер и digest, GET — фактические bytes. Record с другим bucket/key, traversal/query/URL, неизвестной version, небезопасными правами, symlink или произвольным объектом отвергается до cloud requests. Существующий input остаётся побайтно неизменным.

Resume выполняет только ACL/policy read, HEAD/GET, локальное формирование signed URL и HTTP GET/HEAD. **Нет PUT/DELETE, изменений S3/ACL/IAM/registry/DB, включая legacy accounts.** PUT и out-of-prefix помечаются skipped-read-only. Результат всегда partial-read-only, exit 2, даже при всех успешных чтениях: это диагностика, не полный write smoke и не допуск пилота. После успешного GET новый diagnostic record отмечает объект accepted; старый record/submitting marker не переписывается.

Если сохранился только key, но нет доверенного защищённого record, этот режим не принимает произвольный key через CLI. Оператору нужен отдельный разбор происхождения; не создавать фиктивный record и не обходить ограничения.

## Независимые legacy checks

Статусы в stdout/record разделены по именам bot-storage-user и speechkit-user; IDs/секреты в stdout не выводятся. Имена — операционные подписи, а принадлежность ключа аккаунту подтверждается оператором, не угадывается SDK.

Для существующего bot-storage-user key экспортировать в изолированном процессе:

* `PRIVATE_MEDIA_SMOKE_LEGACY_1_PRINCIPAL` — подтверждённый subject ID из deny set;
* `PRIVATE_MEDIA_SMOKE_LEGACY_1_ACCESS_KEY_ID`;
* `PRIVATE_MEDIA_SMOKE_LEGACY_1_SECRET_ACCESS_KEY`.

Для speechkit-user **не создавать статический, авторизованный или временный access key и не выдавать роли ради теста**. После проверки владельцем отсутствия static credentials достаточно:

* `PRIVATE_MEDIA_SMOKE_LEGACY_2_PRINCIPAL` — второй подтверждённый subject ID;
* `PRIVATE_MEDIA_SMOKE_LEGACY_2_NO_STATIC_CREDENTIALS=true`.

Оба `_2_ACCESS_KEY_ID`/`_2_SECRET_ACCESS_KEY` оставить отсутствующими. При противоречивых или неполных парах credentials — ошибка конфигурации, а не попытка угадать секрет. Для обеих пар запрещены повторные IDs/access-key IDs. Без `--legacy-probes` ключи legacy вообще не используются.

Доступные credentials проверяются независимо, отсутствие второго не мешает первому. SignatureDoesNotMatch/InvalidAccessKeyId/ExpiredToken/InvalidToken не считаются подтверждённым запретом доступа: это authentication_unverified. Даже AccessDenied требует предварительного подтверждения владельцем принадлежности и действительности credentials; универсальный 403 сам по себе не доказывает IAM restriction. Для обычного write smoke доступны HEAD/GET/PUT; в resume — только HEAD/GET. Если одна проба не проходит, другая всё равно проверяется, её результат не теряется. Статусы: passed/failed/not tested, scope и reason. Для speechkit-user при указанном marker: **not tested, no_static_credentials**, absence_is_not_authorization_proof=true. Нет ключа — не доказательство запрета IAM-token/STS/другой авторизации.

Один прошедший account + второй not tested — partial-legacy-not-tested (для write) либо partial-read-only (для resume), exit 2. Не считать частичный отчёт полностью пройденным, не включать пилот автоматически. Для нового независимого write smoke по-прежнему требуются --execute, новый UUID и новый record; recovery прежнего объекта не повторяет PUT.

## Проверить speechkit-user без static key и без новых прав

Официальная [аутентификация S3 API](https://yandex.cloud/ru/docs/storage/api-ref/authentication) и [S3 usage](https://yandex.cloud/ru/docs/storage/s3/) (проверено 28.11.2026) поддерживают IAM-token с Authorization Bearer без SigV4. Действия выполняются от имени субъекта токена. Наличие/отсутствие static key этого не отменяет.

Допустимы только **уже доступные** оператору способы аутентификации именно speechkit-user:

* IAM-token от существующей аутентификации сервисного аккаунта; новый authorized/static/access key не создавать.
* Имперсонация, только если у оператора **уже есть** соответствующее право. [Инструкция](https://yandex.cloud/ru/docs/iam/operations/sa/impersonate-sa) описывает получение токена; отсутствие права означает not tested, а не назначение tokenCreator/admin.
* Уже доступный токен привязанного к доверенной VM/service identity — только после подтверждения, что субъект именно speechkit-user. Не использовать токен runtime/admin вместо него.

Токен выдаётся/получается только владельцем в доверенном контуре. Проверить его свежесть и identity по доверенному issuer/операторским данным; invalid/expired token не является успешной проверкой Deny. Не печатать токен, не передавать его в curl/CLI аргументах, не создавать ключи STS ради обхода запрета этой задачи.

Ниже **подготовленный, не выполненный** read-only пример для владельца. Предварительно задать `SPEECHKIT_IAM_TOKEN` в защищённом environment и подготовить существующий synthetic record. Это отдельная проверка IAM HEAD/GET; она не меняет статический отчёт на fully passed и не проверяет PUT:

```python
import os, json, hashlib, logging
from urllib.parse import quote
import httpx
from private_media_yandex_smoke import configuration, load_record, validate_record, PAYLOAD
from private_media import storage_client
from private_media_integrity import verify_s3_integrity
logging.disable(logging.CRITICAL)
s = None
try:
    c = configuration()
    r = load_record('/secure/operator/existing-112-smoke-record.json', c.private_media_bucket)
    key = validate_record(r, c.private_media_bucket)
    s = storage_client(c)
    verify_s3_integrity(s.head_object(Bucket=c.private_media_bucket, Key=key), len(PAYLOAD), hashlib.sha256(PAYLOAD).hexdigest())
    url = 'https://storage.yandexcloud.net/' + quote(c.private_media_bucket, safe='') + '/' + quote(key, safe='/')
    token = os.environ['SPEECHKIT_IAM_TOKEN']
    with httpx.Client(verify=True, trust_env=False, follow_redirects=False, timeout=10) as h:
        codes = [h.request(method, url, headers={'Authorization': 'Bearer ' + token}).status_code for method in ('HEAD', 'GET')]
    print(json.dumps({'account':'speechkit-user', 'auth':'iam-token', 'scope':'HEAD/GET only', 'status':'passed' if codes == [403,403] else 'failed'}))
except Exception:
    print(json.dumps({'account':'speechkit-user', 'status':'failed', 'error_code':'iam_probe_failed'}))
finally:
    if s is not None: s.close()
```

Не превращать отказ выпуска токена или невалидную авторизацию в доказательство Deny. Если нет уже разрешённого способа получить действующий токен, сохранить not tested и описать блокер. Runtime roles, bucket policy, public flags и feature flags этой проверкой не меняются.

## Результаты и ограничения

Точные SHA, тестовые счётчики и полное сравнение с 337d485 публикуются в PR №266 после окончательного прогона. Сохраняются исходные 237 test ID; fixtures HEAD/GET дополнены реальными по форме полями Metadata/ContentLength, без снятия assertions. Новые проверки используют вымышленные IDs/keys и локальные synthetic files. Dependency/TLS/IAM/profile/платёжные проверки не ослаблены. Все private feature flags по умолчанию off. Реальный resume/expiry/legacy IAM и product pilot должны выполняться владельцем отдельно; merge/deployment не выполнялись.
