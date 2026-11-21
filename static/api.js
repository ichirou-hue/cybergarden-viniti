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

export async function uploadPDF(file, track, signal) {
  const summary = await fetch("/api/documents", {
    method: "POST",
    body: file,
    signal,
    headers: {
      "Content-Type": "application/pdf",
      "X-Track": track,
      "X-Filename": encodeURIComponent(file.name),
    },
  }).catch(networkError).then(unwrap);
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

export function openDemo(track, signal) {
  return fetch("/api/demo", {
    method: "POST",
    signal,
    headers: {"X-Track": track},
  }).catch(networkError).then(unwrap);
}
