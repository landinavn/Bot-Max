# Security 1.1.1 — план исторической миграции

Подготовка, не запуск миграции. Не использовались рабочая БД, клиентские материалы или реальные бакеты. [private_media_migration.py](../private_media_migration.py) сохраняет принцип: сначала read-only inventory и новые private copies, затем отдельно согласованный cutover; никаких ACL/CDN/delete изменений в этом PR.

## Шесть отдельных этапов

| Этап | Проверка и результат | Что запрещено делать автоматически |
|---|---|---|
| 1. Инвентаризация | SQLite mode=ro, канонический legacy bucket/base, table/column/rowid/JSON path, key, source hash; агрегаты без raw URL | Не открывать произвольные внешние URL и не объявлять весь найденный контент принадлежащим row.user_id |
| 2. Владелец | Связь с заказом/проектом; source и owner повторно проверяются перед копированием | Не выводить владельца по filename, share token или соседнему событию |
| 3. Private copy | Authenticated SDK HEAD/GET, ограничение размера по типу, SHA, separate bucket/key, stable operation ID | Не менять старый объект, DB URL, ACL или CDN; не делать public fallback |
| 4. Потребители старого URL | Бизнес-БД, FSM/RAM, provider jobs, pending results, admin scripts, share tokens, CDN/cache и backup dependencies | Не считать queued безопасным только из-за пустого provider_id; не очищать submitting/ID |
| 5. Переключение | Остановленная нужная обработка, owner/provider checks, private reader, compare-and-swap | Не менять финансовые статусы/балансы/схему и не запускать новые генерации для проверки |
| 6. Отзыв public доступа | Только отдельное согласование, после доказанной готовности всех потребителей, с планом недоставленных результатов | Этот PR не меняет старый bucket policy/ACL/CDN, не удаляет объекты и не отзывает share tokens |

**Private copy и даже смена DB ref не закрывают старый публичный URL.** До отдельно выполненного этапа 6 исторические данные остаются публично доступны в прежнем контуре.

## Инвентаризация

CLI только читает SQLite и не обращается к bucket/API. Пример будущего запуска на отдельно разрешённой **тестовой копии**, не текущая команда для production:

```bash
python private_media_migration.py --help
python private_media_migration.py --database /secure/test-copy.sqlite3 --plan /secure/new-private-plan.json
```

План создаётся эксклюзивно с 0600, stdout содержит количества. Plan metadata (owner/order/key) всё равно чувствительны: не помещать в Git/PR/публичный чат. Signed/query/fragment URLs, credentials в URL, traversal/неоднозначный encoding и чужие base исключаются. WITHOUT ROWID и неизвестные структуры не переписываются.

## Подтверждение владельца

* В обычной строке — проверенный user_id/order_id.
* `photo_results` — JOIN к photo_orders.
* `cartoon_assets` — JOIN к cartoon_projects.
* shared_images, произвольные input refs, неизвестный owner, неоднозначность — quarantine `ownership-unverified`.

Автоматический whitelist ограничен служебно сгенерированными output полями и ожидаемыми prefixes: photo_results.image_url/vision-boards, cartoon_orders.result_url/videos, musical_orders.result_url/videos, audio_orders.preview_url/audio-previews, cartoon_assets.public_url/studio|multstudio. Это не доказательство права на произвольную ссылку внутри fsm_data.

`copy_one` повторно сверяет locator/hash/owner, читает через авторизованный legacy SDK и записывает новую private копию. Копирование не меняет корневую policy заказа и не ставит private marker: legacy-обработка остаётся доступной до отдельного cutover. Повтор той же операции идемпотентен; неизвестный PUT не повторяется. Пределы MIME теперь 8/16/32 MiB, поэтому крупные legacy объекты могут быть намеренно отклонены. Общий historical bulk streaming cutover для них не заявлен: это отдельная проверка, не повод поднять всем буфер до 200 MiB.

## Безопасный первый cutover

Реализован только `cutover_completed_photo`: доставленный результат завершённой фотосессии. Проверяются owner/order, частная копия и её operation/SHA, исходная строка и delivered flag. Меняется только photo_results.image_url. Повтор того же переключения возвращает already-private. Если заказ уже закреплён за legacy policy, cutover останавливается до отдельного review; immutable policy автоматически не переписывается.

Нужны подтверждения оператора: обработка остановлена, provider IDs проверены GET, RAM FSM разобраны/сохранены, нет невыясненных active jobs. `activity()` — консервативный сигнал по БД, не доказательство отсутствия внешних запросов. Manual/unknown/submitting без понятного результата требует остановки и расследования. Остальные сценарии cutover блокируются до отдельного review читателей.

Новые product policies не означают массовую миграцию старых заказов. Legacy orders без policy сохраняют legacy; сохранённый private ref/marker требует private. Policy не переписывать ради успешного старта. Уже оплаченные заказы не аннулировать и не выставлять заново.

## shared_images и незавершённые заказы

Отдельный список должен содержать: ранее опубликованный share token, исходный объект, подтверждённого владельца и законное основание публикации, всех получателей/потребителей и последствия отзыва. Нельзя молча заменить public share ссылку на private ref и считать согласие полученным. Уже скачанную копию отозвать невозможно.

Для незавершённых задач сохранить provider/job ID, stage, submitting marker, references и финансовую историю. Expired URL — повод обновить URL существующего результата GET либо обратиться в поддержку; не повторять paid POST. RAM-only административные тесты/незавершённые FSM требуют отдельного drain перед сменой режима.

## Backup и согласованность

Сохранять согласованные business DB, media registry, SQLite sidecars, `.required` marker, environment/config и local media. Версия registry 111 совместима с известной версией 0 на чтение/контролируемое дополнение; повреждённая/неизвестная схема, небезопасные права или потерянный registry — отказ. Не удалять marker для обхода ошибки и не создавать пустой registry вместо утраченного.

Не восстанавливать старую бизнес-БД при rollback кода: будут потеряны новые оплаты и заказы. Если metadata утрачены, остановить затронутые операции, восстановить проверенную согласованную копию/точечное mapping после отдельного решения и выполнить reconciliation.

Emergency mode сохраняет авторизованные чтение/доставку, запрещая новые private mutations. Старые SHA 57744a0/196d577 не являются допустимым откатом после private refs. Не делать private объекты публичными ради совместимости. [Runbook](private-media-deployment-runbook.md).


Security 1.1.2: для Yandex empty ACL нужен [профиль yandex-v1](private-media-yandex-smoke.md), проверенные controls и точные четыре правила. Пустой ACL не подтверждает privacy исторических объектов или отсутствие унаследованного IAM/object ACL. Эта доработка не копирует/не переключает объекты. При rollback Yandex reader должен содержать 1.1.2; старый f5dd8de не принимает фактический empty ACL. Формат refs/schema 111 не изменены.
