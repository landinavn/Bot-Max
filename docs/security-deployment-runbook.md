# Security 1.0: переход и безопасный откат кода

Дата: 27 ноября 2026. **Это инструкция, а не отчёт о deployment. Ни одна команда на VPS в рамках подготовки не выполнялась.**

## 1. Версии, границы и ответственный

| Назначение | Версия |
|---|---|
| Проверенный production/base | `48d4c5918013069483a2292c5e46e626798f24cb` |
| Основной Security 1.0, draft PR [№264](https://github.com/landinavn/Bot-Max/pull/264) | `57744a0b600a9736cbaf1d00a2ccb6660869c903` |
| Независимый резерв | `security/secure-fallback`, точный полный SHA из одобренного draft PR этой ветки |

Перед окном оператор записывает полный резервный SHA в release checklist. Ветка может двигаться: её имя не является release pin. Не использовать `main`, `origin/main`, короткий SHA или последний PR HEAD вместо согласованного SHA. Отдельно фиксируются разрешение владельца, оператор, окно МСК, контакты поддержки и предельная длительность недоступности. **Не выполнять эту инструкцию без отдельного согласования размещения.**

Меняется только код `botmax.service`. Не обновлять production venv/пакеты, не выполнять `pip install`, миграции вручную, рассылки, пересоздание заказов, регенерации или тестовые платные POST. Не менять Small Talk, Nginx, DNS, HTTPS, systemd unit, `.env`, бакет и другие службы. Не делать merge. Нельзя подменять checkout работающего процесса или одновременно запускать второй polling/worker с рабочими ключами.

## 2. Что представляет собой резерв

Резерв основан непосредственно на production, а не на слиянии №264. Runtime diff — `bot.py`, `max_client.py`, новый `fallback_logging.py`; дополнительно тесты и документация. Нет `safe_logging`, новой LogRecord factory, facade для десятков модулей или новых runtime-зависимостей.

MAX connector использует `ssl.create_default_context()`, `CERT_REQUIRED`, hostname/chain/expiry validation системного OpenSSL. Он отклоняет HTTP до connect, включая HTTPS → HTTP redirect. Нет fallback на `ssl=False`, дополнительного trust bundle или переключателя обхода проверки.

При `python /root/Bot-Max/bot.py` или `python -m bot` защита ставится **до импорта SDK/приложения**. Дескрипторы 1/2 перенаправляются в `/dev/null`, стандартное logging отключается. Поэтому print, stderr, traceback, warnings, native writes и наследуемые потоки дочерних процессов не попадают в журнал. В приватный non-inheritable дубликат исходного stderr пишутся только фиксированные byte literals:

| Сообщение | Значение |
|---|---|
| `secure_fallback starting` | Защита установлена; это ещё не готовность бота |
| `secure_fallback polling_starting` | Выполнение дошло до вызова polling; это ещё не доказательство получения обновлений |
| `secure_fallback stopped` | `asyncio.run(main())` завершился нормально |
| `secure_fallback fatal` | Ошибка entry point/необработанное исключение; без текста/traceback |
| `secure_fallback thread_failed` | Необработанное исключение потока |
| `secure_fallback unraisable` | Исключение, переданное `sys.unraisablehook` |
| `secure_fallback guard_failed` и exit 78 | Защита не установилась; приложение не запускается |

Диагностика очередей/заказов в резерве проводится по защищённым записям БД и подтверждениям доставки, а не через полный API response. Это сознательно ограниченный аварийный режим. HTTP-ответы, пользовательские сообщения MAX и сохранённые материалы не перенаправляются в `/dev/null`: меняются только диагностические потоки процесса.

Защита не действует при простом `import bot; await bot.main()` в другом launcher и не объявляется политикой произвольных CLI. Поэтому неизвестный ExecStart — блокирующее условие. Не запускать вспомогательные скрипты с рабочими данными без самостоятельной защиты. Не включать verbose logging/traceback для диагностики. Старые журналы, произвольные пользовательские файлы, БД, ответы клиентам, nginx/journald других служб, Python startup/site hooks **до entry point** и внешние поставщики находятся вне этой защиты. В проверенном коде нет FileHandler; не добавлять его. Защита не обещает безопасность нового кода, который явно откроет другой файл/tty/дескриптор для вывода или обойдёт entry point.

## 3. Проверки до окна — без изменений production

1. Проверить оба release на отдельной машине. Проверки владельца на VPS (Python 3.12.3, MAX SDK 1.2.3, aiohttp 3.14.5, реальные TLS/auth/upload и тестовая отправка администратору) относятся к указанному им основному Security SHA; это не автоматическое подтверждение резервной версии. Для резерва нужен собственный изолированный smoke до разрешения отката. Локальные тесты подготовки описаны в `security-secure-fallback.md`.
2. Получить approved SHA и source archive/ветку **до остановки**, проверить `git cat-file -t`, ancestry, `git diff --name-status BASE SHA`, `git diff --check BASE SHA`. Для №264 runtime diff шире резерва: проверить его фактический список, включая logging-only изменения обработчиков/webhook. Нельзя считать №264 трёхфайловым патчем.
3. Убедиться, что `requirements.txt` для всех трёх версий идентичен; изменение `requirements-dev.txt` №264 касается только тестов и **не устанавливается на VPS**. Проверить список существующих installed distributions локально, не печатая переменные окружения/пути с токенами. Не исполнять `pip freeze` в публичный чат: direct URLs могут содержать секреты.
4. Проверить ровно один процесс/службу бота и отсутствие cron/tmux/ручного запуска того же token. Прочитать unit/override локально: ExecStart/WorkingDirectory/пользователь/TimeoutStopSec/Restart/EnvironmentFile. Не публиковать unit или `systemctl show Environment`: они могут содержать секреты. Если executable не существующий venv Python + `bot.py`/`-m bot`, остановить подготовку и согласовать отдельное решение; не менять unit скрытно.
5. Убедиться, что deploy checkout действительно `/root/Bot-Max`, DB — `/root/Bot-Max/database.sqlite3`, рабочие файлы и symlink targets перечислены. Базовый путь БД фиксирован в `database.py`; `.env` загружается относительно `config.py`. Поэтому нельзя просто запустить копию кода в другом каталоге: это может создать другую БД/потерять config.
6. Проверить свободное место, права backup, владельца файлов, отсутствие незакоммиченных tracked изменений. При локальных изменениях, особенно поддержке платежей/заказов, не стирать их `reset --hard`: остановиться для отдельного разбора. Проверить `git ls-files` для `.env`, БД, venv, материалов; при tracked runtime data не выполнять checkout без отдельного плана.

Пример проверки pins (значение FALLBACK_SHA оператор заполняет вручную из одобренного PR):

```bash
set -euo pipefail
cd /root/Bot-Max
BASE=48d4c5918013069483a2292c5e46e626798f24cb
SECURITY_SHA=57744a0b600a9736cbaf1d00a2ccb6660869c903
FALLBACK_SHA=REPLACE_WITH_APPROVED_FULL_SHA
test "$(git rev-parse HEAD)" = "$BASE" || exit 1
test -z "$(git status --porcelain --untracked-files=no)" || exit 1
[[ "$FALLBACK_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
for release in "$SECURITY_SHA" "$FALLBACK_SHA"; do
    test "$(git cat-file -t "$release")" = commit || exit 1
    test "$(git merge-base "$BASE" "$release")" = "$BASE" || exit 1
    git diff --exit-code "$BASE" "$release" -- requirements.txt || exit 1
    git diff --check "$BASE" "$release" || exit 1
    git diff --name-status "$BASE" "$release"
done
```

Это пример последовательности для Bash, не автоматический deploy. Нет `git pull`, очистки файлов или обновления пакетов. Доступность commit objects обеспечивается предварительным fetch именно согласованных веток/refs, без checkout/запуска кода.

## 4. Обязательный контроль заказов и памяти

**Нельзя гарантировать нулевую потерю незавершённых сценариев одним backup SQLite.** `Dispatcher(storage=MemoryContext, use_create_task=True)` держит часть FSM и выполняемых callbacks в памяти. Остановка уничтожит её. Встроенного универсального drain/сохранения всей RAM-FSM в этом production нет; резерв его не добавляет. MAX marker в этом запуске тоже не объявляется надёжно сохранённой очередью. Сообщение/кнопку, отправленные в downtime, нельзя без проверки обещать обработать ровно один раз.

В `bot.py` нет собственного обработчика SIGTERM с гарантированным drain всех callbacks. Поэтому `systemctl stop` нельзя объявлять graceful завершением активной генерации только из-за наличия `finally` в Python: сперва должны закончиться запросы и сохраниться checkpoints. Внешний SIGTERM/SIGKILL также не обязан дать fixed marker `stopped`; итог остановки проверяется через service/cgroup и защищённые записи.

Перед окном оператор должен:

- Разобрать активные обращения/долгие callbacks и дождаться окончания всех подтверждённых генераций/сборок/доставок. Не нажимать «повторить», не подтверждать новые задания. Контроль клиента/администратора нужен наряду с БД; отсутствие `running=1` само по себе недостаточно.
- Составить защищённый реестр каждого незавершённого оплаченного заказа: таблица/PK, payment ID, сумма/статус, стадия, `fsm_state/fsm_data`, provider task/job IDs, submit-started flags, результаты и отметки доставки. Не выводить этот реестр в терминал/PR/чат; хранить локально root-only. Отдельно отметить ожидающие оплату счета: они могут оплатиться во время окна.
- Проверить `payment_orders`, `audio_orders`, `musical_orders` и `musical_drafts`, `cartoon_orders` и `mother_admin_sessions`, `photo_orders/photo_results`, `animation_orders/animation_jobs`, `ded_moroz_payments` и задания Деда Мороза, `pending_videos`, `cartoon_jobs/cartoon_assets`, payment incidents/recoveries. Точные дополнительные таблицы уточнять через read-only `sqlite_master` и `PRAGMA table_info`, не предполагая чужую схему.
- Проверить как scalar IDs/status/running/lease, так и JSON (например `song_submitting`, `video_song_submitting_source`, `cartoon_kling_story.shots[*].task_id`, флаги image/video submitting). Сохранить соответствие источник → задание → результат → доставка. Пустой ID при установленном submitting **не означает**, что запрос не был принят провайдером.
- **Блокирующие условия:** неучтённая RAM-сессия/долгий callback; submit без сохранённого ID; незавершённая загрузка/сборка; непонятная аренда/running; provider job существует, но его состояние неизвестно; результат доставлен, но подтверждение не сохранено. В штатном сценарии отложить окно, проверить существующий запрос через GET и закончить обработку до остановки. Нельзя массово очищать флаги или переводить задания в queued.
- Если сохраняются пользовательские RAM-сессии без платных запросов, либо дождаться завершения, либо до окна согласовать с владельцем точный способ восстановления через существующий оплаченный заказ. Не обещать восстановить несохранённый текст/выбор. Автоматический export MemoryContext эта версия не умеет.

Минимальная read-only сводка ниже не содержит пользовательских ID/URL/payload и не заменяет реестр/проверку JSON:

```bash
cd /root/Bot-Max
venv/bin/python - <<'PY'
import sqlite3,json
tables=('payment_orders','audio_orders','musical_orders','cartoon_orders',
        'photo_orders','animation_orders','animation_jobs','pending_videos',
        'ded_moroz_payments','cartoon_jobs','payment_incidents')
with sqlite3.connect('file:database.sqlite3?mode=ro',uri=True) as d:
    d.execute('BEGIN')
    for t in tables:
        cols={r[1] for r in d.execute('PRAGMA table_info('+t+')')}
        if not cols: continue
        group=[c for c in ('status','generation_status','running','delivered','notified','resolved') if c in cols]
        if group:
            fields=','.join(group)
            for r in d.execute('SELECT '+fields+',COUNT(*) FROM '+t+' GROUP BY '+fields):
                print(json.dumps({'table':t,'states':dict(zip(group,r[:-1])),'count':r[-1]}))
        else:
            print(json.dumps({'table':t,'count':d.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]}))
PY
```

Не импортировать `bot`, не вызывать `init_db()`/workers для предварительной проверки. Даже прежний `init_db()` включает startup data reconciliation (например photo awaiting_payment → queued) — код этого не меняет, но запуск бота не является read-only действием.

## 5. Остановка и актуальная резервная копия

Только после закрытия условий раздела 4 и отдельного разрешения окна:

1. Записать время начала UTC/МСК и состояние pending payments. Согласовать окно с владельцем/поддержкой. Не менять маршрут webhook/Nginx ради maintenance.
2. Выполнить **только** `systemctl stop botmax.service`. Проверить `systemctl is-active botmax.service`, `MainPID=0`, пустую service cgroup и отсутствие отдельного polling/worker. При зависшей остановке не переходить автоматически к `kill -9`; проверить задачу/провайдера, отложить изменение. `TimeoutStopSec` может завершить дочерние процессы; после этого нужна отдельная сверка неопределённых запросов. Не останавливать Nginx/Small Talk.
3. После полной остановки сделать актуальный backup SQLite через sqlite backup API, а не одиночный `cp` живой WAL-БД. Сделать приватный архив `.env`, локальных материалов, `studio_files`, дополнительных путей/ссылок, venv и unit/override. Внешние `EnvironmentFile` и targets нужно скопировать отдельно с закрытыми правами: архив checkout их автоматически не включает. Проверить `integrity_check` backup и его SHA-256. Это конфиденциальный server-local backup, не вложение PR. Проверить место и прочитать manifest архива локально; не публиковать конфигурацию.
4. Сохранить исходный release pin и перечень tracked изменений отдельно, не архивировать всё `/root`/другие проекты. Не удалять исходные материалы/бакет. Для symlink directory отдельно сохранить содержимое target: tar по умолчанию хранит ссылку, это не backup её содержимого. Если venv расположен вне checkout — включить фактический target отдельно.

Пример создания backup (исполнять в одной shell; проверить фактические дополнительные пути отдельно):

```bash
set -euo pipefail
umask 077
BACKUP_DIR=$(mktemp -d /root/botmax-security-backup-XXXXXXXX)
export BACKUP_DIR
cd /root/Bot-Max
venv/bin/python - <<'PY'
import sqlite3,os
from pathlib import Path
p=Path(os.environ['BACKUP_DIR'])/'database.sqlite3'
with sqlite3.connect('file:database.sqlite3?mode=ro',uri=True) as src:
    with sqlite3.connect(p) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise SystemExit('Backup integrity failure')
os.chmod(p,0o600)
print('SQLite backup verified')
PY
# Все следующие команды — только после успешного backup API.
git rev-parse HEAD > "$BACKUP_DIR/source-sha.txt"
venv/bin/python -m pip list --format=json > "$BACKUP_DIR/installed-packages.json"
systemctl cat botmax.service > "$BACKUP_DIR/botmax.service.txt"
# Только этот проект; для внешних targets нужен отдельный backup.
# БД берётся из проверенного backup выше, не из этого tar.
tar --exclude='./.git' --exclude='./database.sqlite3' \
    --exclude='./database.sqlite3-wal' --exclude='./database.sqlite3-shm' \
    -czf "$BACKUP_DIR/project-runtime.tar.gz" -C /root/Bot-Max .
sha256sum "$BACKUP_DIR/database.sqlite3" "$BACKUP_DIR/project-runtime.tar.gz" \
    > "$BACKUP_DIR/SHA256SUMS"
sha256sum -c "$BACKUP_DIR/SHA256SUMS"
```

Проверять exit status **каждого** шага; при любой ошибке остановиться. Не выполнять следующий шаг копированием всего блока вслепую. Архив включает venv и `.env` при их наличии внутри проекта; проверить manifest и отдельно подтвердить наличие всех user materials. Не загружать этот архив в GitHub.

## 6. Переход на основной Security SHA

1. После backup ещё раз проверить остановку `botmax.service`, clean tracked tree и exact target. Сохранить `.env`, venv и runtime directories на месте; не применять `git clean`, `reset --hard`, `rsync --delete`, перенос всего checkout или копирование тестового `.env`/БД.
2. `git switch --detach 57744a0b600a9736cbaf1d00a2ccb6660869c903`. Проверить `git rev-parse HEAD`, `git status --porcelain --untracked-files=no`, неизменность `requirements.txt`, `.env`/venv/БД/путей. Проверку конфиденциальных checksums делать только локально и сравнивать, не публиковать hashes секретов.
3. Тем же **существующим** venv проверить импорты и компиляцию изменённых модулей на отдельной копии исходников, без импорта `bot.main`/init_db. Не запускать вторую копию бота с рабочими ключами «для проверки».
4. `systemctl start botmax.service`. Никакого изменения unit/daemon-reload/обновления пакетов. Проверить состояние и MainPID, отсутствие restart loop/двойного polling. Старт обычного main включает прежнюю инициализацию и workers; все доокновые неопределённые задачи должны быть разобраны заранее.
5. Проверить безопасный журнал: только разрешённые metadata, без response bodies, текстов, IDs, URL, токенов и traceback. Не включать HTTP debug для диагностики. Слишком подробный журнал — основание немедленно перейти к разделу 9.

## 7. Проверки после запуска

### MAX и доставка

- Отправить владельцем с тестового admin-аккаунта обычное `/start` и проверить ответ. Это подтверждает polling лучше, чем один startup marker. Не подтверждать платную генерацию и не начинать рассылку.
- Проверить auth и загрузку image/MP3/MP4/TXT на тестовых материалах отдельным согласованным smoke, без публикации ключей/URL/клиентских медиа. Избежать ручного второго polling. Существующие результаты доставлять из сохранённых URL/ID через штатное восстановление; не нажимать новые платные подтверждения.
- Сверить очереди/результаты/отметки доставки с приватным реестром. Если MAX принял доставку, но DB-ack неизвестен, сначала проверить доставку у получателя; нельзя объявить транспорт exactly-once. Повторная доставка готового файла не требует новой генерации, но дубликат сообщения всё равно нужно учитывать.

### Webhook без настоящей оплаты

- Использовать **фактический** private listen address/port из проверенного unit/config (не предполагать 8080, он может принадлежать Small Talk). Проверить, что endpoint `/prodamus/webhook` существует у `botmax`, а не у другого процесса.
- После записи адреса оператор может отправить синтетический unsigned form POST с `payment_status=success`: ожидается **403**, без новой оплаты/заказа/инцидента. Это HTTP callback probe, не платный POST к AI/платёжному провайдеру.
- Пример только с уже проверенным адресом: `curl --max-time 5 -s -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/x-www-form-urlencoded' --data 'payment_status=success' http://127.0.0.1:VERIFIED_PORT/prodamus/webhook`. Не подставлять секрет/подпись. HTTP здесь допустим только для существующего **локального** reverse-proxy upstream, не для MAX или внешнего webhook.
- Проверить тот же отказ по `https://wish-bot.ru/prodamus/webhook`, не меняя Nginx/маршрут. GET/404/405 сам по себе не доказывает успешную обработку подписанного платежа. Реальный успешный callback подтверждается только после сверки следующего настоящего события либо согласованного provider test с корректной подписью. Никакую фиктивную success-запись в БД не делать.

### Платежи в downtime

- Пока сервис остановлен, существующий upstream может отвечать 502/503. Не считать, что Prodamus обязательно повторит доставку в нужное время: фактическую политику retries подтвердить у провайдера/в кабинете.
- Сверить все успешные платежи от начала окна до устойчивого запуска плюс задержанные callback с исходным реестром и БД: merchant order, provider payment ID, сумма/валюта и paid timestamp. UUID транзакции в кабинете может отличаться от `order_id` webhook — сопоставлять по `order_num` и подписанному provider ID, не по одной сумме/почте.
- Если платеж отсутствует в БД, запросить повтор **оригинального подписанного уведомления** через штатный механизм кабинета/поддержки. Не создавать новый счёт, не редактировать сумму/status, не начислять вручную по скриншоту и не обходить verify_signature. Если провайдер не позволяет resend, остановить разбор этого платежа до отдельного безопасного recovery по существующему `payment_incidents/payment_recovery`.
- Повтор одного provider payment ID должен сохранить одно начисление/одно право. Проверить существующие balances/paid rows, incidents и recovery outcomes; число callback не равно числу покупок. Не путать повтор notification с повторным платным AI POST.

## 8. Правила восстановления без повторных платных POST

Runtime файлов бизнес-логики/схемы БД нет в diff резерва; JSON FSM, IDs и результаты не преобразуются. Сохраняются действующие guards submit-started, running/lease и manual review. Это не исправляет все прежние ограничения восстановления.

| Состояние | Действие |
|---|---|
| Сохранён provider task/job ID | Проверять существующее задание через штатный GET/poll, затем сохранить/доставить результат |
| Результат уже сохранён | Доставить/собрать из существующего результата, не отправлять create повторно |
| Submit-started/submitting, ID не сохранён | Не сбрасывать флаг, не нажимать повтор. Проверить существующий запрос у провайдера; ручная поддержка при неопределённости |
| Заказ paid, входные данные сохранены, платный запрос точно не отправлялся | Продолжать только штатной кнопкой этого заказа после проверки entitlement/стадии; не создавать новый заказ |
| Running/lease после остановки | Не массовый SQL reset. Сверить provider state/результат и штатные правила конкретного worker |
| FSM была только в RAM | Восстановить только доступную сохранённую часть; отдельно согласовать недостающие вводы, без новой оплаты |
| Manual/revoked/refund-requested | Не разблокировать автоматически, не возвращать статус paid ради восстановления |

Особенно осторожно с `pending_videos`: наличие записи с avatar/song не является provider ID и не доказывает, что новая попытка безопасна. После прерванного create без ID нужно отдельное решение. Начать новое платное создание можно лишь после доказательства, что предыдущий запрос не принят, и отдельного разрешения владельца. Этот runbook такого разрешения не даёт.

## 9. Откат только кода на secure-fallback

Основания: startup/import ошибки safe_logging, утечка в журнал, restart loop, отказ MAX polling/uploads, отказ webhook, нарушение обработки/доставки оплаченных заказов или несоответствие release SHA. При TLS/certificate ошибке сначала проверить часы/system CA/hostname; откат не делает её допустимой.

1. Зафиксировать время/симптом безопасными словами, без exception body/URL. Если есть новые активные запросы, применить правила разделов 4/8. Не создавать повторные задания для проверки.
2. `systemctl stop botmax.service`, проверить полную остановку. Сделать **новый** backup текущей БД/материалов тем же способом в новом каталоге — после запуска №264 уже могли появиться платежи/заказы.
3. Проверить полный approved FALLBACK_SHA, ancestry от BASE, неизменность dependencies/schema/business-файлов, clean tracked tree. `git switch --detach "$FALLBACK_SHA"`. Убедиться, что `.env`, тот же venv, текущая DB, studio_files/медиа остались на месте. Нет установки пакетов.
4. **Не восстанавливать старую БД, WAL, runtime-архив или старые материалы автоматически.** Не выполнять обратные SQL «миграции»/возврат прежних balances/status. Старая копия — для расследования; live DB остаётся наиболее актуальной. Если подозревается повреждение БД, остановить сервис и отдельно разработать восстановление с переносом всех новых оплат/заказов, не продолжать этот runbook.
5. `systemctl start botmax.service`, повторить проверки раздела 7. В резерве ожидаются только fixed markers из раздела 2; отсутствие подробных логов штатно. Проверить ответы admin `/start`, webhook отказ unsigned request, существующие provider IDs/результаты, доставки и оплаты окна. Не считать `polling_starting` успешным polling.
6. Если безопасный резерв не запускается/не работает, остановить `botmax.service`, не возвращать `48d4c59` с `ssl=False` и подробными логами. Когда нет проверенного независимого переключателя отдельной обработки, остановка всей службы безопаснее произвольного изменения workers/очередей. Small Talk/Nginx остаются нетронутыми. Платежи downtime — в защищённый список сверки/resend; отдельная диагностика TLS/guard/application без секретов и платных retries.

## 10. Критерии принятия и завершения окна

Обновление успешно только если одновременно:

1. Полный SHA соответствует одобренному, установленные production packages, конфигурация и схема БД не менялись этим релизом; нет второго polling/worker.
2. Служба стабильна, MAX реально отвечает на admin input, обязательные upload/delivery операции проходят с TLS validation. Нет unsafe fallback.
3. Webhook доступен через существующий домен/upstream, unsigned callbacks отвергаются; настоящие success callbacks сверены с одним paid entitlement/начислением. Все оплаты downtime разобраны; нет необъяснённых missing/duplicate credits.
4. Для каждого прежнего активного заказа сохранены provider IDs/материалы/статусы; доставка подтверждена либо есть конкретная безопасная стадия ожидания/manual review. Нет повторных платных POST/неучтённых списаний у провайдеров. Нельзя доказать это только совпадением счётчика заказов.
5. Нет персональных данных, произвольных URL/API responses/секретов/traceback в новых application logs. Старые логи и внешние access logs этим релизом не очищаются.
6. Потерянные RAM-only вводы отдельно учтены и восстановление согласовано; не обещано невозможное восстановление памяти.

Оператор фиксирует результат каждого пункта и выбранный SHA, время начала/окончания, приватные backup paths и оставшиеся incident references локально. Период наблюдения согласуется с владельцем; одна минута без ошибок не подтверждает работоспособность всех оплаченных сценариев. При незакрытом обязательном пункте окно не считается успешным.
