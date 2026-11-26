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
}
export const media: MediaItem[] = [
  { id: "mother-card", title: "Открытка ко Дню матери · исходник и результат", category: "postcards", kind: "image", poster: "/media/works/postcard-mother.webp", demo: false },
  { id: "ny-card", title: "Новогодняя открытка · кадр музыкального примера", category: "postcards", kind: "image", poster: "/media/works/musical-card.webp", demo: false },
  { id: "dream", title: "Карта желаний: путешествия и новые возможности", category: "vision-board", kind: "image", poster: "/media/works/vision-after.webp", demo: false },
  { id: "family", title: "Семейная новогодняя фотосессия", category: "photosessions", kind: "image", poster: "/media/works/family-1.webp", demo: false },
  { id: "mom", title: "Мультфильм для мамы · 1 минута", category: "cartoons", kind: "video", poster: "/media/works/mother-cartoon.webp", src: "/media/works/mother-cartoon.mp4", demo: false },
  { id: "santa", title: "Дед Мороз поздравляет нескольких детей", category: "ded-moroz", kind: "video", poster: "/media/works/santa-family.webp", src: "/media/works/santa-family.mp4", demo: false },
  { id: "card", title: "Открытка на день рождения · исходник и результат", category: "postcards", kind: "image", poster: "/media/works/postcard-birthday.webp", demo: false },
  { id: "family-2", title: "Светлая семейная фотосессия", category: "photosessions", kind: "image", poster: "/media/works/family-2.webp", demo: false },
  { id: "family-3", title: "Новогодняя история дома", category: "photosessions", kind: "image", poster: "/media/works/family-3.webp", demo: false },
  { id: "birthday", title: "Мульт-открытка на день рождения", category: "cartoons", kind: "video", poster: "/media/works/birthday-cartoon.webp", src: "/media/works/birthday-cartoon.mp4", demo: false },
  { id: "music", title: "Новогодняя музыкальная открытка", category: "musical-postcards", kind: "video", poster: "/media/works/musical-card.webp", src: "/media/works/musical-card.mp4", demo: false },
  { id: "song", title: "Песня для мамы · минутный фрагмент из мультфильма", category: "songs", kind: "audio", poster: "/media/works/mother-cartoon.webp", src: "/media/works/mother-song.mp3", demo: false },
];
export const productMedia: Record<string, string> = {
  postcards: "card", "vision-board": "dream", photosessions: "family",
  "musical-postcards": "music", songs: "song", cartoons: "mom", "ded-moroz": "santa",
};
export function exampleFor(productId: string) {
  return media.find(item => item.id === productMedia[productId]);
}
export const seasonalCards = [
  { title: "Дед Мороз", text: "Личное видеопоздравление для 1–6 детей", path: "/ded-moroz", mediaId: "santa", action: "Выбрать поздравление" },
  { title: "Новогодняя фотосессия", text: "Семейные образы и огоньки праздника", path: "/photosessions/new-year", mediaId: "family", action: "Выбрать сюжет" },
  { title: "Новогодние открытки", text: "Ваше фото и тёплые слова для близких", path: "/postcards", mediaId: "ny-card", action: "Создать открытку" },
  { title: "Карта желаний 2027", text: "Ваша фотография в истории о мечте", path: "/vision-board", mediaId: "dream", action: "Навстречу мечте" },
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
