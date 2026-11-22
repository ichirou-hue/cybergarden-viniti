const JSON_HEADERS = {Accept: "application/json"};

// CSRF-токен живёт только в памяти вкладки. Сервер выдаёт его вместе с
// cookie-сессией в /api/auth/me, login и register; после перезагрузки его
// возвращает тот же /api/auth/me.
let csrfToken = "";

export function setCSRF(token) {
  csrfToken = token || "";
}

export function getCSRF() {
  return csrfToken;
}

function unauthorized(response) {
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent("viniti:unauthorized"));
  }
  return response;
}

async function unwrap(response) {
  const type = response.headers.get("Content-Type") || "";
  let data;
  if (type.includes("application/json")) {
    try {
      data = await response.json();
    } catch {
      const error = new Error("Сервер вернул некорректный JSON.");
      error.status = response.status;
      throw error;
    }
  } else {
    data = await response.text();
  }
  if (!response.ok) {
    const message = data && typeof data === "object" && data.error ? data.error : `Ошибка ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

function networkError(error) {
  if (error.name === "AbortError") throw error;
  throw new Error("Не удалось связаться с сервером. Проверьте соединение.");
}

function request(path, options = {}, signal) {
  const headers = new Headers(options.headers || {});
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  return fetch(path, {
    ...options,
    headers,
    signal: options.signal || signal,
    credentials: "same-origin",
  }).catch(networkError).then(unauthorized);
}

export function getJSON(path, params = {}, signal) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value === "" || value === null || value === undefined) continue;
    url.searchParams.set(key, value);
  }
  return fetch(url, {signal, headers: JSON_HEADERS, credentials: "same-origin"})
    .catch(networkError).then(unauthorized).then(unwrap);
}

export function postJSON(path, body, signal) {
  return request(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", Accept: "application/json"},
    body: JSON.stringify(body),
  }, signal).then(unwrap);
}

export function deleteJSON(path, signal) {
  return request(path, {method: "DELETE"}, signal).then(unwrap);
}

export function putJSON(path, body, signal) {
  return request(path, {
    method: "PUT",
    headers: {"Content-Type": "application/json", Accept: "application/json"},
    body: JSON.stringify(body),
  }, signal).then(unwrap);
}

// Сырой запрос с CSRF и cookie: нужен страницам, которые отдают не JSON
// (например, PDF на /api/profile/source).
export function apiFetch(path, options = {}, signal) {
  return request(path, options, signal);
}

// Сессия на сервере: cookie ставится HttpOnly, а CSRF-токен возвращается
// в теле. Ответ {"user": null} - штатное «не вошёл», а не ошибка.
export async function loadSession(signal) {
  const data = await getJSON("/api/auth/me", {}, signal);
  setCSRF(data && data.csrf_token);
  return data || {user: null};
}

export async function uploadPDF(file, track, signal) {
  const summary = await request("/api/documents", {
    method: "POST",
    body: file,
    signal,
    headers: {
      "Content-Type": "application/pdf",
      "X-Track": track,
      "X-Filename": encodeURIComponent(file.name),
    },
  }, signal).then(unwrap);
  if (summary.recognition_job_id) {
    const path = "/api/parse/" + encodeURIComponent(summary.recognition_job_id);
    let job;
    do {
      if (signal?.aborted) throw new DOMException("Загрузка отменена", "AbortError");
      await new Promise(resolve => setTimeout(resolve, 1000));
      job = await getJSON(path, {}, signal);
    } while (job.status === "queued" || job.status === "running");
    if (job.status !== "ok") throw new Error("Docling: " + (job.error || "разбор не завершён"));
  }
  return summary;
}

export async function uploadPDFWithProgress(file, track, onProgress, signal) {
  const response = await request("/api/documents/stream", {
    method: "POST", body: file, signal,
    headers: {"Content-Type": "application/pdf", "X-Track": track,
      "X-Filename": encodeURIComponent(file.name)},
  }, signal);
  if (!response.ok) return unwrap(response);
  if (!response.body) throw new Error("Браузер не поддерживает потоковый ответ сервера.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let result;
  while (true) {
    const {value, done} = await reader.read();
    pending += decoder.decode(value || new Uint8Array(), {stream: !done});
    const lines = pending.split("\n");
    pending = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      onProgress(event);
      if (event.error) throw new Error(event.error);
      if (event.result) result = event.result;
    }
    if (done) break;
  }
  if (!result) throw new Error("Сервер завершил ответ без результата обработки.");
  return result;
}

export function getProfiles(signal) {
  return getJSON("/api/profiles", {}, signal);
}

export function getLLM(signal) {
  return getJSON("/api/llm", {}, signal);
}

export function startParse(file, options = {}, signal) {
  const llm = options.llm === "api" ? "api" : "off";
  return request("/api/parse", {
    method: "POST",
    body: file,
    signal,
    headers: {
      "Content-Type": "application/pdf",
      "X-Profile": options.profile || "solar",
      "X-Parser": options.parser || "pdfplumber",
      "X-OCR": options.ocr || "auto",
      "X-Charts": options.charts ? "1" : "0",
      "X-LLM": llm,
      "X-Filename": encodeURIComponent(file.name),
    },
  }, signal).then(unwrap);
}

export function openDemo(track, signal) {
  return request("/api/demo", {
    method: "POST",
    signal,
    headers: {"X-Track": track},
  }, signal).then(unwrap);
}
