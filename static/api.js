const JSON_HEADERS = {Accept: "application/json"};

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

export function getJSON(path, params = {}, signal) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value === "" || value === null || value === undefined) continue;
    url.searchParams.set(key, value);
  }
  return fetch(url, {signal, headers: JSON_HEADERS}).catch(networkError).then(unwrap);
}

export function uploadPDF(file, track, signal) {
  return fetch("/api/documents", {
    method: "POST",
    body: file,
    signal,
    headers: {
      "Content-Type": "application/pdf",
      "X-Track": track,
      "X-Filename": encodeURIComponent(file.name),
    },
  }).catch(networkError).then(unwrap);
}

export async function uploadPDFWithProgress(file, track, onProgress, signal) {
  const response = await fetch("/api/documents/stream", {
    method: "POST", body: file, signal,
    headers: {"Content-Type": "application/pdf", "X-Track": track,
      "X-Filename": encodeURIComponent(file.name)},
  }).catch(networkError);
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

export function openDemo(track, signal) {
  return fetch("/api/demo", {
    method: "POST",
    signal,
    headers: {"X-Track": track},
  }).catch(networkError).then(unwrap);
}
