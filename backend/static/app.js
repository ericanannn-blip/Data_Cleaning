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
  recordsToken: 0,
  category: "all",
  schemas: [],
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
      signal: AbortSignal.timeout(15000),
    });
    const body = await response.json();
    if (!response.ok)
      throw new Error(
        typeof body.detail === "string"
          ? body.detail
          : "The request could not be completed.",
      );
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

function queueError(file) {
  if (state.limits && file.size > state.limits.max_file_bytes)
    return `Exceeds the ${formatBytes(state.limits.max_file_bytes)} file limit.`;
  return null;
}

function renderQueue() {
  $("selected-files").replaceChildren();
  for (const [index, file] of state.queue.entries()) {
    const item = element("li");
    item.append(element("span", "file-glyph", file.name.split(".").pop().slice(0, 5).toUpperCase()));
    const name = element("div", "queue-file", file.name);
    name.append(element("span", "queue-size", formatBytes(file.size)));
    const error = queueError(file);
    if (error) name.append(element("span", "queue-error", error));
    item.append(name);
    const remove = element("button", "remove-file", "×");
    remove.type = "button";
    remove.disabled = state.busy;
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.addEventListener("click", () => {
      state.queue.splice(index, 1);
      renderQueue();
    });
    item.append(remove);
    $("selected-files").append(item);
  }
  $("selection").classList.toggle("hidden", state.queue.length === 0);
  $("selection-count").textContent =
    `${state.queue.length} file${state.queue.length === 1 ? "" : "s"} selected`;
  const valid = state.queue.filter((file) => !queueError(file));
  $("upload-button").disabled =
    state.busy || !state.limits || valid.length === 0;
  $("upload-button").textContent = state.busy
    ? "Processing…"
    : "Upload & inspect";
  $("clear-selection").disabled = state.busy;
  $("file-input").disabled = state.busy;
  $("upload-hint").textContent = state.busy
    ? "Inspecting files. Please keep this page open."
    : valid.length
      ? `${valid.length} file${valid.length === 1 ? "" : "s"} ready to inspect.`
      : "Select files to start inspecting.";
}

function addFiles(files) {
  if (state.busy || !state.limits) return;
  const incoming = Array.from(files);
  if (incoming.length + state.queue.length > state.limits.max_files) {
    message(
      `Select up to ${state.limits.max_files} files per batch. Remove files before adding more.`,
      true,
    );
    return;
  }
  const combined = [...state.queue, ...incoming];
  const bytes = combined
    .filter((file) => !queueError(file))
    .reduce((total, file) => total + file.size, 0);
  // Reserve multipart headers/boundaries within the server's request limit.
  if (bytes + combined.length * 2048 + 1024 > state.limits.max_request_bytes) {
    message(
      `This batch exceeds the ${formatBytes(state.limits.max_request_bytes)} request limit. Upload fewer files at a time.`,
      true,
    );
    return;
  }
  state.queue = combined;
  message("");
  renderQueue();
}

function renderErrors(errors) {
  $("error-list").replaceChildren();
  for (const error of errors)
    $("error-list").append(
      element("li", "", `${error.original_name}: ${error.error_message}`),
    );
  $("batch-errors").classList.toggle("hidden", errors.length === 0);
}

function sendBatch(files) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file, file.name));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/files");
    xhr.timeout = 120000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 100);
        $("progress-bar").value = percent;
        $("progress-label").textContent =
          percent === 100
            ? "Upload received. Inspecting and saving…"
            : `Uploading… ${percent}%`;
      }
    };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status < 200 || xhr.status >= 300)
          return reject(
            new Error(
              typeof body.detail === "string"
                ? body.detail
                : "Upload rejected. Check the selected files.",
            ),
          );
        connection(true);
        resolve(body);
      } catch (_) {
        reject(
          new Error(
            "The server returned an unexpected response. Refresh records before retrying.",
          ),
        );
      }
    };
    xhr.onerror = xhr.ontimeout = () => {
      connection(false);
      reject(
        new Error(
          "The upload connection was interrupted. Some files may have been saved. Refresh records before retrying.",
        ),
      );
    };
    xhr.send(form);
  });
}

