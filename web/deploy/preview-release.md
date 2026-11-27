# Будущее размещение preview — только после отдельного согласования

**Ничего из этого на сервере не выполнялось.** Подготовлен статический архив, а не публичный запуск. Сервер `landinavn.fvds.ru`, HTTPS-домен `app.wish-bot.ru`, root `/var/www/wish-app`, vhost `/etc/nginx/sites-available/wish-app` указаны владельцем. Текущая конфигурация Nginx здесь не получена и не изменялась.

Noindex/robots — запрет индексации, **не защита доступа**. На время ручного тестирования весь тестовый сайт, включая медиа и 404, закрывается HTTP Basic Auth по HTTPS. Пароль не размещать в URL, архиве, JS, Git или чате. Проверить, что access/debug-логи и внешняя аналитика не записывают Authorization header.

## 1. Перед любыми изменениями

Получить отдельное согласование размещения. Сохранить копию действующего vhost и текущую временную страницу с её зависимостями **вне публичной выдачи**. Проверить существующий symlink sites-enabled и вывод `nginx -T` локально на сервере, не публикуя полный конфиг с приватными настройками.

Определить:

- Только server-блоки `app.wish-bot.ru`, их listen/TLS и текущие security headers.
- Действующий HTTP `/.well-known/acme-challenge/`: root/alias либо другой фактически настроенный handler. **Сохранить этот HTTP-блок и его путь без изменений.** Не предполагать `/var/www/certbot` или другой неизвестный webroot.
- Если HTTPS тоже содержит ACME location, сохранить его содержимое/root/alias и явно добавить там `auth_basic off`. Не добавлять Basic Auth в HTTP ACME location. Остальные HTTP-запросы должны по-прежнему уходить на HTTPS, где нужна авторизация.
- Все вложенные HTTPS locations: убрать для статической витрины необоснованные `auth_basic off`, `satisfy any` и proxy/fallback, способные обойти защиту. Если там уже есть backend/API или неизвестная инфраструктура — остановиться для разбора, не заменять её автоматически.
- Реального пользователя/группу Nginx для чтения static и htpasswd (в Ubuntu обычно www-data, это нужно подтвердить).

Нельзя менять `wish-bot.ru`, платёжный webhook, `smalltalk.wish-bot.ru`, общие upstream и серверные сертификаты. Не запускать backend, MAX-бота, миграции или постоянные дополнительные процессы. Node.js/npm на VPS **не нужны**. Все сборки и тесты выполняются вне production.

## 2. Артефакт

Передать только `wish-app-preview.tar.gz` и его `.sha256` через имеющийся SSH-доступ, например сначала в `/root/wish-preview-upload/`. Не копировать исходники/репозиторий/служебный registry в docroot.

Проверить checksum из предоставленного отдельно отчёта, затем содержимое:

```bash
cd /root/wish-preview-upload
sha256sum -c wish-app-preview.tar.gz.sha256
tar -tzf wish-app-preview.tar.gz
```

Ожидаются обычные относительные файлы `index.html`, `404.html`, HTML маршрутов, хешированные JS/CSS, robots/sitemap и согласованные media. Нет каталогов dist/node_modules, симлинков, абсолютных путей, `..`, .env, .git, README, manifest разрешений, Python, БД или конфигов Nginx. Сверить список с отдельным `wish-app-preview-files.txt`.

## 3. Отдельные релизы и временная страница

План структуры:

```text
/var/www/wish-app/
  releases/
    holding-<дата>/       # сохранённая действующая временная страница
    preview-<commit>/    # только содержимое проверенного архива
  current -> releases/holding-<дата>  # перед включением preview
```

Сначала создать holding-релиз из **проверенных файлов действующей временной страницы** (index.html и только реально используемые им локальные CSS/изображения). Не переносить вслепую весь `/var/www` и не копировать каталог внутрь самого себя. До изменения vhost проверить holding-релиз: исходная временная страница и зависимости доступны в нём, нет приватных файлов/симлинков за пределы root.

