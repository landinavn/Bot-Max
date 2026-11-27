import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {routes} from '../src/site.ts';
const base=process.env.WEB_TEST_URL??'http://127.0.0.1:4173';
const out=process.env.WEB_SCREENSHOT_DIR??'test-results';
await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH??'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
const context=await browser.newContext({viewport:{width:1440,height:900},reducedMotion:'reduce'});
await context.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
const page=await context.newPage();const errors=[],missing=[],mediaRequests=[];let expected404=false;
page.on('pageerror',e=>errors.push(e.message));
page.on('console',m=>{if(m.type()==='error'&&!expected404)errors.push(m.text());});
page.on('response',r=>{if(r.status()>=400&&!r.url().endsWith('/does-not-exist'))missing.push(r.url());});
page.on('request',r=>{if(/\.(mp4|mp3)(\?|$)/.test(r.url()))mediaRequests.push(r.url());});
const report={routes:[],viewports:[],media:[],screenshots:[]};
try{
 for(const route of routes){
  const response=await page.goto(base+route.path);await page.waitForLoadState('networkidle');
  assert.equal(response.status(),200,route.path);assert.equal(await page.locator('h1').count(),1);assert.ok(await page.locator('.brand-logo').evaluate(e=>e.complete&&e.naturalWidth>0&&Math.abs(e.getBoundingClientRect().width-e.getBoundingClientRect().height)<1));
  assert.equal(await page.locator('form,input[type=email],input[type=file]').count(),0);
  assert.equal(await page.locator('video[src],audio[src]').count(),0,'No eager media at '+route.path);
  report.routes.push(route.path);
 }
 assert.deepEqual(mediaRequests,[],'No video/audio requests on initial route loads');
 console.log('PASS: all 19 routes hydrate; no unfinished forms or eager media requests.');
 for(const [width,height] of [[320,568],[375,667],[390,844],[393,852],[430,932],[768,1024],[1024,768],[1440,900],[1920,1080]]){
  await page.setViewportSize({width,height});
  for(const path of ['/','/ded-moroz','/photosessions/new-year','/postcards','/vision-board','/cartoons','/songs','/prices','/gallery','/animation']){
   await page.goto(base+path);await page.waitForLoadState('networkidle');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width} ${path}`);
   if(path==='/'){
    assert.ok(await page.locator('.hero-feature img').evaluate(e=>{const b=e.getBoundingClientRect();return e.complete&&e.naturalWidth>0&&b.width>=90&&b.top+b.height<=innerHeight-50;}),'Real example fully visible on first screen '+width);
    const captionOverlaps=await page.evaluate(()=>[...document.querySelectorAll('.hero-mosaic figcaption,.hero-mosaic a>span')].some(e=>{const b=e.getBoundingClientRect();if(!b.width||!b.height)return false;const i=e.parentElement.querySelector('img');return getComputedStyle(e).position==='absolute'||e.offsetTop<i.offsetTop+i.offsetHeight-1;}));
    assert.equal(captionOverlaps,false,'Hero labels never overlap photos '+width);
    assert.ok(await page.locator('.hero-actions .button').first().evaluate(e=>{const b=e.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight-(innerWidth<768?62:0);}), 'Primary CTA visible above mobile navigation '+width);
    if([320,375,390,393,430,1440].includes(width)){const f=`hero-${width}x${height}.png`;await page.screenshot({path:out+'/'+f});report.screenshots.push(f);}
   }
   if(['/ded-moroz','/photosessions/new-year','/postcards','/vision-board','/cartoons'].includes(path)&&[390,1440].includes(width)){
    const f=`product-${path.slice(1).replaceAll('/','-')}-${width}.png`;await page.screenshot({path:out+'/'+f});report.screenshots.push(f);
   }
   report.viewports.push({width,height,path});
  }
 }
 console.log('PASS: 9 viewports × 10 routes, zero overflow; all requested hero viewports show real work and no image/label overlap.');
 await page.setViewportSize({width:390,height:844});await page.goto(base);
 const menu=page.locator('.menu-toggle');await menu.click();assert.equal(await menu.getAttribute('aria-expanded'),'true');await page.keyboard.press('Escape');assert.equal(await menu.getAttribute('aria-expanded'),'false');assert.ok(await menu.evaluate(e=>e===document.activeElement));
 assert.ok(await page.locator('.mobile-bottom-nav').isVisible());
 assert.equal(await page.locator('.mobile-bottom-nav a').count(),5);
 await page.setViewportSize({width:1440,height:900});await page.goto(base);
 assert.equal(await page.locator('.season-grid .season-card').count(),4);assert.equal(await page.locator('#catalog .product-card').count(),9);
 assert.equal(await page.locator('.comparison-tabs button').count(),5);
 const range=page.getByRole('slider');await range.focus();await page.keyboard.press('ArrowRight');assert.equal(await range.inputValue(),'51');
 const box=await range.boundingBox();await page.mouse.click(box.x+box.width*.75,box.y+box.height/2);assert.ok(Number(await range.inputValue())>65);
 await page.getByRole('button',{name:'Мультфильм',exact:true}).click();assert.equal(await page.locator('.comparison-alternative .media-player').count(),1);assert.equal(await page.locator('video,audio').count(),0);
 await page.getByRole('button',{name:'Оживление фотографии',exact:true}).click();assert.equal(await page.locator('.comparison-alternative img').count(),1);assert.ok((await page.locator('.comparison-alternative').textContent()).includes('Отдельное исходное фото'));assert.equal(await page.locator('.comparison-alternative video').count(),0);
 await page.locator('.faq-list summary').first().click();assert.notEqual(await page.locator('.faq-list details').first().getAttribute('open'),null);
 await page.goto(base+'/gallery');await page.getByRole('button',{name:'Открытки',exact:true}).click();assert.equal(await page.locator('.gallery-grid figure').count(),6);
 const trigger=page.locator('.gallery-open').first();await trigger.click();assert.ok(await page.locator('dialog').evaluate(d=>d.open));await page.keyboard.press('Escape');assert.ok(await trigger.evaluate(e=>e===document.activeElement));
 await page.getByRole('button',{name:'Оживление',exact:true}).click();assert.equal(await page.locator('.gallery-grid figure').count(),2);assert.equal(await page.locator('video[src]').count(),0);
 // Real files: click opt-in loader, then native play without autoplay.
 for(const [category,kind,index=0] of [['Мультфильмы','video'],['Мультфильмы','video',1],['Дед Мороз','video'],['С музыкой','video'],['Песни','audio'],['Оживление','video'],['Оживление','video',1]]){
  await page.getByRole('button',{name:category,exact:true}).click();await page.locator('.gallery-open').nth(index).click();
  const before=mediaRequests.length;await page.locator('dialog .media-start').click();
  const player=page.locator('dialog '+kind);assert.equal(await player.getAttribute('autoplay'),null);assert.equal(await player.getAttribute('preload'),'none');await page.waitForFunction(()=>{const e=document.querySelector('dialog video,dialog audio');return e&&!e.paused;});
  const playback=await player.evaluate(async e=>{await e.play();await new Promise(r=>setTimeout(r,200));const result={duration:e.duration,width:e.videoWidth??null,currentTime:e.currentTime,error:e.error?.code??null};e.pause();return result;});
  assert.equal(playback.error,null);assert.ok(playback.duration>(category==='Оживление'?4.9:10));assert.ok(playback.currentTime>0);
  if(category==='Оживление'){const expected=index===0?784/1176:1072/856;assert.ok(await player.evaluate((e,ratio)=>Math.abs(e.videoWidth/e.videoHeight-ratio)<.001,expected));}assert.ok(mediaRequests.length>before);report.media.push({category,...playback});await page.getByRole('button',{name:'Закрыть просмотр'}).click();
 }
 console.log('PASS: 7 real media players play on demand; no autoplay or media fetches before opt-in.');
 await page.goto(base+'/ded-moroz#family');await page.waitForFunction(()=>document.querySelector('.family-options button:nth-child(2)').getAttribute('aria-pressed')==='true');
 await page.evaluate(()=>{window.events=[];window.addEventListener('wish:analytics',e=>window.events.push(e.detail));document.addEventListener('click',e=>{if(e.target.closest('a[target="_blank"]'))e.preventDefault();});});
 await page.locator('main a[target="_blank"]').first().click();assert.ok(await page.evaluate(()=>window.events.some(e=>e.name==='max_redirect')));
 assert.ok(!(await page.locator('main a[target="_blank"]').first().getAttribute('href')).includes('?'));
 const touch=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,isMobile:true});const tp=await touch.newPage();await tp.goto(base);const tr=tp.getByRole('slider');await tr.scrollIntoViewIfNeeded();const tb=await tr.boundingBox();await tp.touchscreen.tap(tb.x+tb.width*.8,tb.y+tb.height/2);assert.ok(Number(await tr.inputValue())>65);await touch.close();
 const nojs=await browser.newContext({javaScriptEnabled:false});const np=await nojs.newPage();await np.goto(base+'/songs');assert.ok((await np.locator('h1').textContent()).length>5);assert.equal(await np.locator('.product-grid article').count(),3);await nojs.close();
 for(const [width,height] of [[1440,900],[393,852]]){
  await page.setViewportSize({width,height});await page.goto(base);await page.evaluate(async()=>{for(let y=0;y<document.body.scrollHeight;y+=700){scrollTo(0,y);await new Promise(r=>setTimeout(r,25));}scrollTo(0,0);});await page.screenshot({path:`${out}/home-full-${width}.png`,fullPage:true});report.screenshots.push(`home-full-${width}.png`);
 }
 // Full homepage remains substantive without JavaScript; no invented original-photo pairs.
 const staticContext=await browser.newContext({javaScriptEnabled:false,viewport:{width:393,height:852}});
 const staticPage=await staticContext.newPage();await staticPage.goto(base);
 for(const id of ['photosession-gallery','postcard-stories','santa-story','vision-stories','animation-stories','musical-gifts'])assert.ok((await staticPage.locator('#'+id).textContent()).length>80);
 assert.equal(await staticPage.locator('#animation-stories img').count(),2);await staticContext.close();
 // Photo categories, keyboard-friendly mobile rail, and no fabricated couple category.
 await page.setViewportSize({width:393,height:852});await page.goto(base);
 const photo=page.locator('#photosession-gallery');
 for(const [label,count] of [['Детские',1],['Индивидуальные',1],['Мама и дочка',2],['Семейные',3],['Все',7]]){
  await photo.getByRole('button',{name:label,exact:true}).click();assert.equal(await photo.locator('.photo-rail figure').count(),count);
 }
 assert.equal(await photo.getByRole('button',{name:'Для пары',exact:true}).count(),0);
 await photo.getByRole('button',{name:'Далее',exact:true}).click();assert.ok(await photo.locator('.photo-rail').evaluate(e=>e.scrollLeft>0));
 await photo.getByRole('button',{name:'Назад',exact:true}).click();
 await photo.locator('.gallery-open').first().click();assert.ok(await photo.locator('dialog').evaluate(e=>e.open));await page.keyboard.press('Escape');
 // Record fresh initial network bytes at both target viewports, BEFORE scrolling/clicking.
 report.initialNetwork=[];
 for(const [width,height] of [[1440,900],[393,852]]){
  const fresh=await browser.newContext({viewport:{width,height},reducedMotion:'reduce'});
  await fresh.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  const p=await fresh.newPage();await p.goto(base);await p.waitForLoadState('networkidle');
  const metrics=await p.evaluate(()=>({navigation:performance.getEntriesByType('navigation').map(e=>({encoded:e.encodedBodySize,transfer:e.transferSize})),resources:performance.getEntriesByType('resource').map(e=>({url:new URL(e.name).pathname,encoded:e.encodedBodySize,transfer:e.transferSize}))}));
  assert.ok(!metrics.resources.some(e=>/\.(mp4|mp3)$/.test(e.url)));
  const originals=metrics.resources.filter(e=>/(photoshoot|postcard-new-year|vision-board-)/.test(e.url)&&!/-480\.webp|-900\.webp/.test(e.url));
  assert.ok(originals.length<14,'Not every full-size new image is fetched initially');
  report.initialNetwork.push({width,height,...metrics,totalEncodedBytes:[...metrics.navigation,...metrics.resources].reduce((n,e)=>n+e.encoded,0),totalTransferBytes:[...metrics.navigation,...metrics.resources].reduce((n,e)=>n+e.transfer,0)});
  await p.screenshot({path:`${out}/first-screen-${width}x${height}.png`});report.screenshots.push(`first-screen-${width}x${height}.png`);
  await p.evaluate(async()=>{for(let y=0;y<document.body.scrollHeight;y+=550){scrollTo(0,y);await new Promise(r=>setTimeout(r,35));}scrollTo(0,0);});await p.waitForLoadState('networkidle');
  await p.screenshot({path:`${out}/homepage-full-${width}.png`,fullPage:true});report.screenshots.push(`homepage-full-${width}.png`);
  for(const id of ['photosession-gallery','postcard-stories','santa-story','vision-stories','animation-stories','musical-gifts']){
   const name=`${id}-${width}.png`;await p.locator('#'+id).screenshot({path:out+'/'+name,style:'.header,.mobile-bottom-nav{visibility:hidden!important;}'});report.screenshots.push(name);
  }
  await fresh.close();
 }
 report.photoFilters=true;report.animationExamples=2;report.noJsHomepage=true;
 assert.deepEqual(errors,[],'browser errors');assert.deepEqual(missing,[],'missing files');expected404=true;assert.equal((await page.goto(base+'/does-not-exist')).status(),404);
 for(const privatePath of ['/media/works/manifest.json','/media/README.md','/media/logo/README.md','/.env','/.git/config','/worker.py','/database.sqlite3','/.vite/manifest.json','/media/frontend-1.3-sources.tsv','/media/manifest.json','/README.md']){
  const r=await page.request.get(base+privatePath);assert.equal(r.status(),404,privatePath);
  assert.ok((await r.text()).includes('Страница не найдена'),privatePath);
 }
 report.privatePathsBlocked=11;
 await writeFile(out+'/browser-report.json',JSON.stringify({...report,errors,missing},null,2));
 console.log('PASS: menu/focus, keyboard/mouse/touch comparison, filters/dialog, FAQ, family anchor, MAX event, no-JS, 404; zero browser errors/missing assets.');
}finally{await browser.close();}
