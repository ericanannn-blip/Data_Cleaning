import { Miniflare } from 'miniflare';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
const root=new URL('../',import.meta.url),digest=b=>createHash('sha256').update(b).digest('hex');
const mf=new Miniflare({modules:true,scriptPath:fileURLToPath(new URL('dist/server/index.js',root)),compatibilityDate:'2026-10-08',d1Databases:['DB'],r2Buckets:['BUCKET']});
const results=[];
const req=(path,options)=>mf.dispatchFetch('https://test.invalid'+path,options);
async function api(path,options,status=200){const r=await req(path,options);assert.equal(r.status,status,`${path}: ${await r.clone().text()}`);return r.json();}
async function check(name,fn){await fn();results.push(name);console.log('PASS '+name);}
const post=body=>({method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
const outputs=[];
try{
 const db=await mf.getD1Database('DB');
 const files=(await fs.readdir(new URL('drizzle/',root))).filter(x=>x.endsWith('.sql')).sort();
 for(const name of files){const sql=await fs.readFile(new URL('drizzle/'+name,root),'utf8');for(const statement of sql.split('--> statement-breakpoint')){if(statement.trim())await db.prepare(statement).run();}}
 await check('health and resumable config',async()=>{assert.equal((await api('/health')).status,'ok');const c=await api('/config');assert.equal(c.resumable_uploads,true);assert.equal(c.chunk_bytes,8388608);});
 const batch=(await api('/batches',{method:'POST'})).batch_id;
 async function upload(name,bytes,mappedName){
  let s=await api('/uploads',post({filename:name,size:bytes.length,mapped_name:mappedName,batch_id:batch}),201);
  for(let offset=0,n=1;offset<bytes.length;offset+=s.chunk_bytes,n++){const part=bytes.subarray(offset,offset+s.chunk_bytes);await api(`/uploads/${s.upload_id}/parts/${n}`,{method:'PUT',headers:{'x-chunk-sha256':digest(part)},body:part});}
  const done=await api(`/uploads/${s.upload_id}/complete`,{method:'POST'});const f=done.files[0];assert.equal(f.sha256,digest(bytes));const downloaded=await req(`/files/${f.id}/download`);assert.equal(digest(Buffer.from(await downloaded.arrayBuffer())),digest(bytes));assert.equal(f.source_filename,name);if(mappedName)assert.equal(f.original_name,mappedName);
  const a=await api(`/files/${f.id}/analysis`),schemas=await api(`/files/${f.id}/schemas`);assert.ok(schemas.schema.candidates[0].json_schema.$schema.endsWith('/2020-12/schema'));assert.ok(schemas.selection_criteria.length>=2);assert.equal(a.analysis.review.status,'pending');assert.equal(a.analysis.classification.status,'insufficient_evidence');outputs.push({name,analysis:a.analysis});return {s,f,a:a.analysis};
 }
 await check('PDF/JPEG/PNG/ZIP/unknown/empty originals and mapped DAT',async()=>{
  for(const [name,bytes,mapped,type] of [
   ['a.dat',Buffer.from('%PDF-1.4\nsynthetic fixture\n%%EOF'),'paper.pdf','pdf'],
   ['a.dat',Buffer.from([255,216,255,224,0,0]),'photo.jpg','jpeg'],
   ['img.dat',Buffer.from([137,80,78,71,13,10,26,10,0]),null,'png'],
   ['z.dat',Buffer.from([80,75,5,6,...new Array(18).fill(0)]),'bundle.zip','zip'],
   ['unknown.dat',Buffer.from([0,1,2,3,255]),null,'unknown'],
   ['empty.dat',Buffer.alloc(0),null,'empty']]){const {f}=await upload(name,bytes,mapped);assert.equal(f.detected_type,type);const alt=await req(`/files/${f.id}/download?name=detected`);assert.equal(alt.status,200);if(type==='pdf')assert.match(alt.headers.get('content-disposition'),/pdf/);}
 });
 await check('JSON nested/mixed/null/prototype keys and distinct fingerprints',async()=>{
  const x=await upload('nested.json',Buffer.from('{"__proto__":{"safe":true},"items":[{"a":1},{"a":null,"b":[true,"x"]}]}'));
  const s=x.a.schema.candidates[0].json_schema;assert.equal(Object.hasOwn(s.properties,'__proto__'),true);assert.deepEqual(s.properties.items.items.properties.a.type,['integer','null']);
  const y=await upload('nested.json',Buffer.from('{"__proto__":{"safe":true},"items":[{"a":"s"}]}'));assert.notEqual(x.a.schema.schema_fingerprint,y.a.schema.schema_fingerprint);
  for(const length of [100,1000]){const fixture=[...Array.from({length},(_,i)=>i),'late string'];const late=await upload(`heterogeneous-${length+1}.json`,Buffer.from(JSON.stringify(fixture)));assert.equal(late.a.extraction_status,'complete');assert.deepEqual(late.a.schema.candidates[0].json_schema.items.type,['integer','string']);assert.deepEqual(late.a.observed_sample,fixture);}
  const wide=Object.fromEntries(Array.from({length:125},(_,i)=>[`field_${i}`,i]));const wideFile=await upload('wide.json',Buffer.from(JSON.stringify(wide)));assert.equal(Object.keys(wideFile.a.schema.candidates[0].json_schema.properties).length,125);
 });
 await check('CSV quoted rows / malformed headers / large prefix explicit coverage',async()=>{
  const x=await upload('data.csv',Buffer.from('name,note\r\nA,"line 1\nline 2"\r\nB,"hello, world"\r\n'));assert.equal(x.a.row_sample_count,2);assert.equal(x.a.schema.candidates[0].json_schema.type,'array');
  const bad=await upload('bad.csv',Buffer.from('a,a\n1,2\n'));assert.equal(bad.a.extraction_status,'insufficient_data');assert.equal(bad.a.selection_criteria.find(r=>r.rule_id==='tabular-columns-v1').result,'fail');
  const large=await upload('long.json',Buffer.from(JSON.stringify({data:'x'.repeat(70000)})));assert.equal(large.a.evidence.coverage,'prefix_only');assert.notEqual(large.a.extraction_status,'complete');
 });
 await check('multipart interrupted/resumed/repeated chunk/conflict/missing checksum',async()=>{
  const bytes=Buffer.alloc(8388608+123,65),s=await api('/uploads',post({filename:'large.dat',size:bytes.length}),201),part=bytes.subarray(0,s.chunk_bytes),headers={'x-chunk-sha256':digest(part)};
  await api(`/uploads/${s.upload_id}/parts/1`,{method:'PUT',headers,body:part});
  const resumed=await api(`/uploads/${s.upload_id}`);assert.equal(resumed.parts.length,1);
  assert.equal((await api(`/uploads/${s.upload_id}/parts/1`,{method:'PUT',headers,body:part})).already_received,true);
  const altered=Buffer.alloc(part.length,66);await api(`/uploads/${s.upload_id}/parts/1`,{method:'PUT',headers:{'x-chunk-sha256':digest(altered)},body:altered},409);
  await api(`/uploads/${s.upload_id}/complete`,{method:'POST'},409);
  await api(`/uploads/${s.upload_id}/parts/2`,{method:'PUT',body:bytes.subarray(s.chunk_bytes)},422);
  await api(`/uploads/${s.upload_id}/parts/2`,{method:'PUT',headers:{'x-chunk-sha256':digest(bytes.subarray(s.chunk_bytes))},body:bytes.subarray(s.chunk_bytes)});
  const done=await api(`/uploads/${s.upload_id}/complete`,{method:'POST'});assert.equal(done.files[0].sha256,digest(bytes));
  const twice=await api(`/uploads/${s.upload_id}/complete`,{method:'POST'});assert.equal(twice.files[0].id,done.files[0].id);
 });
 await check('large 48 MiB file exceeds old 40 MiB limit with exact original hash',async()=>{const bytes=Buffer.alloc(48*1024*1024+17,84);bytes.set(Buffer.from('%PDF-1.4'));const {f}=await upload('big.dat',bytes);assert.equal(f.file_size,bytes.length);});
 await check('validation, safe names, abort, catalog and filtered pagination',async()=>{
  await api('/uploads',post({filename:'x',size:-1}),400);await api('/uploads',post(null),400);await api('/uploads',post({filename:'x',size:256*1024*1024+1}),400);await api('/files?limit=NaN',undefined,400);
  const s=await api('/uploads',post({filename:'../../outside.dat',mapped_name:'../safe.pdf',size:1}),201);assert.equal(s.filename,'outside.dat');assert.equal(s.mapped_name,'safe.pdf');await api(`/uploads/${s.upload_id}`,{method:'DELETE'});assert.equal((await api(`/uploads/${s.upload_id}`)).state,'aborted');
  assert.ok((await api('/schemas')).schemas.length>0);const list=await api('/files?type=pdf');assert.ok(list.files.every(f=>f.detected_type==='pdf'));const bySchema=await api('/files?schema='+outputs[0].analysis.schema.schema_fingerprint);assert.ok(bySchema.total>0);const b=await api('/batches/'+batch);assert.ok(b.succeeded>=10);
 });
 await fs.mkdir(new URL('test-results/',root),{recursive:true});await fs.writeFile(new URL('test-results/site-analysis-fixtures.json',root),JSON.stringify(outputs,null,2));await fs.writeFile(new URL('test-results/site-api-results.json',root),JSON.stringify({status:'PASS',checks:results},null,2));
 console.log(`PASS ${results.length} integration scenarios; synthetic fixtures only.`);
}finally{await mf.dispose();}