До защиты vhost нельзя распаковывать preview в `/var/www/wish-app/releases`: прежний root `/var/www/wish-app` может выдавать его по URL `/releases/...`. Сначала используйте подтверждённый **непубличный staging вне всех Nginx roots/aliases**. В примере — `/var/lib/wish-app-preview-stage` с закрытым родительским каталогом.

Пример подготовки нового **пустого** staging после выбора полного commit из sidecar:

```bash
RELEASE_ID='preview-REPLACE_WITH_VERIFIED_FULL_COMMIT'
install -d -m 0700 /var/lib/wish-app-preview-stage
mkdir -m 0755 "/var/lib/wish-app-preview-stage/$RELEASE_ID" &&
tar --extract --gzip --file=/root/wish-preview-upload/wish-app-preview.tar.gz \
  --directory="/var/lib/wish-app-preview-stage/$RELEASE_ID" \
  --no-same-owner --no-same-permissions
```

Заменить `REPLACE_WITH_VERIFIED_FULL_COMMIT` фактическим полным коммитом из sidecar. `mkdir` должен остановиться, если такой релиз уже существует: не распаковывать поверх действующих файлов. Содержимое архива заранее проверено, но на сервере checksum и список проверяются повторно. Назначить файлам 0644, каталогам 0755 и владельца с учётом действующего Nginx. Никаких прав 777 и записи со стороны веб-процесса.

Holding-релиз содержит только уже существующую временную страницу; его можно подготовить в `/var/www/wish-app/releases` до защиты. Сам preview остаётся в непубличном staging. Создать `current` на holding-релиз до изменения root в vhost. Существующий путь `current` не перезаписывать без проверки его назначения; если он уже используется, включить его в план отката.

## 4. Basic Auth и Nginx

Файл паролей — вне docroot: `/etc/nginx/auth/wish-app.htpasswd`. Создать интерактивно через `htpasswd`; пароль вводить по запросу, не через `-b` и не в командной строке. Если htpasswd отсутствует, отдельно согласовать установку утилиты apache2-utils либо подготовить файл безопасно на доверенной машине. Node.js не устанавливается.

Создать каталог только для root и подтверждённой группы Nginx; файл должен читаться worker-процессом, например root:www-data и 0640 после проверки группы. Для **нового** файла используют `htpasswd -c /etc/nginx/auth/wish-app.htpasswd owner`, для существующего — без `-c`, чтобы не стереть других пользователей. Это будущие действия, пароль сейчас не создавался и не назначался.

[nginx/wish-app-static.inc](nginx/wish-app-static.inc) — предлагаемые директивы **внутри уже существующего HTTPS server**. Это не замена полного vhost: в нём нет выдуманных путей сертификатов, listen или ACME webroot.

При будущем ручном редактировании `/etc/nginx/sites-available/wish-app`:

1. Сохранить текущие TLS/listen/server_name, необходимые security headers и HTTP ACME handler.
2. Заменить только root/index/location / для статической витрины и соответствующие error_page/headers. Если используется include, положить его в отдельный подтверждённый конфигурационный путь Nginx, не в public/media.
3. Basic Auth задать на уровне HTTPS server; исключить только реально используемый ACME handler. Проверить наследование в каждом дочернем location. `satisfy all` не позволяет обходить пароль правилом allow при `satisfy any`.
4. Сохранить или объединить существующие security headers: добавление add_header меняет наследование. Не удалять HSTS/CSP, если они уже настроены.
5. Проверить diff: изменения только `app.wish-bot.ru`; основной домен/webhook/smalltalk и HTTP ACME stanza неизменны.
6. Выполнить `nginx -t`. При любой ошибке — не делать reload, вернуть предыдущую конфигурацию.
7. После отдельного разрешения применить **reload Nginx**, не restart бота/backend. Этот reload пока не выполнялся.

