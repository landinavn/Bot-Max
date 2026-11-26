import { useEffect, useState } from "react";
import { commonFAQ, products } from "./catalog.ts";
import { season, site, media, seasonalCards } from "./config.ts";
import type { Route } from "./site.ts";
import {
  Comparison,
  FAQ,
  Footer,
  Gallery,
  Header,
  OrderAction,
  Picture,
  ProductVisual,
  MobileNav,
  ProductGrid,
  SectionHeading,
} from "./components.tsx";
import { trackEvent } from "./analytics.ts";

function Home() {
  return (
    <>
      <section className="hero container">
        <div className="hero-copy">
          <p className="eyebrow">Персональные подарки с искусственным интеллектом</p>
          <h1>Маленькое волшебство <em>для больших эмоций</em></h1>
          <p className="hero-description">Превращайте обычные фотографии в уникальные открытки, фотосессии, мультфильмы и подарки для близких</p>
          <div className="hero-actions"><OrderAction product={products[0]} label="Создать открытку бесплатно"/><a className="button button-outline" href="/#catalog">Посмотреть все возможности</a></div>
          <p className="trial-note">Сейчас создание доступно в MAX.<br/>Первая генерация открытки — бесплатно. Другие продукты оплачиваются отдельно.</p>
        </div>
        <div className="hero-art" aria-label="Реальные примеры подарков">
          <figure className="hero-feature"><Picture src="/media/works/family-1.webp" alt="Пример новогодней семейной нейрофотосессии" eager/><figcaption><span>Ваши фотографии</span><strong>Совсем другая история</strong></figcaption></figure>
          <div className="hero-mini-grid">
            {[["card", "Открытки"],["mom", "Мультик"],["santa", "Дед Мороз"],["dream", "Карты желаний"]].map(([id,label])=> { const item=media.find(m=>m.id===id)!; return <a href={id === "card" ? "/postcards" : id === "mom" ? "/cartoons" : id === "santa" ? "/ded-moroz" : "/vision-board"} key={id}><Picture src={item.poster} alt={item.title}/><span>{label}</span></a>; })}
          </div>
        </div>
      </section>
      <div className="promise-strip"><div className="container"><span>Ваше фото</span><span>Ваши слова</span><span>Подарок с личной историей</span></div></div>
      {season.enabled && <section className="season-section container"><div className="season-panel">
        <div className="season-heading"><p className="eyebrow">{season.eyebrow} · 2027</p><h2>Дарите тепло.<br/><em>Создавайте чудеса.</em></h2><p>{season.text}</p></div>
        <div className="season-grid">{seasonalCards.map(card=><a className="season-card" key={card.path} href={card.path}>
          <Picture src={media.find(m=>m.id===card.mediaId)!.poster} alt={media.find(m=>m.id===card.mediaId)!.title}/>
          <div><h3>{card.title}</h3><p>{card.text}</p><span className="card-cta">{card.action} <span aria-hidden="true">↗</span></span></div>
        </a>)}</div>
      </div></section>}
      <section id="catalog" className="section container">
        <SectionHeading
          eyebrow="Каждому чувству — свой подарок"
          title="Что создадим сегодня?"
          text="Для большого праздника. Для маленького «спасибо». Или просто потому, что хочется порадовать."
        />
        <ProductGrid />
      </section>
      <section className="comparison-section">
        <div className="container">
          <Comparison />
        </div>
      </section>
      <section id="how" className="section container">
        <SectionHeading
          eyebrow="Проще, чем кажется"
          title="От вашей идеи — к тёплым эмоциям."
        />
        <div className="steps">
          <article>
            <span>01</span>
            <h3>Выберите свою историю</h3>
            <p>
              Открытка, мечта, песня или мультфильм — начните с того, что
              откликается.
            </p>
          </article>
          <article>
            <span>02</span>
            <h3>Добавьте немного личного</h3>
            <p>
              Загрузите фото и расскажите о важном. Точный порядок подскажем в
              выбранном продукте.
            </p>
          </article>
          <article>
            <span>03</span>
            <h3>Получите свой подарок</h3>
            <p>В текущей версии готовый результат приходит в чат MAX.</p>
          </article>
        </div>
      </section>
      <section className="gallery-section section">
        <div className="container">
          <div className="heading-row">
            <SectionHeading
              eyebrow="Идеи для вдохновения"
              title="У каждого подарка свой характер."
            />
            <a className="text-link" href="/gallery">
              Вся галерея
            </a>
          </div>
          <Gallery compact />
        </div>
      </section>
      <section className="section container benefits">
        <div>
          <p className="eyebrow">С вниманием к вашей истории</p>
          <h2>
            Самое ценное
            <br />в подарке — <em>личное.</em>
          </h2>
          <p>
            Имя, знакомое лицо, общая мечта или любимая фраза. Детали, благодаря
            которым обычный повод становится особенным.
          </p>
        </div>
        <div className="benefit-list">
          {[
            [
              "Ваши детали",
              "Фотография и слова помогают создать персональную историю.",
            ],
            [
              "Разные способы порадовать",
              "От открытки до мультфильма — под настроение и повод.",
            ],
            [
              "Для себя и близких",
              "Чтобы сказать спасибо, поддержать мечту или удивить.",
            ],
            [
              "Можно начать с открытки",
              "Первая генерация открытки бесплатная; другие продукты оплачиваются отдельно.",
            ],
          ].map(([title, text]) => (
            <article key={title}>
              <span aria-hidden="true">✧</span>
              <div>
                <h3>{title}</h3>
                <p>{text}</p>
              </div>
            </article>
          ))}
        </div>
      </section>
      <section className="section container faq-section">
        <SectionHeading
          eyebrow="Осталось чуть-чуть узнать"
          title="Частые вопросы"
        />
        <FAQ items={commonFAQ} />
      </section>
      <section className="closing-section">
        <div className="container">
          <p className="eyebrow">Повод не обязательно искать</p>
          <h2>
            Порадуйте того,
            <br />
            кто вам <em>дорог.</em>
          </h2>
          <OrderAction product={products[0]} label="Начать с открытки" />
          <p>Сейчас создание доступно в MAX.</p>
        </div>
      </section>
    </>
  );
}
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
          <figcaption>{p.id === "animation" || p.id === "custom-order" ? "" : "Реальный пример · ваш результат создаётся по вашим фото и словам"}</figcaption>
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
        <div className="product-example">
          <ProductVisual productId={p.id} />
          <div>
            <h3>{p.label}</h3>
            <p>{p.summary}</p>
            <a className="text-link" href="/gallery">
              Посмотреть галерею
            </a>
          </div>
        </div>
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
