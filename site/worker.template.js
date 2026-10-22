import { sha256 } from '@noble/hashes/sha256';
import { SAMPLE_BYTES, safeName, extOf, classify, analysisFor, taxonomyInfo } from './file-analysis.js';
const INDEX=__INDEX_HTML__, APP=__APP_JS__, CSS=__STYLES_CSS__, FAVICON=__FAVICON_SVG__;
const CHUNK_BYTES=8*1024*1024, MAX_FILE_BYTES=256*1024*1024;
const json=(value,status=200,headers={})=>new Response(JSON.stringify(value),{status,headers:{'content-type':'application/json; charset=utf-8','cache-control':'no-store',...headers}});
const error=(detail,status)=>json({detail},status);
const now=()=>new Date().toISOString();
const q=(db,sql,...args)=>db.prepare(sql).bind(...args);
const hex=b=>[...b].map(x=>x.toString(16).padStart(2,'0')).join('');
const number=(value,min,max,fallback)=>{if(value===null)return fallback;const n=Number(value);return Number.isSafeInteger(n)&&n>=min&&n<=max?n:null;};
async function bounded(body,limit){if(!body)return new Uint8Array();const reader=body.getReader(),parts=[];let total=0;try{while(true){const {done,value}=await reader.read();if(done)break;total+=value.length;if(total>limit){await reader.cancel();throw new RangeError('Request body exceeds limit.');}parts.push(value);}const bytes=new Uint8Array(total);let at=0;for(const p of parts){bytes.set(p,at);at+=p.length;}return bytes;}finally{reader.releaseLock();}}
async function bodyJson(request){return JSON.parse(new TextDecoder().decode(await bounded(request.body,8192)));}
async function fileRecord(env,id){return q(env.DB,'SELECT * FROM files WHERE id=?',id).first();}
function publicRecord(r){const {r2_key,analysis_json,...rest}=r;const a=JSON.parse(analysis_json||'null');return {...rest,source_filename:a?.source_filename||r.original_name,mapped_name:a?.mapped_name||null,schema_fingerprint:a?.schema?.schema_fingerprint||null};}
async function inspectStream(stream){const reader=stream.getReader(),hash=sha256.create(),parts=[];let bytes=0,sampled=0;while(true){const {done,value}=await reader.read();if(done)break;bytes+=value.length;hash.update(value);if(sampled<SAMPLE_BYTES){const part=value.slice(0,SAMPLE_BYTES-sampled);parts.push(part);sampled+=part.length;}}const sample=new Uint8Array(sampled);let at=0;for(const part of parts){sample.set(part,at);at+=part.length;}return {bytes,sample,sha256:hex(hash.digest())};}
async function register(env,{id,key,name,sourceName,mappedName,batchId,audit}){
 const existing=await fileRecord(env,id);if(existing)return publicRecord(existing);
 const meta=classify(audit.sample,audit.bytes,name),analysis=analysisFor(meta,audit.sample);Object.assign(analysis,{file_id:id,source_filename:sourceName||name,mapped_name:mappedName||null,source_sha256:audit.sha256});
 Object.assign(meta,{id,original_name:name,extension:extOf(name),created_at:now(),sha256:audit.sha256,r2_key:key,analysis_json:JSON.stringify(analysis)});
 const insert=q(env.DB,'INSERT OR IGNORE INTO files(id,original_name,extension,file_size,mime_type,encoding,magic_bytes,printable_ratio,sample_bytes,detected_type,confidence,status,created_at,error_message,sha256,r2_key,analysis_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',id,name,meta.extension,meta.file_size,meta.mime_type,meta.encoding,meta.magic_bytes,meta.printable_ratio,meta.sample_bytes,meta.detected_type,meta.confidence,meta.status,meta.created_at,meta.error_message,meta.sha256,key,meta.analysis_json);
 if(batchId)await env.DB.batch([insert,q(env.DB,'INSERT INTO batch_items(batch_id,file_id,rejection) SELECT ?,?,NULL WHERE NOT EXISTS(SELECT 1 FROM batch_items WHERE batch_id=? AND file_id=?)',batchId,id,batchId,id)]);else await insert.run();
 return publicRecord(meta);
}
async function batchResult(env,id,limit,after){const b=await q(env.DB,'SELECT * FROM batches WHERE id=?',id).first();if(!b)return null;const rows=await q(env.DB,'SELECT bi.sequence,bi.file_id,bi.rejection,f.* FROM batch_items bi LEFT JOIN files f ON f.id=bi.file_id WHERE bi.batch_id=? AND bi.sequence>? ORDER BY bi.sequence LIMIT ?',id,after,limit+1).all();const sums=await q(env.DB,'SELECT SUM(CASE WHEN file_id IS NOT NULL THEN 1 ELSE 0 END) succeeded,SUM(CASE WHEN file_id IS NULL THEN 1 ELSE 0 END) failed FROM batch_items WHERE batch_id=?',id).first();const page=rows.results||[];return {batch_id:id,state:b.state,files:page.slice(0,limit).map(r=>r.file_id?publicRecord(r):JSON.parse(r.rejection)),succeeded:sums.succeeded||0,failed:sums.failed||0,results_truncated:page.length>limit,next_cursor:page.length>limit?page[limit-1].sequence:null};}
async function session(env,id){return q(env.DB,'SELECT * FROM upload_sessions WHERE id=?',id).first();}
async function sessionView(env,s){const parts=await q(env.DB,'SELECT part_number,size,sha256,etag FROM upload_parts WHERE upload_id=? ORDER BY part_number',s.id).all();return {upload_id:s.id,filename:s.source_name,mapped_name:s.mapped_name,size:s.size,chunk_bytes:CHUNK_BYTES,state:s.state,file_id:s.state==='completed'?s.id:null,parts:parts.results||[]};}
async function uploads(request,env,path){
 const method=request.method;
 if(path==='/uploads'&&method==='POST'){
  const b=await bodyJson(request);if(!b||typeof b!=='object'||Array.isArray(b)||typeof b.filename!=='string'||!Number.isSafeInteger(b.size)||b.size<0||b.size>MAX_FILE_BYTES||b.batch_id!==undefined&&typeof b.batch_id!=='string')return error('filename and size (0–256 MiB) are required.',400);
  if(b.batch_id&&!await q(env.DB,'SELECT id FROM batches WHERE id=?',b.batch_id).first())return error('Batch not found.',404);
  const id=crypto.randomUUID(),key=`originals/${id}`,sourceName=safeName(b.filename),mappedName=typeof b.mapped_name==='string'?safeName(b.mapped_name):null;
  const multipart=b.size?await env.BUCKET.createMultipartUpload(key,{httpMetadata:{contentType:'application/octet-stream'}}):null;
  try{await q(env.DB,'INSERT INTO upload_sessions(id,r2_key,multipart_id,source_name,mapped_name,size,batch_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',id,key,multipart?.uploadId||'',sourceName,mappedName,b.size,b.batch_id||null,'open',now(),now()).run();}catch(e){if(multipart)await multipart.abort().catch(()=>{});throw e;}
  return json(await sessionView(env,await session(env,id)),201);
 }
 const match=path.match(/^\/uploads\/([a-zA-Z0-9-]+)(?:\/(complete|parts\/([0-9]+)))?$/);if(!match)return null;
 let s=await session(env,match[1]);if(!s)return error('Upload not found.',404);
 if(!match[2]&&method==='GET')return json(await sessionView(env,s));
 if(!match[2]&&method==='DELETE'){
  if(s.state==='aborted')return json({state:'aborted'});
  if(s.state==='completed')return error('Completed originals cannot be removed through the upload endpoint.',409);
  if(s.state==='finalizing')return error('Upload is being finalized.',409);
  if(s.multipart_id)await env.BUCKET.resumeMultipartUpload(s.r2_key,s.multipart_id).abort();
  await q(env.DB,"UPDATE upload_sessions SET state='aborted',updated_at=? WHERE id=?",now(),s.id).run();return json({state:'aborted'});
 }
 if(match[3]&&method==='PUT'){
  if(s.state!=='open')return error('Upload is not open.',409);
  const n=number(match[3],1,Math.ceil(s.size/CHUNK_BYTES),null);if(n===null)return error('Invalid part number.',400);
  const size=Math.min(CHUNK_BYTES,s.size-(n-1)*CHUNK_BYTES),bytes=await bounded(request.body,size);if(bytes.length!==size)return error('Chunk size does not match the expected range.',400);
  const digest=hex(sha256(bytes));if(request.headers.get('x-chunk-sha256')!==digest)return error('Chunk SHA-256 mismatch.',422);
  await q(env.DB,'INSERT OR IGNORE INTO upload_parts(upload_id,part_number,size,sha256,etag) VALUES(?,?,?,?,NULL)',s.id,n,size,digest).run();
  let part=await q(env.DB,'SELECT * FROM upload_parts WHERE upload_id=? AND part_number=?',s.id,n).first();if(part.sha256!==digest||part.size!==size)return error('A different chunk is already assigned to this part.',409);
  if(part.etag)return json({part_number:n,size,sha256:digest,etag:part.etag,already_received:true});
  const result=await env.BUCKET.resumeMultipartUpload(s.r2_key,s.multipart_id).uploadPart(n,bytes);
  await q(env.DB,'UPDATE upload_parts SET etag=? WHERE upload_id=? AND part_number=? AND sha256=?',result.etag,s.id,n,digest).run();
  await q(env.DB,'UPDATE upload_sessions SET updated_at=? WHERE id=?',now(),s.id).run();return json({part_number:n,size,sha256:digest,etag:result.etag});
 }
 if(match[2]==='complete'&&method==='POST'){
  if(s.state==='completed')return json({files:[publicRecord(await fileRecord(env,s.id))],succeeded:1,failed:0});
  if(s.state==='aborted')return error('Upload was aborted.',409);
  const rows=(await q(env.DB,'SELECT * FROM upload_parts WHERE upload_id=? ORDER BY part_number',s.id).all()).results||[];
  if(rows.length!==Math.ceil(s.size/CHUNK_BYTES)||rows.some((p,i)=>p.part_number!==i+1||!p.etag)||rows.reduce((n,p)=>n+p.size,0)!==s.size)return error('Upload has missing or incomplete chunks.',409);
  const claim=await q(env.DB,"UPDATE upload_sessions SET state='finalizing',updated_at=? WHERE id=? AND (state='open' OR (state='finalizing' AND updated_at<?))",now(),s.id,new Date(Date.now()-120000).toISOString()).run();
  if(!claim.meta.changes)return error('Finalization in progress. Retry after two minutes if interrupted.',409);
  try{
   let object=await env.BUCKET.get(s.r2_key);
   if(!object){if(s.size)await env.BUCKET.resumeMultipartUpload(s.r2_key,s.multipart_id).complete(rows.map(p=>({partNumber:p.part_number,etag:p.etag})));else await env.BUCKET.put(s.r2_key,new Uint8Array());object=await env.BUCKET.get(s.r2_key);}
   if(!object)throw new Error('Original missing after completion.');
   const audit=await inspectStream(object.body);if(audit.bytes!==s.size)throw new Error('Stored length does not match expected length.');
   const file=await register(env,{id:s.id,key:s.r2_key,name:s.mapped_name||s.source_name,sourceName:s.source_name,mappedName:s.mapped_name,batchId:s.batch_id,audit});
   await q(env.DB,"UPDATE upload_sessions SET state='completed',updated_at=? WHERE id=?",now(),s.id).run();return json({files:[file],succeeded:1,failed:0});
  }catch(e){await q(env.DB,"UPDATE upload_sessions SET state='open',updated_at=? WHERE id=?",now(),s.id).run();throw e;}
 }
 return error('Method not allowed.',405);
}
async function route(request,env){
 const url=new URL(request.url),path=url.pathname,method=request.method;
 if(path==='/'&&method==='GET')return new Response(INDEX,{headers:{'content-type':'text/html; charset=utf-8','content-security-policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}});
 const assets={'/static/app.js':[APP,'text/javascript'],'/static/styles.css':[CSS,'text/css'],'/static/favicon.svg':[FAVICON,'image/svg+xml']};if(assets[path])return new Response(assets[path][0],{headers:{'content-type':assets[path][1]}});
 if(path==='/health'){await q(env.DB,'SELECT 1').first();return json({status:'ok'});}
 if(path==='/config')return json({max_file_bytes:MAX_FILE_BYTES,max_request_bytes:null,chunk_bytes:CHUNK_BYTES,max_files:null,authentication_required:false,resumable_uploads:true,concurrent_files:2});
 if(path==='/taxonomy')return json(taxonomyInfo);
 if(path==='/docs')return json({upload:'POST /uploads {filename,size,mapped_name?,batch_id?}; PUT /uploads/{id}/parts/{n} + X-Chunk-SHA256; GET /uploads/{id}; POST /uploads/{id}/complete',original:'GET /files/{id}/download?name=detected',schema:'GET /files/{id}/schemas?download=1',limits:{file_bytes:MAX_FILE_BYTES,chunk_bytes:CHUNK_BYTES,sample_bytes:SAMPLE_BYTES}});
 if(path.startsWith('/uploads'))return await uploads(request,env,path)||error('Not found.',404);
 if(path==='/batches'&&method==='POST'){const id=crypto.randomUUID();await q(env.DB,'INSERT INTO batches(id,created_at,state) VALUES(?,?,?)',id,now(),'open').run();return json({batch_id:id});}
 const batch=path.match(/^\/batches\/([^/]+)$/);if(batch&&method==='GET'){const limit=number(url.searchParams.get('limit'),1,200,200),after=number(url.searchParams.get('after'),0,Number.MAX_SAFE_INTEGER,0);if(limit===null||after===null)return error('Invalid pagination.',400);const result=await batchResult(env,batch[1],limit,after);return result?json(result):error('Batch not found.',404);}
 if(path==='/files'&&method==='GET'){
  const limit=number(url.searchParams.get('limit'),1,200,100),offset=number(url.searchParams.get('offset'),0,Number.MAX_SAFE_INTEGER,0);if(limit===null||offset===null)return error('Invalid pagination.',400);
  const type=url.searchParams.get('type'),search=url.searchParams.get('q'),fingerprint=url.searchParams.get('schema');const clauses=[],args=[];if(type){clauses.push('detected_type=?');args.push(type);}if(fingerprint){clauses.push("json_extract(analysis_json,'$.schema.schema_fingerprint')=?");args.push(fingerprint);}if(search){clauses.push("original_name LIKE ? ESCAPE '\\'");args.push('%'+search.replace(/[\\%_]/g,'\\$&')+'%');}const where=clauses.length?' WHERE '+clauses.join(' AND '):'';
  const list=await q(env.DB,'SELECT * FROM files'+where+' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',...args,limit,offset).all();const total=await q(env.DB,"SELECT COUNT(*) total,SUM(CASE WHEN confidence='signature' THEN 1 ELSE 0 END) signatures,SUM(CASE WHEN confidence!='signature' THEN 1 ELSE 0 END) review FROM files"+where,...args).first();return json({files:(list.results||[]).map(publicRecord),total:total.total,summary:{total:total.total,signatures:total.signatures||0,review:total.review||0},limit,offset});
 }
 // Compatibility raw-stream endpoint for existing tools, capped to one chunk. Larger files use /uploads.
 if(path==='/files'&&method==='POST'){
  const bytes=await bounded(request.body,CHUNK_BYTES),id=crypto.randomUUID(),key=`originals/${id}`,name=safeName(url.searchParams.get('filename'));
  const batchId=url.searchParams.get('batch_id');if(batchId&&!await q(env.DB,'SELECT id FROM batches WHERE id=?',batchId).first())return error('Batch not found.',404);
  await env.BUCKET.put(key,bytes);const file=await register(env,{id,key,name,batchId,audit:{bytes:bytes.length,sample:bytes.slice(0,SAMPLE_BYTES),sha256:hex(sha256(bytes))}});return json({batch_id:batchId,files:[file],succeeded:1,failed:0});
 }
 const m=path.match(/^\/files\/([^/]+)(?:\/(analysis|schemas|download|reprocess))?$/);if(m){const id=m[1],action=m[2],r=await fileRecord(env,id);if(!r)return error('File record not found.',404);
  if(method==='GET'&&!action)return json(publicRecord(r));
  if(method==='GET'&&(action==='analysis'||action==='schemas')){let a=JSON.parse(r.analysis_json||'null');if(!a?.contract_version){const obj=await env.BUCKET.get(r.r2_key,{range:{offset:0,length:SAMPLE_BYTES}});const sample=obj?new Uint8Array(await obj.arrayBuffer()):new Uint8Array();a={...analysisFor(r,sample),file_id:id,source_sha256:r.sha256,source_filename:r.original_name,mapped_name:null};}const body=action==='schemas'?{file_id:id,source_sha256:r.sha256,job:{state:'completed'},schema:a.schema,selection_criteria:a.selection_criteria,evidence:a.evidence,classification:a.classification,review:a.review,extraction_status:a.extraction_status}: {file_id:id,job:{state:'completed',attempts:1,error_code:null},analysis:a};return json(body,200,url.searchParams.get('download')==='1'?{'content-disposition':`attachment; filename="${id}.schema.json"`}:{});}
  if(method==='GET'&&action==='download'){const object=await env.BUCKET.get(r.r2_key);if(!object)return error('Original bytes are missing.',410);let name=r.original_name;const ext={jpeg:'jpg',gzip:'gz'}[r.detected_type]||r.detected_type;if(url.searchParams.get('name')==='detected'&&!['unknown','empty','iso_media','ole'].includes(ext)&&['dat',''].includes(extOf(name)))name=(extOf(name)==='dat'?name.slice(0,-4):name)+'.'+ext;return new Response(object.body,{headers:{'content-type':'application/octet-stream','content-disposition':`attachment; filename*=UTF-8''${encodeURIComponent(name).replace(/'/g,'%27')}`,'content-length':String(r.file_size),'x-content-sha256':r.sha256,'cache-control':'private, no-store'}});}
  if(method==='POST'&&action==='reprocess')return error('Online inspection is bounded to 64 KiB; OCR and archive extraction are available only in the local Python backend.',501);
 }
 if(path==='/schemas'&&method==='GET'){const rows=await q(env.DB,"SELECT detected_type,json_extract(analysis_json,'$.schema.schema_fingerprint') schema_fingerprint,COUNT(*) file_count FROM files GROUP BY detected_type,schema_fingerprint ORDER BY detected_type LIMIT 200").all();return json({schemas:(rows.results||[]).map(r=>({...r,filter_url:r.schema_fingerprint?'/files?schema='+encodeURIComponent(r.schema_fingerprint):'/files?type='+encodeURIComponent(r.detected_type),note:'Observed schema grouping; semantic labels remain pending.'}))});}
 return error('Not found.',404);
}
export default {async fetch(request,env){let response;try{response=await route(request,env);}catch(e){if(e instanceof RangeError)response=error(e.message,413);else if(e instanceof SyntaxError)response=error('Invalid JSON request.',400);else {console.error('request failed',e?.name||'Error');response=error('The service could not complete this request. Retry or check upload status.',500);}}const headers=new Headers(response.headers);headers.set('x-content-type-options','nosniff');headers.set('referrer-policy','no-referrer');headers.set('x-frame-options','DENY');if(!headers.has('cache-control'))headers.set('cache-control','no-store');return new Response(response.body,{status:response.status,headers});}};