Сначала верифицировать Basic Auth с root на holding-страницу: без пароля 401 для HTML и media; неверный пароль 401; верный пароль позволяет открыть временную страницу. Затем можно переключать current на проверенный preview.

Сама конфигурация здесь не проверялась на действующем Nginx: его актуальный файл и бинарник недоступны в рабочей среде. Это явно отмеченное ограничение, а не подтверждение применимости к неизвестному vhost.

## 5. Атомарное переключение и откат

Только когда Basic Auth проверен на holding-странице и root указывает на current, перенести подготовленный preview из закрытого staging в releases. Сначала проверить отсутствие другого публичного root/alias, который мог бы отдавать releases без пароля; при обнаружении остановиться, не менять другие домены.

```bash
test ! -e "/var/www/wish-app/releases/$RELEASE_ID" &&
mv -T "/var/lib/wish-app-preview-stage/$RELEASE_ID" "/var/www/wish-app/releases/$RELEASE_ID"
```

Если проверки или перенос не прошли, не переключать current. Когда root уже указывает на `/var/www/wish-app/current`, переключение одного static-релиза не требует Node, backend или нового процесса. Пример будущих команд, где имена **сначала проверены**:

```bash
cd /var/www/wish-app
test -L current &&
test -f "releases/$RELEASE_ID/index.html" &&
ln -s "releases/$RELEASE_ID" current.next &&
mv -Tf current.next current
```

Команды соединены через `&&`, поэтому сбой проверки/ln не запускает mv. `current.next` не должен существовать; при наличии остановиться и разобраться. `mv -T` атомарно меняет симлинк в той же файловой системе, не копирует релиз. Каталог current должен быть симлинком, а не реальным каталогом с данными. Не удалять holding-релиз.

Откат на временную страницу тем же способом:

```bash
cd /var/www/wish-app
test -L current &&
test -f "releases/holding-REPLACE_WITH_VERIFIED_DATE/index.html" &&
ln -s "releases/holding-REPLACE_WITH_VERIFIED_DATE" current.next &&
mv -Tf current.next current
```

HTTP Basic Auth и запрет индексации при откате остаются включёнными. Проверить страницу и её CSS/изображения после отката. Если возникла проблема самой конфигурации, восстановить сохранённый vhost, проверить `nginx -t` и лишь после разрешения выполнить reload. Не удалять новые/старые релизы до окончания проверки.

## 6. Приёмка после отдельного согласованного размещения

- Без credentials: HTTPS `/`, `/gallery`, `/media/works/mother-cartoon.mp4`, неизвестный путь — закрыты Basic Auth (401); директивы не позволяют обход в дочернем location.
- С credentials: все 19 URL дают 200; `/does-not-exist` — **404** с правильным HTML. Не делать SPA fallback на index.html.
- Получение credentials в curl — `curl --user owner ...` с интерактивным вводом пароля, без пароля в URL или аргументе user:password. В браузере — штатный диалог по HTTPS.
- HTTP ACME challenge по существующему handler доступен без Basic Auth: проверить безопасный тестовый token в подтверждённом текущем webroot и убрать его после проверки. Не подменять чужой token и не запускать переоформление сертификата.
- В ответах no-store/private и X-Robots-Tag; в HTML noindex, robots Disallow: /. Директории не листятся, README/registry/.env/.git/.py/.sqlite3 отсутствуют.
- MAX-ссылки работают как обычные переходы; веб-оплаты/генераций/фиктивного входа нет. Все четыре видео и аудиофрагмент играют только по нажатию, в том числе seek/range через Nginx.
- Desktop 1440×900 и mobile 390×844, отсутствие горизонтального скролла и перекрытий.
- Неавторизованные люди не получают ни HTML, ни картинки/видео. Noindex не заменяет эту проверку.
- Основной домен, webhook и smalltalk остаются в исходном состоянии. Проверка доступности выполняется без отправки реального платежа/POST webhook.

После этой работы остановиться: размещение, `nginx -t` на VPS, reload и переключение current требуют отдельного согласования владельца.
