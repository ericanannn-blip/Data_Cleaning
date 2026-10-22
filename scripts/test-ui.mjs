// Execute the browser upload logic with synthetic Files and a simulated API.
// This verifies control flow, not browser rendering or the real R2 service.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

class Element {
  constructor() {
    this.value = '';
    this.open = false; this.isConnected = true;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; for(const handler of this.listeners.close||[])handler({target:this}); }
  get options() { return this.children; }

}

const elements = new Map();
const localValues = new Map();
const context = vm.createContext({
  document: {
    addEventListener() {},
    createElement: () => new Element(),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
  },
  window: { addEventListener() {} },
  crypto: webcrypto,
  Blob, File, AbortSignal, console, setTimeout, clearTimeout,
  localStorage: {
    getItem: key => localValues.get(key) || null,
    setItem: (key, value) => localValues.set(key, value),
    removeItem: key => localValues.delete(key),
  },
});
const markup = fs.readFileSync(new URL('../backend/static/index.html', import.meta.url), 'utf8');
assert.doesNotMatch(markup, /<\/(?:area|base|br|col|embed|hr|img|input|link|meta|param|source|track|wbr)\s*>/i, 'Void HTML elements must not have closing tags.');
const styles = fs.readFileSync(new URL('../backend/static/styles.css', import.meta.url), 'utf8');
const drawerStyle = styles.match(/\.detail-drawer\s*\{([^}]+)\}/)?.[1];
assert.ok(drawerStyle, 'File drawer sizing rule must exist.');
assert.match(drawerStyle, /width:\s*min\(640px,\s*100%\)/);
assert.match(drawerStyle, /max-width:\s*100%/);
assert.doesNotMatch(drawerStyle, /100vw/, 'Drawer width must use the document containing block, excluding its scrollbar.');
const source = fs.readFileSync(new URL('../backend/static/app.js', import.meta.url), 'utf8');
assert.match(source, /initialize\(\);\s*$/);
vm.runInContext(source.replace(/initialize\(\);\s*$/, ''), context);
const run = code => vm.runInContext(code, context);
// Match the native select defaults supplied by index.html.
elements.get('taxonomy-axis').value = 'all';
elements.get('type-filter').value = 'all';

const bytes = new Uint8Array(20 * 1024 * 1024 + 10);
bytes[0] = 3;
bytes[9 * 1024 * 1024] = 7;
bytes[bytes.length - 1] = 9;
const fixture = new File([bytes], 'sample.dat', { lastModified: 100 });
context.fixture = fixture;
const session = {
  upload_id: 'synthetic-session',
  filename: fixture.name,
  size: fixture.size,
  chunk_bytes: 8 * 1024 * 1024,
  state: 'uploading',
  mapped_name: null,
  parts: [],
};
let putCount = 0;
let createCount = 0;
let completeCount = 0;
let lostPartResponse = false;
context.apiStub = async (path, options = {}) => {
  if (path === '/uploads') { createCount++; return session; }
  if (path.endsWith('/complete')) {
    completeCount++;
    session.state = 'completed';
    return { files: [{ id: session.upload_id }] };
  }
  if (options.method === 'PUT') {
    putCount++;
    const number = Number(path.split('/').pop());
    session.parts.push({
      part_number: number,
      size: options.body.size,
      sha256: options.headers['X-Chunk-SHA256'],
      etag: `synthetic-etag-${number}`,
    });
    // The server accepted part 2, but its response never arrived.
    if (number === 2 && !lostPartResponse) {
      lostPartResponse = true;
      throw new Error('Synthetic lost response');
    }
    return session;
  }
  return session;
};
run('api=apiStub; state.limits={max_file_bytes:268435456,resumable_uploads:true}; state.transferTotal=fixture.size;');
const uploaded = await run('uploadChunked(fixture,"batch")');
assert.equal(uploaded.id, session.upload_id);
assert.equal(putCount, 3, 'Lost part response must reconcile without sending the part twice.');
assert.equal(createCount, 1);
assert.equal(completeCount, 1);
assert.deepEqual(session.parts.map(p => p.size), [8388608, 8388608, 4194314]);

// A completed session can be recovered after its final response is lost.
await run('sessionKey(fixture).then(key=>saveSession(key,"synthetic-session"))');
putCount = 0;
const recovered = await run('uploadChunked(fixture,"new-batch")');
assert.equal(recovered.id, session.upload_id);
assert.equal(putCount, 0);
assert.equal(createCount, 1);

