"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  queue: [],
  records: [],
  total: 0,
  busy: false,
  loading: false,
  limits: null,
  detailToken: 0,
  nameMap: Object.create(null),
  pauseRequested: false,
  progress: new Map(),
  active: new Map(),
  transferTotal: 0,
  queueOccurrence: new WeakMap(),
  workspace: 'files',
  schemaGroups: [], schemaGroup: null, schemaFiles: [], schemaFilesTotal: 0,
  schemaGroupsRequest: 0, schemaFilesRequest: 0, schemaDetailRequest: 0,
  taxonomy: null, taxonomyFiles: [], taxonomyFilesTotal: 0,
  taxonomyRequest: 0, taxonomyFilesRequest: 0, taxonomyDetailRequest: 0,
};
const formatBytes = (bytes) =>
  bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 ** 2
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
const formatDate = (value) =>
  new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function message(text, error = false) {
  $("page-message").textContent = text;
  $("page-message").className =
    `notice${error ? " error" : ""}${text ? "" : " hidden"}`;
}

function connection(online) {
  $("connection").className = `connection ${online ? "online" : "offline"}`;
  $("connection-label").textContent = online
    ? "Service online"
    : "Connection unavailable";
}

async function api(path, options = {}) {
  try {
    const response = await fetch(path, {
      ...options,
      headers: { ...options.headers, ...authHeaders() },
      signal: AbortSignal.timeout(options.timeout || 90000),
    });
    const body = await response.json();
    if (!response.ok) {
      const failure = new Error(typeof body.detail === "string" ? body.detail : "请求未完成，请重试。");
      failure.status = response.status;
      throw failure;
    }
    connection(true);
    return body;
  } catch (error) {
    if (error instanceof TypeError || error.name === "TimeoutError") {
      connection(false);
      throw new Error(
        "Cannot reach the service. Check your connection and try again.",
      );
    }
    throw error;
  }
}

