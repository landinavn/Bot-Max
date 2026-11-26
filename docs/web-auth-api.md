# Браузерная авторизация: этап 1.2А.1

Зависимый PR от `feature/web-core-auth` / `4c8207910bf27a19b1f6eeb666e4f35a4b22c0b2`. На момент начала работы PR #255 и #256 открыты и не слиты. Frontend развивается независимо и сюда не включён. Привязка MAX, объединение аккаунтов, баланс, платежи и генерации не реализуются.

## Архитектура и границы

`web_api.app:create_app` — application factory без глобального приложения, миграций, чтения конфигурации MAX и фоновых процессов при импорте. Фабрика принимает `APISettings`, `EmailSender`, тестовые часы и `AuthSettings`. Используются существующие `AuthService`, `AuthRepository`, `AccountRepository`. Ни MAX_BOT_TOKEN, ни bot.py/handlers/FSM для API не нужны.

`web_api/config.py` — отдельные настройки; `security.py` — cookie/CSRF/pre-auth/доверенный IP; `dependencies.py` — проверка аккаунта; `schemas.py` — строгие модели; `routes/` — auth/account/health. `auth/smtp.py` — SMTP-адаптер. `web_api.cleanup` — отдельная одноразовая очистка. FastAPI/Pydantic/Uvicorn/aiosqlite находятся в `requirements-web.txt`, тестовые зависимости — `requirements-web-dev.txt`. Проверенные версии зафиксированы; существующие requirements бота не меняются.

В production планируется один Uvicorn worker, loopback, Nginx на том же origin. Нет Redis, Celery, PostgreSQL или постоянно работающего Node.js. Сетевые операции SMTP выполняются после завершения SQLite write-транзакции. Соединения короткие, таймаут SQLite по умолчанию 1 с; busy даёт безопасный 503.

## HTTP-контракт

Все ответы имеют `Cache-Control: no-store`. Ошибка единообразна: `{"error":{"code":"csrf_denied"}}`. В ответе нет исходных исключений, SQL, путей, email-кодов, сессионных токенов и данных чужих аккаунтов. Production debug выключен, `/docs`, `/redoc`, `/openapi.json` не опубликованы.

| Метод и путь | Вход | Успех |
|---|---|---|
| GET `/api/v1/health/live` | — | `{"status":"ok"}` |
| GET `/api/v1/health/ready` | — | `{"status":"ok"}` или 503 |
| GET `/api/v1/auth/csrf` | cookie, если есть | `{"csrf_token":"<HMAC>"}` и pre-auth cookie для нового браузера |
| POST `/api/v1/auth/email/request` | `{"email":"alice@example.test"}` | `{"challenge_id":"<uuid>","expires_in":600,"status":"requested","message":"Запрос принят. Если письмо придёт, используйте код из него. Доставка не гарантируется."}` |
| POST `/api/v1/auth/email/verify` | `{"challenge_id":"<uuid>","code":"123456"}` | `{"account":{"id":"<uuid>","created_at":"<timestamp>"},"csrf_token":"<new HMAC>"}` + session cookie |
| GET `/api/v1/me` | session cookie | `{"id":"<uuid>","created_at":"<timestamp>"}` |
| POST `/api/v1/auth/logout` | `{}`, session cookie | `{"status":"ok"}`, отзыв сессии и удаление cookie |
| POST `/api/v1/auth/logout-all` | `{}`, session cookie | `{"status":"ok"}`, отзыв всех сессий аккаунта и удаление cookie |

POST требует `Content-Type: application/json`, точный `Origin` и `X-CSRF-Token`. Неожиданные поля запрещены, типы не приводятся автоматически. Email ограничен 254 символами и ASCII-форматом существующего ядра; код — строка из шести ASCII-цифр. `source` и `account_id` не принимаются. Query-параметры в чувствительных операциях отвергаются. В `/me` владелец всегда определяется сервером по сессии.

Статусы: 400 invalid_code/host_denied; 401 unauthenticated; 403 origin_denied/csrf_denied/preauth_required/source_denied; 409 already_authenticated; 413 body_too_large; 415 json_required; 422 invalid_request; 429 rate_limited; 503 store_unavailable/not_ready/service_unavailable. 404 тоже имеет безопасный JSON. Тело ограничено 4096 байт **до** JSON-разбора, включая chunked-запросы. Не отдавать токены/входные данные в validation errors.

Готовность проверяет схему v3 без миграции. Параметры SMTP валидируются при явном создании адаптера; ready не подключается к SMTP и не гарантирует доступность провайдера/доставку. Live не открывает БД. Проверка схемы включает существующий контроль DDL/FK; при росте БД нужно измерить её стоимость и ограничить частоту health polling.

