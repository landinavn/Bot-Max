export const site = {
  name: "Открытки и Карта желаний | Нейрофото",
  slogan: "Ваши фотографии. Ваши мечты. Наше волшебство.",
  origin: "https://app.wish-bot.ru",
  maxUrl: "https://max.ru/se14596751_1_bot",
  maxSource: "config.py:DEFAULT_BOT_LINK (main 6cc6c10), проверено 2026-11-26",
  logo: "/media/logo/bot-logo.webp" as string | null,
  webOrdering: false,
  readyForIndexing: false,
  legal: { offer: null, privacy: null, consent: null } as Record<
    string,
    string | null
  >,
  contacts: null as string | null,
};
export const season = {
  enabled: true,
  eyebrow: "Сезон тёплых чудес",
  title: "Новый год начинается с маленького волшебства.",
  text: "Поздравление из сказки, семейный образ у ёлки или мечта о большем. Выберите свою новогоднюю историю.",
  links: [
    ["Поздравление ребёнку", "/ded-moroz"],
    ["Для нескольких детей", "/ded-moroz#family"],
    ["Новогодняя фотосессия", "/photosessions/new-year"],
    ["Новогодняя открытка", "/postcards"],
    ["Карта желаний — 2027", "/vision-board"],
  ],
};
export type Category =
  | "all"
  | "postcards"
  | "vision-board"
  | "photosessions"
  | "ded-moroz"
  | "cartoons"
  | "songs"
  | "musical-postcards"
  | "animation";
