import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { routes, routeFor, validateCatalogue } from "../src/site.ts";
import { products } from "../src/catalog.ts";
import { site } from "../src/config.ts";
import { head, indexable, robots, sitemap } from "../src/seo.ts";
import { attribution } from "../src/analytics.ts";
const htmlFor = (r) =>
  fs.readFileSync(
    r.path === "/" ? "dist/index.html" : `dist${r.path}/index.html`,
    "utf8",
  );
test("19 routes prerender real content and unique public SEO without JavaScript", () => {
  assert.equal(routes.length, 19);
  const publicRoutes = routes.filter((r) => r.kind !== "private");
  assert.equal(
    new Set(publicRoutes.map((r) => r.title)).size,
    publicRoutes.length,
  );
  assert.equal(
    new Set(publicRoutes.map((r) => r.description)).size,
    publicRoutes.length,
  );
  for (const r of routes) {
    const html = htmlFor(r);
    assert.equal((html.match(/<h1[ >]/g) || []).length, 1, r.path);
    assert.ok(html.includes("<main"));
    assert.ok(html.includes("<title>"));
    assert.ok(html.includes('rel="canonical"'));
    assert.ok(html.includes('property="og:image"'));
    assert.ok(html.includes("noindex,nofollow"));
    assert.ok(!html.includes("<!--app-html-->"));
    assert.ok(!/<(?:form|input[^>]+type="(?:email|file)")\b/.test(html));
  }
});
test("all internal navigation and media resolve to generated files/anchors", () => {
  for (const r of routes) {
    const html = htmlFor(r);
    for (const m of html.matchAll(/(?:href|src)="([^"\s]+)"/g)) {
      const raw = m[1];
      if (!raw.startsWith("/") && !raw.startsWith("#")) continue;
      const u = new URL(raw, "https://local.test" + r.path);
      const dest = u.pathname;
      const local = path.join("dist", dest);
      const file =
        fs.existsSync(local) && fs.statSync(local).isFile()
          ? local
          : path.join(local, "index.html");
      assert.ok(fs.existsSync(file), `${r.path}: ${raw}`);
      if (u.hash && file.endsWith(".html"))
        assert.ok(
          fs.readFileSync(file, "utf8").includes(`id="${u.hash.slice(1)}"`),
          raw,
        );
    }
  }
});
test("product modes fail closed and preserve verified pricing provenance", () => {
  validateCatalogue();
  assert.equal(products.length, 9);
  assert.ok(products.every((p) => p.mode === "max_redirect"));
  assert.equal(site.maxUrl, "https://max.ru/se14596751_1_bot");
  assert.equal(new URL(site.maxUrl).search, "");
  assert.throws(() => validateCatalogue([...products, products[0]]));
  assert.throws(() =>
    validateCatalogue([
      { ...products[0], mode: "web_enabled", webPath: "/create" },
    ]),
  );
  assert.throws(() => validateCatalogue([{ ...products[0], mode: "invalid" }]));
  assert.throws(() =>
    validateCatalogue([
      { ...products[0], pricing: { source: "", reviewedAt: "" } },
    ]),
  );
  assert.ok(products.every((p) => p.pricing.reviewedAt === "2026-11-26"));
});
test("preview stays unindexed; production SEO excludes unfinished/private routes", () => {
  assert.equal(robots("preview"), "User-agent: *\nDisallow: /\n");
  assert.ok(!sitemap("preview").includes("<url>"));
  for (const r of routes) {
    assert.equal(indexable(r, "preview"), false);
    if (r.noindex || r.kind === "private")
      assert.equal(indexable(r, "production"), false);
  }
  assert.ok(sitemap("production").includes("/postcards"));
  assert.ok(!sitemap("production").includes("/account"));
  assert.ok(!sitemap("production").includes("/blog"));
  assert.ok(
    head(routeFor("/account"), "production").includes("noindex,nofollow"),
  );
  assert.equal(site.readyForIndexing, false);
  assert.ok(
    fs
      .readFileSync("src/prerender.tsx", "utf8")
      .match(/mode\s*===\s*['"]production['"]/),
  );
  assert.ok(
    fs.readFileSync("dist/404.html", "utf8").includes("Страница не найдена"),
  );
});
test("UTM keeps campaign slugs only, excluding arbitrary user text and secrets", () => {
  assert.deepEqual(
    attribution(
      "?utm_source=max&utm_campaign=ny_2027&utm_term=gift-idea&email=test@example.org&token=secret",
    ),
    { utm_source: "max", utm_campaign: "ny_2027", utm_term: "gift-idea" },
  );
  assert.deepEqual(
    attribution(
      "?utm_source=user@example.org&utm_campaign=%3Cscript%3E&utm_content=" +
        "a".repeat(60),
    ),
    {},
  );
});
test("bundle contains no API credentials or fake transactional calls", () => {
  const files = fs.readdirSync("dist/assets").filter((p) => p.endsWith(".js"));
  const js = files
    .map((p) => fs.readFileSync("dist/assets/" + p, "utf8"))
    .join("\n");
  assert.ok(
    !/sk-(?:proj-)?[A-Za-z0-9_-]{20,}|BEGIN (?:RSA |OPENSSH )?PRIVATE KEY|xi-api-key|X-Api-Key|api\.heygen\.com|api\.openai\.com/.test(
      js,
    ),
  );
  assert.ok(!/\bfetch\(/.test(fs.readFileSync("src/analytics.ts", "utf8")));
  for (const e of [
    "signup_started",
    "generation_started",
    "payment_started",
    "payment_success",
  ])
    assert.ok(
      !new RegExp(`trackEvent\\(\\s*['\"]${e}`).test(
        fs.readFileSync("src/App.tsx", "utf8") +
          fs.readFileSync("src/components.tsx", "utf8"),
      ),
    );
  assert.ok(
    !/<(?:video|audio)[^>]*\bsrc=/.test(htmlFor(routeFor("/"))),
    "media never loads on opening homepage",
  );
});

test("production prerender gate refuses publishing and leaves preview intact", () => {
  const before = fs.readFileSync("dist/index.html", "utf8");
  const result = spawnSync(process.execPath, [".ssr/prerender.js"], {
    encoding: "utf8",
    env: { ...process.env, WEB_BUILD_MODE: "production" },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Production indexing is blocked/);
  assert.equal(fs.readFileSync("dist/index.html", "utf8"), before);
});