// Same name/size/mtime/first/last bytes do not permit changed middle bytes.
const changedBytes = bytes.slice();
changedBytes[9 * 1024 * 1024] = 8;
context.changed = new File([changedBytes], 'sample.dat', { lastModified: 100 });
assert.notEqual(await run('sessionKey(changed)'), await run('sessionKey(fixture)'));
// Even tampered local session storage cannot merge different saved bytes.
await run('sessionKey(changed).then(key=>saveSession(key,"synthetic-session"))');
await assert.rejects(() => run('uploadChunked(changed,"batch")'), /内容发生变化/);

run('state.pauseRequested=true');
assert.equal(await run('uploadChunked(fixture,"batch")'), null);
assert.equal(putCount, 0);

// Changed middle content selects a separate upload when local storage is valid.
const differentSession = { ...session, upload_id: 'different-content', state: 'uploading', parts: [] };
let differentCreates = 0;
context.differentApi = async (path, options = {}) => {
  if (path === '/uploads') { differentCreates++; return differentSession; }
  if (path.endsWith('/complete')) return { files: [{ id: differentSession.upload_id }] };
  if (options.method === 'PUT') {
    differentSession.parts.push({
      part_number: Number(path.split('/').pop()), size: options.body.size,
      sha256: options.headers['X-Chunk-SHA256'],
      etag: `different-etag-${path.split('/').pop()}`,
    });
  }
  return differentSession;
};
await run('sessionKey(changed).then(key=>saveSession(key,null))');
run('state.pauseRequested=false;api=differentApi');
assert.equal((await run('uploadChunked(changed,"batch")')).id, 'different-content');
assert.equal(differentCreates, 1);
assert.equal(differentSession.parts.length, 3);

// A reserved database row is not a durable R2 acknowledgement until it has an ETag.
for (const etag of [null, '', undefined, 0, '   ']) {
  context.checkPart = { etag };
  assert.equal(run('acknowledgedPart(checkPart)'), false);
}
context.checkPart = { etag: 'valid-r2-etag' };
assert.equal(run('acknowledgedPart(checkPart)'), true);
context.pendingFile = new File([bytes], 'pending.dat', { lastModified: 100 });
const pendingSession = {
  ...session, upload_id: 'pending-session', filename: 'pending.dat', state: 'open',
  parts: session.parts.map((part, index) => ({ ...part, etag: index === 1 ? '' : null })),
};
let pendingPuts = 0;
let pendingCompletes = 0;
context.pendingApi = async (path, options = {}) => {
  assert.notEqual(path, '/uploads', 'A stored pending session should resume.');
  if (options.method === 'PUT') {
    pendingPuts++;
    const part = pendingSession.parts.find(p => p.part_number === Number(path.split('/').pop()));
    part.etag = `confirmed-${part.part_number}`;
    return part;
  }
  if (path.endsWith('/complete')) {
    assert.ok(pendingSession.parts.every(part => typeof part.etag === 'string' && part.etag));
    pendingCompletes++;
    return { files: [{ id: pendingSession.upload_id }] };
  }
  return pendingSession;
};
await run('sessionKey(pendingFile).then(key=>saveSession(key,"pending-session"))');
run('api=pendingApi');
assert.equal((await run('uploadChunked(pendingFile,"batch")')).id, 'pending-session');
assert.equal(pendingPuts, 3, 'All three reserved null/empty-ETag rows must be uploaded again.');
assert.equal(pendingCompletes, 1);

// A lost response with only a pending reservation must cause a retry, not false success.
context.reservedFile = new File(['reserved'], 'reserved.dat', { lastModified: 100 });
const reservedSession = {
  ...session, upload_id: 'reserved-session', filename: 'reserved.dat', size: 8, state: 'open',
  parts: [{ part_number: 1, size: 8, sha256: await run('digest(reservedFile)'), etag: null }],
};
let reservedPuts = 0;
context.reservedApi = async (path, options = {}) => {
  if (path === '/uploads') return reservedSession;
  if (options.method === 'PUT') {
    reservedPuts++;
    if (reservedPuts === 1) throw new Error('Synthetic failure before R2 acknowledgement');
    reservedSession.parts[0].etag = 'r2-final-etag';
    return reservedSession.parts[0];
  }
  if (path.endsWith('/complete')) {
    assert.equal(reservedSession.parts[0].etag, 'r2-final-etag');
    return { files: [{ id: reservedSession.upload_id }] };
  }
  return reservedSession;
};
run('api=reservedApi');
assert.equal((await run('uploadChunked(reservedFile,"batch")')).id, 'reserved-session');
assert.equal(reservedPuts, 2, 'A matching hash/size with null ETag must not reconcile as success.');

