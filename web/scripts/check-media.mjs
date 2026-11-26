import {checkPublic} from './release-safety.mjs';
console.log(`Public media checked: ${checkPublic(process.cwd()).length} unique registered files; no service documents.`);
