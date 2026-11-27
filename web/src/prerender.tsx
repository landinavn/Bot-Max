import { renderToString } from "react-dom/server";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { App } from "./App.tsx";
import { routeFor, routes, validateCatalogue } from "./site.ts";
import { head, robots, sitemap } from "./seo.ts";
import type { BuildMode } from "./seo.ts";
import { site } from "./config.ts";
validateCatalogue();
const mode = (process.env.WEB_BUILD_MODE ?? "preview") as BuildMode;
if (!["preview", "production"].includes(mode))
  throw Error("WEB_BUILD_MODE must be preview or production");
if (mode === "production" && !site.readyForIndexing)
  throw Error(
    "Production indexing is blocked: complete owner assets, legal information and release review first.",
  );
const template = await readFile("dist/index.html", "utf8");
for (const route of [...routes, routeFor("/404")]) {
  const output =
    route.path === "/404"
      ? "dist/404.html"
      : path.join("dist", route.path, "index.html");
  await mkdir(path.dirname(output), { recursive: true });
  const html = template
    .replace("<!--app-head-->", head(route, mode))
    .replace("<!--app-html-->", renderToString(<App route={route} />));
  if (!html.includes("<h1") || html.includes("<!--app-html-->"))
    throw Error("Missing prerendered content");
  await writeFile(output, html);
}
await writeFile("dist/robots.txt", robots(mode));
await writeFile("dist/sitemap.xml", sitemap(mode));
console.log(
  `Prerendered ${routes.length} routes + 404, mode=${mode}. No runtime Node server required.`,
);
