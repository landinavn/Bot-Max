# Этап 1.3А: независимый учёт прав и read-only сверка

Статус: подготовлено для ревью. Новое ядро **не подключено** к боту, webhook,
FastAPI, оплатам или генерациям. Использованы только новые синтетические SQLite-базы.
Рабочие данные и их резервные копии не использовались. Frontend PR №257 не затронут.
Основа: `feature/web-max-linking`, `04b91be474e99922be7ced4c56f3ca22a38174b4`.
Зависимая ветка: `feature/web-entitlements`; [draft PR №262](https://github.com/landinavn/Bot-Max/pull/262) в ветку основы.

## Аудит фактического учёта

Проверены `database.py`, `handlers.py`, `prodamus.py`, `webhook.py`, продуктовые
очереди, `ded_moroz*.py`, `promo1111.py`, `black_friday.py`, `promo_service.py`,
`payment_incidents.py`, `payment_recovery.py`, восстановление мультфильмов и тесты.
Ниже цены приведены для описания проверенного кода, **не реализованы повторно**
в новом каталоге. Скидки и исторические допустимые суммы проверяет legacy-код.
Сумма прошлых платежей никогда не используется как текущий доступный остаток.

### Полная матрица: тариф, источник истины, единица, начисление, расход

| Продукт | Источник тарифа | Источник истины права | Единица | Начисление | Расход |
|---|---|---|---|---|---|
| Открытка с фото и карта желаний | `prodamus.PACKAGES`: 1/3/5/15 за 99/199/299/799 ₽; персональные акции отдельно | Текущий `users.balance`; `payment_orders` — доказательство происхождения части средств | Общий image credit для обоих продуктов | `add_credits`, подтверждённый платёж атомарно с балансом | `handlers.run_generation` → `deduct_credit` **после** готовой картинки; условный UPDATE `balance>=1` |
| Бесплатная первая картинка | `grant_free_trial`, без оплаты | `users.balance` и `users.free_trial_used` | Тот же общий credit | Один раз `balance+1,free_trial_used=1` в транзакции | Общий debit; флаг не доказывает фактическое использование |
| Акция 11.11: 111 и 333 | `promo1111.PACKAGES`: 3/11 за 111/333 ₽ | `users.promo_image_balance`; связанный `payment_orders` | Ограниченные image credits, используются открыткой **и картой желаний** | `add_credits`, отдельный счётчик; 111 — одноразовый checkout | `deduct_credit(allow_promo_images=True)` сначала расходует этот счётчик |
| Акция 11.11: 799 | `promo1111.PACKAGES`: 15 за 799 ₽ + 1 оживление | `users.balance`, `free_animation_count`, затем материализованный bonus order | 15 image credits и отдельное animation right | Обе части атомарно в `add_credits` | Картинки — общий debit; подарок — `claim_animation` атомарно уменьшает счётчик и создаёт оплаченный zero-price заказ |
| Фотосессия | `PHOTO_TARIFFS`, `PHOTO_PACKAGE_SIZES`: ключи **590/990/1490**, цены **490/690/990 ₽**, 3/5/7 кадров | `photo_orders.status='paid'` + уникальные слоты `photo_results` | Кадр в конкретном оплаченном заказе | `mark_photo_paid`, очередь оплаченных пакетов | `save_photo_result` занимает слот при сохранении, даже если `delivered=0`; баланс = размер минус все сохранённые кадры |
| Короткая и полная песня | `AUDIO_TARIFFS`: `fun` 159 ₽, `gift` 290 ₽; `valid_audio_tariff` знает исторические цены | `audio_orders`, платёж и durable submission/result/delivery flags | Один заказ песни, не число полученных вариантов | `mark_audio_paid`; admin `free_access` не платёж | Состояния `submit_started/job_id/tracks_json/delivered`; нет универсального списания `users.balance` |
| Музыкальная открытка и апгрейд | `MUSICAL_TARIFFS`: direct 299 ₽, upsell 149 ₽ | `musical_orders`, `input_ready/prepaid`, durable draft/results/delivery | Один заказ; апгрейд связан с уже готовой открыткой | `mark_musical_paid` | `claim_musical_order` и worker; `delivered=1` завершает услугу |
| Мульт-открытки для мамы | `CARTOON_TARIFFS`: mother и mother_bday 590 ₽ | `cartoon_orders` и сохранённые song/avatar/story/tasks | Один заказ с песней и сценами | `mark_cartoon_paid` | `finish_cartoon_order`: completed либо manual; сохранённые сцены не новые права |
| Остальные мульт-открытки / исторический long | `CARTOON_TARIFFS`: short 390 ₽, long 1290 ₽ (наличие ключа не означает публичную доступность нового заказа) | `cartoon_orders` | Один заказ соответствующей темы | `mark_cartoon_paid` | Та же машина исполнения; не конвертируется в image credits |
| Оживление фотографий | `ANIMATION_TARIFFS`: intro 199 ₽, one 249 ₽, three 599 ₽, five 899 ₽ | `animation_orders.quantity` минус **все** `animation_jobs` | 1/3/5 слотов внутри пакета | `mark_animation_paid`; подарок `promo1111_bonus`, admin orders отдельно | `queue_animation_photo`: `BEGIN IMMEDIATE`, уникальные `(order,slot)` и `(user,source_message_id)`; слот занят ещё до POST |
| Дед Мороз одному ребёнку | `ded_moroz_sales.price_at`, календарь Москвы: исторически 990 ₽ до 18.11, затем 1490 ₽ в ноябре, 1990 ₽ в декабре | `ded_moroz_payments` + уникальный `claimed_id` → `ded_moroz_orders` | Одно видеопоздравление | `mark_paid` и `_claim` **в одной транзакции** | Claim материализует услугу; версии озвучки, правки и видео — части одного заказа |
| Дед Мороз нескольким детям | `family_price_at`: исторически 990 ₽ 19–22.11, затем 1490 ₽, декабрь 1990 ₽ | Те же payment/claim таблицы + family variant и число детей | Одна семейная услуга на 2–6 детей, **не 2–6 кредитов** | Та же атомарная обработка | Одна услуга со своей машиной исполнения |
| Ручное восстановление платежа | `payment_recovery.claim_photo_recovery`, ранее проверенный конкретный случай | `payment_recoveries` + созданный оплаченный `photo_orders` + уже увеличенный `users.balance` | Фотопакет и один gift image credit | Все изменения и закрепление receipt атомарны | Обычные фото/image механизмы; повторный учёт recovery row запрещён |

### Полная матрица: бонусы, повтор, состояния, восстановление, идемпотентность, конкуренция, новая модель

| Продукт | Бонусы и повторные покупки | Состояния | Возврат / восстановление | Идемпотентность | Конкурентный риск legacy | Представление нового ядра |
|---|---|---|---|---|---|---|
| Общие картинки | Любое число отдельных оплаченных пакетов; цена промо не новая валюта; admin bypass | payment pending/paid; текущий остаток смешивает платные, trial и подарочные единицы | Cash refund API не найден; incident/recovery — отдельные механизмы | `order_id`, provider ID, проверка владельца и credits; paid retry без повторного начисления | Условный debit защищает от минуса, но provider вызывается раньше; нет общей durable reservation между каналами | `image_credit`, продукты postcard + vision_board, отдельные лоты происхождения |
| Trial | `grant_free_trial` ровно один grant; `mark_free_trial_used` только флаг | Флаг 1 = **предоставлен**, а не обязательно потрачен | Автоматического сброса права нет | Условный UPDATE `free_trial_used=0` | Один legacy grant защищён транзакцией; будущая web-проверка не должна выдавать второй | `image_credit/bucket=trial`; в этом PR не выдаётся |
| 11.11 image-only | Купленные единицы не истекают по тексту акции; срок акции относится к покупке; 111 одноразовый | Pending/paid, promo wallet | Не выводить expiry из даты покупки/акции | Уникальный checkout 111 и обычная платёжная дедупликация | Та же проблема POST до durable debit | `image_credit/bucket=promo`, scope обоих image продуктов |
| 11.11 799 и оживление | 15 + 1 разные единицы; после claim подарок уже удалён из счётчика | Counter → zero-price paid bonus order → jobs | Manual job удерживает слот; не возвращается автоматически | Повтор claim переиспользует ещё не начатый bonus order | Claim и слот защищены SQLite; неизвестный POST требует проверки | Один event, два атомарных grant; материализованное право не дублирует counter |
| Фотосессия | Пакеты независимы; repeat offer — скидка на новую покупку, не бесплатный кадр | awaiting_payment → queued → completed; delivery отдельно | Сохранённые слоты переиспользуются; недоставленный результат можно заменить, доставленный — нельзя | PK `(order_id,slot)`; paid receipt защищён | Генерация слота опирается в том числе на in-memory/FSM; возможен POST без сохранённого результата. Остаток показываем с предупреждением | `photo_frame`, order scope; сохранённые кадры consumed, остаток требует проверки in-flight |
| Песни | Один разрешённый regeneration при выполнении условий, `previous_tracks_json`; это повтор исполнения услуги, не второй пакет; `free_access` — admin | submit_started → job_id → tracks → delivered, failed/running | `submit_started` без ID: **не повторять POST**; готовые tracks переиспользуются | Payment idem; durable submission barrier; regeneration_used | Требуется сохранить единый generation/version ref; при переносе включённый повтор отдельно согласовать | `audio_order` qty1, song_short/song_full, order scope; не угадывать остаток из paid |
| Музыкальная открытка | Direct/upsell — разные ограничения услуги; повторная покупка — новый заказ | pending/paid, input_ready/prepaid, running/failed/delivered | Восстановление по saved draft/media; неизвестный вложенный POST требует review | mark_paid + условный claim, сохранённые результаты | running сбрасывается после перезапуска; одного `paid && !delivered` мало для нового запуска | `musical_order` qty1, direct/upsell scope; незавершённые held/manual |
| Мульт-открытки обоих типов | Переписывание, варианты песни, архивы и сохранённые сцены — исполнение одного заказа | pending/paid/completed/manual | mother recovery, identity repair, сохранённые provider IDs; повтор может быть платным — не считать новой покупкой | Order/receipt, checkpoints и source binding | Нельзя отпускать/переисполнять неизвестный image/video POST; FSM payload намеренно не импортируется | `cartoon_order` qty1, отдельные mother/mother_bday/short/long scopes |
| Оживление | intro price только первой покупке; promo/admin отдельно; новые пакеты независимы | queued/creating/processing/ready/delivered/manual | Saved ID — poll; ready — повтор доставки; creating без ID/manual удерживает слот | Уникальность слота и входного сообщения, атомарный enqueue | Межпроцессный enqueue защищён; неизвестный POST не даёт новый слот | `animation` количественные лоты; paid/bonus раздельно; все jobs уменьшают legacy available |
| Дед Мороз: single/family | Один включённый пересмотр по условиям; family дети не отдельные credits; admin bypass отдельно | input, audio_queued/audio_publish/audio_ready, editing, video_queued/polling/download/render, done/manual/cancelled | Resume по version/provider/media, неизвестный POST требует review; cash refund не описан | Receipt/claimed_id уникальны, claim и paid атомарны; owner/variant проверяются | Не считать claimed input новым неиспользованным балансом; paid без claim — аномалия | `ded_moroz_order` qty1, ded_single/ded_family; scope заказа; done consumed, остальные held/ambiguous |
| Recovery | Gift уже попал в общий wallet; recovery не отдельная новая покупка | Recovery receipt/order ownership, notification flags | Существующая ручная процедура конкретного случая | provider ID + order number + photo order уникальны; связь с владельцем | Нужна межпродуктовая проверка receipt; legacy recovery проверяет шесть основных таблиц, не отдельный Ded Moroz payment registry | Отчёт происхождения/дубликатов, **не ещё одно начисление** |

`mark_photo_paid`, `mark_audio_paid`, `mark_musical_paid`, `mark_cartoon_paid`,
`mark_animation_paid`, `add_credits` и Ded Moroz `mark_paid` используют короткие
SQLite-транзакции и проверки повторного provider payment ID. `webhook.py` проверяет
подпись, параметры и сохранённый заказ, затем выбирает соответствующий обработчик;
отклонённые уведомления записывает в incidents. Это не основание автоматически
переподключить webhook к новому сервису: нужна отдельная схема переключения.

### Остальные обнаруженные таблицы

| Таблицы / группа | Роль и правило чтения |
|---|---|
| `active_promos`, `promo_redemptions`, `promo_offers`, поля repeat/song promo | Цена, eligibility, резервирование скидки, отложенное уведомление. Не генерации и не новый grant |
| `payment_incidents` | Ошибки и повторы уведомлений; не подтверждённая оплата. Recovery worker использует обычный mark_paid, не второй grant |
| `payment_recoveries` | Receipt ownership, восстановленная покупка и уже выданный подарок. Только источник проверки происхождения |
| `pending_videos` | Manual fulfillment с готовыми avatar/song; не самостоятельное доказательство покупки |
| `shared_images`, `cartoon_avatar_archive`, `mother_admin_sessions` | Медиа и восстановление админских тестов; не остатки |
| `ded_moroz_audio_versions`, `ded_moroz_avatar`, `ded_moroz_mobile_jobs`, `ded_moroz_reviews` | Версии/ресурсы/доставка/отзывы одной услуги. Не оплаченные пакеты |
| `promo1111_recipients`, `mother_broadcast_recipients`, `ded_moroz_broadcast`, `black_friday_recipients`, `service_notice_recipients` | Рассылки/eligibility, не начисления |
| `user_counters`, `global_counters`, `stats_migrations`, `mother_events`, `user_activity_days`, поля статистики users | Статистика, не финансовый источник истины |
| `cartoon_projects`, `cartoon_characters`, `cartoon_episodes`, `cartoon_scenes`, `cartoon_versions`, `cartoon_jobs`, `cartoon_assets`, `cartoon_job_events`, selection/restoration/speech/editing/validation tables | Администраторская Мультстудия и её ресурсы. Не customer balance; не импортировать job как покупку |
| `accounts`, `account_identities`, auth/browser/linking tables | Владение и авторизация, не факт покупки. Привязка не начисляет credits/trial |

`black_friday.py` меняет только quote/сохранённую сумму в разрешённом периоде,
не количество прав. Платёжные ссылки, нулевые суммы в merchant UI и истечение
checkout не доказывают ни списание, ни возврат. Статусы cancelled/refunded,
если обнаружены в неизвестной схеме, требуют ручной проверки.

## Архитектура

`entitlements/catalog.py` содержит только виды единиц и продуктовые scopes, **без цен**.
`models.py` — внутренние утверждения и безопасный будущий DTO.
`repository.py` — SQL-инварианты и атомарные операции; `service.py` — внутренний
use-case boundary. `legacy_reader.py` и `reconciliation.py` выполняют только чтение.
Нет MAX/FastAPI/FSM/Prodamus/provider импортов, HTTP-клиентов или default DB path.
Конструкторы не открывают файлы. Импорт не читает env/config, не запускает миграции.

Сервис использует существующие `accounts.models` и `accounts.storage`:
канонический UUID владельца, явный путь к **существующей** SQLite-базе, foreign keys,
короткие `BEGIN IMMEDIATE`, rollback при исключении/отмене, безопасный `AccountStoreBusy`.
Публичная авторизация не реализована повторно. **Вызывающий доверенный серверный
адаптер обязан авторизовать account_id; прямой доступ браузера к этим методам запрещён.**

### Таблицы v5

| Таблица | Назначение |
|---|---|
| `entitlement_events` | Неизменяемое подтверждённое событие, owner, source, external ID, fingerprint, verified timestamp; UNIQUE(source,external_id) |
| `entitlement_grants` | Неизменяемый lot: kind, paid/bonus/promo/trial/compensation bucket, qty, products, purchase/order refs, optional expiry |
| `entitlement_reservations` | Устойчивый reserve, owner, product/kind/order, qty, generation_ref, state; UNIQUE(owner,generation_ref) |
| `entitlement_allocations` | Разбиение reserve по grant lots, целые положительные количества |
| `entitlement_commands` | Глобальный idempotency key → action, owner, fingerprint и сохранённый result ID |
| `entitlement_ledger` | Append-only операции и parent linkage: grant/reserve/submitting/uncertain/commit/release/compensate |

История защищена зарегистрированными триггерами от UPDATE/DELETE. Reservation
разрешает только переходы state, остальные поля неизменяемы; удаления нет.
Allocation trigger проверяет владельца, вид, scope и capacity лота/резерва.
Уникальные order/purchase refs на вид права не дают повторно начислить одну
покупку другим receipt/владельцу. Компенсационный lot ссылается на исходный debit
и отдельно исключён из этих purchase uniqueness indexes.

`package_ref` — **уникальный идентификатор конкретной покупки**, не SKU вроде «3 фото».
`order_ref` — ограничение расхода конкретным заказом. Платёж с разными видами прав
может разделять package_ref, например image + bonus animation. Сервис допускает
несколько пакетов одного владельца, не смешивает разные виды единиц.
Количество в одном lot/reserve ограничено 1…1 000 000 000 и строго `int`, не `bool`
и не `float`. Денежные цены в ядре отсутствуют. Время — целые Unix seconds.

DDL/checksum v1–v4 сохранены. В `accounts.migrations.versions()` только добавлена v5.
Миграция вызывается исключительно явно; старт бота/FastAPI её не запускает.
Новая версия будет видна стандартному readiness/schema inspector как pending
до явного применения. Это изменение не следует выкатывать без отдельного решения.
Отключение использования модуля не требует удаления таблиц или истории.

### Методы

| Метод | Результат / ограничения |
|---|---|
| `grant(VerifiedGrantEvent, idempotency_key=...)` | Tuple opaque grant IDs; все права одного события атомарно |
| `reserve(account_id, kind, product, quantity, generation_ref=..., order_ref=..., idempotency_key=...)` | Сохранённый Reservation; баланс и allocation в одной транзакции |
| `mark_submitting(...)` | Durable barrier **до** будущего платного POST |
| `mark_uncertain(...)` | Удержание при timeout/неизвестном результате; повтор POST запрещён |
| `commit(..., verification=VerifiedOutcome('success', evidence_ref), ...)` | Окончательное списание существующего резерва |
| `release(..., verification=VerifiedOutcome('failed'/'not_submitted', evidence_ref), ...)` | Возврат только при подтверждении; not_submitted разрешён только до submitting |
| `get_available(account_id, kind, product, order_ref=...)` | Только новые units подходящего scope, без legacy |
| `get_reservation(account_id, reservation_id)` | Только принадлежащий owner резерв; чужой ID даёт тот же NotFound |
| `list_packages(account_id)` | Allowlisted PackageView; нет payment/provider/order refs, metadata, чужих IDs |
| `compensate(account_id, commit_operation_id, quantity, evidence_ref=..., expires_at=..., idempotency_key=...)` | Только support-internal коррекция: новый lot, исходный debit не меняется |

Verified dataclasses — **утверждения доверенного компонента**, не криптографическая
проверка платежа. Нельзя просто преобразовать JSON браузера с `paid=true` в это
утверждение. В этом этапе события только синтетические. Будущий payment adapter
проверяет подпись/статус/amount/currency/сохранённый сервером order/владельца/состав
продукта и фиксирует namespace `payment.prodamus`; клиент не задаёт количество.
Canonical provider ID, состав и verified timestamp сохраняются в durable inbox:
при retry повторяется тот же payload, а не пересобирается событие с новым временем.
Регистр/формат provider ID нормализует конкретный adapter по контракту провайдера;
generic ядро не угадывает, являются ли разные строки одним идентификатором.
Для metadata разрешены только технические correlation_id/reason_code/policy_version,
без пробелов, URL, email, изображений и произвольных payload. Финансовый ledger
не хранилище пользовательского контента. Компенсация требует авторизованного
support-решения и явного выбора expiry; это **не реализация cash refund**.

### Состояния и конкурентные гарантии

```text
reserve → reserved → submitting → uncertain
              │          │           │
              └──────────┴───────────┴──→ committed (confirmed success)
              └──────────┴───────────┴──→ released  (confirmed failure)
```

Из terminal-состояния в другое перейти нельзя. `not_submitted` допустим только
из reserved. Unknown/elapsed time не основание для release. Истечение grant expiry
не снимает уже созданный резерв: его можно завершить после expiry; освобождённые
просроченные единицы не становятся доступны вновь. Автоматических sweepers нет.

Доступность вычисляется по immutable grant qty минус allocations активных и
committed reserves. Отдельного изменяемого balance нет. Expiry-first allocation,
затем стабильный порядок lots. Проверка остатка и запись резерва находятся под
одной SQLite write transaction; нет расчёта в Python перед BEGIN и нет зависимости
от asyncio.Lock. Конкуренция проверена в разных соединениях и отдельных процессах.
SQLite capacity/check/foreign-key constraints дополняют сервисные проверки.
При busy запрос не записывается частично: повторяется с тем же ключом после backoff.
Не нужно держать SQLite-транзакцию во время внешнего API-вызова.

Каждый command key связан с owner/action/payload fingerprint. Точный retry
возвращает прежнюю сущность (с её актуальным состоянием), changed payload — Conflict.
Повтор reserve с тем же generation_ref и новым ключом возвращает тот же резерв,
включая released/submitting/uncertain — это **не разрешение повторять POST**.
Один receipt не создаёт второй event при новом command key. Terminal retry не
создаёт повторный ledger debit/release. Compensation суммарно не больше исходного
commit; receipt support-решения также дедуплицируется.

Прямой писатель с произвольным SQLite/DDL-доступом остаётся доверенной частью
системы: триггеры не защита от администратора, способного DROP TRIGGER. Schema
inspector обнаруживает подмену DDL/checksum, отсутствующие и лишние triggers/indexes.

## Read-only legacy adapter и dry-run

`LegacyReader.read(account_id)` не принимает MAX ID. В read-only snapshot он:

1. Находит **уже существующий** account и verified MAX identity; проверяет timestamp
   и канонический положительный 64-битный subject. Web-only account не становится MAX account.
2. Проверяет legacy users key, существование пользователя, ожидаемые таблицы,
   имена/типы колонок, payment order keys, количества/флаги/слоты и ownership claims.
3. Читает только перечисленные технические поля через SELECT, без `SELECT *`.
   `mode=ro`, `PRAGMA query_only=ON` и read transaction исключают запись.
4. Выдаёт отдельные wallet/order observations. Не считает pending оплатой, paid
   без receipt/paid_at — автоматически подтверждённой покупкой, а все paid — неиспользованными.
5. Различает известный zero-price promo/admin animation и денежный платёж. Непонятные
   free_access/admin origins остаются review, не становятся paid автоматически.

При неизвестной структуре или несовместимых слотах — LegacySchemaError; при отсутствии
связи — UnverifiedLink. Неизвестные статусы отражаются как unknown, без available.
У фотосессии остаток по saved slots известен, но **может уже исполняться POST без
результата**: предупреждение сохраняется, transfer blocked. У песен/мультфильмов
вложенный draft/FSM намеренно не читается; одного `running=0` недостаточно для safe resume.
Готовые, ещё не доставленные кадры уже consumed; animation jobs удерживают свои слоты.

`reconcile(path, account_id)` в одном read snapshot получает legacy и новые lots,
ищет повторные provider refs между продуктами и совпадения с новыми payment events,
выдаёт ambiguities/unknown_states/discrepancies/transfer_plan. Провайдерские refs
доступны только этому внутреннему отчёту, не PackageView. Нет `total_available`,
`apply`, `import` и автоматического сложения двух источников. **Все** предложенные
строки плана имеют `BLOCKED_MANUAL_REVIEW`; `execution_allowed=False`.
Recovery/incident/promo/asset таблицы отмечены как не создающие дополнительный grant.
Адаптер не устанавливает paid origin смешанного wallet по сумме истории.

Синтетический проверочный пример `test_readonly_dry_run_detects_cross_product_and_new_event_duplicates`:

| Проверка | Результат |
|---|---|
| Legacy wallet | 7 shared image + отдельные 3 promo image и 2 bonus animation |
| Новые права | Отдельный lot из 3 image units |
| Один receipt встречается в двух legacy продуктах и новом event | 3 possible-duplicate observations |
| Суммарный «7+3» баланс | Не вычисляется; источники раздельны |
| План переноса | Все строки blocked/manual, выполнение запрещено |
| Данные после dry-run | SQL dump и байты SQLite идентичны исходным |
| Media/lyrics/private links | Не прочитаны и отсутствуют в отчёте |

Это демонстрация на выдуманных данных, не отчёт о настоящих клиентах.

## Бесплатные предложения и решения владельца

Текущее `free_trial_used=1` означает, что welcome-grant уже предоставлялся.
Нельзя выдавать второй trial после email/MAX linking, даже если невозможно доказать,
что первый был потрачен. Отдельный `mark_free_trial_used` также может выставить флаг
без увеличения balance. В смешанном balance нет точной разбивки остатка trial/paid.
Email не удостоверяет уникальность физического лица. Регистрация, linking,
чтение legacy и этот модуль **не начисляют** trial автоматически.

До включения потребуются решения:

- Давать ли trial web-пользователю до MAX link; как учитывать уже потраченный web
  trial при обнаружении старого MAX welcome-grant; запрет автоматического суммирования.
- Как подтвердить paid/bonus/trial происхождение смешанных legacy wallets.
- Как переносить право на включённый regeneration/редактирование и версии услуги.
- Правила cash refund/chargeback/принудительной компенсации при уже потраченном праве;
  не переводить один продукт в другой и не придумывать отрицательный balance.
- Как завершать старые unknown POST/manual/cancelled orders; нельзя автоматически
  освобождать удержанные units или повторять платный запрос.
- Кому доступны support-compensation и namespace событий, политика evidence retention,
  сроки хранения операций linking и доступа к техническим аудитам.

## Стратегия будущего подключения MAX и веба

В этом PR стратегия только описана, live adapters и importer отсутствуют.

1. Утвердить scopes, trial/refund/regeneration политику и ownership links. Сделать
   синтетическую репетицию, затем отдельно согласовать доступ к рабочим данным.
2. Добавить durable payment inbox с уникальным provider receipt ID, проверкой подписи
   и параметров. Новые подтверждённые оплаты во время cutover сохранять; не терять
   их из-за временного закрытия расхода. Один router назначает событию **одного** получателя.
3. Ввести устойчивый fence/source-of-truth marker по owner/product. До cutover
   авторитетен legacy; после — ledger. Оба интерфейса обращаются к одному источнику.
   Запрет независимого dual-write/dual-spend; нет fallback «ledger пуст → тратить legacy».
4. Поставить на паузу новые submissions выбранного owner/product. Завершить/проверить
   in-flight provider IDs; неизвестные POST остаются held. Снять согласованный snapshot
   с watermark событий, проверить saved outputs/bonus materialization/recovery receipts.
5. Подготовить идемпотентный importer отдельно: ключ из snapshot/owner/product/order/slot,
   зафиксированный import batch и происхождение. Wallet нельзя восстановить просто
   суммированием payment_orders. Не переносить старую покупку и её текущий wallet дважды.
6. Сверить available/reserved/consumed/expired, ownership, дубликаты, manual exclusions
   до и после; применить только утверждённый план с машинно проверяемым watermark.
7. Одной транзакцией переключить source marker. Inbox replay проводит каждый receipt
   один раз в назначенный ledger; старые обработчики начисления/дебита уже fenced.
8. Будущие очереди: reserve → durable submitting barrier → POST → durable provider ID.
   При неизвестном POST — hold и reconciliation, не retry. Сохранить correlation/generation
   ref до запроса. Poll/delivery retry не новые платные генерации.
9. Откат: выключить новые submissions, оставить inbox и audit, остановить dual paths,
   сверить операции **после** watermark. Нельзя просто восстановить старые balances:
   нужно отразить post-cutover grants/holds/commits/compensations и незавершённые IDs
   в выбранном авторитете. Иначе возможны повторное начисление и расход.

## API, TLS и включение

Новых HTTP routes нет. PackageView для будущего кабинета возвращает opaque lot ID,
kind, products, source bucket, available/reserved/consumed/expired, статус права и
optional expires_at. Внутренние payment/order refs, provider IDs и metadata исключены.
Полный customer balance endpoint допускается только после утверждения cutover.

`MAX_LINKING_ENABLED`, browser/auth flags не включены; `bot.py`, transport/config/TLS
код не изменён. Обязательный `verify_ssl=True` для будущей активной MAX-привязки
сохранён. Нельзя обходить проблемы сертификата через `verify_ssl=False`.
Отдельный предрелизный TLS compatibility plan:

- Проверить доверенную цепочку/hostname/expiry и корректное CA bundle в окружении.
- Проверить polling, start/callback/text, linking code/reply, отправку/редактирование
  сообщений, фото/file/audio/video upload/download, public example и готовый результат.
- Проверить proxy/no-proxy, redirects только к допустимым HTTPS-hosts, timeouts,
  expired/wrong-host/untrusted certificates, отсутствие downgrade/retry без проверки.
- Убедиться, что TLS failures сохраняют durable jobs/holds, не повторяют неизвестный
  платный POST и не раскрывают credentials/приватные ссылки в логах.
- Smoke test выполнять отдельно на разрешённых тестовых аккаунтах и бесплатных/mock
  providers; включение flag/deployment требует отдельного решения.
- Утвердить retention, purge/anonymization, audit access и необходимость хранения
  `account_link_operations` до production включения. Этот PR данные не очищает.

## Проверки

Точные команды, результаты baseline/новой ветки, список старых падений и diff
приведены в [web-entitlements-checks.md](web-entitlements-checks.md).
Полный прогон с сохранёнными прежними падениями **не считается успешным**.
Ни deploy, ни merge, ни migration реальных остатков этим этапом не разрешаются.
