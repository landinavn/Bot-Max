import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {checkDist,distDigest} from './release-safety.mjs';
const result=checkDist(process.cwd());
const git=(...args)=>execFileSync('git',args,{encoding:'utf8'}).trim();
fs.mkdirSync('.ssr',{recursive:true});
fs.writeFileSync('.ssr/build-receipt.json',JSON.stringify({sourceCommit:git('rev-parse','HEAD'),sourceClean:git('status','--porcelain','--untracked-files=normal','--','.')==='',distSha256:distDigest(path.resolve('dist'),result.files)},null,2)+'\n');
console.log(`Preview checked: ${result.html} HTML files, ${result.media} media files, ${result.files.length} total files; ${result.bytes} bytes. No unexpected files or known credential patterns.`);