// Client normalization must match the authoritative server function exactly.
const serverAnalysis = fs.readFileSync(new URL('../site/file-analysis.js', import.meta.url), 'utf8');
const safeNameLine = serverAnalysis.split(/\r?\n/).find(line => line.startsWith('export const safeName='));
assert.ok(safeNameLine, 'The test must compare against the actual server safeName implementation.');
const serverSafeName = vm.runInNewContext(safeNameLine.replace('export const safeName=', '').replace(/;$/, ''));
const nameCases = [
  ['folder/source.dat', 'folder/report.pdf'],
  ['C:\\private\\source.dat', 'C:\\private\\photo.png'],
  ['control\u0000\u001f\u007f.dat', 'report\u0000\u001f\u007f.pdf'],
  ['s'.repeat(300) + '.dat', 'm'.repeat(600) + '.pdf'],
  ['empty.dat', '\u0000\u001f\u007f'],
];
for (const [sourceName, mappingName] of nameCases) {
  context.sourceNameCase = sourceName;
  context.mappingNameCase = mappingName;
  assert.equal(run('safeFileName(sourceNameCase)'), serverSafeName(sourceName));
  assert.equal(run('safeFileName(mappingNameCase)'), serverSafeName(mappingName));
  context.namedFile = new File(['x'], sourceName, { lastModified: 200 });
  context.nameMapping = new File([JSON.stringify({ [sourceName]: mappingName })], 'mapping.json');
  await run('importMapping(nameMapping)');
  assert.equal(run('mappedName(namedFile)'), serverSafeName(mappingName));
  const nameSession = {
    ...session, upload_id: 'normalized-name', state: 'open', filename: serverSafeName(sourceName),
    mapped_name: serverSafeName(mappingName), size: 1,
    parts: [{ part_number: 1, size: 1, sha256: await run('digest(namedFile)'), etag: 'already-stored' }],
  };
  let namedPuts = 0;
  context.nameApi = async (path, options = {}) => {
    if (path === '/uploads') {
      const body = JSON.parse(options.body);
      assert.equal(body.filename, serverSafeName(sourceName));
      assert.equal(body.mapped_name, serverSafeName(mappingName));
      return nameSession;
    }
    if (options.method === 'PUT') namedPuts++;
    return path.endsWith('/complete') ? { files: [{ id: nameSession.upload_id }] } : nameSession;
  };
  run('api=nameApi');
  assert.equal((await run('uploadChunked(namedFile,"batch")')).id, nameSession.upload_id);
  await run('sessionKey(namedFile).then(key=>saveSession(key,"normalized-name"))');
  assert.equal((await run('uploadChunked(namedFile,"batch")')).id, nameSession.upload_id);
  assert.equal(namedPuts, 0, 'Valid acknowledged parts must skip PUT on normalized-name resume.');
}

context.mapping = new File([JSON.stringify({ 'file_0.dat': 'photo.png' })], 'mapping.json');
context.invalidMapping = new File(['[]'], 'mapping.json');
context.files = Array.from({ length: 3901 }, (_, index) => new File(['x'], `file_${index}.dat`));
context.files.push(new File(['synthetic private placeholder'], 'conversations-000.json'));
await run('importMapping(mapping)');
assert.equal(run('mappedName(files[0])'), 'photo.png');
await assert.rejects(() => run('importMapping(invalidMapping)'), /JSON 对象/);
run('state.busy=false;addFiles(files,true)');
assert.equal(run('state.queue.length'), 3901);
assert.equal(elements.get('selected-files').children.length, 101);
assert.equal(run('state.queue.some(file=>file.name.endsWith(".json"))'), false);
context.sameName = [new File(['one'], 'repeat.dat', {lastModified:1}), new File(['two'], 'repeat.dat', {lastModified:1})];
run('state.queue=[];addFiles(sameName,false)');
assert.equal(run('state.queue.length'),2,'Distinct same-name/size/mtime files must remain queued.');
assert.notEqual(await run('sessionKey(sameName[0])'),await run('sessionKey(sameName[1])'));
context.identicalCopies = [new File(['same'], 'copies.dat', {lastModified:1}), new File(['same'], 'copies.dat', {lastModified:1})];
run('state.queue=[];addFiles(identicalCopies,false)');
assert.notEqual(await run('sessionKey(identicalCopies[0])'),await run('sessionKey(identicalCopies[1])'),'Repeated identical copies need independent sessions.');