Готовый контракт для frontend: `docs/web-auth-openapi.json` (OpenAPI 3.1). Его можно пересоздать без БД/SMTP; Pydantic-схемы, cookie/CSRF security schemes и Origin включены:

```bash
python -m web_api.openapi /tmp/wish-auth-openapi.json
```

Экспорт использует случайный временный секрет и FakeEmailSender только в режиме test; БД и SMTP не открываются. Frontend может генерировать типы из этого файла. Не размещать техническую документацию публично до отдельного решения.

## Схема входа и модель угроз

1. Frontend делает GET `/auth/csrf` с `credentials: 'same-origin'`. Если активной сессии нет, сервер создаёт случайный 256-битный pre-auth идентификатор, время выдачи и HMAC. Cookie живёт 15 минут. JSON содержит отдельный HMAC CSRF, а не значение cookie.
2. Запрос кода отправляет email, CSRF-заголовок и браузерную cookie. Сервер определяет source по реальному peer/доверенному proxy. HMAC контекста сохраняется вместе с challenge в одной транзакции. Открытой pre-auth cookie в БД нет.
3. Проверка требует тот же browser context, code и challenge. Чужой браузер с известным challenge и даже правильным кодом не сможет войти. Время контекста и его одноразовость проверяются внутри той же транзакции, где погашается код и создаётся случайная новая сессия.
4. CSRF bootstrap обновляет уже использованный контекст (например, при потере успешного HTTP-ответа) или контекст, оставшегося срока которого не хватит на полный TTL кода. Запрос кода со старым почти истёкшим контекстом отклоняется до SMTP, после чего frontend должен обновить CSRF и начать новый запрос. После успеха контекст отмечается использованным, остальные его challenges аннулируются, pre-auth cookie удаляется. Повторное использование старой подписанной cookie не создаст новый challenge и не подтвердит код.
5. Новая выдача кода в том же браузере аннулирует его предыдущие запросы, включая смену email. Вкладки разделяют cookie: последняя успешно созданная заявка актуальна; старые ответы могут дать invalid_code, после входа остальные вкладки обновляют `/me` и CSRF. При одновременных verify только одна транзакция создаёт сессию.
6. Активная сессия запрещает незаметный повторный вход/смену аккаунта (409). Сначала нужен явный logout с CSRF. Новый session token всегда выбирается сервером; значения клиента не принимаются.

Cookie: `__Host-wish_session` и `__Host-wish_preauth`, `Secure; HttpOnly; SameSite=Lax; Path=/`, без Domain. Они не распространяются на Small Talk или платёжный домен. Max-Age сессии согласован с абсолютным временем ядра (30 суток), но сервер дополнительно проверяет idle expiry (7 суток), отзыв и срок. Удаление использует те же имя/Path/Secure/SameSite и Max-Age=0. Дубли защищённых cookie отвергаются. Сессионный токен не возвращается в JSON, не кладётся в local/sessionStorage, URL, события аналитики или логи.

CSRF — HMAC, привязанный к текущей сессии либо подписанной pre-auth cookie. Его нельзя навязать через подконтрольную злоумышленнику обычную cookie. При успешном входе CSRF меняется. Получать его следует заново после входа/выхода или ошибки истёкшего контекста; хранить только в памяти вкладки. Он не заменяет защиту от XSS: frontend должен избегать небезопасного HTML и утечек токенов.

Origin для всех изменяющих запросов обязателен и должен точно совпадать с `https://app.wish-bot.ru` в production. Отсутствующий, null, посторонний или повторяющийся Origin отвергается; sibling-домены не доверены. Запросы `Sec-Fetch-Site: cross-site` также отвергаются. GET bootstrap допускает отсутствие Origin (нормальное поведение same-origin GET браузера); он не создаёт аккаунт/письмо и защищён Same-Origin Policy, no-store, отсутствием production CORS и запретом cross-site Fetch Metadata. Небраузерный клиент должен сознательно пройти cookie/CSRF и отправить Origin — отдельного обхода нет.

CORS в production не включён. Development origins можно разрешить явно через отдельный список; только они получают credentials CORS, конкретные методы и заголовки, без wildcard. В APISettings production невозможно включить этот список или снять Secure. Для development без TLS разрешены только явные IP loopback origins, имена cookie меняются на `wish_dev_*`.

## Reverse proxy и лимиты

Uvicorn обязательно запускать с `--no-proxy-headers`: иначе peer в ASGI может быть уже заменён недоверенным X-Forwarded-For. По умолчанию trusted proxies пусты. Для Nginx явно указать `127.0.0.1/32,::1/128`. Поддерживаются только точные loopback-адреса. Только от такого peer принимается **единственный** валидный X-Real-IP; при его отсутствии/невалидности запрос кода или verify отклоняется. X-Forwarded-For и Forwarded игнорируются. У недоверенного peer все proxy headers игнорируются. IPv6 нормализуется, IPv4-mapped IPv6 приводится к IPv4; в ядро идёт серверный source, хешируемый HMAC.

