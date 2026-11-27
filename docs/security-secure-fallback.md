# Security 1.0: независимый secure-fallback

Дата: 27 ноября 2026. База: `48d4c5918013069483a2292c5e46e626798f24cb`. Ветка: `security/secure-fallback`. Основной Security 1.0: [draft PR №264](https://github.com/landinavn/Bot-Max/pull/264), `57744a0b600a9736cbaf1d00a2ccb6660869c903`.

## Реализация и scope

Это отдельная альтернатива от production, без cherry-pick/merge logging-системы №264. Runtime меняется только в `bot.py`, `max_client.py`, `fallback_logging.py`. Тарифы, платежи, очереди, FSM-правила, JSON/provider IDs и схема БД остаются исходными. Runtime и dev requirements не меняются. AST `bot.py` после исключения добавленных diagnostic calls/entry-point guard совпадает с production; бизнес-файлы, init_db и workers побайтно не менялись.

- TLS: явный системный `SSLContext`, CERT_REQUIRED, hostname/validity checks, блокировка HTTP/HTTPS→HTTP перед connect; закрытие session/connector при ошибке setup и отмене. Нет ssl=False fallback.
- `log_max_error()` больше не читает/форматирует `exc`, `.raw`, `response.text`, headers/URL; выводит только фиксированное сообщение. В службе подавляется и оно.
- Минимальная независимая политика: entry-point guard **до импортов приложения и SDK**, logging disabled, FD 1/2 → `/dev/null`; только фиксированные byte literals в приватный non-inheritable stderr duplicate. Никаких regex, разбора API responses, factory, logger facade, product/user/order IDs или случайной корреляции.
- Hooks для необработанных исключений entry point/потоков/unraisable; прямые/native/child stdout/stderr и faulthandler вывод подавлены. Ошибка установки guard завершает процесс exit 78, без запуска бота с прежними логами.
- Поддерживаемый production launcher: существующий Python `bot.py` либо `-m bot`; простой импорт не меняет output тестов/стороннего процесса. Внешние CLI и launchers, которые импортируют main, автоматически не защищаются и не должны использоваться с рабочими данными в резервном режиме.

Это режим уменьшенной наблюдаемости: fixed `polling_starting` не доказывает успешное получение MAX updates, подробных worker logs нет. Доставку/платежи/очереди проверяют по защищённым существующим записям и подтверждениям. БД, клиентские сообщения, исходники/результаты, публичные URL в storage, старые журналы, чужие процессы и Python startup/site hooks до entry point эта политика не меняет. Для произвольных новых FileHandlers/tty/file output не даётся гарантия редактирования содержимого; в проверенном runtime FileHandler отсутствует.

## Тестовые среды и воспроизводимость

Выполнялись только локальные проверки в Cloud, без VPS/production/рабочей БД/реальных credentials и без запросов в платные API. Реальные сетевые проверки используют loopback HTTPS и одноразовый синтетический CA. Сетевая обвязка запрещает Python socket connect/connect_ex вне localhost; тот же runner и ограничения используются для baseline/fallback. В sandbox self-pipe asyncio заменён на неблокирующий os.pipe; runtime бота не меняется. Все тестовые БД — временные fixtures.

| Компонент | Среда A (основная регрессия) | Среда B (совместимость VPS) |
|---|---|---|
| Python | 3.12.14 | 3.12.14 |
| maxapi | 1.2.3 | 1.2.3 |
| aiohttp | 3.13.5 | **3.14.5** |
| aioresponses | 0.7.9 | 0.7.9 |
| pytest | 9.1.1 | 9.1.1 |

Python 3.12.3 на VPS самим исполнителем не запускался; сведения о VPS smoke основного Security SHA предоставлены владельцем. Нельзя выдавать их за smoke резервного SHA. Пакеты среды B установлены в отдельный локальный venv; production venv и requirements не менялись. `requirements-dev.txt` production ограничивает aiohttp `<3.14` из-за несовместимости aioresponses; отдельная compatibility-среда намеренно использует 3.14.5 без изменения этого файла.

Команды сравнительных полных прогонов (оба checkout, один набор параметров):

```text
<venv>/bin/python <offline-test-runner> -q --timeout=90 --timeout-method=signal -o faulthandler_timeout=60 --tb=short --junitxml=<report.xml>
```

Адресные проверки: `tests/test_secure_fallback_tls.py`, `tests/test_secure_fallback_output.py`, `tests/test_bot_ssl.py`. Сохранены все исходные тестовые функции/параметры; в `test_bot_ssl.py`, одной проверке connector в `test_image_service.py` и одной параметризованной функции `test_handlers.py` старые требования CERT_NONE/распечатки response заменены безопасными assertions. Проверки retry/числа генераций/списаний в этих тестах сохранены. Остальные исходные тесты не отредактированы, skip/xfail не добавлены.

## Адресные результаты

**52 passed** на maxapi 1.2.3 + aiohttp 3.14.5:

- Реальные TLS handshakes: trusted valid certificate принят; unknown CA, expired и wrong hostname отвергнуты. Для API polling, uploads и downloads при TLS-ошибках нет принятого сервером HTTP-запроса/обхода проверки.
- Реальный MAX SDK против локального HTTPS: updates/message callback, send/edit/ack, image/file/audio/video upload, media send/download; connector/session закрыты.
- Прямой HTTP и реальный HTTPS→HTTP redirect заблокированы до запроса plain server.
- stdout/stderr/sys.__stderr__/buffer/os.write, warnings, logging.exception/extra/stack_info, дочерний процесс с исключением и faulthandler dump не раскрывают payload/traceback.
- Main/thread/unraisable exception hooks дают только fixed markers; guard idempotent, factory не меняется, неизвестные event values не сериализуются; ошибка setup fail-closed. Реальный bot entry point с синтетической ошибкой Config закрывает вывод до старта БД/API.

**8 passed под уже установленным process guard** на aiohttp 3.14.5: существующие integration tests оплаченных песен/музыкальных открыток/оживления и payment recovery. Проверены восстановление после потери FSM, повторная доставка сохранённого файла без render/create, неизвестный submit без второго POST, сохранённый task ID при timeout/restart, отмена worker и идемпотентное восстановление права. Журнал этого прогона содержит ровно `secure_fallback starting` и `secure_fallback stopped`; pytest output тоже подавлен. Это отдельная проверка работы бизнес-сценариев именно при включённом guard, а не только library-import режиме.

**18 passed под установленным process guard** на aiohttp 3.14.5: повторён весь набор реальных локальных TLS/SDK проверок `test_secure_fallback_tls.py`. Подтверждены и сетевой контракт, и работа MAX SDK при уже отключённых logging/stdout/stderr. Журнал — только fixed starting/stopped. Локальный сервер принимает синтетические bytes и не проверяет реальные MAX credentials/содержимое медиа; это не выдаётся за отправку в production MAX.

Эти тесты подтверждают конкретные покрытые сценарии, не гарантию exactly-once для всех внешних запросов. Неизвестный исход create без provider ID остаётся manual-review случаем; новая RAM-персистентность не добавляется.

## Полное сравнение

| Полный прогон | Всего | Passed | Failed | Errors / skipped |
|---|---:|---:|---:|---:|
| Production, aiohttp 3.13.5 | 3286 | 3274 | 12 | 0 / 0 |
| Secure-fallback, aiohttp 3.13.5 | 3325 | 3313 | 12 | 0 / 0 |
| Production, aiohttp 3.14.5 | 3286 | 3137 | 149 | 0 / 0 |
| Secure-fallback, aiohttp 3.14.5 | 3325 | 3175 | 150 | 0 / 0 |

Сравнение выполнено по точным `(classname, name)` из JUnit, включая параметризацию. На 3.13.5 **все 3286 исходных outcomes совпадают**, пропавших тестов нет, все 39 добавленных testcase passed. На 3.14.5 также нет пропавших тестов и все 39 новых passed; **один исходный testcase изменил outcome**: `test_ded_moroz_customer_flow.py::test_customer_complete_flow_two_audios_choose_first_and_next_invoice` один раз превысил лимит 90 секунд в полном параллельном прогоне. При отдельном последовательном повторе он прошёл на резерве и production с теми же aiohttp 3.14.5/maxapi 1.2.3. Runtime после полного прогона не менялся. Тайм-аут не скрыт/не превращён в skip; полный результат остаётся 150 failed, а повтор — отдельное свидетельство невоспроизводимости. Приписывать ему установленную причину или объявлять полный прогон 3.14.5 зелёным нельзя.

Все **149 неуспешных testcase baseline 3.14.5** остались неуспешными в полном прогоне резерва. В 127 их JUnit failure reports явно встречается `TypeError: ClientResponse.__init__() missing 1 required keyword-only argument: 'stream_writer'` — несовместимость тестового aioresponses 0.7.9 с API aiohttp 3.14.5. Остальные 22 отчёта не содержат эту строку; все они также воспроизводятся у исходного production. Их нельзя автоматически считать исправленными или целиком приписывать одной причине. Тестовую обвязку/цены/сценарии в этой задаче не переделывали. Для проверки самого transport на 3.14.5 использованы реальные локальные HTTPS-соединения/SDK без aioresponses (18 passed, включая повтор под guard).

12 исходных падений в основной среде 3.13.5:

| Файл / тестовая функция | Количество |
|---|---:|
| `test_cartoon_avatar_archive.py::test_restore_keeps_reviewed_words_and_never_generates_picture_or_music` | 1 |
| `test_custom_orders.py::test_custom_order_menu_position_and_retired_button` | 1 |
| `test_custom_orders.py::test_unpaid_legacy_long_state_cannot_create_a_new_invoice` | 1 |
| `test_ded_moroz_family.py::test_family_stats_separate_customer_interest_from_admin_tests` | 1 |
| `test_photo_payment_refresh.py::test_expired_photo_link_is_replaced_on_each_checkout` | 4 |
| `test_photo_payment_refresh.py::test_provider_error_never_returns_the_old_photo_link` | 2 |
| `test_welcome_ads.py::test_exact_main_buttons_and_welcome_copy` | 1 |
| `test_welcome_ads.py::test_stats_shows_eight_labels_and_clicks` | 1 |

Ни один из этих продуктовых дефектов/несовпадений тестовых ожиданий не исправляется скрытно в security-резерве. Считать всю регрессию успешной нельзя; локальных детерминированных новых падений на основной тестовой среде не найдено. Помимо полных прогонов, три обновлённых security assertions (параметризованный MAX error/retry + image connector) отдельно дали 3 passed на каждой версии aiohttp.

Промежуточные прогоны до обновления старых небезопасных assertions прерваны как superseded; их частичные XML не включены в эту таблицу/сравнение. Сами runtime-файлы после первоначальных адресных проверок не менялись. Итоговые JUnit, сравнение и полные тестовые логи хранятся вне Git: логи содержат синтетические payload/traceback для negative assertions и не являются production logs. В PR включены код/тесты и документация, без логов/БД/конфигурации/секретов.

SHA-256 итоговых свидетельств (длительность/пути тестовой среды в JUnit могут отличаться при воспроизведении):

| Свидетельство | SHA-256 |
|---|---|
| `baseline-313.xml` | `7a809402e4a3297cba646c41c0a0ea55d368ff8c1a7335d07da83928e9454c12` |
| `fallback-final-313.xml` | `774a27fb05b598b160452c20a845dffde12eef9445fd107082864372528026f3` |
| `baseline-314.xml` | `633e480b9dccf6a979322c3f4fa1064d90637e5ca109675eade66bfdd4bdc6f2` |
| `fallback-final-314.xml` | `7032f5297dccab3ef3541d5d1cb8242ad0295be8ae5ffbbc37d2a6da953e2fdd` |
| `security-314.xml` | `8a5584e091e372ee5c47037f6f997f6ffeaf696ca346c349a6b97bebeb61fbc4` |
| `guarded-recovery.xml` | `2118af887176011a26575429fbe235539a04a18beccdb7007dec5cccc958ca43` |
| `guarded-sdk-314.xml` | `b5c9d1a280b5d9489cdc18bcfffeca120bcd6e2df9528a7cb894ea6ffc447c24` |
| `offline-runner` | `39562423aa613dfeded9595bc1e477ef7db19f68bb311577b5b61ba0ae83c409` |


## Deployment и ограничения

Полный порядок перехода на №264, проверки заказов/RAM-FSM, backup, сверка оплат downtime, критерии успеха и откат только кода: [security-deployment-runbook.md](security-deployment-runbook.md).

Главное: действующая БД/материалы/`.env`/venv сохраняются при переключении кода. Старый backup **не восстанавливается автоматически**, новые оплаты не теряются из-за возврата прежнего файла БД. Не сбрасываются submit/running flags, не отправляются повторные платные POST. Если secure-fallback не работает, служба останавливается для отдельного устранения; production-код с отключённым TLS и подробными логами не используется как откат.

Production, VPS, Small Talk, Nginx, рабочая БД не читались/не менялись. Merge и deployment не выполнялись.
