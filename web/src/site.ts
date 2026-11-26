import { products } from "./catalog.ts";
import type { Product } from "./catalog.ts";
import { site } from "./config.ts";
export interface Route {
  path: string;
  title: string;
  description: string;
  kind:
    "home" | "product" | "gallery" | "prices" | "blog" | "private" | "notfound";
  product?: Product;
  noindex?: boolean;
}
const productTitles: Record<string, string> = {
  postcards: "Персональные открытки с вашим фото — Нейрофото",
  "vision-board": "Карта желаний по фотографии — Нейрофото",
  photosessions: "Нейрофотосессии по фото — Нейрофото",
  "musical-postcards": "Музыкальная открытка с персональной песней — Нейрофото",
  songs: "Персональная песня с вашими словами — Нейрофото",
  animation: "Оживление фотографий — Нейрофото",
  cartoons: "Персональные мультфильмы по фото — Нейрофото",
  "ded-moroz": "Поздравление Деда Мороза для ребёнка или семьи — Нейрофото",
  "custom-order": "Индивидуальный творческий подарок — Нейрофото",
};
export const routes: Route[] = [
  {
    path: "/",
    title: "Персональные открытки, песни и подарки по фото — Нейрофото",
    description:
      "Открытки с вашим фото, карты желаний, нейрофотосессии, песни и видеопоздравления Деда Мороза. Выберите персональный творческий подарок.",
    kind: "home",
  },
  ...products.map((product) => ({
    path: product.path,
    title: productTitles[product.id] ?? product.name + " — Нейрофото",
    description: product.summary + " " + product.intro,
    kind: "product" as const,
    product,
    noindex: product.mode === "coming_soon",
  })),
  {
    path: "/photosessions/new-year",
    title: "Новогодняя нейрофотосессия — семейные образы | Нейрофото",
    description:
      "Новогодние семейные сюжеты по фотографиям: мама и дочка, семейный образ и праздничное настроение. Выбор доступных вариантов в MAX.",
    kind: "product",
    product: {
      ...products.find((p) => p.id === "photosessions")!,
      heading: "Сохраните это новогоднее тепло.",
      intro:
        "Ёлка, огоньки и ощущение дома. Выберите доступный новогодний сюжет в MAX — для семьи, мамы и дочки или другого праздничного образа.",
    },
  },
  {
    path: "/gallery",
    title: "Вдохновение и примеры творческих подарков — Нейрофото",
    description:
      "Галерея открыток, карт желаний и идей персональных подарков. На этапе подготовки представлены демонстрационные иллюстрации.",
    kind: "gallery",
    noindex: true,
  },
  {
    path: "/prices",
    title: "Каталог услуг и актуальная стоимость — Нейрофото",
    description:
      "Как узнать стоимость открыток, песен, нейрофотосессий и видео. Актуальная цена и доступные условия подтверждаются в MAX перед оплатой.",
    kind: "prices",
  },
  {
    path: "/blog",
    title: "Журнал о подарках и мечтах — Нейрофото",
    description:
      "Будущие статьи о персональных подарках, фотографиях и творческих идеях. Раздел готовится к публикации.",
    kind: "blog",
    noindex: true,
  },
  ...[
    "/login",
    "/account",
    "/account/works",
    "/account/balance",
    "/account/settings",
  ].map((path) => ({
    path,
    title: "Личный кабинет — Нейрофото",
    description:
      "Веб-кабинет ещё не подключён. Заказы доступны в действующем MAX-боте.",
    kind: "private" as const,
    noindex: true,
  })),
];
export function routeFor(path: string): Route {
  const clean = path.replace(/\/+$/, "") || "/";
  return (
    routes.find((r) => r.path === clean) ?? {
      path: clean,
      title: "Страница не найдена — Нейрофото",
      description: "Выберите продукт в каталоге творческих подарков.",
      kind: "notfound",
      noindex: true,
    }
  );
}
export function validateCatalogue(items: Product[] = products) {
  if (
    new Set(items.map((p) => p.id)).size !== items.length ||
    new Set(items.map((p) => p.path)).size !== items.length
  )
    throw Error("Duplicate product");
  const max = new URL(site.maxUrl);
  if (
    max.protocol !== "https:" ||
    max.hostname !== "max.ru" ||
    max.search ||
    max.hash
  )
    throw Error("Invalid MAX URL");
  for (const p of items) {
    if (!["max_redirect", "web_enabled", "coming_soon"].includes(p.mode))
      throw Error("Invalid mode");
    if (
      p.mode === "web_enabled" &&
      (!site.webOrdering ||
        !p.webPath?.startsWith("/") ||
        p.webPath.startsWith("//"))
    )
      throw Error("Web ordering is not connected");
    if (!p.pricing.source || !/^\d{4}-\d{2}-\d{2}$/.test(p.pricing.reviewedAt))
      throw Error("Missing pricing provenance");
  }
}