Ядро сохраняет бюджеты в SQLite: окно 1 час, запросы по email 5, по источнику 30, глобально 1000; попытки проверки по email 20, источнику 100, глобально 5000; до 5 неверных кодов на challenge; cooldown 60 секунд. Перезапуск API не сбрасывает лимиты. HTTP не возвращает существование аккаунта. 429 безопасен; Retry-After не добавляется, потому что существующее ядро не возвращает точное время освобождения нескольких бюджетов. Придумывать его нельзя.

Остаточный риск: существующее ядро при новом запросе того же email аннулирует все его прежние challenges, включая другой браузер. Злоумышленник, знающий email, может мешать входу в пределах email/source/global бюджетов. Этот PR сохраняет прежнюю бизнес-логику, добавляя browser binding против входа по чужому challenge, но не обещает устранить email DoS. Возможное следующее решение — отдельное согласованное изменение resend, независимые challenges по браузеру при общих email-лимитах; нельзя просто снять email-бюджет.

Origin не защищает публичный endpoint от программных запросов. Распределённый злоумышленник всё ещё может исчерпать global budget. Перед production нужны локальные лимиты Nginx (по IP и общий), мониторинг, лимиты размера заголовков/времени чтения и при необходимости отдельная антибот-защита. В БД нет сетевых запросов; при достижении бюджета дополнительные строки не создаются. Превышение body режется до Pydantic, но защита от медленных клиентов/неограниченного числа TCP-соединений — обязанность Nginx/Uvicorn. Ничего из этого не применяется к действующему серверу этим PR.

## SMTP

Пример без секретов: `docs/web-auth-api.env.example`. Параметры: SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL, SMTP_FROM_NAME, SMTP_TLS_MODE (`tls` или `starttls`), SMTP_TIMEOUT (больше 0 и не больше 30 секунд, по умолчанию 5). Отправитель `noreply@wish-bot.ru` пока только предложение, его адрес/домен ещё нужно создать и подтвердить.

Стандартный smtplib работает в executor, не блокируя event loop. На адаптер не больше двух одновременно незавершённых SMTP-операций; нет неограниченной очереди. Только TLS/STARTTLS с системным trust store, обязательной проверкой сертификата/hostname; нет plaintext fallback. До аутентификации выполняется TLS. Проверяются ASCII email, длины/управляющие символы заголовков; письма формируются через EmailMessage/Address в UTF-8. Debug SMTP не включается, код/пароль/адрес не логируются.

Контракт: успех означает подтверждение приёма SMTP-сервером, не попадание во входящие. `DeliveryFailed` — доказанный отказ (до DATA или явный отрицательный SMTP-ответ). `DeliveryUncertain` — сетевой сбой после начала отправки: сервер мог принять письмо. AuthService сохраняет `failed` или `unknown`, HTTP не обещает доставку. Автоматических повторов нет. Отмена async-задачи/таймаут не останавливает уже работающий smtplib-поток: слот остаётся занятым до фактического завершения, результат не меняет unknown задним числом и не вызывает повторную отправку. SMTP_TIMEOUT ограничивает сокетные операции, но системное DNS-разрешение может занять дольше; общий async deadline возвращает управление, а два занятых слота не позволяют бесконтрольно накапливать потоки. Перезапуск процесса во время SMTP тоже оставляет неопределённость; ядро ограниченно обрабатывает pending.

Нужны данные владельца: выбранный SMTP-провайдер, host/port/TLS, подтверждённый From и отображаемое имя, учётные данные через секретное окружение; SPF/DKIM, согласованная DMARC-политика и подтверждение домена/отправителя. Пароли не передавать через PR/логи. Нужны отдельное разрешение на реальную тестовую отправку и проверка доставляемости/антиспама. В рамках этапа писем не отправляли и провайдера не подключали.

## Миграции и очистка

Добавлена v3 `browser_bound_email_login`: таблицы `auth_browser_contexts`, `auth_browser_challenges`, индексы; FK ON DELETE CASCADE относятся только к новым browser-данным. DDL/checksum v1/v2 не менялись, журнал дополняется. Старые внутренние unbound сценарии сохраняются; browser-bound challenge нельзя обойти прямым внутренним verify без контекста. Миграция выполняется только явно оператором после отдельного согласования production. Ready на v1/v2 даёт 503 и не мигрирует автоматически.

`python -m web_api.cleanup` — одноразовая очистка challenges, sessions, browser contexts и старых rate budgets. Она не запускается при импорте или старте приложения. В будущем можно вызвать через отдельный systemd timer раз в час с тем же защищённым окружением. Удаление browser contexts каскадно удаляет только их привязки; бюджеты сохраняются на полное окно. Не запускать произвольное количество фоновых уборщиков.