export interface MediaItem {
  id: string;
  title: string;
  category: Category;
  kind: "image" | "video" | "audio";
  poster: string;
  src?: string;
  avif?: string;
  webp?: string;
  demo: boolean;
  photoGroup?: "family" | "women" | "children" | "mother-daughter" | "couple";
  presentation?: "collage" | "result" | "video-frame";
}
export const media: MediaItem[] = [
  {
    "id": "card",
    "title": "Открытка подруге · рекламный коллаж с исходником и результатом",
    "category": "postcards",
    "kind": "image",
    "presentation": "collage",
    "poster": "/media/works/postcard-birthday-friends-before-after.webp",
    "demo": false
  },
  {
    "id": "ny-card",
    "title": "Новогодняя открытка для семьи · рекламный коллаж",
    "category": "postcards",
    "kind": "image",
    "presentation": "collage",
    "poster": "/media/works/postcard-new-year-family-before-after.webp",
    "demo": false
  },
  {
    "id": "mother-card",
    "title": "Новогодняя открытка для мамы · рекламный коллаж",
    "category": "postcards",
    "kind": "image",
    "presentation": "collage",
    "poster": "/media/works/postcard-new-year-mother-before-after.webp",
    "demo": false
  },
  {
    "id": "ny-child",
    "title": "Новогодняя открытка для ребёнка · рекламный коллаж",
    "category": "postcards",
    "kind": "image",
    "presentation": "collage",
    "poster": "/media/works/postcard-new-year-child-before-after.webp",
    "demo": false
  },
  {
    "id": "ny-couple",
    "title": "Новогодняя открытка для пары · рекламный коллаж",
    "category": "postcards",
    "kind": "image",
    "presentation": "collage",
    "poster": "/media/works/postcard-new-year-couple-before-after.webp",
    "demo": false
  },
  {
    "id": "granddaughter",
    "title": "Новогодняя открытка для внучки · готовая работа",
    "category": "postcards",
    "kind": "image",
    "presentation": "result",
    "poster": "/media/works/postcard-new-year-granddaughter.webp",
    "demo": false
  },
  {
    "id": "dream",
    "title": "Карта желаний: путешествия и новые возможности",
    "category": "vision-board",
    "kind": "image",
    "poster": "/media/works/vision-after.webp",
    "demo": false
  },
  {
    "id": "financial-dream",
    "title": "Карта желаний · финансовая мечта",
    "category": "vision-board",
    "kind": "image",
    "poster": "/media/works/vision-board-financial-dream.webp",
    "demo": false
  },
  {
    "id": "seaside-home",
    "title": "Карта желаний · дом у моря",
    "category": "vision-board",
    "kind": "image",
    "poster": "/media/works/vision-board-seaside-home.webp",
    "demo": false
  },
  {
    "id": "family",
    "title": "Семейная новогодняя фотосессия · коллекция кадров",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "family",
    "poster": "/media/works/family-1.webp",
    "demo": false
  },
  {
    "id": "family-2",
    "title": "Светлая семейная фотосессия · коллекция кадров",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "family",
    "poster": "/media/works/photoshoot-family-light.webp",
    "demo": false
  },
  {
    "id": "family-3",
    "title": "Уютная семейная фотосессия · коллекция кадров",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "family",
    "poster": "/media/works/photoshoot-family-cozy.webp",
    "demo": false
  },
  {
    "id": "mother-daughter",
    "title": "Мама и дочка · вечерняя элегантность",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "mother-daughter",
    "poster": "/media/works/photoshoot-mother-daughter-elegant.webp",
    "demo": false
  },
  {
    "id": "mother-daughter-red",
    "title": "Мама и дочка · красное и белое",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "mother-daughter",
    "poster": "/media/works/photoshoot-mother-daughter-red.webp",
    "demo": false
  },
  {
    "id": "child-candy",
    "title": "Детский портрет · маленький праздник",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "children",
    "poster": "/media/works/photoshoot-child-candy.webp",
    "demo": false
  },
  {
    "id": "woman-red",
    "title": "Индивидуальная фотосессия · в красном",
    "category": "photosessions",
    "kind": "image",
    "photoGroup": "women",
    "poster": "/media/works/photoshoot-woman-red.webp",
    "demo": false
  },
  {
    "id": "mom",
    "title": "Мультфильм для мамы · 1 минута",
    "category": "cartoons",
    "kind": "video",
    "poster": "/media/works/mother-cartoon.webp",
    "demo": false,
    "src": "/media/works/mother-cartoon.mp4"
  },
  {
    "id": "birthday",
    "title": "Мульт-открытка на день рождения",
    "category": "cartoons",
    "kind": "video",
    "poster": "/media/works/birthday-cartoon.webp",
    "demo": false,
    "src": "/media/works/birthday-cartoon.mp4"
  },
  {
    "id": "santa",
    "title": "Дед Мороз поздравляет нескольких детей",
    "category": "ded-moroz",
    "kind": "video",
    "poster": "/media/works/santa-family.webp",
    "demo": false,
    "src": "/media/works/santa-family.mp4"
  },
  {
    "id": "music",
    "title": "Новогодняя музыкальная открытка",
    "category": "musical-postcards",
    "kind": "video",
    "poster": "/media/works/musical-card.webp",
    "demo": false,
    "src": "/media/works/musical-card.mp4"
  },
  {
    "id": "song",
    "title": "Песня для мамы · минутный фрагмент из мультфильма",
    "category": "songs",
    "kind": "audio",
    "src": "/media/works/mother-song.mp3",
    "poster": "/media/works/mother-cartoon.webp",
    "demo": false
  },
  {
    "id": "animation-vertical",
    "title": "Семейный портрет · вертикальное видео, 5 секунд",
    "category": "animation",
    "kind": "video",
    "presentation": "video-frame",
    "poster": "/media/works/animation-family-portrait-vertical.webp",
    "demo": false,
    "src": "/media/works/animation-family-portrait-vertical.mp4"
  },
  {
    "id": "animation-horizontal",
    "title": "Семейный портрет · горизонтальное видео, 5 секунд",
    "category": "animation",
    "kind": "video",
    "presentation": "video-frame",
    "poster": "/media/works/animation-family-portrait-horizontal.webp",
    "demo": false,
    "src": "/media/works/animation-family-portrait-horizontal.mp4"
  }
];
export const productMedia: Record<string, string> = {
  postcards: "ny-child", "vision-board": "financial-dream", photosessions: "mother-daughter-red", animation: "animation-horizontal",
  "musical-postcards": "music", songs: "song", cartoons: "mom", "ded-moroz": "santa",
};
export function exampleFor(productId: string) {
  return media.find(item => item.id === productMedia[productId]);
}
export const seasonalCards = [
  { title: "Дед Мороз", text: "Личное видеопоздравление для 1–6 детей", path: "/ded-moroz", mediaId: "santa", action: "Выбрать поздравление" },
  { title: "Новогодняя фотосессия", text: "Семейные образы и огоньки праздника", path: "/photosessions/new-year", mediaId: "family-3", action: "Выбрать сюжет" },
  { title: "Новогодние открытки", text: "Ваше фото и тёплые слова для близких", path: "/postcards", mediaId: "ny-card", action: "Создать открытку" },
  { title: "Карта желаний 2027", text: "Ваша фотография в истории о мечте", path: "/vision-board", mediaId: "seaside-home", action: "Навстречу мечте" },
];
export const categoryLabels: Record<Category, string> = {
  all: "Все работы",
  postcards: "Открытки",
  "vision-board": "Мечты",
  photosessions: "Фотосессии",
  "ded-moroz": "Дед Мороз",
  cartoons: "Мультфильмы",
  songs: "Песни",
  "musical-postcards": "С музыкой",
  animation: "Оживление",
};