// Workspaces use real response shapes, retain active upload state, and render distinct panels.
const textOf = element => [element.textContent || '', ...element.children.map(textOf)].join(' ');
const currentQueue = run('state.queue');
const currentProgress = run('state.progress');
run('state.busy=true;state.active.set(fixture,"uploading");state.progress.set(fixture,8388608)');
await run('switchWorkspace("structure",{load:false})');
assert.equal(elements.get('workspace-files').hidden, true);
assert.equal(elements.get('workspace-structure').hidden, false);
assert.equal(elements.get('tab-structure').attributes['aria-selected'], 'true');
assert.equal(elements.get('tab-files').tabIndex, -1);
assert.equal(elements.get('upload-dialog').open,false);
assert.equal(run('state.busy'), true);
assert.equal(run('state.queue'), currentQueue);
assert.equal(run('state.progress'), currentProgress);
assert.equal(run('state.progress.get(fixture)'), 8388608);
assert.equal(run('rememberedWorkspace()'), 'structure');
await run('switchWorkspace(rememberedWorkspace(),{load:false})');
assert.equal(run('state.workspace'), 'structure');

const schemaFile = { id: 'schema-file', original_name: 'observed.json', detected_type: 'json', confidence: 'heuristic', file_size: 40 };
const schemaResponse = {
  file_id: schemaFile.id, extraction_status: 'complete', review: { status: 'pending' },
  evidence: { byte_start: 0, byte_end: 40, file_bytes: 40, coverage: 'whole_file' },
  selection_criteria: [{ rule_id: 'coverage-v1', field: 'sample_bytes', operator: 'equals_file_bytes', expected: 40, result: 'pass', reason: 'Synthetic complete sample.' }],
  schema: { candidates: [{ purpose: 'Observed structure', json_schema: { type: 'object', properties: { count: { type: 'integer' }, names: { type: 'array', items: { type: 'string' } } }, required: ['count'] } }] },
};
const taxonomyData = JSON.parse(fs.readFileSync(new URL('../site/taxonomy.json', import.meta.url), 'utf8'));
const workspaceCalls = [];
context.workspaceApi = async path => {
  workspaceCalls.push(path);
  if (path === '/schemas') return { schemas: [{ detected_type: 'json', schema_fingerprint: 'sha256:synthetic', file_count: 1 }] };
  if (path.startsWith('/files?')) return { files: [schemaFile], total: 1 };
  if (path === '/files/schema-file/schemas') return schemaResponse;
  if (path === '/taxonomy') return taxonomyData;
  if (path === '/files/schema-file/analysis') return { analysis: { classification: { status: 'insufficient_evidence', topics: [], tasks: [], reason: 'Format evidence does not establish semantic labels.', label_evidence: [] }, review: { status: 'pending' }, evidence: schemaResponse.evidence } };
  throw new Error(`Unexpected workspace request ${path}`);
};
run('api=workspaceApi');
await run('loadSchemaGroups()');
assert.ok(workspaceCalls.includes('/files?schema=sha256%3Asynthetic&limit=100&offset=0'));
assert.equal(elements.get('schema-group-list').children.length, 1);
assert.match(textOf(elements.get('schema-inspector')), /count/);
assert.match(textOf(elements.get('schema-inspector')), /integer/);
assert.match(textOf(elements.get('schema-inspector')), /必填/);
assert.match(textOf(elements.get('schema-inspector')), /coverage-v1/);
assert.match(textOf(elements.get('schema-inspector')), /字节覆盖不等于正文提取/);

await run('switchWorkspace("taxonomy")');
assert.equal(elements.get('workspace-structure').hidden, true);
assert.equal(elements.get('workspace-taxonomy').hidden, false);
const ruleCount = Object.keys(taxonomyData.topics).length + Object.keys(taxonomyData.tasks).length;
assert.equal(elements.get('taxonomy-cards').children.length, ruleCount);
assert.doesNotMatch(textOf(elements.get('taxonomy-cards')), /纳入标准/,'Rules list must stay concise.');
assert.match(textOf(elements.get('taxonomy-rule-detail')), /纳入标准/);
assert.match(textOf(elements.get('taxonomy-rule-detail')), /排除标准/);
assert.match(textOf(elements.get('taxonomy-rule-detail')), /边界说明/);
const firstRuleText=textOf(elements.get('taxonomy-rule-detail'));
elements.get('taxonomy-cards').children[1].listeners.click[0]();
assert.notEqual(textOf(elements.get('taxonomy-rule-detail')),firstRuleText,'Selecting a different rule must update its detail.');

