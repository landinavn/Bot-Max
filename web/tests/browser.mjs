import { chromium } from "playwright";
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { routes } from "../src/site.ts";
const base = process.env.WEB_TEST_URL ?? "http://127.0.0.1:4173";
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  reducedMotion: "reduce",
});
await context.route("**/*", (r) =>
  new URL(r.request().url()).origin === base ? r.continue() : r.abort(),
);
const page = await context.newPage();
const errors = [];
const missing = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (m) => {
  if (m.type() === "error") errors.push(m.text());
});
page.on("response", (r) => {
  if (r.status() >= 400 && !r.url().endsWith("/does-not-exist"))
    missing.push(r.url());
});
try {
  for (const route of routes) {
    const response = await page.goto(base + route.path);
    assert.equal(response.status(), 200, route.path);
    await page.waitForLoadState("networkidle");
    assert.equal(await page.locator("h1").count(), 1);
    assert.equal(
      await page.locator("form,input[type=email],input[type=file]").count(),
      0,
    );
  }
  console.log("PASS: 19 static routes hydrate, no unfinished forms.");
  for (const width of [320, 375, 390, 768, 1024, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const path of [
      "/",
      "/ded-moroz",
      "/prices",
      "/gallery",
      "/photosessions/new-year",
    ]) {
      await page.goto(base + path);
      assert.ok(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth + 1,
        ),
        `overflow ${width} ${path}`,
      );
    }
  }
  console.log(
    "PASS: 7 widths × 5 representative routes, no horizontal overflow.",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  const menu = page.locator(".menu-toggle");
  await menu.click();
  assert.equal(await menu.getAttribute("aria-expanded"), "true");
  await page.keyboard.press("Escape");
  assert.equal(await menu.getAttribute("aria-expanded"), "false");
  assert.ok(await menu.evaluate((e) => e === document.activeElement));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base);
  const range = page.getByRole("slider");
  await range.focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await range.inputValue(), "51");
  const box = await range.boundingBox();
  await page.mouse.click(box.x + box.width * 0.75, box.y + box.height / 2);
  assert.ok(Number(await range.inputValue()) > 65);
  await page.getByRole("button", { name: "Анимация", exact: true }).click();
  assert.equal(await page.locator(".comparison-video figure").count(), 1);
  assert.equal(await page.locator("video,audio").count(), 0);
  await page.locator(".faq-list summary").first().click();
  assert.ok(
    (await page.locator(".faq-list details").first().getAttribute("open")) !==
      null,
  );
  await page.goto(base + "/gallery");
  await page.getByRole("button", { name: "Открытки", exact: true }).click();
  assert.equal(await page.locator(".gallery-grid figure").count(), 1);
  const trigger = page.locator(".gallery-open").first();
  await trigger.click();
  assert.ok(await page.locator("dialog").evaluate((d) => d.open));
  await page.keyboard.press("Escape");
  assert.ok(!(await page.locator("dialog").evaluate((d) => d.open)));
  assert.ok(await trigger.evaluate((e) => e === document.activeElement));
  await page.goto(base + "/ded-moroz#family");
  await page.waitForFunction(
    () =>
      document
        .querySelector(".family-options button:nth-child(2)")
        .getAttribute("aria-pressed") === "true",
  );
  assert.ok(
    (await page.locator(".family-detail").textContent()).includes(
      "каждого ребёнка",
    ),
  );
  await page.evaluate(() => {
    window.events = [];
    window.addEventListener("wish:analytics", (e) =>
      window.events.push(e.detail),
    );
    document.addEventListener("click", (e) => {
      if (e.target.closest('a[target="_blank"]')) e.preventDefault();
    });
  });
  await page.locator('main a[target="_blank"]').first().click();
  assert.ok(
    await page.evaluate(() =>
      window.events.some((e) => e.name === "max_redirect"),
    ),
  );
  assert.ok(
    !(
      await page.locator('main a[target="_blank"]').first().getAttribute("href")
    ).includes("?"),
  );
  console.log(
    "PASS: mobile menu, keyboard/mouse comparison, image/video comparison, FAQ, gallery/filter/dialog/focus, family deep anchor, MAX event.",
  );
  const touch = await browser.newContext({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });
  const tp = await touch.newPage();
  await tp.goto(base);
  const tr = tp.getByRole("slider");
  await tr.scrollIntoViewIfNeeded();
  const tb = await tr.boundingBox();
  await tp.touchscreen.tap(tb.x + tb.width * 0.8, tb.y + tb.height / 2);
  assert.ok(Number(await tr.inputValue()) > 65);
  await touch.close();
  console.log("PASS: touch comparison.");
  const nojs = await browser.newContext({ javaScriptEnabled: false });
  const np = await nojs.newPage();
  await np.goto(base + "/songs");
  assert.ok((await np.locator("h1").textContent()).length > 5);
  assert.equal(await np.locator(".product-grid article").count(), 3);
  await nojs.close();
  // Native lazy image loading: scroll through every section before capturing.
  await page.goto(base);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 800) {
      scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 30));
    }
    scrollTo(0, 0);
  });
  await page.waitForTimeout(150);
  const out = process.env.WEB_SCREENSHOT_DIR ?? "test-results";
  await mkdir(out, { recursive: true });
  await page.screenshot({ path: out + "/desktop.png", fullPage: true });
  await page.screenshot({ path: out + "/desktop-hero.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: out + "/mobile.png", fullPage: true });
  await page.screenshot({ path: out + "/mobile-hero.png" });
  assert.deepEqual(errors, [], "browser errors");
  assert.deepEqual(missing, [], "missing assets");
  // Last: expected 404 logs a browser console error, excluded from clean routes assertion.
  assert.equal((await page.goto(base + "/does-not-exist")).status(), 404);
  console.log(
    "PASS: no-JS product content, screenshots, zero browser errors/missing assets, real 404.",
  );
} finally {
  await browser.close();
}
