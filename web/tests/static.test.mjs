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

test('public media use explicit approvals; no SVG placeholder is presented as a real result', async () => {
  const {media,exampleFor}=await import('../src/config.ts');
  const manifest=JSON.parse(fs.readFileSync('media/manifest.json','utf8'));
  for(const item of media){
    assert.equal(item.demo,false);
    for(const asset of [item.poster,item.src].filter(Boolean)){
      const provenance=manifest.find(x=>x.file===asset);
      assert.ok(provenance,asset+' must have source + approval');
      assert.ok(provenance.approval&&provenance.sourceSha256);
      assert.equal(fs.statSync('public'+asset).size,provenance.bytes);
    }
  }
  assert.equal(exampleFor('animation').id,'animation-horizontal','Use a frame from a real provided animation');
  assert.equal(exampleFor('custom-order'),undefined,'Custom order is not a fixed result');
  assert.equal(media.filter(m=>m.kind==='video').length,6);
  assert.equal(media.filter(m=>m.kind==='audio').length,1);
  for(const r of routes)assert.ok(!/<(?:img|source)[^>]+(?:src|srcset)="[^"]+\.svg/.test(htmlFor(r)));
});

test('release keeps provenance private, unique and bound to every public file', async () => {
  const {checkPublic,checkDist,readRegistry}=await import('../scripts/release-safety.mjs');
  const registry=readRegistry(process.cwd());
  assert.equal(registry.length,65);
  assert.equal(new Set(registry.map(x=>x.file)).size,registry.length);
  const os=await import('node:os');
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'wish-registry-test-'));
  try{
    fs.mkdirSync(path.join(temporary,'media'));
    fs.writeFileSync(path.join(temporary,'media/manifest.json'),JSON.stringify([...registry,registry[0]]));
    assert.throws(()=>readRegistry(temporary),/Duplicate provenance/);
  }finally{fs.rmSync(temporary,{recursive:true,force:true});}
  assert.equal(checkPublic(process.cwd()).length,65);
  const result=checkDist(process.cwd());
  assert.equal(result.html,20);
  assert.equal(result.files.length,89);
  for(const name of ['media/works/manifest.json','media/README.md','media/logo/README.md','.vite/manifest.json'])
    assert.ok(!result.files.includes(name),name+' must not be public');
});

test('release rejects leaked service/source files and symlinks', async () => {
  const {mkdtempSync,cpSync,writeFileSync,mkdirSync,unlinkSync,rmSync,symlinkSync}=fs;
  const os=await import('node:os');
  const {checkDist}=await import('../scripts/release-safety.mjs');
  const temporary=mkdtempSync(path.join(os.tmpdir(),'wish-release-test-'));
  try{
    const dist=path.join(temporary,'dist');cpSync('dist',dist,{recursive:true});
    for(const name of ['.env','.git/config','database.sqlite3','worker.py','README.md','media/manifest.json','media/frontend-1.3-sources.tsv','private.json','node_modules/index.js','assets/source.ts','assets/script.js.map']){
      const file=path.join(dist,name);mkdirSync(path.dirname(file),{recursive:true});writeFileSync(file,'synthetic sensitive file');
      assert.throws(()=>checkDist(process.cwd(),dist),undefined,name);
      unlinkSync(file);
      let parent=path.dirname(file);
      while(parent!==dist&&fs.readdirSync(parent).length===0){fs.rmdirSync(parent);parent=path.dirname(parent);}
      assert.doesNotThrow(()=>checkDist(process.cwd(),dist));
    }
    const link=path.join(dist,'private-link');symlinkSync(path.resolve('package.json'),link);
    assert.throws(()=>checkDist(process.cwd(),dist),/Symlink/);unlinkSync(link);
  }finally{rmSync(temporary,{recursive:true,force:true});}
});

test('release rejects modified media and detectable credentials', async () => {
  const os=await import('node:os');
  const {checkDist,secretPatterns}=await import('../scripts/release-safety.mjs');
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'wish-integrity-test-'));
  try{
    const dist=path.join(temporary,'dist');fs.cpSync('dist',dist,{recursive:true});
    const media=path.join(dist,'media/works/vision-before.webp');const original=fs.readFileSync(media);
    const corrupt=Buffer.from(original);corrupt[20]^=1;fs.writeFileSync(media,corrupt);
    assert.throws(()=>checkDist(process.cwd(),dist),/integrity mismatch/);fs.writeFileSync(media,original);
    const jsName=fs.readdirSync(path.join(dist,'assets')).find(x=>x.endsWith('.js'));
    assert.ok(jsName);const js=path.join(dist,'assets',jsName);
    const saved=fs.readFileSync(js);fs.appendFileSync(js,'\n// '+['sk','proj', 'X'.repeat(32)].join('-'));
    assert.throws(()=>checkDist(process.cwd(),dist),/Possible credential/);fs.writeFileSync(js,saved);
    for(const value of ['-----BEGIN PRIVATE KEY-----','https://name:password@example.test/file','postgresql://user:password@db.test/main'])
      assert.ok(secretPatterns.some(p=>p.test(value)));
  }finally{fs.rmSync(temporary,{recursive:true,force:true});}
});