async function upload() {
  if (state.busy || !state.limits) return;
  const valid = state.queue.filter((file) => !queueError(file));
  if (!valid.length) return;
  const localErrors = state.queue
    .filter(queueError)
    .map((file) => ({
      original_name: file.name,
      error_message: queueError(file),
    }));
  state.busy = true;
  renderQueue();
  message("");
  renderErrors([]);
  $("upload-progress").classList.remove("hidden");
  $("progress-bar").value = 0;
  $("progress-label").textContent = "Uploading…";
  try {
    const body = await sendBatch(valid);
    const errors = [
      ...localErrors,
      ...body.files.filter((file) => file.status === "error"),
    ];
    renderErrors(errors);
    state.queue = state.queue.filter(
      (file) =>
        queueError(file) ||
        body.files.some(
          (result) =>
            result.status === "error" && result.original_name === file.name,
        ),
    );
    const review = body.files.filter(
      (file) => file.id && file.confidence !== "signature",
    ).length;
    message(
      `${body.succeeded} file${body.succeeded === 1 ? "" : "s"} saved.${review ? ` ${review} classified with uncertainty; review the details.` : ""}${errors.length ? ` ${errors.length} file${errors.length === 1 ? "" : "s"} could not be uploaded.` : ""}`,
      body.succeeded === 0,
    );
    await loadRecords();
    await loadSchemas();
  } catch (error) {
    message(error.message, true);
    renderErrors(localErrors);
  } finally {
    state.busy = false;
    $("upload-progress").classList.add("hidden");
    renderQueue();
  }
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
  const filtered = state.records;
  $("results-body").replaceChildren();
  for (const file of filtered) {
    const row = element("tr");
    const name = element("td", "filename");
    name.append(element("span", "file-glyph", file.detected_type.slice(0, 5).toUpperCase()));
    const label = element("span", "filename-text", file.original_name);
    label.title = file.original_name;
    name.append(label);
    if (file.duplicate_files) name.append(element("small", "duplicate-tag", "Duplicate bytes"));
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
    const button = element("button", "detail-button", "Read & inspect");
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
  $("empty-title").textContent = state.total || $("search").value || state.category !== "all" || $("type-filter").value !== "all"
    ? "No matching records"
    : "Your inspection log starts here";
  $("empty-copy").textContent = $("empty-title").textContent === "No matching records"
    ? "Try a different filename, category, or format. Filters search all saved records."
    : "Upload PDF, images, ZIP, structured text, or DAT files to begin.";
  $("showing-count").textContent =
    `${filtered.length} loaded of ${state.total} matching records`;
  $("load-more").classList.toggle(
    "hidden",
    state.records.length >= state.total,
  );
}

async function loadRecords(append = false) {
  if (append && state.loading) return;
  const token = ++state.recordsToken;
  state.loading = true;
  $("refresh-button").disabled = true;
  $("load-more").disabled = true;
  $("list-error").classList.add("hidden");
  try {
    const params = new URLSearchParams({limit: "100", offset: String(append ? state.records.length : 0), q: $("search").value});
    if (state.category !== "all") params.set("category", state.category);
    if ($("type-filter").value !== "all") params.set("detected_type", $("type-filter").value);
    const data = await api(`/files?${params}`);
    if (token !== state.recordsToken) return;
    state.records = append ? [...state.records, ...data.files] : data.files;
    state.total = data.total;
    $("record-count").textContent = data.summary.total;
    $("metric-total").textContent = data.summary.total;
    $("metric-signatures").textContent = data.summary.signatures;
    $("metric-review").textContent = data.summary.review;
    const selectedType = $("type-filter").value;
    $("type-filter").replaceChildren(new Option("All formats", "all"));
    data.formats
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
    renderCategories(data.categories, data.summary.total);
  } catch (error) {
    if (token !== state.recordsToken) return;
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
    if (token !== state.recordsToken) return;
    state.loading = false;
    $("refresh-button").disabled = false;
    $("load-more").disabled = false;
  }
}

function action(label, handler, className = "button button-secondary") {
  const button = element("button", className, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function renderCategories(counts, total) {
  const categories = [["all", "All files"], ["table", "Tables & JSON"], ["document", "Documents"],
    ["image", "Images"], ["archive", "Archives"], ["text", "Text"], ["audio", "Audio"],
    ["video", "Video"], ["database", "Databases"], ["unknown", "Unrecognized"]];
  $("category-nav").replaceChildren();
  for (const [key, label] of categories) {
    const button = action(`${label} ${key === "all" ? total : counts[key] || 0}`, () => {
      state.category = key;
      renderCategories(counts, total);
      loadRecords();
    }, `category-button${state.category === key ? " active" : ""}`);
    button.setAttribute("aria-pressed", String(state.category === key));
    $("category-nav").append(button);
  }
}

function sampleTable(profile) {
  const wrap = element("div", "sample-table-wrap");
  if (!profile.rows.length) {
    wrap.append(element("p", "", "No readable records in this sample."));
    return wrap;
  }
  const columns = [...new Set(profile.rows.flatMap((row) => Object.keys(row)))];
  const table = element("table", "sample-table");
  const header = element("tr");
  for (const key of columns) header.append(element("th", "", key));
  const head = element("thead"); head.append(header); table.append(head);
  const body = element("tbody");
  for (const row of profile.rows) {
    const line = element("tr");
    for (const key of columns) {
      const text = !(key in row) ? "(missing)" : row[key] === null ? "null" :
        typeof row[key] === "object" ? JSON.stringify(row[key]) : String(row[key]);
      const cell = element("td", "", text.slice(0, 500));
      cell.title = text.slice(0, 1000);
      line.append(cell);
    }
    body.append(line);
  }
  table.append(body); wrap.append(table);
  return wrap;
}

function schemaFields(fields, statistics = false) {
  const wrap = element("div", "sample-table-wrap");
  const table = element("table", "sample-table field-table");
  const head = element("thead"), row = element("tr");
  for (const label of ["Original path", "Proposed name", "Inferred types", statistics ? "Missing / null" : "Nullable"])
    row.append(element("th", "", label));
  head.append(row); table.append(head);
  const body = element("tbody");
  for (const field of fields) {
    const line = element("tr");
    const types = Array.isArray(field.types) ? field.types : Object.keys(field.types);
    for (const value of [field.path, field.normalized_name, types.join(" | "),
      statistics ? `${field.missing_count + field.null_count} · ${(field.null_ratio * 100).toFixed(1)}%` : field.nullable ? "Yes" : "Not observed"])
      line.append(element("td", "", value));
    body.append(line);
  }
  table.append(body); wrap.append(table);
  return wrap;
}

function samplingNote(profile) {
  const sample = profile.sampling;
  return `${sample.sampled_rows} sampled of ${sample.scanned_rows} scanned records · ${formatBytes(sample.bytes_read)} of ${formatBytes(sample.source_bytes)} · ${sample.complete ? "Complete within this record grain" : "Partial evidence"}`;
}

function readerView(file, profile, entry) {
  const view = element("div", "reader-view");
  const base = `/files/${encodeURIComponent(file.id)}`;
  const suffix = entry === undefined ? "" : `?entry=${entry}`;
  if (profile.reader === "image") {
    const controls = element("div", "reader-controls");
    const image = element("img", "preview-image");
    image.alt = file.original_name;
    image.src = `${base}/image${suffix}`;
    controls.append(element("span", "", `${profile.metadata.width} × ${profile.metadata.height} · ${profile.metadata.mode}`),
      action("Toggle zoom", () => image.classList.toggle("zoomed")));
    image.addEventListener("error", () => view.append(element("p", "notice error", "Image preview is unavailable. Inspect the quality checks.")));
    const canvas = element("div", "image-canvas"); canvas.append(image);
    view.append(controls, canvas);
  } else if (profile.reader === "pdf") {
    let page = 1, request = 0;
    const token = state.detailToken;
    const controls = element("div", "reader-controls");
    const label = element("span");
    const image = element("img", "preview-image pdf-page");
    const canvas = element("div", "image-canvas"); canvas.append(image);
    const text = element("pre", "text-preview");
    const details = element("details", "pdf-text");
    details.append(element("summary", "", "Extracted page text"), text);
    const previous = action("Previous page", () => { page--; update(); });
    const next = action("Next page", () => { page++; update(); });
    controls.append(previous, label, next);
    image.addEventListener("error", () => { if (token === state.detailToken) text.textContent = "Page preview is unavailable. Try another page or inspect quality checks."; });
    async function update() {
      const current = ++request;
      previous.disabled = page <= 1;
      next.disabled = page >= profile.metadata.pages;
      label.textContent = `Page ${page} / ${profile.metadata.pages}`;
      image.alt = `${file.original_name}, page ${page}`;
      image.src = `${base}/pages/${page}/image${suffix}`;
      text.textContent = "Loading page text…";
      try {
        const data = await api(`${base}/pages/${page}${suffix}`);
        if (current === request && token === state.detailToken)
          text.textContent = (data.text || "No text layer on this page. Read the page image.") + (data.truncated ? "\n[Text preview truncated]" : "");
      } catch (error) { if (current === request && token === state.detailToken) text.textContent = error.message; }
    }
    view.append(controls, canvas, details); update();
  } else if (profile.reader === "archive") {
    view.append(element("p", "sampling-note", `${profile.metadata.entries} entries · ${formatBytes(profile.metadata.expanded_bytes)} expanded`));
    const list = element("div", "archive-list");
    const child = element("div", "archive-preview");
    const token = state.detailToken;
    let selection = 0;
    for (const member of profile.entries) {
      const item = element("div", "archive-entry");
      const name = element("div", "archive-name", member.name);
      name.append(element("small", "", member.blocked || (member.directory ? "Directory" : formatBytes(member.size))));
      item.append(name);
      if (!member.directory) {
        const button = action("Read member", async () => {
          const current = ++selection;
          child.replaceChildren(element("p", "", "Reading archive member…"));
          try {
            const data = await api(`${base}/archive/${member.index}/profile`);
            if (token !== state.detailToken || current !== selection) return;
            child.replaceChildren(element("h3", "", data.name), element("p", "sampling-note", samplingNote(data.profile)),
              readerView({...file, original_name: data.name}, data.profile, member.index),
              schemaFields(data.profile.fields, true));
            for (const note of data.profile.issues) child.append(element("p", "quality-note", note.message));
          } catch (error) { if (token === state.detailToken && current === selection) child.replaceChildren(element("p", "notice error", error.message)); }
        });
        button.disabled = Boolean(member.blocked) || entry !== undefined;
        if (entry !== undefined) button.title = "Nested archives are listed without further traversal.";
        item.append(button);
      }
      list.append(item);
    }
    view.append(list, child);
  } else if (profile.reader === "table") {
    view.append(element("p", "sampling-note", "Up to 50 sampled records. Field paths preserve original names and values."), sampleTable(profile));
    const raw = element("details"); raw.append(element("summary", "", "Original text sample"), element("pre", "text-preview", profile.preview_text));
    view.append(raw);
  } else {
    view.append(element("p", "sampling-note", profile.reader === "hex" ? "Hexadecimal preview · first 512 bytes" : "Original text preview · up to 12,000 characters"),
      element("pre", "text-preview", profile.preview_text || "Empty file."));
  }
  return view;
}

function tabs(panels) {
  const root = element("div"), bar = element("div", "detail-tabs");
  bar.setAttribute("role", "tablist");
  const buttons = [];
  const select = (index) => {
    buttons.forEach((button, i) => {button.setAttribute("aria-selected", String(i === index)); button.tabIndex = i === index ? 0 : -1;});
    panels.forEach(([, panel], i) => panel.classList.toggle("hidden", i !== index));
  };
  panels.forEach(([label, panel], index) => {
    const button = action(label, () => select(index), "detail-tab");
    button.id = `detail-tab-${index}`;
    button.setAttribute("role", "tab"); button.setAttribute("aria-controls", `detail-panel-${index}`);
    panel.id = `detail-panel-${index}`; panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", button.id);
    panel.classList.add("detail-panel");
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? panels.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + panels.length) % panels.length;
      select(next); buttons[next].focus();
    });
    buttons.push(button); bar.append(button);
  });
  root.append(bar, ...panels.map(([, panel]) => panel)); select(0);
  return {root, select};
}