assert.match(textOf(elements.get('taxonomy-file-status')), /证据不足，尚未生成语义标签/);
assert.match(textOf(elements.get('taxonomy-file-status')), /未生成/);
assert.equal(run('state.busy'), true, 'Workspace loading must not stop an upload.');
elements.get('taxonomy-axis').value = 'tasks';
run('renderTaxonomy()');
assert.equal(elements.get('taxonomy-cards').children.length, Object.keys(taxonomyData.tasks).length);
elements.get('taxonomy-search').value = 'a definitely unmatched rule';
run('renderTaxonomy()');
assert.equal(elements.get('taxonomy-cards').children.length, 0);
assert.match(elements.get('taxonomy-status').textContent, /没有匹配/);
elements.get('taxonomy-axis').value = 'all';
elements.get('taxonomy-search').value = '';

// Older group-file responses cannot replace a newer selection.
await run('switchWorkspace("structure",{load:false})');
let releaseOld;
context.raceApi = path => {
  if (path.includes('schema=old')) return new Promise(resolve => { releaseOld = resolve; });
  if (path.includes('schema=new')) return Promise.resolve({ files: [{ ...schemaFile, id: 'new-file', original_name: 'new.json' }], total: 1 });
  if (path === '/files/new-file/schemas') return Promise.resolve(schemaResponse);
  throw new Error(`Unexpected race request ${path}`);
};
run('api=raceApi');
const oldSelection = run('selectSchemaGroup({detected_type:"json",schema_fingerprint:"old"})');
await run('selectSchemaGroup({detected_type:"json",schema_fingerprint:"new"})');
releaseOld({ files: [schemaFile], total: 1 });
await oldSelection;
assert.equal(run('state.schemaFiles[0].id'), 'new-file');
assert.match(textOf(elements.get('schema-inspector')), /new.json/);

// Keyboard navigation uses roving focus and selected-tab state.
run('api=workspaceApi');
let prevented = false;
elements.get('tab-structure').listeners.keydown[0]({ key: 'End', preventDefault() { prevented = true; } });
await new Promise(resolve => setImmediate(resolve));
assert.equal(prevented, true);
assert.equal(elements.get('tab-taxonomy').focused, true);
assert.equal(elements.get('tab-taxonomy').tabIndex, 0);
assert.equal(elements.get('workspace-taxonomy').hidden, false);
assert.equal(run('state.queue'), currentQueue);
assert.equal(run('state.progress'), currentProgress);

// The upload dialog is a persistent shell: close/reopen never loses File objects or progress.
const uploadTrigger=new Element();context.uploadTrigger=uploadTrigger;
run('openUploadDialog(uploadTrigger)');
assert.equal(elements.get('upload-dialog').open,true);
run('closeUploadDialog()');
assert.equal(elements.get('upload-dialog').open,false);
assert.equal(uploadTrigger.focused,true);
assert.equal(run('state.queue'),currentQueue);
assert.equal(run('state.progress'),currentProgress);
run('openUploadDialog(uploadTrigger)');
await run('switchWorkspace("files",{load:false})');
assert.equal(elements.get('upload-dialog').open,true,'View changes must not destroy an open upload.');
run('closeUploadDialog()');

// Main file filtering and pagination are performed by the server, not the loaded subset.
const recordCalls=[];
context.recordsApi=async path=>{recordCalls.push(path);return {files:[schemaFile],total:201};};
run('api=recordsApi;state.busy=false');
elements.get('search').value='计算 & report';elements.get('type-filter').value='json';
await run('loadRecords()');
assert.equal(recordCalls[0],'/files?limit=100&offset=0&q=%E8%AE%A1%E7%AE%97%20%26%20report&type=json');
assert.equal(run('state.total'),201);
assert.match(elements.get('showing-count').textContent,/匹配文件/);
await run('loadRecords(true)');
assert.match(recordCalls[1],/offset=1/);
assert.equal(elements.get('results-body').children[0].children.length,5,'File table should have only five essential columns.');

