import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const source=await fs.readFile(new URL('../site/worker.template.js',import.meta.url),'utf8');
for(const route of ['/health','/config','/batches','/files','download','reprocess']) assert.ok(source.includes(route),`missing route ${route}`);
const config=JSON.parse(await fs.readFile(new URL('../.openai/hosting.json',import.meta.url),'utf8'));
assert.equal(config.project_id,'appgprj_6ac388f8dd9c8191ae05d3b8480e7b4b');assert.equal(config.d1,'DB');assert.equal(config.r2,'BUCKET');
const html=await fs.readFile(new URL('../backend/static/index.html',import.meta.url),'utf8');const app=await fs.readFile(new URL('../backend/static/app.js',import.meta.url),'utf8');
assert.match(html,/drop-zone/);assert.match(app,/application\/octet-stream/);assert.match(app,/\/batches/);
console.log('Source checks passed: private-site identity, D1/R2 bindings, Worker routes, frontend upload flow.');
