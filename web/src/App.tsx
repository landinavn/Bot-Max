import { Home } from "./showcases.tsx";
import { useEffect, useState } from "react";
import { commonFAQ, products } from "./catalog.ts";
import { media } from "./config.ts";
import type { Route } from "./site.ts";
import {
  FAQ,
  Footer,
  Gallery,
  Header,
  OrderAction,
  ProductVisual,
  MobileNav,
  ProductGrid,
  SectionHeading,
} from "./components.tsx";
import { trackEvent } from "./analytics.ts";

function ProductPage({ route }: { route: Route }) {
  const p = route.product!;
  const [family, setFamily] = useState(false);
  useEffect(() => {
    if (location.hash === "#family") setFamily(true);
  }, []);
  return (
    <>
      <div className="container breadcrumbs">
        <a href="/">Главная</a>
        <span>/</span>
        <a href="/#catalog">Каталог</a>
        <span>/</span>
        <span>
          {route.path.includes("/new-year") ? "Новогодняя фотосессия" : p.name}
        </span>
      </div>
      <section className={"product-hero container " + p.color}>
        <div>
          <p className="eyebrow">{p.name}</p>
          <h1>{p.heading}</h1>
          <p className="lead">{p.intro}</p>
          <OrderAction
            product={p}
            label={
              p.id === "custom-order" ? "Обсудить идею в MAX" : "Создать в MAX"
            }
          />
          <p className="small-note">{p.pricing.note}</p>
          {p.id === "postcards" && (
            <p className="free-product-note">
              Первая генерация открытки — бесплатно. Проверка доступности — в
              MAX.
            </p>
          )}
        </div>
        <figure className={"product-main-visual visual-" + p.id}>
          <ProductVisual productId={p.id} eager playable />
          <figcaption>{p.id === "animation" ? "Пример оживления семейной фотографии" : p.id === "custom-order" ? "" : "Реальный пример · ваш результат создаётся по вашим фото и словам"}</figcaption>
        </figure>
      </section>
      {p.id === "ded-moroz" && (
        <section id="family" className="section container">
          <SectionHeading
            eyebrow="Чудо для каждого"
            title="Одному ребёнку или всей семье?"
            text="Каждому — своё имя, свои маленькие победы и добрые слова."
          />
          <div className="family-options">
            <button aria-pressed={!family} onClick={() => setFamily(false)}>
              <span>01</span>
              <strong>Одному ребёнку</strong>
              <p>Имя, несколько фактов и фотография в новогоднем оформлении.</p>
            </button>
            <button aria-pressed={family} onClick={() => setFamily(true)}>
              <span>02–06</span>
              <strong>Нескольким детям</strong>
              <p>Данные каждого по очереди. Общее фото можно пропустить.</p>
            </button>
          </div>
          <p className="family-detail">
            {family
              ? "Для каждого ребёнка укажите домашнее имя, возраст, пол, один факт и пожелание. Затем добавьте общий факт о детях. Перед озвучкой всё можно проверить."
              : "Расскажите об увлечениях, достижениях и качествах ребёнка. Факты можно проверить до озвучки, а затем прослушать готовый вариант."}
          </p>
          <OrderAction product={p} label="Выбрать вариант в MAX" />
        </section>
      )}
      <section className="section container">
        <SectionHeading
          eyebrow="Для ваших поводов"
          title="Идеи, с которых можно начать"
        />
        <div className="use-grid">
          {p.uses.map((use, index) => (
            <article key={use}>
              <span aria-hidden="true">✧</span>
              <h3>{use}</h3>
              <p>{p.features[index] ?? p.summary}</p>
            </article>
          ))}
        </div>
      </section>
      <section className="soft-section">
        <div className="container section">
          <SectionHeading
            eyebrow="Шаг за шагом"
            title="Как создаётся ваша история"
          />
          <ol className="product-steps">
            {p.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      </section>
      <section className="section container">
        <SectionHeading
          eyebrow="Вдохновение"
          title="Как это может выглядеть"
          text="Посмотрите примеры сервиса и выберите настроение для своего подарка."
        />
        {["postcards","photosessions","vision-board","animation"].includes(p.id) ? <Gallery items={media.filter(item=>item.category===p.id)} showCategories={false} photoFilters={p.id==="photosessions"} layout={p.id==="photosessions" ? "photo-rail" : p.id==="postcards" ? "postcard-collection" : "product-collection"}/> : <div className="product-example">
          <ProductVisual productId={p.id} />
          <div>
            <h3>{p.label}</h3>
            <p>{p.summary}</p>
            <a className="text-link" href="/gallery">
              Посмотреть галерею
            </a>
          </div>
        </div>}
      </section>
      <section className="container pricing-note">
        <div>
          <p className="eyebrow">Стоимость и доступные варианты</p>
          <h2>Точная цена — перед заказом.</h2>
          <p>
            В MAX вы увидите действующие тарифы и предложения. Мы не фиксируем
            цену на витрине, чтобы не вводить вас в заблуждение во время акций.
          </p>
        </div>
        <OrderAction product={p} label="Узнать стоимость в MAX" />
      </section>
      <section className="section container faq-section">
        <SectionHeading title="Что ещё важно знать" />
        <FAQ items={p.faq} />
      </section>
      <section className="section container">
        <SectionHeading
          eyebrow="Ещё немного волшебства"
          title="Может понравиться"
        />
        <ProductGrid
          items={products.filter((other) => other.id !== p.id).slice(0, 3)}
        />
      </section>
    </>
  );
}
function Prices() {
  return (
    <section className="section container">
      <p className="eyebrow">Подарок под ваш повод</p>
      <h1>Стоимость услуг</h1>
      <p className="page-intro">
        Выберите продукт — актуальная стоимость и доступные акции появятся в MAX
        перед оплатой. На сайте нет отдельного расчёта или формы платежа.
      </p>
      <div className="prices-list">
        {products
          .filter((p) => p.mode !== "coming_soon")
          .map((p) => (
            <article key={p.id}>
              <div>
                <h2>
                  <a href={p.path}>{p.name}</a>
                </h2>
                <p>
                  {p.id === "postcards"
                    ? "Первая генерация открытки бесплатная. Доступность проверяется в MAX."
                    : p.id === "custom-order"
                      ? "Индивидуальный расчёт после обсуждения идеи."
                      : "Актуальные тарифы и условия — при выборе в MAX."}
                </p>
              </div>
              <OrderAction
                product={p}
                label="Посмотреть в MAX"
                className="button button-outline"
              />
            </article>
          ))}
      </div>
      <div className="faq-section">
        <SectionHeading title="Об оплате" />
        <FAQ items={commonFAQ.slice(1, 4)} />
      </div>
    </section>
  );
}
function PrivatePage({ route }: { route: Route }) {
  return (
    <section className="section container private-page">
      <p className="eyebrow">Скоро на сайте</p>
      <h1>
        {route.path === "/login"
          ? "Личный кабинет готовится к открытию."
          : route.path === "/account/works"
            ? "Ваши работы будут здесь."
            : route.path === "/account/balance"
              ? "Баланс появится после подключения кабинета."
              : route.path === "/account/settings"
                ? "Настройки кабинета готовятся."
                : "Рады встрече. Кабинет пока не подключён."}
      </h1>
      <p>
        Сейчас создавать подарки и получать результаты можно в действующем
        MAX-боте. На этой странице мы не собираем почту, фотографии и данные для
        оплаты.
      </p>
      <OrderAction label="Перейти в MAX" />
      <a href="/#catalog" className="text-link">
        Вернуться к каталогу
      </a>
    </section>
  );
}
export function App({ route }: { route: Route }) {
  useEffect(() => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || !('IntersectionObserver' in window)) return;
    const elements = Array.from(document.querySelectorAll<HTMLElement>('.reveal'));
    const observer = new IntersectionObserver(entries=>entries.forEach(entry=>{
      if(entry.isIntersecting){(entry.target as HTMLElement).dataset.reveal='shown';observer.unobserve(entry.target);}
    }), {rootMargin:'0px 0px 80px 0px', threshold:0.03});
    elements.forEach(element=>{element.dataset.reveal='pending';observer.observe(element);});
    return ()=>{observer.disconnect();elements.forEach(element=>delete element.dataset.reveal);};
  }, []);
  useEffect(() => {
    if (route.kind === "product" && route.product)
      trackEvent("product_view", { product: route.product.id });
  }, [route]);
  return (
    <>
      <a className="skip-link" href="#main">
        Перейти к содержимому
      </a>
      <Header />
      <main id="main">
        {route.kind === "home" ? (
          <Home />
        ) : route.kind === "product" ? (
          <ProductPage route={route} />
        ) : route.kind === "prices" ? (
          <Prices />
        ) : route.kind === "gallery" ? (
          <section className="section container">
            <p className="eyebrow">Немного вдохновения</p>
            <h1>У подарков есть характер.</h1>
            <p className="page-intro">
              Открытки, фотосессии, мультфильмы и песни, созданные сервисом. Откройте работу, чтобы рассмотреть или включить запись.
            </p>
            <Gallery />
          </section>
        ) : route.kind === "blog" ? (
          <section className="section container journal">
            <p className="eyebrow">Вдохновение рядом</p>
            <h1>
              Журнал о подарках
              <br />и маленьких чудесах.
            </h1>
            <p className="page-intro">
              Здесь появятся идеи для поздравлений, советы по выбору фотографий
              и истории о мечтах. Первые материалы готовятся.
            </p>
            <div className="journal-topics">
              <span>Подарки для близких</span>
              <span>Фотографии с настроением</span>
              <span>Мечты и вдохновение</span>
            </div>
            <a href="/#catalog" className="button">
              Найти свою идею
            </a>
          </section>
        ) : route.kind === "private" ? (
          <PrivatePage route={route} />
        ) : (
          <section className="section container private-page">
            <p className="eyebrow">Кажется, эта история ещё не написана</p>
            <h1>Страница не найдена.</h1>
            <a href="/" className="button">
              На главную
            </a>
          </section>
        )}
      </main>
      <Footer />
      <MobileNav />
    </>
  );
}