function authHeaders() {
  const token = $("api-token").value;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function queueError(file) {
  return state.limits?.max_file_bytes && file.size > state.limits.max_file_bytes
    ? `超过单文件 ${formatBytes(state.limits.max_file_bytes)} 上限` : null;
}
// Keep this basename normalization identical to the server's safeName contract.
function safeFileName(name) { return String(name || 'unnamed.dat').replace(/[\\/]/g,'/').split('/').pop().replace(/[\u0000-\u001f\u007f]/g,'').slice(0,255) || 'unnamed.dat'; }
function mappedName(file) { return Object.hasOwn(state.nameMap, file.name) ? safeFileName(state.nameMap[file.name]) : null; }
function acknowledgedPart(part) { return typeof part?.etag === 'string' && part.etag.trim().length > 0; }
function renderQueue() {
  const list = $("selected-files"); list.replaceChildren();
  state.queue.slice(0, 100).forEach((file, index) => {
    const item = element("li");
    const label = element("div", "queue-file", file.webkitRelativePath || file.name);
    label.append(element("span", "queue-size", formatBytes(file.size)));
    if (mappedName(file)) label.append(element("span", "queue-mapped", `原名：${mappedName(file)}`));
    if (queueError(file)) label.append(element("span", "queue-error", queueError(file)));
    const remove = element("button", "remove-file", "×"); remove.type = "button"; remove.disabled = state.busy;
    remove.setAttribute("aria-label", `移除 ${file.name}`);
    remove.addEventListener("click", () => { state.queue.splice(index, 1); renderQueue(); });
    item.append(label, remove); list.append(item);
  });
  if (state.queue.length > 100) list.append(element("li", "queue-more", `另有 ${state.queue.length - 100} 个文件；为保持流畅，仅预览前 100 个。`));
  $("selection").classList.toggle("hidden", !state.queue.length);
  const valid = state.queue.filter(f => !queueError(f));
  if (!state.busy) $("compact-upload-status").textContent = state.queue.length ? `${state.queue.length} 个文件待上传 / 续传` : '上传面板在三个工作区共用';
  $("selection-count").textContent = `已选 ${state.queue.length} 个 · ${formatBytes(valid.reduce((n,f)=>n+f.size,0))}`;
  $("upload-button").disabled = state.busy || !state.limits || !valid.length;
  $("upload-button").textContent = state.busy ? "上传中…" : "上传 / 继续 →";
  for (const id of ["clear-selection", "file-input", "directory-input", "mapping-input"]) $(id).disabled = state.busy;
  $("pause-upload").disabled = !state.busy || state.pauseRequested;
  $("pause-upload").classList.toggle("hidden", !state.busy || !state.limits?.resumable_uploads);
  $("upload-hint").textContent = state.busy ? "可暂停；当前分块完成后保存进度。" : "选择后才上传；目录默认只加入 .dat 文件。";
}
function addFiles(files, directory = false) {
  if (state.busy || !state.limits) return;
  const incoming = Array.from(files).filter(f => !directory || /\.dat$/i.test(f.name));
  const occurrences = new Map();
  for (const file of state.queue) {
    const key = JSON.stringify([file.webkitRelativePath || file.name,file.size,file.lastModified]);
    occurrences.set(key, (occurrences.get(key) || 0) + 1);
  }
  for (const file of incoming) {
    const key = JSON.stringify([file.webkitRelativePath || file.name,file.size,file.lastModified]);
    state.queueOccurrence.set(file, occurrences.get(key) || 0);
    occurrences.set(key, (occurrences.get(key) || 0) + 1);
    state.queue.push(file);
  }
  const ignored = files.length - incoming.length;
  message(directory ? `目录中加入 ${incoming.length} 个 .dat 文件，跳过 ${ignored} 个其他文件。其他格式可用“选择文件”单独添加。` : "");
  renderQueue();
}
async function importMapping(file) {
  if (file.size > 4 * 1024 * 1024) throw new Error("原名映射超过 4 MiB，请检查选中的文件。");
  const value = JSON.parse(await file.text());
  if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("映射需为 JSON 对象：完整 .dat 文件名 → 原始文件名。");
  const entries = Object.entries(value);
  if (!entries.every(([k,v]) => k && typeof v === "string" && v.trim() && v.length <= 1000)) throw new Error("映射中的名称必须是非空字符串。");
  state.nameMap = Object.fromEntries(entries);
  $("mapping-status").textContent = `已载入 ${entries.length} 条原名映射（仅在当前页面读取）。`;
  renderQueue();
}
function renderErrors(errors) {
  $("error-list").replaceChildren();
  for (const failure of errors) $("error-list").append(element("li", "", `${failure.original_name}: ${failure.error_message}`));
  $("batch-errors").classList.toggle("hidden", !errors.length);
}
function updateProgress() {
  const bytes = [...state.progress.values()].reduce((n,v)=>n+v,0);
  const percent = state.transferTotal ? Math.min(100, Math.round(bytes / state.transferTotal * 100)) : 0;
  $("progress-bar").value = percent;
  $("progress-label").textContent = `${state.pauseRequested ? "正在暂停" : "上传中"} · ${percent}% · ${formatBytes(bytes)} / ${formatBytes(state.transferTotal)}`;
  $("compact-upload-status").textContent = state.busy ? `${state.pauseRequested ? "正在暂停" : "上传中"} ${percent}% · ${state.active.size} 个文件处理中` : (state.queue.length ? `${state.queue.length} 个文件待上传 / 续传` : '上传进度在工作区切换时保留');
  $("active-uploads").replaceChildren(...[...state.active].map(([file,status]) => element("li", "", `${file.name} · ${status}`)));
}
async function digest(blob) {
  const value = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(value)].map(b => b.toString(16).padStart(2,"0")).join("");
}
const sessionStorageKey = "dat-lab-upload-sessions-v1";
function sessions() { try { const value=JSON.parse(localStorage.getItem(sessionStorageKey)||"{}");return value && typeof value==='object'&&!Array.isArray(value)?value:{}; } catch { return {}; } }
function saveSession(key, id) { try { const value=sessions();if(id)value[key]=id;else delete value[key];localStorage.setItem(sessionStorageKey,JSON.stringify(value)); } catch { /* Upload still works without persistence. */ } }
const identityHashes = new WeakMap();
async function sessionKey(file) {
  let hashes = identityHashes.get(file);
  if (!hashes) {
    hashes = [];
    const size = state.limits?.chunk_bytes || 8 * 1024 * 1024;
    if (!Number.isInteger(size) || size <= 0 || size > 16 * 1024 * 1024) throw new Error('服务器分块大小无效。');
    // Fingerprint the whole immutable File through bounded chunks, never one giant buffer.
    for (let offset=0; offset<file.size; offset+=size) hashes.push(await digest(file.slice(offset,offset+size)));
    identityHashes.set(file, hashes);
  }
  return await digest(new Blob([JSON.stringify([file.webkitRelativePath||file.name,file.size,file.lastModified,mappedName(file),state.queueOccurrence.get(file)||0,hashes])]));
}
async function retry(operation) {
  for (let attempt=0;;attempt++) {
    try { return await operation(); } catch (error) {
      if (attempt>=2 || (error.status && error.status<500 && ![408,429].includes(error.status))) throw error;
      await new Promise(resolve=>setTimeout(resolve,500*(attempt+1)));
    }
  }
}
async function uploadChunked(file, batchId) {
  const key=await sessionKey(file); let session;
  const previous=sessions()[key];
  if (previous) { try { session=await api(`/uploads/${encodeURIComponent(previous)}`); } catch(error) { if(error.status!==404)throw error;saveSession(key,null); } }
  if (!session || session.state==='aborted') {
    session=await api('/uploads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:safeFileName(file.name),size:file.size,mapped_name:mappedName(file),batch_id:batchId})});
    saveSession(key,session.upload_id);
  }
  const path=`/uploads/${encodeURIComponent(session.upload_id)}`;
  if(session.size!==file.size || session.filename!==safeFileName(file.name) || (session.mapped_name||null)!==(mappedName(file)||null)) throw new Error('已保存会话与当前文件不匹配，请清除本地续传记录后重试。');
  const chunkBytes=session.chunk_bytes;
  if(!Number.isInteger(chunkBytes)||chunkBytes<=0||chunkBytes>16*1024*1024)throw new Error('服务器分块大小无效。');
  const parts=new Map((session.parts||[]).map(p=>[p.part_number,p]));
  const total=Math.ceil(file.size/chunkBytes); let acknowledged=0;
  // Recheck every saved chunk against the reselected bytes, including middle chunks.
  for(let index=0;index<total;index++) {
    if(state.pauseRequested) return null;
    const number=index+1, blob=file.slice(index*chunkBytes,Math.min(file.size,(index+1)*chunkBytes));
    state.active.set(file,`校验 / 上传分块 ${number}/${total}`); updateProgress();
    const hash=await digest(blob), saved=parts.get(number);
    if(saved && (saved.sha256!==hash || saved.size!==blob.size)) throw new Error('续传文件内容发生变化，已停止，避免混合不同文件。请移除该会话后重新上传。');
    if(!acknowledgedPart(saved)) {
      await retry(async()=>{
        try { return await api(`${path}/parts/${number}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream','X-Chunk-SHA256':hash},body:blob}); }
        catch(error) {
          // A response can be lost after the server saved the part. Reconcile before retry.
          const current=await api(path).catch(()=>null);
          const received=current?.parts?.find(p=>p.part_number===number);
          if(acknowledgedPart(received) && received.sha256===hash && received.size===blob.size)return current;
          throw error;
        }
      });
    }
    acknowledged+=blob.size;state.progress.set(file,acknowledged);updateProgress();
  }
  if(state.pauseRequested)return null;
  state.active.set(file,'已收齐，正在校验与保存原件…');updateProgress();
  const body=await retry(()=>api(`${path}/complete`,{method:'POST',timeout:180000}));
  if(!body.files?.[0]?.id) throw new Error('保存未完成，请点击继续以查询会话。');
  saveSession(key,null);return body.files[0];
}
function sendBatch(files) {
  return (async()=>{
    const created=await api('/batches',{method:'POST'}), all=[];
    for(const file of files) {
      const body=await new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();xhr.open('POST',`/files?batch_id=${encodeURIComponent(created.batch_id)}&filename=${encodeURIComponent(file.name)}`);
        xhr.setRequestHeader('Content-Type','application/octet-stream');Object.entries(authHeaders()).forEach(([k,v])=>xhr.setRequestHeader(k,v));
        xhr.upload.onprogress=e=>{if(e.lengthComputable){state.progress.set(file,e.loaded);updateProgress();}};
        xhr.onload=()=>{try{const body=JSON.parse(xhr.responseText);xhr.status>=200&&xhr.status<300?resolve(body):reject(new Error(body.detail||'上传失败'));}catch{reject(new Error('服务器响应异常'));}};
        xhr.onerror=()=>reject(new Error('连接中断，请刷新记录后重试。'));xhr.send(file);
      });
      all.push(...body.files);
    }
    return {files:all};
  })();
}
async function upload() {
  if(state.busy||!state.limits)return;
  const valid=state.queue.filter(f=>!queueError(f));if(!valid.length)return;
  const errors=state.queue.filter(queueError).map(f=>({original_name:f.name,error_message:queueError(f)})), saved=new Set();
  state.busy=true;state.pauseRequested=false;state.progress=new Map();state.active=new Map();state.transferTotal=valid.reduce((n,f)=>n+f.size,0);
  renderQueue();renderErrors([]);message('');$('upload-progress').classList.remove('hidden');updateProgress();
  try {
    if(state.limits.resumable_uploads) {
      const created=await api('/batches',{method:'POST'});let next=0;
      await Promise.all(Array.from({length:Math.min(2,valid.length)},async()=>{
        while(next<valid.length&&!state.pauseRequested) {
          const file=valid[next++];state.active.set(file,'准备上传');updateProgress();
          try {const record=await uploadChunked(file,created.batch_id);if(record)saved.add(file);}
          catch(error){errors.push({original_name:file.name,error_message:error.message});}
          finally{state.active.delete(file);updateProgress();}
        }
      }));
    } else {
      const body=await sendBatch(valid);
      body.files.forEach((record,index)=>record.id?saved.add(valid[index]):errors.push({original_name:valid[index].name,error_message:record.error_message||'上传失败'}));
    }
    state.queue=state.queue.filter(file=>!saved.has(file));
    renderErrors(errors);message(`已保存 ${saved.size} 个文件。${state.pauseRequested?'已暂停，点击“继续”可从已确认的分块恢复。':''}${errors.length?` ${errors.length} 个文件需处理，可保留队列重试。`:''}`,!saved.size&&!!errors.length);
    await loadRecords();
    if (state.workspace === 'structure') await loadSchemaGroups();
    if (state.workspace === 'taxonomy') await loadTaxonomyFiles();
  } catch(error) {message(error.message,true);renderErrors(errors);}
  finally{state.busy=false;state.active.clear();updateProgress();renderQueue();}
}

function badge(file) {
  let group = "";
  if (["pdf", "json", "xml", "html", "sqlite"].includes(file.detected_type))
    group = "document";
  if (
    ["zip", "gzip", "bzip2", "xz", "zlib", "7z", "rar"].includes(
      file.detected_type,
    )
  )
    group = "archive";
  if (["mp3", "mp4", "wav", "flac", "ogg"].includes(file.detected_type))
    group = "media";
  if (file.confidence === "unknown") group = "unknown";
  return element(
    "span",
    `type-badge ${group}`,
    file.detected_type.toUpperCase() +
      (file.confidence === "heuristic" ? " ≈" : ""),
  );
}

function renderRecords() {
  const query = $("search").value.toLocaleLowerCase();
  const type = $("type-filter").value;
  const filtered = state.records.filter(
    (file) =>
      file.original_name.toLocaleLowerCase().includes(query) &&
      (type === "all" || type === file.detected_type),
  );
  $("results-body").replaceChildren();
  for (const file of filtered) {
    const row = element("tr");
    const name = element("td", "filename");
    name.append(element("span", "file-glyph", "DAT"));
    const label = element("span", "filename-text", file.original_name);
    label.title = file.original_name;
    name.append(label);
    row.append(name, element("td", "", formatBytes(file.file_size)));
    const detected = element("td");
    detected.append(badge(file));
    row.append(detected, element("td", "", file.encoding || "—"));
    const status = element("td");
    status.append(
      element(
        "span",
        `status${file.confidence !== "signature" ? " review" : ""}`,
        file.confidence === "signature"
          ? "✓ Matched"
          : file.confidence === "heuristic"
            ? "≈ Heuristic"
            : "! Needs review",
      ),
    );
    row.append(status, element("td", "", formatDate(file.created_at)));
    const action = element("td");
    const button = element("button", "detail-button", "Details →");
    button.type = "button";
    button.setAttribute("aria-label", `Details for ${file.original_name}`);
    button.addEventListener("click", () => showDetails(file.id));
    action.append(button);
    row.append(action);
    [
      "File",
      "Size",
      "Detected type",
      "Encoding",
      "Status",
      "Uploaded",
      "",
    ].forEach((label, index) => (row.children[index].dataset.label = label));
    $("results-body").append(row);
  }
  $("table-wrapper").classList.toggle("hidden", filtered.length === 0);
  $("empty-state").classList.toggle("hidden", filtered.length !== 0);
  $("empty-title").textContent = state.records.length
    ? "No matching records"
    : "Your inspection log starts here";
  $("empty-copy").textContent = state.records.length
    ? "Try a different filename or format filter. Filters apply to loaded records."
    : "Upload a few DAT files to see their underlying formats and metadata.";
  $("showing-count").textContent =
    `${filtered.length} shown · ${state.records.length} loaded of ${state.total} saved`;
  $("load-more").classList.toggle(
    "hidden",
    state.records.length >= state.total,
  );
}

async function loadRecords(append = false) {
  if (state.loading) return;
  state.loading = true;
  $("refresh-button").disabled = true;
  $("load-more").disabled = true;
  $("list-error").classList.add("hidden");
  try {
    const data = await api(
      `/files?limit=100&offset=${append ? state.records.length : 0}`,
    );
    state.records = append ? [...state.records, ...data.files] : data.files;
    state.total = data.total;
    $("record-count").textContent = data.total;
    $("metric-total").textContent = data.summary.total;
    $("metric-signatures").textContent = data.summary.signatures;
    $("metric-review").textContent = data.summary.review;
    const selectedType = $("type-filter").value;
    $("type-filter").replaceChildren(new Option("All formats", "all"));
    [...new Set(state.records.map((file) => file.detected_type))]
      .sort()
      .forEach((type) =>
        $("type-filter").add(new Option(type.toUpperCase(), type)),
      );
    $("type-filter").value = [...$("type-filter").options].some(
      (option) => option.value === selectedType,
    )
      ? selectedType
      : "all";
    renderRecords();
  } catch (error) {
    $("list-error").textContent =
      `${error.message} Use Refresh records to retry.`;
    $("list-error").classList.remove("hidden");
    if (!state.records.length) {
      $("empty-title").textContent = "Records couldn't be loaded";
      $("empty-copy").textContent =
        "Saved records will appear when the service is available.";
      $("showing-count").textContent = "Records unavailable";
    }
  } finally {
    state.loading = false;
    $("refresh-button").disabled = false;
    $("load-more").disabled = false;
  }
}

async function showDetails(id) {
  const token = ++state.detailToken;
  $("detail-title").textContent = "File details";
  $("detail-body").replaceChildren(
    element("p", "dialog-message", "Loading metadata…"),
  );
  $("detail-dialog").showModal();
  try {
    const file = await api(`/files/${encodeURIComponent(id)}`);
    if (token !== state.detailToken || !$("detail-dialog").open) return;
    $("detail-title").textContent = file.original_name;
    $("detail-body").replaceChildren();
    if (file.confidence !== "signature")
      $("detail-body").append(
        element(
          "p",
          "detail-notice",
          file.error_message ||
            "Text format and encoding are heuristic estimates. Review this classification before relying on it.",
        ),
      );
    else
      $("detail-body").append(
        element(
          "p",
          "detail-notice",
          "Signature match only. A recognizable header does not verify file integrity or safety.",
        ),
      );
    const list = element("dl", "detail-metadata");
    const fields = [
      ["Record ID", file.id, true],
      ["Original name", file.original_name],
      ["来源文件名", file.source_filename || file.original_name],
      ["映射原名", file.mapped_name || "无映射"],
      ["Extension", file.extension],
      [
        "File size",
        `${formatBytes(file.file_size)} (${file.file_size.toLocaleString()} bytes)`,
      ],
      ["Detected type", file.detected_type.toUpperCase()],
      ["MIME type", file.mime_type, true],
      ["Encoding", file.encoding || "Not identified"],
      ["Evidence", file.confidence],
      ["Status", file.status],
      ["Uploaded", new Date(file.created_at).toLocaleString()],
      ["Magic bytes (hex)", file.magic_bytes || "None — empty file", true],
      [
        "文本可读比例（估计）",
        `${(file.printable_ratio * 100).toFixed(1)}% of sampled bytes`,
      ],
      [
        "Sample inspected",
        `${formatBytes(file.sample_bytes)} of ${formatBytes(file.file_size)}`,
      ],
      ["SHA-256", file.sha256, true],
      ["Error / review note", file.error_message || "None"],
    ];
    const advancedMetadata=element('details','schema-details');
    advancedMetadata.append(element('summary','','高级：记录标识、哈希与文件头'));
    const technicalList=element('dl','detail-metadata');
    const primaryFields=new Set(['Original name','来源文件名','映射原名','File size','Detected type','Uploaded','Error / review note']);
    for (const [label, value, mono] of fields) {
      const row = element("div");
      row.append(
        element("dt", "", label),
        element("dd", mono ? "mono" : "", value),
      );
      (primaryFields.has(label)?list:technicalList).append(row);
    }
    advancedMetadata.append(technicalList);
    $("detail-body").append(list,advancedMetadata);
    const actions=element('div','detail-actions');
    for(const [label,suffix] of [['下载原件','/download'],['按识别格式下载','/download?name=detected'],['下载 schema 与筛选依据','/schemas?download=1']]) {
      const link=element('a','button button-secondary',label);link.href=`/files/${encodeURIComponent(id)}${suffix}`;link.setAttribute('download','');actions.append(link);
    }
    $('detail-body').append(actions);

    const reading = await api(`/files/${encodeURIComponent(id)}/analysis`);
    if (token !== state.detailToken || !$("detail-dialog").open) return;
    const analysis = reading.analysis;
    $("detail-body").append(element("p", "detail-notice",
      analysis ? `识别格式：${analysis.format || "unknown"}。以下展示文件的实际提取范围与规则结果。`
        : `读取状态：${reading.job.state}。可重新打开文件详情查看进度。`));
    if (analysis) {
      $('detail-body').append(element('p','detail-notice',`提取状态：${analysis.extraction_status||analysis.status} · 主题分类：${analysis.classification?.status||'待复核'}。格式分组不等于语义主题分类。${analysis.extraction_note||''}`));
      const rules=analysis.selection_criteria||analysis.schema?.candidates?.[0]?.selection_criteria||[];
      if(rules.length){
        const section=element('section','criteria-section');section.append(element('h3','','本文件的筛选标准'));
        for(const rule of rules){const item=element('div','criterion');item.append(element('strong','',`${rule.rule_id} · ${rule.result}`),element('p','',`${rule.field} ${rule.operator} ${JSON.stringify(rule.expected)}`),element('p','',rule.reason||''));section.append(item);}
        $('detail-body').append(section);
      }
      const details = element("details", "schema-details");
      details.append(element("summary", "", "查看生成的 schema、样本和证据 JSON"));
      details.append(element("pre", "schema-json", JSON.stringify(analysis, null, 2)));
      $("detail-body").append(details);
    }
  } catch (error) {
    if (token === state.detailToken)
      $("detail-body").replaceChildren(
        element("p", "dialog-message", error.message),
      );
  }
}


const workspaceModes = ['files', 'structure', 'taxonomy'];
const workspacePreferenceKey = 'dat-lab-workspace-v1';
function rememberedWorkspace() {
  try { const mode=localStorage.getItem(workspacePreferenceKey); return workspaceModes.includes(mode)?mode:'files'; }
  catch { return 'files'; }
}
async function switchWorkspace(mode, {load=true, focus=false}={}) {
  if(!workspaceModes.includes(mode)) mode='files';
  state.workspace=mode;
  $('shared-intake').open=mode==='files';
  for(const value of workspaceModes) {
    const selected=value===mode, tab=$(`tab-${value}`), panel=$(`workspace-${value}`);
    tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;panel.hidden=!selected;
  }
  try {localStorage.setItem(workspacePreferenceKey,mode);}catch{}
  if(focus)$(`tab-${mode}`).focus();
  // Upload state, File objects, in-flight requests, and progress never reset here.
  if(!load)return;
  if(mode==='structure')await loadSchemaGroups();
  if(mode==='taxonomy')await Promise.all([loadTaxonomy(),loadTaxonomyFiles()]);
}
function wireWorkspaceTabs() {
  for(const mode of workspaceModes) {
    const tab=$(`tab-${mode}`);
    tab.addEventListener('click',()=>switchWorkspace(mode));
    tab.addEventListener('keydown',event=>{
      const index=workspaceModes.indexOf(mode);let next;
      if(event.key==='ArrowRight')next=workspaceModes[(index+1)%workspaceModes.length];
      if(event.key==='ArrowLeft')next=workspaceModes[(index+workspaceModes.length-1)%workspaceModes.length];
      if(event.key==='Home')next=workspaceModes[0];
      if(event.key==='End')next=workspaceModes[workspaceModes.length-1];
      if(next){event.preventDefault();switchWorkspace(next,{focus:true});}
    });
  }
}
function groupKey(group) {return group.schema_fingerprint || `format:${group.detected_type}`;}
function renderSchemaGroups() {
  const query=$('schema-group-search').value.toLocaleLowerCase();
  const groups=state.schemaGroups.filter(group=>`${group.detected_type} ${group.schema_fingerprint||''}`.toLocaleLowerCase().includes(query));
  $('schema-group-list').replaceChildren();
  for(const group of groups) {
    const button=element('button','schema-group');button.type='button';
    const selected=state.schemaGroup && groupKey(group)===groupKey(state.schemaGroup);
    button.setAttribute('aria-pressed',String(!!selected));
    button.append(element('strong','',String(group.detected_type||'unknown').toUpperCase()),element('span','',`${group.file_count} 个文件`),element('small','mono',group.schema_fingerprint?group.schema_fingerprint.slice(0,20)+'…':'旧记录 · 按格式查看'));
    button.addEventListener('click',()=>selectSchemaGroup(group));$('schema-group-list').append(button);
  }
  if(!groups.length && state.schemaGroups.length)$('schema-group-list').append(element('p','workspace-status','没有匹配的结构分组，请更换关键词。'));
}
async function loadSchemaGroups() {
  const token=++state.schemaGroupsRequest;
  $('schema-groups-status').textContent='正在读取真实文件的结构分组…';$('refresh-schemas').disabled=true;
  try {
    const data=await api('/schemas');
    if(token!==state.schemaGroupsRequest||state.workspace!=='structure')return;
    state.schemaGroups=Array.isArray(data.schemas)?data.schemas:[];
    $('schema-group-count').textContent=`${state.schemaGroups.length} 组`;
    $('schema-groups-status').textContent=state.schemaGroups.length?'按生成的 Schema 指纹分组；选择一组查看文件。':'尚无已保存文件。上传文件后再刷新。';
    const previous=state.schemaGroup;
    const selected=state.schemaGroups.find(group=>previous&&groupKey(group)===groupKey(previous))||state.schemaGroups[0];
    state.schemaGroup=null;renderSchemaGroups();
    if(selected)await selectSchemaGroup(selected);
    else {state.schemaFiles=[];$('schema-file-list').replaceChildren();$('schema-files-count').textContent='0';$('schema-files-status').textContent='还没有可关联的文件。';$('schema-inspector').replaceChildren(element('p','workspace-status','上传文件后可查看结构与筛选依据。'));}
  } catch(error) {
    if(token===state.schemaGroupsRequest&&state.workspace==='structure')$('schema-groups-status').textContent=`加载失败：${error.message} 可点击“刷新 Schema”重试。`;
  } finally {if(token===state.schemaGroupsRequest)$('refresh-schemas').disabled=false;}
}
async function selectSchemaGroup(group) {
  state.schemaGroup=group;state.schemaFiles=[];state.schemaFilesTotal=0;++state.schemaDetailRequest;
  renderSchemaGroups();$('schema-file-list').replaceChildren();$('schema-files-more').classList.add('hidden');
  $('schema-inspector').replaceChildren(element('p','workspace-status','正在加载关联文件…'));
  await loadSchemaFiles();
}
async function loadSchemaFiles(append=false) {
  const group=state.schemaGroup;if(!group)return;
  const token=++state.schemaFilesRequest, key=groupKey(group);
  $('schema-files-status').textContent='正在筛选关联文件…';$('schema-files-more').disabled=true;
  try {
    const filter=group.schema_fingerprint?`schema=${encodeURIComponent(group.schema_fingerprint)}`:`type=${encodeURIComponent(group.detected_type)}`;
    const data=await api(`/files?${filter}&limit=100&offset=${append?state.schemaFiles.length:0}`);
    if(token!==state.schemaFilesRequest||state.workspace!=='structure'||groupKey(state.schemaGroup)!==key)return;
    state.schemaFiles=append?[...state.schemaFiles,...data.files]:data.files;state.schemaFilesTotal=data.total;
    $('schema-files-count').textContent=`${data.total} 个文件`;
    $('schema-files-status').textContent=`已加载 ${state.schemaFiles.length} / ${data.total} 个。${group.schema_fingerprint?'以下文件共享相同的结构指纹。':'旧记录没有结构指纹，此处按格式分组。'}`;
    renderSchemaFiles();
    if(!append&&state.schemaFiles.length)await inspectSchemaFile(state.schemaFiles[0]);
    if(!state.schemaFiles.length)$('schema-inspector').replaceChildren(element('p','workspace-status','这个分组暂时没有关联文件。请刷新分组。'));
  }catch(error){if(token===state.schemaFilesRequest&&state.workspace==='structure')$('schema-files-status').textContent=`加载失败：${error.message} 重新选择分组可重试。`;}
  finally {if(token===state.schemaFilesRequest)$('schema-files-more').disabled=false;}
}
function renderSchemaFiles(selectedId) {
  $('schema-file-list').replaceChildren();
  for(const file of state.schemaFiles) {
    const button=element('button','schema-file');button.type='button';button.setAttribute('aria-pressed',String(file.id===selectedId));
    button.append(element('span','',file.original_name),element('small','',`${formatBytes(file.file_size)} · ${file.detected_type}`));
    button.addEventListener('click',()=>inspectSchemaFile(file));$('schema-file-list').append(button);
  }
  $('schema-files-more').classList.toggle('hidden',state.schemaFiles.length>=state.schemaFilesTotal);
}
function schemaType(schema) {
  if(typeof schema==='boolean')return schema?'任意值':'不允许任何值';
  if(Array.isArray(schema?.type))return schema.type.join(' | ');
  return schema?.type || (schema?.$ref?'引用':Object.hasOwn(schema||{},'const')?'固定值':schema?.enum?'枚举':'未限定');
}
function schemaTree(schema) {
  let count=0;
  function entry(value,name,required,depth) {
    const item=element('li'), row=element('div','field-row');
    row.append(element('span','field-name',name),element('span','field-type',schemaType(value)));
    if(required)row.append(element('span','field-required','必填'));
    if(value && Object.hasOwn(value,'const'))row.append(element('span','field-constant',String(value.const)));
    item.append(row);count++;
    if(value?.description)item.append(element('p','field-description',value.description));
    if(depth<8 && count<250) {
      const children=element('ul');
      for(const [key,child] of Object.entries(value?.properties||{})) {if(count>=250)break;children.append(entry(child,key,(value.required||[]).includes(key),depth+1));}
      if(value?.items && !Array.isArray(value.items) && count<250)children.append(entry(value.items,'[] 数组元素',false,depth+1));
      if(children.children.length)item.append(children);
    }
    return item;
  }
  const tree=element('ul','field-tree');tree.append(entry(schema,'根节点',false,0));
  if(count>=250)tree.append(element('li','workspace-status','字段过多，仅展开前 250 项；完整契约可下载查看。'));
  return tree;
}
function criteriaView(rules) {
  const section=element('section','inspector-section');section.append(element('h4','','筛选标准与结果'));
  if(!rules?.length){section.append(element('p','workspace-status','此记录尚未提供筛选标准。'));return section;}
  for(const rule of rules) {
    const item=element('div','criterion'), title=element('div','criterion-heading');
    const label={pass:'符合',fail:'不符合',unknown:'待确认'}[rule.result]||rule.result||'未记录';
    title.append(element('strong','',rule.rule_id||'未命名规则'),element('span',`rule-result ${rule.result||''}`,label));
    item.append(title,element('p','',`${rule.field||''} · ${rule.operator||''} · ${JSON.stringify(rule.expected)}`),element('p','',rule.reason||''));
    if(rule.evidence)item.append(element('small','',evidenceText(rule.evidence)));
    section.append(item);
  }
  return section;
}
function evidenceText(evidence) {
  if(!evidence)return '未提供证据覆盖范围。';
  const coverage=evidence.coverage==='whole_file'?'字节样本覆盖全文件':evidence.coverage==='prefix_only'?'仅文件前缀':'覆盖范围未声明';
  return `${coverage} · 字节 ${evidence.byte_start??0}–${evidence.byte_end??'?'} / ${evidence.file_bytes??'?'}；字节覆盖不等于正文提取。`;
}
async function inspectSchemaFile(file) {
  const token=++state.schemaDetailRequest;renderSchemaFiles(file.id);
  $('schema-inspector').replaceChildren(element('p','workspace-status','正在读取该文件的 Schema 与证据…'));
  try {
    const data=await api(`/files/${encodeURIComponent(file.id)}/schemas`);
    if(token!==state.schemaDetailRequest||state.workspace!=='structure')return;
    const section=$('schema-inspector');section.replaceChildren();
    const heading=element('div','panel-heading');heading.append(element('h3','',file.original_name),badge(file));section.append(heading);
    const status=element('p','workspace-status',`提取状态：${data.extraction_status||'未记录'} · 审核：${data.review?.status||'未记录'}`);
    section.append(status,element('p','coverage-note',evidenceText(data.evidence)));
    const candidate=data.schema?.candidates?.[0];
    if(candidate) {
      const metadataOnly=/metadata|元数据/i.test(`${candidate.purpose||''} ${candidate.json_schema?.description||''}`);
      section.append(element('p',metadataOnly?'metadata-notice':'schema-purpose',metadataOnly?'此处是文件元数据结构。PDF、图片与压缩包正文尚未提取，未执行 OCR 或解压。':candidate.purpose||'基于已检查样本推断，待复核。'));
      const fields=element('section','inspector-section');fields.append(element('h4','','字段与类型'),schemaTree(candidate.json_schema));section.append(fields);
    }else section.append(element('p','workspace-status','当前文件没有可显示的结构 Schema。'));
    section.append(criteriaView(data.selection_criteria||candidate?.selection_criteria));
    const actions=element('div','inspector-actions');
    for(const [label,suffix] of [['下载 Schema 与依据','/schemas?download=1'],['下载原件','/download'],['查看完整文件详情',null]]) {
      if(suffix){const link=element('a','button button-secondary',label);link.href=`/files/${encodeURIComponent(file.id)}${suffix}`;link.setAttribute('download','');actions.append(link);}
      else{const button=element('button','text-button',label);button.type='button';button.addEventListener('click',()=>showDetails(file.id));actions.append(button);}
    }
    const advanced=element('details','advanced-data');advanced.append(element('summary','','高级：原始 Schema 与证据 JSON'),element('pre','schema-json',JSON.stringify(data,null,2)));
    section.append(actions,advanced);
  }catch(error){if(token===state.schemaDetailRequest&&state.workspace==='structure')$('schema-inspector').replaceChildren(element('p','workspace-status',`读取失败：${error.message} 请再次选择此文件。`));}
}
function ruleText(value) {return typeof value==='string'?value:value==null?'未提供':JSON.stringify(value);}
function renderTaxonomy() {
  const taxonomy=state.taxonomy;if(!taxonomy)return;
  const query=$('taxonomy-search').value.toLocaleLowerCase(), axis=$('taxonomy-axis').value;
  const entries=['topics','tasks'].flatMap(kind=>Object.entries(taxonomy[kind]||{}).map(([code,rule])=>({kind,code,rule})));
  const visible=entries.filter(({kind,code,rule})=>(axis==='all'||axis===kind)&&`${code} ${JSON.stringify(rule)}`.toLocaleLowerCase().includes(query));
  $('taxonomy-cards').replaceChildren();
  for(const {kind,code,rule} of visible) {
    const card=element('article','card taxonomy-card');const header=element('div','taxonomy-card-heading');
    header.append(element('span','taxonomy-axis-tag',kind==='topics'?'主题':'任务'),element('span','mono taxonomy-code',code));
    card.append(header,element('h3','',rule.label_zh||code));
    const list=element('dl');
    for(const [title,key] of [['纳入标准','include'],['排除标准','exclude'],['边界说明','boundary']]) {const row=element('div');row.append(element('dt','',title),element('dd','',ruleText(rule[key])));list.append(row);}
    card.append(list);$('taxonomy-cards').append(card);
  }
  $('taxonomy-status').textContent=visible.length?`显示 ${visible.length} / ${entries.length} 条真实词表规则。规则展示不代表已对文件完成语义分类。`:'没有匹配的规则，请更换关键词或规则范围。';
}
async function loadTaxonomy() {
  const token=++state.taxonomyRequest;$('taxonomy-status').textContent='正在读取设计 Schema 的分类词表…';$('refresh-taxonomy').disabled=true;
  try {
    const data=await api('/taxonomy');if(token!==state.taxonomyRequest||state.workspace!=='taxonomy')return;
    state.taxonomy=data;$('taxonomy-version').textContent=`v${data.reference?.version||'未声明'}`;renderTaxonomy();
  }catch(error){if(token===state.taxonomyRequest&&state.workspace==='taxonomy'){$('taxonomy-cards').replaceChildren();$('taxonomy-status').textContent=`规则读取失败：${error.message} 可刷新重试。`;}}
  finally{if(token===state.taxonomyRequest)$('refresh-taxonomy').disabled=false;}
}
async function loadTaxonomyFiles(append=false) {
  const token=++state.taxonomyFilesRequest;$('taxonomy-files-status').textContent='正在读取已保存文件…';$('taxonomy-files-more').disabled=true;
  try {
    const data=await api(`/files?limit=100&offset=${append?state.taxonomyFiles.length:0}`);if(token!==state.taxonomyFilesRequest||state.workspace!=='taxonomy')return;
    const selected=$('taxonomy-file-select').value;
    state.taxonomyFiles=append?[...state.taxonomyFiles,...data.files]:data.files;state.taxonomyFilesTotal=data.total;
    const select=$('taxonomy-file-select');select.replaceChildren();
    for(const file of state.taxonomyFiles){const option=element('option','',file.original_name);option.value=file.id;select.append(option);}
    $('taxonomy-files-count').textContent=`${data.total} 个文件`;$('taxonomy-files-status').textContent=data.total?`已加载 ${state.taxonomyFiles.length} / ${data.total} 个文件。选择一个文件读取其真实分类状态。`:'还没有已保存文件，上传后可查看实际分类状态。';
    $('taxonomy-files-more').classList.toggle('hidden',state.taxonomyFiles.length>=data.total);
    const file=state.taxonomyFiles.find(item=>item.id===selected)||state.taxonomyFiles[0];
    if(file){select.value=file.id;await inspectTaxonomyFile(file.id);}else{const option=element('option','','暂无文件');option.value='';select.append(option);$('taxonomy-file-status').replaceChildren();}
  }catch(error){if(token===state.taxonomyFilesRequest&&state.workspace==='taxonomy')$('taxonomy-files-status').textContent=`文件列表读取失败：${error.message} 可刷新重试。`;}
  finally{if(token===state.taxonomyFilesRequest)$('taxonomy-files-more').disabled=false;}
}
function renderTaxonomyFile(file,analysis) {
  const target=$('taxonomy-file-status');target.replaceChildren();
  const classification=analysis?.classification, review=analysis?.review;
  const stateLabel={insufficient_evidence:'证据不足，尚未生成语义标签',classified:'已有分类候选，需按审核状态复核',no_user_text:'没有可用于此契约的用户文本'}[classification?.status]||'尚未提供语义分类状态';
  const heading=element('div','classification-state');heading.append(element('strong','',stateLabel),element('span','',`审核：${review?.status||'未记录'}`));target.append(heading);
  target.append(element('p','',classification?.reason||'此记录没有提供分类理由。'));
  const labels=element('dl','classification-labels');
  for(const [name,key] of [['主题','topics'],['任务','tasks']]) {
    const actual=classification?.[key]||[];
    const text=actual.length?actual.map(item=>{const code=typeof item==='string'?item:item.code||JSON.stringify(item);return state.taxonomy?.[key]?.[code]?.label_zh?`${state.taxonomy[key][code].label_zh} (${code})`:code;}).join('、'):'未生成';
    const row=element('div');row.append(element('dt','',name),element('dd','',text));labels.append(row);
  }
  target.append(labels,element('p','coverage-note',evidenceText(analysis?.evidence)),element('p','taxonomy-status-note','此处展示文件分析接口实际返回的状态。格式识别与结构 Schema 不会自动授予主题、任务、训练或知识库资格。'));
  const evidence=classification?.label_evidence||[];
  target.append(element('p','',evidence.length?`当前提供 ${evidence.length} 条标签证据映射；可在文件详情查看。`:'当前没有标签证据映射，保持待复核。'));
  const button=element('button','text-button','查看该文件的来源与分析详情');button.type='button';button.addEventListener('click',()=>showDetails(file.id));target.append(button);
}
async function inspectTaxonomyFile(id) {
  const token=++state.taxonomyDetailRequest, file=state.taxonomyFiles.find(item=>item.id===id);if(!file)return;
  $('taxonomy-file-status').replaceChildren(element('p','workspace-status','正在读取真实分类状态…'));
  try {
    const data=await api(`/files/${encodeURIComponent(id)}/analysis`);if(token!==state.taxonomyDetailRequest||state.workspace!=='taxonomy')return;
    renderTaxonomyFile(file,data.analysis);
  }catch(error){if(token===state.taxonomyDetailRequest&&state.workspace==='taxonomy')$('taxonomy-file-status').replaceChildren(element('p','workspace-status',`读取失败：${error.message} 再次选择文件可重试。`));}
}
wireWorkspaceTabs();
$('refresh-schemas').addEventListener('click',loadSchemaGroups);
$('schema-group-search').addEventListener('input',renderSchemaGroups);
$('schema-files-more').addEventListener('click',()=>loadSchemaFiles(true));
$('refresh-taxonomy').addEventListener('click',()=>Promise.all([loadTaxonomy(),loadTaxonomyFiles()]));
$('taxonomy-search').addEventListener('input',renderTaxonomy);
$('taxonomy-axis').addEventListener('change',renderTaxonomy);
$('taxonomy-file-select').addEventListener('change',event=>inspectTaxonomyFile(event.target.value));
$('taxonomy-files-more').addEventListener('click',()=>loadTaxonomyFiles(true));

$('directory-input').addEventListener('change', async event=>{
  const files=Array.from(event.target.files);
  const mapping=files.find(file=>file.name==='conversation_asset_file_names.json');
  if(mapping){try{await importMapping(mapping);}catch(error){message(error.message,true);return;}}
  addFiles(files,true);event.target.value='';
});
$('mapping-input').addEventListener('change',async event=>{try{if(event.target.files[0])await importMapping(event.target.files[0]);}catch(error){message(error.message,true);}finally{event.target.value='';}});
$('pause-upload').addEventListener('click',()=>{state.pauseRequested=true;renderQueue();updateProgress();});
$('forget-sessions').addEventListener('click',()=>{if(state.busy)return;try{localStorage.removeItem(sessionStorageKey);}catch{}message('已清除本机续传记录；服务器上的未完成会话仍保留。已保存原件不受影响。');});
$("file-input").addEventListener("change", (event) => {
  addFiles(event.target.files);
  event.target.value = "";
});
for (const event of ["dragenter", "dragover"])
  $("drop-zone").addEventListener(event, (e) => {
    e.preventDefault();
    if (!state.busy) $("drop-zone").classList.add("dragging");
  });
for (const event of ["dragleave", "drop"])
  $("drop-zone").addEventListener(event, (e) => {
    e.preventDefault();
    $("drop-zone").classList.remove("dragging");
  });
$("drop-zone").addEventListener("drop", (event) =>
  addFiles(event.dataTransfer.files),
);
// Dropping outside the target must not navigate away to an untrusted local file.
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => event.preventDefault());
$("clear-selection").addEventListener("click", () => {
  if (!state.busy) {
    state.queue = [];
    renderQueue();
  }
});
$("upload-button").addEventListener("click", upload);
$("refresh-button").addEventListener("click", () => loadRecords());
$("load-more").addEventListener("click", () => loadRecords(true));
$("search").addEventListener("input", renderRecords);
$("type-filter").addEventListener("change", renderRecords);
$("dismiss-errors").addEventListener("click", () => renderErrors([]));
$("close-dialog").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("click", (event) => {
  if (event.target === $("detail-dialog")) {
    const rect = event.target.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      event.target.close();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (state.busy) {
    event.preventDefault();
    event.returnValue = "";
  }
});

async function initialize() {
  const workspaceLoad=switchWorkspace(rememberedWorkspace());
  await Promise.all([
    workspaceLoad,
    loadRecords(),
    (async () => {
      try {
        state.limits = await api("/config");
        $("upload-limits").textContent =
          `不限制文件数量 · 单文件 ${state.limits.max_file_bytes ? formatBytes(state.limits.max_file_bytes) : "流式上传"}${state.limits.resumable_uploads ? " · 8 MiB 分块 · 最多 2 个文件并行" : ""}`;
        $("authentication").classList.toggle("hidden", !state.limits.authentication_required);
        renderQueue();
      } catch (error) {
        $("upload-limits").textContent = "Upload settings unavailable";
        message(`${error.message} Reload the page to enable uploads.`, true);
      }
    })(),
    api("/health").catch(() => {}),
  ]);
}
initialize();
