import { site } from "./config.ts";
import { routes } from "./site.ts";
import type { Route } from "./site.ts";
export type BuildMode = "preview" | "production";
export function escape(value: string) {
  return value.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
}
export function indexable(route: Route, mode: BuildMode) {
  return (
    mode === "production" &&
    !route.noindex &&
    route.kind !== "private" &&
    route.kind !== "notfound"
  );
}
export function head(route: Route, mode: BuildMode) {
  const url = site.origin + (route.path === "/" ? "/" : route.path);
  const desc = route.description.slice(0, 240);
  return `<title>${escape(route.title)}</title><meta name="description" content="${escape(desc)}"><meta name="robots" content="${indexable(route, mode) ? "index,follow" : "noindex,nofollow"}"><link rel="canonical" href="${escape(url)}"><meta property="og:locale" content="ru_RU"><meta property="og:type" content="website"><meta property="og:site_name" content="${escape(site.name)}"><meta property="og:title" content="${escape(route.title)}"><meta property="og:description" content="${escape(desc)}"><meta property="og:url" content="${escape(url)}"><meta property="og:image" content="${site.origin}/media/hero/og-cover.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta name="twitter:card" content="summary_large_image">`;
}
export function robots(mode: BuildMode) {
  return mode === "preview"
    ? "User-agent: *\nDisallow: /\n"
    : `User-agent: *\nAllow: /\nDisallow: /account\nDisallow: /login\nDisallow: /blog\nDisallow: /gallery\nSitemap: ${site.origin}/sitemap.xml\n`;
}
export function sitemap(mode: BuildMode) {
  return (
    '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
    routes
      .filter((r) => indexable(r, mode))
      .map((r) => `<url><loc>${site.origin}${escape(r.path)}</loc></url>`)
      .join("") +
    "</urlset>"
  );
}
