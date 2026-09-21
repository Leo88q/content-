global.window = { addEventListener(){}, devicePixelRatio: 1 };
global.document = { querySelector: () => ({ addEventListener(){}, classList:{add(){},remove(){}}, textContent:"", innerHTML:"" }), querySelectorAll: () => [], addEventListener(){} };
global.localStorage = { getItem: () => null, setItem(){} };
global.location = { hash: "" };
const fs = require("fs");
const src = fs.readFileSync(__dirname + "/../site/app.js", "utf8");
const test = `
const snap = JSON.parse(require('fs').readFileSync('${__dirname}/../site/data/snapshot.json','utf8'));
for (const p of snap.pools) console.log(p.base_symbol + ': ' + narrative(p,'ru').text);
console.log('SMOKE2 OK');
`;
eval(src.replace('"use strict";', "") + test);