let releaseOldRecords;
context.recordsRace=path=>path.includes('q=old')?new Promise(resolve=>{releaseOldRecords=resolve;}):Promise.resolve({files:[{...schemaFile,id:'new',original_name:'new.json'}],total:1});
run('api=recordsRace');elements.get('search').value='old';const oldRecords=run('loadRecords()');
elements.get('search').value='new';await run('loadRecords()');releaseOldRecords({files:[schemaFile],total:1});await oldRecords;
assert.equal(run('state.records[0].id'),'new','Old search results must not overwrite a newer query.');

// Legacy Python ignores q/type: filter only the loaded records and retain unfiltered paging.
const legacyCalls=[];
const legacyRows=[{...schemaFile,id:'legacy-json',original_name:'report.json'}, {...schemaFile,id:'legacy-csv',original_name:'other.csv',detected_type:'csv'}];
context.legacyApi=async path=>{legacyCalls.push(path);return {files:path.includes('offset=2')?[{...schemaFile,id:'legacy-later',original_name:'later.json'}]:legacyRows,total:201};};
run('api=legacyApi;state.limits={max_file_bytes:1000000};state.records=[]');
elements.get('search').value='';elements.get('type-filter').value='json';
await run('loadRecords()');
assert.equal(legacyCalls[0],'/files?limit=100&offset=0');
assert.equal(run('state.records.length'),2);
assert.equal(elements.get('results-body').children.length,1,'Python response may include CSV; it must not be shown as a JSON match.');
assert.match(textOf(elements.get('results-body')),/report.json/);
assert.doesNotMatch(textOf(elements.get('results-body')),/other.csv/);
assert.match(elements.get('showing-count').textContent,/已加载 2 \/ 全部 201，当前筛选显示 1/);
assert.equal(elements.get('search').attributes['aria-label'],'搜索已加载文件名');
assert.equal(elements.get('search').placeholder,'搜索已加载文件名');
const legacyLoaded=run('state.records');
elements.get('search').value='missing';run('scheduleRecordSearch()');
assert.equal(legacyCalls.length,1,'Legacy search should render immediately without resetting loaded pages.');
assert.equal(run('state.records'),legacyLoaded);
assert.equal(elements.get('results-body').children.length,0);
assert.equal(elements.get('empty-title').textContent,'已加载文件中无匹配');
elements.get('search').value='';elements.get('type-filter').value='all';run('applyRecordTypeFilter()');
assert.equal(legacyCalls.length,1);
assert.equal(elements.get('results-body').children.length,2);
elements.get('type-filter').value='json';run('applyRecordTypeFilter()');
await run('loadRecords(true)');
assert.equal(legacyCalls[1],'/files?limit=100&offset=2','Legacy pagination offsets count every loaded row, not visible matches.');
assert.equal(run('state.records.length'),3);
assert.equal(elements.get('results-body').children.length,2);
assert.match(elements.get('showing-count').textContent,/已加载 3 \/ 全部 201，当前筛选显示 2/);
assert.equal(run('supportsServerFileFilters()'),false);
run('state.limits={resumable_uploads:true}');run('renderRecords()');
assert.equal(elements.get('search').attributes['aria-label'],'搜索全部文件名');
assert.equal(elements.get('search').placeholder,'搜索文件名');

// A loaded file opens the drawer and returns keyboard focus on close.
context.detailTrigger=new Element();
context.drawerApi=async path=>path.endsWith('/analysis')?{analysis:{...schemaResponse,classification:{status:'insufficient_evidence',topics:[],tasks:[]},schema:schemaResponse.schema}}:{...schemaFile,source_filename:'source.dat',extension:'json',mime_type:'application/json',created_at:'2026-10-22T00:00:00Z'};
run('api=drawerApi');
await run('showDetails("schema-file",detailTrigger)');
assert.equal(elements.get('detail-dialog').open,true);
assert.match(textOf(elements.get('detail-body')),/下载原件/);
assert.match(textOf(elements.get('detail-body')),/尚未分类/);
run('$("detail-dialog").close()');
assert.equal(context.detailTrigger.focused,true);

console.log('PASS: compact file table, persistent upload dialog, drawer focus, server search/pagination, selected rule details; bounded chunks, ETag retry, safe names, duplicate files, 3901-file queue; three workspace panels; schema filtering/tree/criteria; actual taxonomy rules and honest pending state; preference; keyboard focus; stale response rejection; active upload preserved.');
