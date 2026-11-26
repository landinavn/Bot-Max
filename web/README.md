# Витрина «Открытки и Карта желаний | Нейрофото» — Frontend 1.2

Статический preview для ручного тестирования владельцем. Ветка `feature/web-frontend`, PR #257, продолжение согласованного дизайна 1.1 (`2213dcd0cec76b78129c3b05184d7dc80978e232`). Изменения ограничены `web/`; backend-ветки развиваются независимо. Deployment и merge не выполнялись.

## Архитектура и границы

React + TypeScript + Vite; build-time React SSR сохраняет полноценный HTML для **19 маршрутов и 404**. Браузер гидратирует страницы. На VPS нужен только существующий Nginx; Node.js, backend, миграции и дополнительные процессы там не запускаются.

Сохранены сливово-кремовый дизайн, оригинальный логотип, реальные согласованные работы, интерактивное сравнение, галерея, 4 видео + 1 аудиофрагмент по нажатию. JS/CSS интерфейса и тарифы в 1.2 не менялись. Создание и стоимость — в действующем MAX `https://max.ru/se14596751_1_bot`. Веб-вход, оплата и генерации не имитируются; `/login` и `/account*` — честные заглушки. Первая бесплатная генерация относится только к открытке.

Структура:

```text
src/                 React, 19 маршрутов, каталог, SEO, SSG, аналитика
public/media/        только 21 разрешённый публичный медиафайл
media/manifest.json  служебный реестр происхождения/разрешений/хешей, НЕ публикуется
media/README.md      статус согласованных и недостающих материалов, НЕ публикуется
scripts/             проверки, SSG tooling, локальный QA, упаковка
tests/               статические и браузерные проверки
deploy/              инструкция и Nginx proposal, НЕ публикуются
```

Маршруты: `/`, `/postcards`, `/vision-board`, `/photosessions`, `/photosessions/new-year`, `/ded-moroz`, `/cartoons`, `/songs`, `/musical-postcards`, `/animation`, `/custom-order`, `/gallery`, `/prices`, `/blog`, `/login`, `/account`, `/account/works`, `/account/balance`, `/account/settings`. Неизвестный URL возвращает HTTP 404 с `404.html`, без SPA fallback.

## Сборка вне production

Node.js 24 и npm 11. Из `web/`:

```bash
npm ci --cache /tmp/wish-npm-cache
npm run build
npm test
```

`build` включает проверку публичных медиа, TypeScript, Vite, SSG и проверку конечного dist. Зависимости устанавливаются на машине сборки; на VPS npm/Node не нужны. Секреты и `.env` для витрины не требуются.

Preview — режим по умолчанию: все HTML `noindex,nofollow`, robots Disallow: /, sitemap пуст. Production-индексация заблокирована `site.readyForIndexing=false`. Noindex **не ограничивает доступ людей**: на сервере требуется отдельный HTTP Basic Auth.

### Браузерная проверка

```bash
node scripts/serve.mjs
# В другом терминале:
WEB_SCREENSHOT_DIR=/absolute/outside/source npm run test:browser
```

Сервер локальный, 127.0.0.1:4173; можно задать `PORT` и соответствующий `WEB_TEST_URL`. Chromium по `/usr/bin/chromium`, другой путь — `CHROMIUM_PATH`. Тесты блокируют внешние запросы; `--no-sandbox` используется только в доверенном изолированном QA-окружении. Ни MAX-сообщения, ни платежи, ни AI API не вызываются.

Проверяются 19 маршрутов, 8 viewport × 9 страниц, logo/menu/focus, отсутствие overflow и наложений hero, FAQ, сравнение мышью/клавиатурой/touch, gallery/dialog, семейный якорь, локальное analytics-событие MAX, контент без JS, HTTP 404 и реальное воспроизведение всех 5 медиа без autoplay/ранней загрузки. Отдельные первые экраны 1440×900 и 390×844 доступны в screenshot output.

## Контроль публичной сборки

[media/manifest.json](media/manifest.json) вынесен из public. На каждый публичный файл одна запись, есть исходный SHA-256/разрешение и SHA-256/размер конечного файла. Удалены два дубля открыток и ошибочная запись README как логотипа. Неиспользуемые старые SVG-заглушки исключены. Все реальные примеры сохранены побайтово.

`check:media` требует, чтобы public содержал **только** перечисленные медиа, и проверяет размер/хеш каждого. `check:dist` разрешает только HTML известных маршрутов, 404, robots/sitemap, хешированные клиентские JS/CSS и эти медиа. README, служебный registry, Vite manifest, .env, Git, Python, БД, JSON/Markdown/исходники, symlinks и неожиданные файлы запрещены. Текст сканируется на известные форматы credentials. Это проверка известных сигнатур и состава файлов, а не универсальная гарантия обнаружения любого секрета.

Есть негативные тесты утечек/симлинков, подмены содержимого медиа и известных credential-сигнатур. Нельзя копировать весь репозиторий в webroot или маскировать запрещённые файлы разрешённым расширением.

### Рекламные примеры с Telegram

`postcard-birthday.webp` и `postcard-mother.webp` содержат встроенный знак Telegram. Он присутствует в разрешённых оригиналах; на сайте все реальные кнопки ведут в MAX. В этом этапе оригиналы **не ретушировались и не заменялись**. Для публичного запуска нужны согласованные владельцем версии этих двух рекламных картинок с корректным обозначением MAX/без Telegram. Сейчас они сохранены в защищённом preview для проверки.

Список остальных отсутствующих/ожидающих прав материалов — [media/README.md](media/README.md). Контакты, юридические документы и разрешение индексации остаются задачами публичного запуска; тарифы не выдумываются.

## Архив для отдельного согласованного размещения

Сначала commit исходников, затем build и все проверки **этого чистого коммита**. После этого:

```bash
npm run package:preview -- /absolute/artifact-directory
```

Упаковщик проверяет clean Git state, совпадение commit с private build receipt и хеш всего dist. При старой/изменённой сборке упаковка отклоняется. Артефакты должны находиться за пределами checkout `web/`.

Результат:

- `wish-app-preview.tar.gz` — только 45 статических файлов, index.html в корне, без node_modules, внутренних документов или каталогов dist/.ssr.
- `wish-app-preview.tar.gz.sha256` — контрольная сумма.
- `wish-app-preview-files.txt` — полный список.
- `wish-app-preview-metadata.json` — точный source commit/branch, compressed/unpacked bytes, хеши файлов. Sidecars **не внутри** архива.

Архив детерминирован: порядок, timestamp коммита, owner/group 0, обычные файлы 0644, gzip без имени/timestamp. Checksum проверяется отдельно перед будущей распаковкой.

## Будущее размещение — НЕ выполнено

[deploy/preview-release.md](deploy/preview-release.md) описывает private staging, holding/preview релизы, Basic Auth, сохранение текущего HTTP ACME challenge, проверку Nginx и атомарный rollback на временную страницу. [deploy/nginx/wish-app-static.inc](deploy/nginx/wish-app-static.inc) — предложение для существующего HTTPS server, без выдуманных путей сертификатов/ACME.

Текущий vhost не получен; инструкцию нужно сверить с ним до применения. Передача на VPS, пароль, изменения Nginx, reload и переключение current — только после отдельного согласования. Основной wish-bot.ru, webhook, smalltalk.wish-bot.ru и MAX-бот не затрагиваются.
