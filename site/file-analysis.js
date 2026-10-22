import taxonomy from './taxonomy.json';
import { sha256 } from '@noble/hashes/sha256';
export const SAMPLE_BYTES=65536;
const dialect='https://json-schema.org/draft/2020-12/schema';
export const safeName=s=>(String(s||'unnamed.dat').replace(/[\\/]/g,'/').split('/').pop().replace(/[\u0000-\u001f\u007f]/g,'').slice(0,255)||'unnamed.dat');
export const extOf=name=>{const i=name.lastIndexOf('.');return i>0?name.slice(i+1).toLowerCase().slice(0,32):'';};
export function classify(b,fileSize,name){
 const out={file_size:fileSize,magic_bytes:[...b.slice(0,32)].map(x=>x.toString(16).padStart(2,'0')).join(''),printable_ratio:0,sample_bytes:b.length,encoding:null,detected_type:'unknown',mime_type:'application/octet-stream',confidence:'unknown',status:'unsupported',error_message:'Unknown format. Original bytes remain available.'};
 if(!b.length)return {...out,detected_type:'empty',error_message:'Empty file.'};
 const starts=(...xs)=>xs.every((v,i)=>b[i]===v);
 const signatures=[
 [starts(37,80,68,70,45),'pdf','application/pdf'],[starts(80,75,3,4)||starts(80,75,5,6)||starts(80,75,7,8),'zip','application/zip'],
 [starts(137,80,78,71,13,10,26,10),'png','image/png'],[starts(255,216,255),'jpeg','image/jpeg'],[starts(71,73,70,56),'gif','image/gif'],[starts(66,77),'bmp','image/bmp'],[starts(31,139),'gzip','application/gzip'],[starts(55,122,188,175,39,28),'7z','application/x-7z-compressed'],[starts(82,97,114,33,26,7),'rar','application/vnd.rar'],[starts(83,81,76,105,116,101,32,102,111,114,109,97,116,32,51,0),'sqlite','application/vnd.sqlite3'],[starts(79,103,103,83),'ogg','application/ogg'],[starts(73,68,51),'mp3','audio/mpeg']];
 if(starts(82,73,70,70)&&b[8]===87&&b[9]===65&&b[10]===86&&b[11]===69)signatures.push([true,'wav','audio/wav']);
 if(starts(208,207,17,224,161,177,26,225))signatures.push([true,'ole','application/x-ole-storage']);
 if(b.length>=12&&b[4]===102&&b[5]===116&&b[6]===121&&b[7]===112)signatures.push([true,'iso_media','application/octet-stream']);
 const sig=signatures.find(x=>x[0]);if(sig)return {...out,detected_type:sig[1],mime_type:sig[2],confidence:'signature',status:'ok',error_message:null};
 let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(b,{stream:fileSize>b.length});}catch{return out;}
 const controls=[...text].filter(c=>{const n=c.charCodeAt(0);return n<32&&![9,10,13].includes(n);}).length;
 out.printable_ratio=Number((1-controls/Math.max(1,text.length)).toFixed(4));
 if(controls/text.length>.02||text.includes('\0'))return out;
 let type='text',mime='text/plain';
 if(fileSize<=b.length){try{JSON.parse(text);type='json';mime='application/json';}catch{}}
 if(type==='text'&&/^\s*(<!doctype\s+html|<html\b)/i.test(text)){type='html';mime='text/html';}
 if(type==='text'&&/^\s*<\?xml\b/.test(text)){type='xml';mime='application/xml';}
 if(type==='text'&&['csv','tsv'].includes(extOf(name))){type=extOf(name);mime=type==='csv'?'text/csv':'text/tab-separated-values';}
 return {...out,detected_type:type,mime_type:mime,encoding:'utf-8',confidence:'heuristic',status:'ok',error_message:null};
}
function infer(values,depth=0){
 if(depth>5)return {};
 const types=[...new Set(values.map(v=>v===null?'null':Array.isArray(v)?'array':typeof v==='object'?'object':typeof v==='number'?(Number.isInteger(v)?'integer':'number'):typeof v))];
 if(types.includes('number')&&types.includes('integer'))types.splice(types.indexOf('integer'),1);
 const out={type:types.length===1?types[0]:types};
 const objects=values.filter(v=>v&&typeof v==='object'&&!Array.isArray(v));
 if(objects.length){const keys=[...new Set(objects.flatMap(Object.keys))];out.properties=Object.fromEntries(keys.map(k=>[k,infer(objects.filter(v=>Object.hasOwn(v,k)).map(v=>v[k]),depth+1)]));const required=keys.filter(k=>objects.every(v=>Object.hasOwn(v,k)));if(required.length)out.required=required;out.additionalProperties=true;}
 const arrays=values.filter(Array.isArray);if(arrays.length){const entries=arrays.flatMap(v=>v);out.items=entries.length?infer(entries,depth+1):{};}
 return out;
}
function csv(text,delimiter,complete){
 const rows=[];let row=[],cell='',quoted=false;
 for(let i=0;i<text.length;i++){const c=text[i];if(quoted){if(c==='"'&&text[i+1]==='"'){cell+='"';i++;}else if(c==='"')quoted=false;else cell+=c;}else if(c==='"')quoted=true;else if(c===delimiter){row.push(cell);cell='';}else if(c==='\n'){row.push(cell.replace(/\r$/,''));rows.push(row);row=[];cell='';}else cell+=c;}
 if(complete&&!quoted&&(cell||row.length)){row.push(cell.replace(/\r$/,''));rows.push(row);}
 return {rows,unfinished:quoted||(!complete&&(cell.length>0||row.length>0))};
}
export function analysisFor(meta,sample){
 const complete=meta.file_size<=sample.length;
 const evidence={scope:'file_content',byte_start:0,byte_end:sample.length,file_bytes:meta.file_size,coverage:complete?'whole_file':'prefix_only'};
 const result={contract_version:'file-analysis-1.0.0',format:meta.detected_type,status:meta.status,evidence_scope:'file_content',sample_bytes:sample.length,evidence,review:{status:'pending'},usage:{training:'not_authorized',rag:'review_required'},classification:{status:'insufficient_evidence',topics:[],tasks:[],label_evidence:[],reason:'Format and structure inspection does not establish semantic topic labels.',taxonomy_reference:taxonomy.reference},extraction_note:'At most 64 KiB inspected. JSON larger than this is treated as unparsed text, not an inferred JSON structure. PDF/image/archive bodies are retained, not OCRed or unpacked. ZIP may contain Office files; OLE and ISO media are generic containers without subtype recovery.'};
 let observed=null,contentSchema=null,extractStatus='unsupported';const rules=[];
 rules.push({rule_id:'format-v1',field:'header_or_text',operator:'signature_or_utf8_heuristic',expected:'recognized header or valid UTF-8 without control characters',result:meta.detected_type==='unknown'?'unknown':'pass',reason:`${meta.detected_type}; ${meta.confidence}`,evidence});
 if(meta.encoding==='utf-8'){
 const text=new TextDecoder().decode(sample,{stream:!complete});
 if(meta.detected_type==='json'&&complete){try{observed=JSON.parse(text);contentSchema={$schema:dialect,...infer([observed])};extractStatus='complete';}catch{extractStatus='failed';}}
 else if(['csv','tsv'].includes(meta.detected_type)){
 const parsed=csv(text,meta.detected_type==='tsv'?'\t':',',complete),rows=parsed.rows;
 const headers=rows[0]||[];const unique=headers.length&&headers.every(Boolean)&&new Set(headers).size===headers.length;
 const good=rows.slice(1).filter(r=>r.length===headers.length);const bad=rows.slice(1).length-good.length;
 rules.push({rule_id:'tabular-columns-v1',field:'header_and_rows',operator:'unique_headers_and_consistent_width',expected:'Nonempty unique headers; data rows match column count',result:unique&&bad===0?'pass':'fail',reason:`${headers.length} columns; ${bad} inconsistent sampled rows; ${parsed.unfinished?'unfinished trailing row excluded':'complete trailing row'}`,evidence});
 if(unique&&good.length){observed=good.slice(0,100).map(r=>Object.fromEntries(headers.map((h,i)=>[h,r[i]])));contentSchema={$schema:dialect,type:'array',items:infer(observed)};extractStatus=complete&&!parsed.unfinished&&bad===0?'complete':'partial';result.row_sample_count=good.length;}
 else extractStatus='insufficient_data';
 }else {contentSchema={$schema:dialect,type:'string',description:`Observed UTF-8 ${meta.detected_type} prefix; no semantic field extraction.`};extractStatus=complete?'complete':'partial';}
 }
 if(!contentSchema){contentSchema={$schema:dialect,type:'object',title:`${meta.detected_type} file envelope`,description:'Metadata envelope only; no schema inferred for the binary body.',properties:{detected_type:{const:meta.detected_type},mime_type:{const:meta.mime_type},file_size:{type:'integer',minimum:0}},required:['detected_type','mime_type','file_size'],additionalProperties:false};observed={detected_type:meta.detected_type,mime_type:meta.mime_type,file_size:meta.file_size};}
 rules.push({rule_id:'coverage-v1',field:'sample_bytes',operator:'equals_file_bytes',expected:meta.file_size,result:complete?'pass':'unknown',reason:complete?'All bytes were available to bounded inspection.':'Only the prefix was inspected; unseen fields and rows remain unknown.',evidence});
 result.extraction_status=extractStatus;
 const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
 const fingerprint=[...sha256(new TextEncoder().encode(JSON.stringify(canonical(contentSchema))))].map(x=>x.toString(16).padStart(2,'0')).join('');
 result.schema={schema_fingerprint:fingerprint,candidates:[{name:'observed',purpose:meta.encoding==='utf-8'?'Observed structure only; constraints are not a universal domain model.':'Metadata envelope; binary body remains unparsed.',json_schema:contentSchema,selection_criteria:rules,evidence,review_status:'pending'}]};
 result.selection_criteria=rules;result.observed_sample=observed;return result;
}
export const taxonomyInfo=taxonomy;
