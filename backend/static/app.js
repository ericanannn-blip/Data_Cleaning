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
  if (!/\.dat$/i.test(file.name)) return "Only .dat files are accepted.";
  if (state.limits && file.size > state.limits.max_file_bytes)
    return `Exceeds the ${formatBytes(state.limits.max_file_bytes)} file limit.`;
  return null;
}

function renderQueue() {
  $("selected-files").replaceChildren();
  for (const [index, file] of state.queue.entries()) {
    const item = element("li");
    item.append(element("span", "file-glyph", "DAT"));
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
    : "Upload & inspect →";
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
    $("detail-body").append(list);
  } catch (error) {
    if (token === state.detailToken)
      $("detail-body").replaceChildren(
        element("p", "dialog-message", error.message),
      );
  }
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
  await Promise.all([
    loadRecords(),
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
