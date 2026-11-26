export const site = {
  name: "Открытки и Карта желаний | Нейрофото",
  slogan: "Ваши фотографии. Ваши мечты. Наше волшебство.",
  origin: "https://app.wish-bot.ru",
  maxUrl: "https://max.ru/se14596751_1_bot",
  maxSource: "config.py:DEFAULT_BOT_LINK (main 6cc6c10), проверено 2026-11-26",
  logo: null as string | null,
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
}
export const media: MediaItem[] = [
  {
    id: "flowers",
    title: "Слова, которые хочется сохранить",
    category: "postcards",
    kind: "image",
    poster: "/media/postcards/bouquet.svg",
    demo: true,
  },
  {
    id: "sea",
    title: "Море за вашим окном",
    category: "vision-board",
    kind: "image",
    poster: "/media/vision-board/window.svg",
    demo: true,
  },
  {
    id: "home",
    title: "Тёплая семейная история",
    category: "photosessions",
    kind: "image",
    poster: "/media/photosessions/interior.svg",
    demo: true,
  },
  {
    id: "santa",
    title: "Послание из зимней сказки",
    category: "ded-moroz",
    kind: "video",
    poster: "/media/ded-moroz/winter.svg",
    demo: true,
  },
  {
    id: "mom",
    title: "Мультфильм для мамы",
    category: "cartoons",
    kind: "video",
    poster: "/media/cartoons/interior.svg",
    demo: true,
  },
  {
    id: "song",
    title: "Ваша история в музыке",
    category: "songs",
    kind: "audio",
    poster: "/media/songs/record.svg",
    demo: true,
  },
  {
    id: "music",
    title: "Поздравление, которое звучит",
    category: "musical-postcards",
    kind: "audio",
    poster: "/media/musical-postcards/record.svg",
    demo: true,
  },
  {
    id: "moment",
    title: "Ещё одно мгновение",
    category: "animation",
    kind: "video",
    poster: "/media/animation/frame.svg",
    demo: true,
  },
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
