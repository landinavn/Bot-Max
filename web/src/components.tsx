import { useEffect, useRef, useState } from "react";
import { products } from "./catalog.ts";
import type { Product } from "./catalog.ts";
import { categoryLabels, media, site } from "./config.ts";
import type { Category, MediaItem } from "./config.ts";
import { trackEvent } from "./analytics.ts";

export function Picture({
  src,
  alt,
  eager = false,
  avif,
  webp,
  className = "",
}: {
  src: string;
  alt: string;
  eager?: boolean;
  avif?: string;
  webp?: string;
  className?: string;
}) {
  return (
    <picture className={className}>
      {avif && <source srcSet={avif} type="image/avif" />}
      {webp && <source srcSet={webp} type="image/webp" />}
      <img
        src={src}
        alt={alt}
        loading={eager ? "eager" : "lazy"}
        fetchPriority={eager ? "high" : "auto"}
        decoding="async"
        width="600"
        height="720"
      />
    </picture>
  );
}
export function OrderAction({
  product,
  label,
  className = "button",
}: {
  product?: Product;
  label?: string;
  className?: string;
}) {
  const mode = product?.mode ?? "max_redirect";
  if (mode === "coming_soon")
    return (
      <span className={className + " disabled"} aria-disabled="true">
        Скоро
      </span>
    );
  if (mode === "web_enabled")
    return site.webOrdering && product?.webPath ? (
      <a className={className} href={product.webPath}>
        Продолжить онлайн
      </a>
    ) : (
      <span className={className + " disabled"} aria-disabled="true">
        Веб-создание готовится
      </span>
    );
  return (
    <a
      className={className}
      href={site.maxUrl}
      target="_blank"
      rel="noopener noreferrer"
      onClick={() => trackEvent("max_redirect", { product: product?.id })}
    >
      {label ?? "Создать в MAX"}
    </a>
  );
}
export function Header() {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [open]);
  return (
    <header className="header">
      <div className="container header-inner">
        <a href="/" className="brand" aria-label={site.name}>
          {site.logo ? (
            <img src={site.logo} alt={site.name} width="190" height="50" />
          ) : (
            <>
              <span>Открытки и Карта желаний</span>
              <strong>
                Нейрофото<span className="brand-period">.</span>
              </strong>
            </>
          )}
        </a>
        <button
          ref={button}
          className="menu-toggle"
          aria-expanded={open}
          aria-controls="main-navigation"
          onClick={() => setOpen(!open)}
        >
          {open ? "Закрыть" : "Меню"}
          <span aria-hidden="true">{open ? "×" : "☰"}</span>
        </button>
        <nav
          id="main-navigation"
          aria-label="Основная навигация"
          className={open ? "navigation is-open" : "navigation"}
        >
          <a href="/#catalog">Каталог</a>
          <a href="/gallery">Примеры</a>
          <a href="/#how">Как это работает</a>
          <a href="/prices">Цены</a>
          <a href="/login" className="account-link">
            Личный кабинет
          </a>
        </nav>
      </div>
    </header>
  );
}
export function Footer() {
  return (
    <footer className="footer">
      <div className="container footer-main">
        <div>
          <a href="/" className="footer-brand">
            Открытки и Карта желаний
            <br />
            <strong>Нейрофото.</strong>
          </a>
          <p>{site.slogan}</p>
          <a href={site.maxUrl} target="_blank" rel="noopener noreferrer">
            Открыть MAX-бота
          </a>
        </div>
        <div>
          <h2>Ваша история</h2>
          <a href="/postcards">Открытки</a>
          <a href="/vision-board">Карта желаний</a>
          <a href="/ded-moroz">Дед Мороз</a>
          <a href="/photosessions">Фотосессии</a>
        </div>
        <div>
          <h2>Больше вдохновения</h2>
          <a href="/gallery">Галерея</a>
          <a href="/songs">Песни</a>
          <a href="/cartoons">Мультфильмы</a>
          <a href="/blog">Журнал</a>
          <a href="/prices">Стоимость услуг</a>
        </div>
        <div>
          <h2>Связаться</h2>
          <p>Вопросы о заказе можно задать в MAX.</p>
          <OrderAction label="Перейти в MAX" className="footer-contact" />
          <p className="footer-note">
            Контакты владельца и юридические документы готовятся к публикации.
          </p>
        </div>
      </div>
      <div className="container footer-bottom">
        <span>{site.name}</span>
        <div>
          {[
            ["offer", "Публичная оферта"],
            ["privacy", "Политика конфиденциальности"],
            ["consent", "Согласие на обработку данных"],
          ].map(([key, label]) =>
            site.legal[key] ? (
              <a key={key} href={site.legal[key]!}>
                {label}
              </a>
            ) : (
              <span key={key}>
                {label} <small>— готовится</small>
              </span>
            ),
          )}
        </div>
      </div>
    </footer>
  );
}
export function SectionHeading({
  eyebrow,
  title,
  text,
}: {
  eyebrow?: string;
  title: string;
  text?: string;
}) {
  return (
    <div className="section-heading">
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h2>{title}</h2>
      {text && <p>{text}</p>}
    </div>
  );
}
export function ProductCard({ product }: { product: Product }) {
  return (
    <article className={"product-card " + product.color}>
      <a
        className="product-image"
        href={product.path}
        onClick={() => trackEvent("product_click", { product: product.id })}
      >
        <Picture
          src={product.image}
          alt={"Демонстрационная иллюстрация: " + product.name}
        />
        <span className="product-label">{product.label}</span>
      </a>
      <div className="product-card-copy">
        <h3>
          <a
            href={product.path}
            onClick={() => trackEvent("product_click", { product: product.id })}
          >
            {product.name}
          </a>
        </h3>
        <p>{product.summary}</p>
        <a
          className="text-link"
          href={product.path}
          onClick={() => trackEvent("product_click", { product: product.id })}
        >
          Подробнее
        </a>
      </div>
    </article>
  );
}
export function FAQ({ items }: { items: [string, string][] }) {
  return (
    <div className="faq-list">
      {items.map(([question, answer]) => (
        <details key={question}>
          <summary>
            {question}
            <span aria-hidden="true">+</span>
          </summary>
          <p>{answer}</p>
        </details>
      ))}
    </div>
  );
}
export function MediaPlayer({ item }: { item: MediaItem }) {
  const [loaded, setLoaded] = useState(false);
  if (item.kind === "image")
    return (
      <Picture
        src={item.poster}
        alt={item.title}
        avif={item.avif}
        webp={item.webp}
      />
    );
  if (!item.src)
    return (
      <div className="media-pending">
        <Picture src={item.poster} alt={item.title} />
        <p>
          Пример {item.kind === "video" ? "видео" : "песни"} появится после
          добавления материала владельцем.
        </p>
      </div>
    );
  return (
    <div className="media-player">
      {item.kind === "video" ? (
        loaded ? (
          <video
            controls
            playsInline
            preload="none"
            poster={item.poster}
            src={item.src}
            aria-label={item.title}
          />
        ) : (
          <>
            <Picture src={item.poster} alt={item.title} />
            <button className="button" onClick={() => setLoaded(true)}>
              Загрузить видео
            </button>
          </>
        )
      ) : loaded ? (
        <audio controls preload="none" src={item.src} aria-label={item.title} />
      ) : (
        <>
          <Picture src={item.poster} alt={item.title} />
          <button className="button" onClick={() => setLoaded(true)}>
            Загрузить аудио
          </button>
        </>
      )}
    </div>
  );
}
export function Gallery({ compact = false }: { compact?: boolean }) {
  const [category, setCategory] = useState<Category>("all");
  const [chosen, setChosen] = useState<MediaItem | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const filtered = media.filter(
    (item) => category === "all" || item.category === category,
  );
  const shown = compact ? filtered.slice(0, 4) : filtered;
  function close() {
    dialog.current?.close();
    setChosen(null);
    trigger.current?.focus();
  }
  function open(item: MediaItem, event: React.MouseEvent<HTMLButtonElement>) {
    trigger.current = event.currentTarget;
    setChosen(item);
    dialog.current?.showModal();
    trackEvent("gallery_view", { category: item.category });
  }
  return (
    <>
      <div
        className="gallery-filters"
        role="group"
        aria-label="Категории галереи"
      >
        {(Object.keys(categoryLabels) as Category[]).map((key) => (
          <button
            key={key}
            aria-pressed={category === key}
            className={category === key ? "active" : ""}
            onClick={() => setCategory(key)}
          >
            {categoryLabels[key]}
          </button>
        ))}
      </div>
      <p className="demo-notice">
        Демонстрационные иллюстрации — не фотографии клиентов и не результаты
        заказов.
      </p>
      <div className="gallery-grid" aria-live="polite">
        {shown.map((item) => (
          <figure key={item.id}>
            <button
              className="gallery-open"
              onClick={(e) => open(item, e)}
              aria-label={"Рассмотреть: " + item.title}
            >
              <Picture src={item.poster} alt={item.title} />
              <span className="media-kind">
                {item.kind === "image"
                  ? "Рассмотреть"
                  : item.kind === "video"
                    ? item.src
                      ? "Смотреть видео"
                      : "Видео — скоро"
                    : item.src
                      ? "Слушать"
                      : "Аудио — скоро"}
              </span>
            </button>
            <figcaption>{item.title}</figcaption>
          </figure>
        ))}
      </div>
      <dialog
        ref={dialog}
        className="lightbox"
        aria-labelledby="lightbox-title"
        onCancel={() => {
          setChosen(null);
          trigger.current?.focus();
        }}
        onClick={(e) => {
          if (e.target === e.currentTarget) close();
        }}
      >
        <button
          className="close-dialog"
          onClick={close}
          aria-label="Закрыть просмотр"
        >
          ×
        </button>
        {chosen && (
          <div>
            <h2 id="lightbox-title">{chosen.title}</h2>
            <MediaPlayer key={chosen.id} item={chosen} />
            <p className="demo-notice">
              Демонстрационный макет. Реальные примеры будут добавлены отдельно.
            </p>
          </div>
        )}
      </dialog>
    </>
  );
}
export function Comparison() {
  const [position, setPosition] = useState(50);
  const [kind, setKind] = useState<"dream" | "card" | "video">("dream");
  const pair =
    kind === "card"
      ? ["/media/gallery/source.svg", "/media/postcards/bouquet.svg"]
      : ["/media/gallery/source.svg", "/media/vision-board/window.svg"];
  return (
    <div className="comparison-layout">
      <div className="comparison-copy">
        <p className="eyebrow">Одна фотография — много историй</p>
        <h2>
          Посмотрите на привычное
          <br />
          <em>по-новому.</em>
        </h2>
        <p>
          Из снимка — в открытку, мечту или маленькую историю. Здесь можно
          посмотреть, как устроено сравнение.
        </p>
        <div
          className="comparison-tabs"
          role="group"
          aria-label="Варианты сравнения"
        >
          {(
            [
              ["dream", "Карта желаний"],
              ["card", "Открытка"],
              ["video", "Анимация"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              aria-pressed={kind === key}
              onClick={() => setKind(key)}
            >
              {label}
            </button>
          ))}
        </div>
        <p className="small-note">Пока используются демонстрационные макеты.</p>
      </div>
      <div>
        {kind === "video" ? (
          <div className="comparison-video">
            <figure>
              <Picture
                src="/media/animation/frame.svg"
                alt="Демонстрационный исходный кадр для анимации"
              />
              <figcaption>Исходное изображение</figcaption>
            </figure>
            <div>
              <MediaPlayer
                item={{
                  id: "comparison-video",
                  title: "Сравнение исходника и анимации",
                  category: "animation",
                  kind: "video",
                  poster: "/media/animation/frame.svg",
                  demo: true,
                }}
              />
              <p>Видео из этого изображения</p>
            </div>
          </div>
        ) : (
          <div
            className="comparison"
            style={{ "--position": position + "%" } as React.CSSProperties}
          >
            <img
              src={pair[0]}
              alt="Демонстрационный макет исходника"
              width="600"
              height="720"
              loading="lazy"
            />
            <img
              className="comparison-after"
              src={pair[1]}
              alt="Демонстрационный макет результата"
              width="600"
              height="720"
              loading="lazy"
            />
            <span className="compare-label before">Макет исходника</span>
            <span className="compare-label after">Макет результата</span>
            <div className="compare-divider" aria-hidden="true">
              <span>↔</span>
            </div>
            <input
              type="range"
              min="0"
              max="100"
              value={position}
              aria-label="Сравнить исходник и результат"
              aria-valuetext={`${position}% исходника`}
              onChange={(e) => setPosition(Number(e.target.value))}
            />
          </div>
        )}
      </div>
    </div>
  );
}
export function ProductGrid({ items = products }: { items?: Product[] }) {
  return (
    <div className="product-grid">
      {items
        .filter((p) => p.mode !== "coming_soon")
        .map((p) => (
          <ProductCard key={p.id} product={p} />
        ))}
    </div>
  );
}
