import { commonFAQ, products } from "./catalog.ts";
import { media, season, seasonalCards } from "./config.ts";
import { Comparison, FAQ, Gallery, MediaPlayer, OrderAction, Picture, ProductGrid, SectionHeading, WorkCaption } from "./components.tsx";

const work = (id: string) => media.find(item => item.id === id)!;
const product = (id: string) => products.find(item => item.id === id)!;
export function AnimationShowcase() {
  return <section id="animation-stories" className="section memory-section reveal">
    <div className="container">
      <SectionHeading eyebrow="Тепло, которое остаётся с нами" title="Оживите самые дорогие воспоминания" text="Превратите старые семейные фотографии в трогательные живые истории"/>
      <div className="memory-examples">{media.filter(item=>item.category==="animation").map(item=><figure key={item.id}>
        <MediaPlayer item={item}/><figcaption><WorkCaption item={item}/></figcaption>
      </figure>)}</div>
      <div className="section-action"><OrderAction product={product('animation')} label="Оживить свою фотографию"/><p>Сохраните семейную историю — и поделитесь ею с теми, кто дорог.</p></div>
    </div>
  </section>;
}
export function PhotosShowcase() {
  return <section id="photosession-gallery" className="section container photo-showcase reveal">
    <SectionHeading eyebrow="Новый взгляд на знакомые лица" title="Фотосессия мечты — без фотостудии" text="Семейный вечер, яркий портрет или особенный образ для мамы и дочки. Выберите историю, в которой хочется оказаться."/>
    <Gallery items={media.filter(item=>item.category==='photosessions')} photoFilters showCategories={false} layout="photo-rail"/>
    <div className="section-action"><OrderAction product={product('photosessions')} label="Создать свою фотосессию"/><a className="text-link" href="/photosessions">О нейрофотосессиях</a></div>
  </section>;
}
export function Home() {
  return <>
    <section className="hero hero-v13 container">
      <div className="hero-copy">
        <p className="eyebrow">Искусственный интеллект. Настоящие чувства.</p>
        <h1>Маленькое волшебство <em>для больших эмоций</em></h1>
        <p className="hero-description">Превращайте обычные фотографии в уникальные открытки, фотосессии, мультфильмы и подарки для близких</p>
        <div className="hero-actions"><OrderAction product={product('postcards')} label="Создать открытку бесплатно"/><a className="button button-outline" href="/#catalog">Посмотреть все возможности</a></div>
        <p className="trial-note">Создание — в MAX. Первая генерация открытки бесплатная.<br/>Другие продукты оплачиваются отдельно.</p>
      </div>
      <div className="hero-mosaic" aria-label="Реальные примеры персональных подарков">
        <figure className="hero-feature"><Picture src={work('family-2').poster} alt="Светлая семейная нейрофотосессия, коллекция кадров" eager sizes="(max-width: 767px) 52vw, 30vw"/><figcaption>Близкие люди. Новые истории.</figcaption></figure>
        {[["woman-red","Индивидуальная фотосессия","/photosessions","woman"],["granddaughter","Открытка для ребёнка","/postcards","child"],["seaside-home","Место для мечты","/vision-board","dream"],["santa","Поздравление Деда Мороза","/ded-moroz","santa"]].map(([id,label,path,style])=><a className={'hero-tile hero-'+style} href={path} key={id}><Picture src={work(id).poster} alt={label} sizes="(max-width: 767px) 28vw, 14vw"/><span>{label}</span></a>)}
        <span className="mosaic-spark" aria-hidden="true">✧</span>
      </div>
    </section>
    <div className="promise-strip"><div className="container"><span>Ваше фото</span><span>Ваши слова</span><span>Подарок с личной историей</span></div></div>
    {season.enabled && <section id="new-year" className="season-section container reveal"><div className="season-panel season-v13">
      <div className="season-heading"><div><p className="eyebrow">Новогодняя коллекция · 2027</p><h2>Дарите тепло.<br/><em>Создавайте чудеса.</em></h2></div><p>{season.text}</p></div>
      <div className="season-grid">{seasonalCards.map(card=><a className="season-card" key={card.path} href={card.path}><Picture src={work(card.mediaId).poster} alt={work(card.mediaId).title} sizes="(max-width: 767px) 86vw, 23vw"/><div><h3>{card.title}</h3><p>{card.text}</p><span className="card-cta">{card.action}</span></div></a>)}</div>
    </div></section>}
    <section id="santa-story" className="section container santa-showcase reveal">
      <div className="santa-copy"><p className="eyebrow">Пусть сказка обратится по имени</p><h2>А что, если Дед Мороз знает <em>имя вашего ребёнка?</em></h2><p>Дедушка Мороз поздравит вашего ребёнка лично, расскажет о его увлечениях, достижениях и добавит его фотографию в волшебное видео</p><ul><li>Имя и индивидуальные факты о ребёнке</li><li>Поздравление одному ребёнку или нескольким детям</li><li>Проверка данных и озвучки перед созданием видео</li></ul><OrderAction product={product('ded-moroz')} label="Заказать персональное поздравление"/><a className="text-link" href="/ded-moroz#family">Поздравить нескольких детей</a></div>
      <figure><MediaPlayer item={work('santa')}/><figcaption>Реальный пример поздравления для нескольких детей</figcaption></figure>
    </section>
    <section id="catalog" className="section container reveal"><SectionHeading eyebrow="Каждому чувству — свой подарок" title="Что создадим сегодня?" text="Для большого праздника. Для маленького «спасибо». Или просто потому, что хочется порадовать."/><ProductGrid/></section>
    <PhotosShowcase/>
    <section id="postcard-stories" className="section postcard-section reveal"><div className="container">
      <SectionHeading eyebrow="Подарок, который узнают с первого взгляда" title="Обычная фотография. Необыкновенный подарок" text="Семье, маме, подруге или любимому человеку. В каждой открытке — знакомое лицо и ваши тёплые слова."/>
      <Gallery items={media.filter(item=>item.category==='postcards' && item.presentation==='collage')} showCategories={false} layout="postcard-collection"/>
      <div className="postcard-result"><Picture src={work('granddaughter').poster} alt={work('granddaughter').title} sizes="(max-width: 767px) 60vw, 20vw"/><div><p className="eyebrow">А это — готовая открытка</p><h3>Маленький праздник для внучки</h3><p>Знакомая улыбка, праздничное оформление и тёплые слова — открытка, которую захочется сохранить.</p><OrderAction product={product('postcards')} label="Создать открытку бесплатно"/><p className="small-note">Бесплатна первая генерация открытки. Доступность проверяется в MAX.</p></div></div>
    </div></section>
    <section id="vision-stories" className="section dream-section reveal"><div className="container">
      <SectionHeading eyebrow="Дайте мечтам место в вашей жизни" title="Каким будет ваш 2027 год?" text="Дом у моря, любимое дело, новые возможности. Представьте себя в истории, которую хочется написать."/>
      <div className="dream-examples">{['financial-dream','seaside-home'].map(id=><figure key={id}><Picture src={work(id).poster} alt={work(id).title} sizes="(max-width: 767px) 80vw, 32vw"/><figcaption>{work(id).title}</figcaption></figure>)}<div className="dream-copy"><span aria-hidden="true">✧</span><h3>Сначала — увидеть.<br/>Потом — сделать шаг.</h3><p>Карта желаний помогает визуализировать ваши идеи. Она не обещает, что мечты исполнятся сами.</p><OrderAction product={product('vision-board')} label="Создать карту желаний"/></div></div>
      <div className="verified-comparison"><Comparison/></div>
    </div></section>
    <AnimationShowcase/>
    <section id="musical-gifts" className="section container music-showcase reveal"><SectionHeading eyebrow="Когда хочется сказать больше" title="Подарки, которые звучат и оживают" text="Мультфильм, личная песня или открытка с музыкой. Ваши слова могут стать целой историей."/>
      <div className="music-examples">{[['mom','cartoons','Создать мультфильм'],['music','musical-postcards','Создать музыкальную открытку'],['song','songs','Создать песню']].map(([id,pid,label])=><article key={id}><MediaPlayer item={work(id)}/><h3>{product(pid).name}</h3><p>{id==='song'?'Минутный фрагмент песни из мультфильма для мамы.':work(id).title}</p><OrderAction product={product(pid)} label={label} className="button button-outline"/></article>)}</div>
    </section>
    <section id="how" className="section container reveal"><SectionHeading eyebrow="Проще, чем кажется" title="От вашей идеи — к тёплым эмоциям."/><div className="steps">{[['Выберите свою историю','Открытка, мечта, песня или мультфильм — начните с того, что откликается.'],['Добавьте немного личного','Отправьте фото и расскажите о важном. Порядок шагов подскажет выбранный продукт в MAX.'],['Получите свой подарок','Готовый результат приходит в чат MAX. Сохраните его и подарите близким.']].map(([title,text],i)=><article key={title}><span>{String(i+1).padStart(2,'0')}</span><h3>{title}</h3><p>{text}</p></article>)}</div></section>
    <section id="home-pricing" className="container pricing-note reveal"><div><p className="eyebrow">Подарок под ваш повод</p><h2>Выберите эмоцию.<br/>Стоимость уточним перед заказом.</h2><p>Первая генерация открытки бесплатная. Актуальные тарифы и условия остальных услуг показываем в MAX до оплаты.</p><a className="text-link" href="/prices">Все услуги и условия</a></div><OrderAction label="Выбрать подарок в MAX"/></section>
    <section id="home-faq" className="section container faq-section reveal"><SectionHeading eyebrow="Осталось чуть-чуть узнать" title="Частые вопросы"/><FAQ items={commonFAQ}/></section>
    <section className="closing-section"><div className="container"><p className="eyebrow">Повод не обязательно искать</p><h2>Порадуйте того,<br/>кто вам <em>дорог.</em></h2><OrderAction product={product('postcards')} label="Начать с открытки"/><p>Создавайте подарки в MAX — и делитесь настоящими эмоциями.</p></div></section>
  </>;
}