async function showDetails(id, version) {
  const token = ++state.detailToken;
  $("detail-title").textContent = "File details";
  $("detail-body").replaceChildren(
    element("p", "dialog-message", "Reading file and sampled schema…"),
  );
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  try {
    const [file, profile] = await Promise.all([api(`/files/${encodeURIComponent(id)}`),
      api(`/files/${encodeURIComponent(id)}/profile${version === undefined ? "" : `?version=${version}`}`)]);
    const history = await api(`/files/${encodeURIComponent(id)}/history`);
    if (token !== state.detailToken || !$("detail-dialog").open) return;
    renderFileDetail(file, profile, history);
  } catch (error) {
    if (token === state.detailToken)
      $("detail-body").replaceChildren(element("p", "dialog-message", error.message));
  }
}

function renderFileDetail(file, profile, history) {
    $("detail-title").textContent = `${file.original_name} · v${profile.version}`;
    $("detail-body").replaceChildren();
    const read = readerView(file, profile);
    read.prepend(element("p", "sampling-note", `${profile.format.toUpperCase()} · ${samplingNote(profile)}`));
    const schema = element("div");
    schema.append(element("h3", "", "Sampled fields"), element("p", "sampling-note", `Record grain: ${profile.grain} · ${samplingNote(profile)}`));
    if (profile.fields.length) schema.append(schemaFields(profile.fields, true));
    else schema.append(element("p", "", "There is not enough structured evidence for a content schema yet."));
    const checks = element("ul", "quality-list");
    for (const note of profile.issues) checks.append(element("li", `quality-note ${note.severity}`, `${note.message}${note.count > 1 ? ` (${note.count})` : ""}`));
    if (file.duplicate_files) checks.append(element("li", "quality-note", `${file.duplicate_files} other file(s) have identical SHA-256 bytes. Review their origin before deduplicating.`));
    schema.append(element("h3", "", "Data quality checks"), checks);
    if (!checks.children.length) checks.append(element("li", "quality-note info", "No quality issues observed within this sample."));
    schema.append(element("p", "cleaning-note", "Cleaning proposals: normalize field names, review missing values and type conflicts, preserve leading-zero identifiers, and check record grain before removing duplicates. Original values remain unchanged."));
    const controls = element("div", "resample-controls");
    const bytes = element("select"), rows = element("select");
    bytes.setAttribute("aria-label", "Sampling byte budget"); rows.setAttribute("aria-label", "Sampling row budget");
    for (const size of [65536, 262144, 1048576]) bytes.add(new Option(formatBytes(size), String(size)));
    for (const count of [50, 200, 500, 2000]) rows.add(new Option(`${count} records`, String(count)));
    bytes.value = "262144"; rows.value = "500";
    const feedback = element("p", "sampling-note"); feedback.setAttribute("role", "status");
    const token = state.detailToken;
    const resample = action("Resample & save version", async () => {
      resample.disabled = true; feedback.textContent = "Sampling original content…";
      try {
        const next = await api(`/files/${encodeURIComponent(file.id)}/resample?sample_bytes=${bytes.value}&sample_rows=${rows.value}`, {method: "POST"});
        const versions = await api(`/files/${encodeURIComponent(file.id)}/history`);
        if (token === state.detailToken && $("detail-dialog").open) {
          renderFileDetail(file, next, versions);
          $("detail-tab-1").click();
        }
        await Promise.all([loadRecords(), loadSchemas()]);
      } catch (error) { if (token === state.detailToken) feedback.textContent = error.message; }
      finally {resample.disabled = false;}
    });
    controls.append(bytes, rows, resample); schema.append(controls, feedback);
    if (profile.schema_key) schema.append(action("Review schema family", () => showSchema(profile.schema_key)));
    const list = element("dl", "detail-metadata");
    const fields = [
      ["Record ID", file.id, true],
      ["Original name", file.original_name],
      ["Extension", file.extension],
      [
        "File size",
        `${formatBytes(file.file_size)} (${file.file_size.toLocaleString()} bytes)`,
      ],
      ["Detected type", profile.format.toUpperCase()],
      ["Category", profile.category],
      ["MIME type", profile.mime_type, true],
      ["Encoding", profile.encoding || file.encoding || "Not identified"],
      ["Evidence", file.confidence],
      ["Status", file.status],
      ["Uploaded", new Date(file.created_at).toLocaleString()],
      ["Magic bytes (hex)", file.magic_bytes || "None — empty file", true],
      [
        "ASCII printable ratio",
        `${(file.printable_ratio * 100).toFixed(1)}% of sampled bytes`,
      ],
      [
        "Sample inspected",
        `${formatBytes(file.sample_bytes)} of ${formatBytes(file.file_size)}`,
      ],
      ["SHA-256", file.sha256, true],
      ["Error / review note", file.error_message || "None"],
    ];
    for (const [label, value, mono] of fields) {
      const row = element("div");
      row.append(
        element("dt", "", label),
        element("dd", mono ? "mono" : "", value),
      );
      list.append(row);
    }
    const versions = element("div");
    versions.append(element("h3", "", "Profile history"), element("p", "sampling-note", "Earlier samples remain inspectable. Each version records its byte and row budgets."));
    for (const previous of history.versions) {
      const row = element("div", "history-row");
      row.append(element("span", "", `v${previous.version} · ${formatDate(previous.created_at)} · ${previous.sampling.sampled_rows} records · ${formatBytes(previous.sampling.bytes_read)}`));
      row.append(action(previous.version === profile.version ? "Current view" : "View sample", async () => {
        try {
          const data = await api(`/files/${encodeURIComponent(file.id)}/profile?version=${previous.version}`);
          if (token === state.detailToken && $("detail-dialog").open) renderFileDetail(file, data, history);
        } catch (error) {if (token === state.detailToken) row.append(element("p", "notice error", error.message));}
      }));
      versions.append(row);
    }
    const detail = tabs([["Read", read], ["Schema & quality", schema], ["Properties", list], ["History", versions]]);
    $("detail-body").append(detail.root);
}

