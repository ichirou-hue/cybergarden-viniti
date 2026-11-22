// Передача выбранного PDF из мастера на страницу ручного профиля.
// Файл нельзя положить в sessionStorage, поэтому используем IndexedDB и
// забираем запись один раз, чтобы она не занимала место после перехода.

const DB_NAME = "viniti-profile";
const STORE = "pending";
const PDF_KEY = "pdf";
const TTL_MS = 15 * 60 * 1000;
const TIMEOUT_MS = 5000;
const TIMEOUT_MESSAGE = "IndexedDB не ответил за " + TIMEOUT_MS / 1000 + " с";

function timed(promise, message = TIMEOUT_MESSAGE) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), TIMEOUT_MS);
    promise.then(
      value => { clearTimeout(timer); resolve(value); },
      error => { clearTimeout(timer); reject(error); },
    );
  });
}

function openDatabase() {
  return timed(new Promise((resolve, reject) => {
    let request;
    try {
      request = indexedDB.open(DB_NAME, 1);
    } catch (error) {
      reject(error);
      return;
    }
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    // onblocked не завершает запрос: сработает таймаут timed() и переход
    // пойдёт обычным путём, без автозагрузки PDF.
  }));
}

function transactionDone(transaction) {
  return timed(new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  }));
}

export async function stashProfilePDF(file) {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE, "readwrite");
    transaction.objectStore(STORE).put({file, savedAt: Date.now()}, PDF_KEY);
    await transactionDone(transaction);
  } finally {
    database.close();
  }
}

async function readProfilePDF() {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE, "readwrite");
    const store = transaction.objectStore(STORE);
    const request = store.get(PDF_KEY);
    let record;
    request.onsuccess = () => {
      record = request.result;
      if (record) store.delete(PDF_KEY);
    };
    await transactionDone(transaction);
    const fresh = record && Date.now() - Number(record.savedAt) < TTL_MS;
    return fresh && record.file instanceof Blob ? record.file : undefined;
  } finally {
    database.close();
  }
}

/** Забирает PDF и удаляет запись: вызывается мастером при возврате. */
export function takeProfilePDF() {
  return readProfilePDF();
}
