import { useEffect, useRef, useState } from "react";
import { products } from "./catalog.ts";
import type { Product } from "./catalog.ts";
import { categoryLabels, media, site, exampleFor } from "./config.ts";
import type { Category, MediaItem } from "./config.ts";
import { responsiveMedia } from "./responsive-media.ts";
import { trackEvent } from "./analytics.ts";

export function Picture({
  src,
  alt,
  eager = false,
  avif,
  webp,
  className = "",
  sizes = "(max-width: 767px) 92vw, 42vw",
}: {
  src: string;
  alt: string;
  eager?: boolean;
  avif?: string;
  webp?: string;
  className?: string;
  sizes?: string;
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
        width={responsiveMedia[src]?.width ?? 600}
        height={responsiveMedia[src]?.height ?? 720}
        srcSet={responsiveMedia[src]?.srcSet}
        sizes={responsiveMedia[src]?.srcSet ? sizes : undefined}
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
          {site.logo && <img className="brand-logo" src={site.logo} alt="" width="52" height="52" />}
          <span className="brand-copy"><span>Открытки и Карта желаний</span><strong>Нейрофото<span className="brand-period">.</span></strong></span>
        </a>
        <button
          ref={button}
          className="menu-toggle"
          aria-label={open ? "Закрыть меню" : "Открыть меню"}
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
export function ProductVisual({ productId, eager = false, playable = false }: { productId: string; eager?: boolean; playable?: boolean }) {
  const item = exampleFor(productId);
  if (!item) return <div className={"pending-art pending-" + productId}>
    <span aria-hidden="true">{productId === "animation" ? "↗" : "✦"}</span>
    <strong>{productId === "animation" ? "Оживите момент" : "Ваша особенная идея"}</strong>
    <p>{productId === "animation" ? "Видеопример готовится" : "Индивидуальный результат — после обсуждения"}</p>
  </div>;
  if (playable) return <MediaPlayer item={item} />;
  return <Picture src={item.poster} alt={item.title} eager={eager} sizes="(max-width: 767px) 78vw, 28vw" />;
}
export function ProductCard({ product }: { product: Product }) {
  return <article className={"product-card " + product.color}>
    <a className="product-card-link" href={product.path} onClick={() => trackEvent("product_click", { product: product.id })}>
      <div className={"product-image visual-" + product.id}>
        <ProductVisual productId={product.id} />
        <span className="product-label">{product.id === "songs" ? "♫ Персональная песня" : product.label}</span>
      </div>
      <div className="product-card-copy"><h3>{product.name}</h3><p>{product.summary}</p>
        {product.pricing.display && <p className="verified-price">{product.pricing.display.label}</p>}
        <span className="card-cta">Подробнее <span aria-hidden="true">↗</span></span>
      </div>
    </a>
  </article>;
}
export function MobileNav() {
  return <nav className="mobile-bottom-nav" aria-label="Быстрые переходы">
    <a href="/">Главная</a><a href="/#catalog">Каталог</a>
    <OrderAction label="Создать" className="mobile-create" />
    <a href="/gallery">Примеры</a><a href="/login">Профиль</a>
  </nav>;
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
  const [needsPlay, setNeedsPlay] = useState(false);
  const player = useRef<HTMLMediaElement | null>(null);
  useEffect(() => {
    // Only this explicit user action mounts a src. There is no autoplay attribute.
    if (loaded && player.current) void player.current.play().catch(() => setNeedsPlay(true));
  }, [loaded]);
  if (item.kind === "image") return <Picture src={item.poster} alt={item.title} />;
  if (!item.src) return <Picture src={item.poster} alt={item.title} />;
  return <div className={"media-player player-" + item.kind} data-media-id={item.id}>
    {item.kind === "audio" && loaded && <Picture src={item.poster} alt={item.title} />}
    {loaded ? item.kind === "video" ? <video ref={node => {player.current=node;}} controls playsInline preload="none" poster={item.poster} src={item.src} aria-label={item.title}
      width={responsiveMedia[item.poster]?.width} height={responsiveMedia[item.poster]?.height} />
      : <audio ref={node => {player.current=node;}} controls preload="none" src={item.src} aria-label={item.title} />
    : <button className="media-start" onClick={() => setLoaded(true)} aria-label={(item.kind === "video" ? "Смотреть видео: " : "Слушать песню: ") + item.title}>
      <Picture src={item.poster} alt={item.title} />
      <span className="play-badge"><span className="play-circle" aria-hidden="true"/><span>{item.kind === "video" ? "Смотреть видео" : "Слушать песню"}</span></span>
    </button>}
    {item.kind === "audio" && <div className="sound-wave" aria-hidden="true">{[12,22,34,18,42,28,50,32,20,38,54,26,44,30,18,40,24,48,34,16,28,42,22,12].map((height,i)=><span key={i} style={{height}}/>)}</div>}
    {needsPlay && <p className="small-note" role="status">Нажмите кнопку воспроизведения в проигрывателе.</p>}
    <noscript><p><a className="text-link" href={item.src}>Открыть {item.kind === "video" ? "видеопример" : "аудиопример"}</a></p></noscript>
  </div>;
}
export function WorkCaption({ item }: { item: MediaItem }) {
  return <>{item.title}</>;
}
export function Gallery({ compact = false, items = media, photoFilters = false, showCategories = true, layout = "" }: {
  compact?: boolean; items?: MediaItem[]; photoFilters?: boolean; showCategories?: boolean; layout?: string;
}) {
  const [category, setCategory] = useState<Category>("all");
  const [group, setGroup] = useState("all");
  const [chosen, setChosen] = useState<MediaItem | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const rail = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const filtered = items.filter(item => (category === "all" || item.category === category) && (group === "all" || item.photoGroup === group));
  const shown = compact ? filtered.slice(0, 4) : filtered;
  const groups = [["all", "Все"], ["family", "Семейные"], ["women", "Индивидуальные"], ["children", "Детские"], ["mother-daughter", "Мама и дочка"], ["couple", "Для пары"]].filter(([key]) => key === "all" || items.some(i=>i.photoGroup===key));
  function close() { dialog.current?.close(); setChosen(null); trigger.current?.focus(); }
  return <>
    {showCategories && <div className="gallery-filters" role="group" aria-label="Категории галереи">
      {(Object.keys(categoryLabels) as Category[]).map(key=><button key={key} aria-pressed={category===key} className={category===key?"active":""} onClick={()=>setCategory(key)}>{categoryLabels[key]}</button>)}
    </div>}
    {photoFilters && <div className="gallery-filters" role="group" aria-label="Виды фотосессий">
      {groups.map(([key,label])=><button key={key} aria-pressed={group===key} className={group===key?"active":""} onClick={()=>{setGroup(key);rail.current?.scrollTo({left:0});}}>{label}</button>)}
    </div>}
    <div ref={rail} className={"gallery-grid " + layout} aria-live="polite" tabIndex={layout.includes("photo-rail") ? 0 : undefined} aria-label={layout.includes("photo-rail") ? "Примеры фотосессий, прокручивайте горизонтально" : undefined}>
      {shown.length===0 && <p className="empty-gallery">Примеры этой категории готовятся.</p>}
      {shown.map(item=><figure key={item.id} className={"work-"+item.id}>
        <button className="gallery-open" aria-label={"Рассмотреть: "+item.title} onClick={e=>{trigger.current=e.currentTarget;setChosen(item);dialog.current?.showModal();trackEvent("gallery_view",{category:item.category});}}>
          <Picture src={item.poster} alt={item.title} sizes={layout.includes("postcard") ? "(max-width: 767px) 92vw, 46vw" : item.id === "mother-daughter" ? "(max-width: 767px) 78vw, 60vw" : "(max-width: 767px) 78vw, 30vw"}/>
          <span className="media-kind">{item.kind==="image"?"Рассмотреть":item.kind==="video"?"▶ Смотреть видео":"♫ Слушать"}</span>
        </button>
        <figcaption><WorkCaption item={item}/></figcaption>
      </figure>)}
    </div>
    {layout.includes("photo-rail") && <div className="rail-controls" aria-label="Прокрутка примеров">
      <button onClick={()=>rail.current?.scrollBy({left:-rail.current.clientWidth*.85,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'})}>Назад</button>
      <span>Выберите настроение. Нажмите на фото, чтобы рассмотреть.</span>
      <button onClick={()=>rail.current?.scrollBy({left:rail.current.clientWidth*.85,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'})}>Далее</button>
    </div>}
    <dialog ref={dialog} className="lightbox" aria-labelledby="lightbox-title" onCancel={()=>{setChosen(null);trigger.current?.focus();}} onClick={e=>{if(e.target===e.currentTarget)close();}}>
      <button className="close-dialog" onClick={close} aria-label="Закрыть просмотр">×</button>
      {chosen && <div><h2 id="lightbox-title">{chosen.title}</h2><MediaPlayer key={chosen.id} item={chosen}/>
        <p className="demo-notice">{chosen.presentation === "collage" ? "Персональная открытка — знакомое лицо и ваши тёплые слова." : chosen.presentation === "video-frame" ? "Посмотрите, как оживает семейный портрет." : "Реальный пример сервиса. Ваш подарок будет создан по вашим фото и словам."}</p></div>}
    </dialog>
  </>;
}
export function Comparison() {
  const [position, setPosition] = useState(50);
  const [kind, setKind] = useState("dream");
  const choices = [ ["card", "Открытка"], ["photo", "Нейрофотосессия"], ["dream", "Карта желаний"], ["cartoon", "Мультфильм"], ["animation", "Оживление фотографии"] ];
  return <div className="comparison-layout">
    <div className="comparison-copy"><p className="eyebrow">Из фотографии — в новую историю</p><h2>Знакомое лицо.<br/><em>Новые возможности.</em></h2>
      <p>Перемещайте разделитель и сравнивайте исходное фото с результатом.</p>
      <div className="comparison-tabs" role="group" aria-label="Варианты сравнения">
        {choices.map(([key,label]) => <button key={key} aria-pressed={kind === key} onClick={() => setKind(key)}>{label}</button>)}
      </div>
      <p className="small-note">Сравните преображение на карте желаний или посмотрите примеры других подарков.</p>
    </div>
    <div className="comparison-result">
      {kind === "dream" ? <div className="comparison" style={{ "--position": position + "%" } as React.CSSProperties}>
        <img src="/media/works/vision-before.webp" alt="Исходное фото женщины на набережной" width="600" height="720" loading="lazy" />
        <img className="comparison-after" src="/media/works/vision-after.webp" alt="Карта желаний, созданная по этому фото" width="600" height="720" loading="lazy" />
        <span className="compare-label before">До</span><span className="compare-label after">После</span>
        <div className="compare-divider" aria-hidden="true"><span>↔</span></div>
        <input type="range" min="0" max="100" value={position} aria-label="Сравнить исходник и результат" aria-valuetext={`${position}% исходника`} onChange={e=>setPosition(Number(e.target.value))}/>
      </div> : <div className="comparison-alternative">
        <MediaPlayer key={kind} item={media.find(item => item.id === ({ card:"card", photo:"family", cartoon:"mom", animation:"animation-horizontal" }[kind]))!} />
        <p className="small-note">{kind === "animation" ? "Посмотрите пример оживления семейной фотографии." : "Посмотрите готовую работу — ваш подарок создадим по вашим фотографиям и словам."}</p>
        {kind === "animation" && <a className="button button-outline" href="/animation">Об оживлении фото</a>}
      </div>}
    </div>
  </div>;
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