test('Frontend 1.3 homepage preserves the agreed narrative and actual animation examples',async()=>{
 const {media,exampleFor}=await import('../src/config.ts');
 const html=htmlFor(routeFor('/'));
 const order=['new-year','catalog','photosession-gallery','postcard-stories','santa-story','vision-stories','animation-stories','musical-gifts','how','home-pricing','home-faq'];
 let previous=-1;
 for(const id of order){const position=html.indexOf(`id="${id}"`);assert.ok(position>previous,id);previous=position;}
 for(const text of ['Фотосессия мечты — без фотостудии','Обычная фотография. Необыкновенный подарок','Каким будет ваш 2027 год?','Оживите самые дорогие воспоминания','Превратите старые семейные фотографии в трогательные живые истории','Оживить свою фотографию'])assert.ok(html.includes(text),text);
 const animation=media.filter(m=>m.category==='animation');assert.equal(animation.length,2);
 assert.equal(exampleFor('animation').poster,animation[1].poster);
 for(const item of animation){
  assert.equal(item.presentation,'video-frame');
  for(const url of ['/', '/animation','/gallery'])assert.ok(htmlFor(routeFor(url)).includes(item.poster),url+' '+item.id);
 }
 assert.ok(!/autoplay\s*=/i.test(html));
 assert.ok(!/таймер|осталось \d+|скидка \d+%/i.test(html));
});

test('every archive original has approved private provenance and complete responsive derivatives',async()=>{
 const {media}=await import('../src/config.ts');
 const registry=JSON.parse(fs.readFileSync('media/manifest.json','utf8'));
 const lines=fs.readFileSync('media/frontend-1.3-sources.tsv','utf8').trim().split('\n').slice(1);
 assert.equal(lines.length,16);
 for(const line of lines){
  const [source,original,hash]=line.split('\t');
  const derivatives=registry.filter(r=>r.source===source);
  assert.ok(derivatives.length>=3,source);
  for(const r of derivatives){assert.equal(r.sourceSha256,hash);assert.match(r.approval,/all 16 files/);assert.ok(r.width>0&&r.height>0);}
  assert.ok(media.some(item=>item.poster===derivatives.find(r=>r.file.endsWith(source.replace(/\.(png|webp|mp4)$/,'.webp'))).file),source);
 }
 for(const item of media.filter(m=>m.category==='animation')){
  const poster=registry.find(r=>r.file===item.poster),video=registry.find(r=>r.file===item.src);
  assert.equal(poster.sourceSha256,video.sourceSha256);assert.equal(poster.frameTimeSeconds,1);
  assert.equal(poster.width*video.height,poster.height*video.width);
  assert.ok(Math.abs(video.durationSeconds-5)<.1);
 }
 for(const retired of ['postcard-birthday.webp','postcard-mother.webp','family-2.webp','family-3.webp'])assert.ok(!registry.some(r=>r.file.endsWith('/'+retired)));
});

test('new images use srcset and lazy galleries; only the hero work has high priority',()=>{
 const html=htmlFor(routeFor('/'));
 assert.ok((html.match(/srcSet=/gi)||[]).length>=20);
 const images=[...html.matchAll(/<img\b[^>]*>/g)].map(m=>m[0]);
 const high=images.filter(img=>/fetchPriority="high"/i.test(img));
 assert.equal(high.length,1);assert.ok(high[0].includes('photoshoot-family-light'));
 for(const img of images.filter(i=>/\/media\/works\//.test(i)&&!i.includes('fetchPriority="high"')))assert.ok(img.includes('loading="lazy"'),img);
 assert.ok(!/<(?:video|audio)[^>]+src=/.test(html));
 for(const r of routes)assert.ok(!htmlFor(r).includes('sources.tsv'));
 assert.ok(fs.readFileSync('src/storefront13.css','utf8').includes('prefers-reduced-motion'));
});