## Безопасный локальный запуск

Не использовать production-БД или её копию. Для текущих проверок достаточно in-process ASGI и fake SMTP; сеть не нужна:

```bash
python -m venv .venv-web
.venv-web/bin/pip install -r requirements-web-dev.txt
.venv-web/bin/python -m pytest -q web_api/tests
.venv-web/bin/python -m web_api.openapi /tmp/wish-auth-openapi.json
```

Для 157 тестов предыдущих этапов и общего прогона дополнительно нужны legacy-зависимости бота в том же отдельном тестовом окружении:

```bash
.venv-web/bin/pip install -r requirements-dev.txt
.venv-web/bin/python -m pytest -q tests/test_accounts.py tests/test_auth.py web_api/tests
```

 Новые HTTP-тесты не требуют MAX_BOT_TOKEN; отдельный тестовый harness при общем прогоне подставляет только синтетический токен для legacy-тестов.

Для ручной проверки HTTP без писем можно создать **вне репозитория** локальный factory с APISettings(environment='test'), временной новой SQLite и FakeEmailSender. Создание файла и применение `python -m accounts.migrate --database /tmp/NEW-SYNTHETIC.sqlite3 --apply` — отдельные явные шаги. Использовать только свежий синтетический файл. Не печатать код в серверные логи; тесты читают FakeEmailSender в памяти. Для browser cookie предпочтителен локальный HTTPS; при отдельном loopback development режиме разрешить `secure_cookies=False` и указать оба локальных origin явно. Не запускать factory без параметров с production-секретами ради проверки.

Будущая команда после согласования почты/production, сейчас **не запускать**:

```bash
uvicorn web_api.app:create_app --factory --host 127.0.0.1 --port 8091 --workers 1 --limit-concurrency 32 --backlog 64 --timeout-keep-alive 5 --no-proxy-headers --no-access-log
```

Frontend: сначала CSRF bootstrap, затем request/verify с JSON и credentials; Origin браузер выставит сам. После verify обновить CSRF из ответа и запросить `/me`. Не сохранять token/cookie/код в Web Storage или аналитику. 401 означает новый вход, 403 — обновить контекст и показать понятную ошибку, 429 — предложить подождать без автоматической почтовой петли. Не подключать это к frontend ветке автоматически в рамках этапа.

## Будущие Nginx/systemd — только примеры

Не заменять существующую конфигурацию Nginx. После отдельного согласования добавить в HTTPS server block, сохранив действующие SSL/static/noindex-настройки:

```nginx
location /api/v1/ {
    client_max_body_size 4k;
    proxy_pass http://127.0.0.1:8091;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For "";
    proxy_set_header Forwarded "";
    proxy_connect_timeout 2s;
    proxy_read_timeout 20s;
    proxy_buffering off;
}
```

Это фрагмент маршрутизации, **не** полная production-защита. До запуска отдельно определить limit_req zones, лимиты соединений и таймауты чтения клиента; проверить, что нет других путей к upstream. Не проксировать `/docs` и `/openapi.json`.

Пример отдельного сервиса (не botmax):

```ini
[Unit]
Description=Wish browser authentication API
After=network.target

[Service]
User=wish-api
Group=wish-api
WorkingDirectory=/opt/wish-api
EnvironmentFile=/etc/wish-api/auth.env
ExecStart=/opt/wish-api/.venv/bin/uvicorn web_api.app:create_app --factory --host 127.0.0.1 --port 8091 --workers 1 --limit-concurrency 32 --backlog 64 --timeout-keep-alive 5 --no-proxy-headers --no-access-log
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/wish-api
UMask=0077

[Install]
WantedBy=multi-user.target
```

Перед deployment: отдельное разрешение, резервное копирование согласованной БД, проверка v3/отката на синтетических данных, секреты с правами 0600, непривилегированный пользователь, smoke checks и наблюдение за ресурсами VPS. Логи не должны содержать cookie/коды/SMTP debug. Отключение: закрыть только новый API location и остановить только новый сервис; статический frontend и MAX-бот остаются отдельными. Аддитивные таблицы не удалять автоматически при откате; версии v1/v2 не умеют инспектировать журнал v3, поэтому откат к старому auth-коду требует заранее согласованного восстановления схемы, а не запуска старой миграции поверх v3. Production-действия здесь не выполнены.

Результаты проверок и известные legacy-падения: `docs/web-auth-api-checks.md`.

## Зависимый этап 1.2Б

Безопасная привязка MAX, миграция v4 и исправление межбраузерной инвалидации email challenges описаны в [web-max-linking.md](web-max-linking.md). Оба флага привязки выключены по умолчанию. Этот этап не подключает общие балансы, оплаченные права или frontend.