async function loadSchemas() {
  $("refresh-schemas").disabled = true;
  try {
    const data = await api("/schemas"); state.schemas = data.schemas;
    $("schema-error").classList.add("hidden");
    $("schema-count").textContent = state.schemas.length;
    $("schema-library").replaceChildren();
    for (const schema of state.schemas) {
      const card = element("article", "card schema-card");
      const definition = schema.definition;
      card.append(element("span", "small-tag", schema.reviewed_at ? "REVIEWED SAMPLE" : "CANDIDATE"),
        element("h3", "", `${definition.format.toUpperCase()} · ${definition.grain}`),
        element("p", "", `${definition.fields.length} fields · ${definition.source_files} files · v${schema.version}`),
        element("p", "schema-paths", definition.fields.slice(0, 4).map((field) => field.path).join(" · ")),
        action("Review schema", () => showSchema(schema.id)));
      $("schema-library").append(card);
    }
    if (!state.schemas.length) $("schema-library").append(element("p", "card schema-empty", "No content schemas yet. Upload readable files or open a saved file to create its first sample."));
  } catch (error) {
    $("schema-error").textContent = error.message;
    $("schema-error").classList.remove("hidden");
  } finally {$("refresh-schemas").disabled = false;}
}

function downloadSchema(version) {
  const blob = new Blob([JSON.stringify(version, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = element("a"); link.href = url; link.download = `schema-${version.id}-v${version.version}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function showSchema(id) {
  const token = ++state.detailToken;
  $("detail-title").textContent = "Schema history";
  $("detail-body").replaceChildren(element("p", "dialog-message", "Loading schema evidence…"));
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  try {
    const data = await api(`/schemas/${encodeURIComponent(id)}`);
    if (token !== state.detailToken || !$("detail-dialog").open) return;
    const select = element("select"); select.setAttribute("aria-label", "Schema version");
    for (const version of data.versions) select.add(new Option(`v${version.version} · ${formatDate(version.created_at)}`, String(version.version)));
    const content = element("div", "schema-detail");
    const render = () => {
      const version = data.versions.find((item) => item.version === Number(select.value));
      const definition = version.definition;
      $("detail-title").textContent = `${definition.format.toUpperCase()} schema · v${version.version}`;
      content.replaceChildren(element("p", "sampling-note", `${definition.source_files} source files · ${definition.sampled_rows} sampled records · ${definition.grain} grain · ${definition.status === "retired" ? "Retired family" : version.reviewed_at ? "Reviewed sample" : "Candidate"}`),
        schemaFields(definition.fields), element("h3", "", "Changes from the previous schema version"));
      for (const [key, label] of [["added_fields", "Added"], ["removed_fields", "Removed"], ["changed_fields", "Changed types, names, or nullability"]])
        content.append(element("p", "drift-line", `${label}: ${version.drift[key].join(", ") || "None"}`));
      content.append(element("h3", "", "Source evidence"));
      for (const evidence of version.evidence) {
        const row = element("div", "history-row");
        const file = state.records.find((record) => record.id === evidence.file_id);
        row.append(element("span", "", `${file?.original_name || "Source file"} · profile v${evidence.profile_version} · ${evidence.sampling.sampled_rows} sampled records · ${formatBytes(evidence.sampling.bytes_read)}`),
          action("Inspect source", () => showDetails(evidence.file_id, evidence.profile_version)));
        content.append(row);
      }
      const status = element("p", "sampling-note", version.reviewed_at ? `Reviewed ${formatDate(version.reviewed_at)}. Review applies to this sample version.` : "Confirmation records your review of this sample. Later evidence creates another candidate version.");
      status.setAttribute("role", "status");
      const confirm = action("Confirm reviewed schema", async () => {
        confirm.disabled = true;
        try {
          const updated = await api(`/schemas/${encodeURIComponent(id)}/confirm?version=${version.version}`, {method: "POST"});
          if (token === state.detailToken) {version.reviewed_at = updated.reviewed_at; render();}
          await loadSchemas();
        } catch (error) {if (token === state.detailToken) status.textContent = error.message; confirm.disabled = false;}
      });
      confirm.disabled = Boolean(version.reviewed_at) || version.version !== data.versions[0].version || definition.status === "retired";
      const controls = element("div", "reader-controls");
      controls.append(confirm, action("Download schema JSON", () => downloadSchema(version)));
      content.append(status, controls);
    };
    select.addEventListener("change", render);
    const wrapper = element("div", "detail-panel"); wrapper.append(select, content);
    $("detail-body").replaceChildren(wrapper); render();
  } catch (error) {if (token === state.detailToken) $("detail-body").replaceChildren(element("p", "dialog-message", error.message));}
}

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
let searchTimer;
$("search").addEventListener("input", () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => loadRecords(), 180);});
$("type-filter").addEventListener("change", () => loadRecords());
$("refresh-schemas").addEventListener("click", loadSchemas);
$("dismiss-errors").addEventListener("click", () => renderErrors([]));
$("close-dialog").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("close", () => {state.detailToken++;});
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
  await Promise.all([
    loadRecords(),
    loadSchemas(),
    (async () => {
      try {
        state.limits = await api("/config");
        $("upload-limits").textContent =
          `Up to ${state.limits.max_files} files · ${formatBytes(state.limits.max_file_bytes)} each · ${formatBytes(state.limits.max_request_bytes)} per batch`;
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
