// Local build/packaging checks only. This module performs no work on import.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {routes} from '../src/site.ts';

export const sha256 = value => createHash('sha256').update(value).digest('hex');
const mediaExtensions=new Set(['.webp','.png','.ico','.mp4','.mp3','.jpg','.jpeg','.avif']);
const mediaKinds=new Set(['image','poster','video','audio','logo','brand']);

export function listFiles(directory){
  const output=[];
  function visit(current,relative=''){
    if(fs.lstatSync(current).isSymbolicLink())throw Error('Symlink not allowed: '+relative);
    for(const name of fs.readdirSync(current).sort()){
      const local=path.join(current,name), key=relative?relative+'/'+name:name;
      const stat=fs.lstatSync(local);
      if(stat.isSymbolicLink())throw Error('Symlink not allowed: '+key);
      if(stat.isDirectory()){
        if(name.startsWith('.')||name==='node_modules')throw Error('Private directory not allowed: '+key);
        visit(local,key);
      }
      else if(stat.isFile())output.push(key);
      else throw Error('Not a regular file: '+key);
    }
  }
  visit(path.resolve(directory));return output.sort();
}

export function readRegistry(webRoot){
  const registry=JSON.parse(fs.readFileSync(path.join(webRoot,'media/manifest.json'),'utf8'));
  if(!Array.isArray(registry)||!registry.length)throw Error('Empty provenance registry');
  const seen=new Set();
  for(const item of registry){
    if(typeof item.file!=='string'||!/^\/media\/[A-Za-z0-9_./-]+$/.test(item.file)
        ||item.file.split('/').slice(1).some(part=>!part||part.startsWith('.'))
        ||!mediaExtensions.has(path.extname(item.file)))throw Error('Invalid public media path');
    if(seen.has(item.file))throw Error('Duplicate provenance record: '+item.file);
    seen.add(item.file);
    if(!mediaKinds.has(item.kind)||!Number.isSafeInteger(item.bytes)||item.bytes<=0
        ||(typeof item.source!=='string'||!item.source.trim())||!item.approval?.trim()
        ||! /^[a-f0-9]{64}$/.test(item.sourceSha256??'')||! /^[a-f0-9]{64}$/.test(item.sha256??''))throw Error('Incomplete media provenance: '+item.file);
  }
  return registry;
}

function matchMedia(webRoot,directory){
  const registry=readRegistry(webRoot);
  for(const item of registry){
    const filename=path.join(directory,item.file.slice(1));
    const bytes=fs.readFileSync(filename);
    if(bytes.length!==item.bytes||sha256(bytes)!==item.sha256)throw Error('Media integrity mismatch: '+item.file);
  }
  return registry;
}

export function checkPublic(webRoot){
  const actual=listFiles(path.join(webRoot,'public'));
  const registry=matchMedia(webRoot,path.join(webRoot,'public'));
  const allowed=registry.map(item=>item.file.slice(1)).sort();
  if(JSON.stringify(actual)!==JSON.stringify(allowed))throw Error('public/ must contain only registered media; found missing or extra files');
  return registry;
}

export const secretPatterns=[
  /-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----/,
  /\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}/,
  /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/,
  /(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?):\/\/[^\s/]+:[^\s@]+@/i,
  /https?:\/\/[^\s/:]+:[^\s/@]+@/i,
  /(?:MAX_BOT_TOKEN|OPENAI_API_KEY|HEYGEN_API_KEY|SMTP_PASSWORD|SECRET_ACCESS_KEY)\s*[:=]\s*["'][^"'\s]{8,}/,
];

export function checkDist(webRoot,directory=path.join(webRoot,'dist')){
  const actual=listFiles(directory);
  const registry=matchMedia(webRoot,directory);
  const htmlFiles=[...routes.map(r=>r.path==='/'?'index.html':r.path.slice(1)+'/index.html'),'404.html'].sort();
  const required=new Set([...htmlFiles,'robots.txt','sitemap.xml',...registry.map(x=>x.file.slice(1))]);
  for(const name of required)if(!actual.includes(name))throw Error('Missing build file: '+name);
  for(const name of actual){
    if(name.split('/').some(part=>part.startsWith('.')))throw Error('Hidden metadata not allowed: '+name);
    if(!required.has(name)&&!/^assets\/[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8,}\.(?:js|css)$/.test(name))throw Error('Unexpected build file: '+name);
    if(/\.(?:html|js|css|txt|xml)$/.test(name)){
      const content=fs.readFileSync(path.join(directory,name),'utf8');
      if(secretPatterns.some(pattern=>pattern.test(content)))throw Error('Possible credential in build file: '+name);
    }
  }
  if(!actual.some(n=>/^assets\/.*\.js$/.test(n))||!actual.some(n=>/^assets\/.*\.css$/.test(n)))throw Error('Missing client assets');
  for(const name of htmlFiles){
    const html=fs.readFileSync(path.join(directory,name),'utf8');
    if(!html.includes('noindex,nofollow')||(html.match(/<h1[ >]/g)||[]).length!==1)throw Error('Missing SSG content or preview noindex: '+name);
  }
  if(fs.readFileSync(path.join(directory,'robots.txt'),'utf8')!=='User-agent: *\nDisallow: /\n')throw Error('Preview robots must disallow all');
  if(fs.readFileSync(path.join(directory,'sitemap.xml'),'utf8').includes('<url>'))throw Error('Preview sitemap must be empty');
  return {files:actual,media:registry.length,html:htmlFiles.length,bytes:actual.reduce((sum,f)=>sum+fs.statSync(path.join(directory,f)).size,0)};
}

export function distDigest(directory,files){
  return sha256(files.map(file=>file+'\0'+sha256(fs.readFileSync(path.join(directory,file)))).join('\n'));
}
