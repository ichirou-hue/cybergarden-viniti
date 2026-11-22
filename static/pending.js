// Передача выбранного PDF из мастера на страницу ручного профиля.
// Файл нельзя положить в sessionStorage, поэтому используем IndexedDB.
// Мастер забирает запись один раз, а страница профиля читает её без удаления:
// так PDF переживает возврат в мастер и повторную загрузку страницы, а место
// освобождается после успешного разбора или по истечении TTL.

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

function fresh(record) {
  return Boolean(record) && Date.now() - Number(record.savedAt) < TTL_MS;
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

async function readProfilePDF(consume) {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE, "readwrite");
    const store = transaction.objectStore(STORE);
    const request = store.get(PDF_KEY);
    let record;
    request.onsuccess = () => {
      record = request.result;
      // Просроченная запись не нужна ни мастеру, ни странице профиля,
      // поэтому удаляется при любом чтении.
      if (record && (consume || !fresh(record))) store.delete(PDF_KEY);
    };
    await transactionDone(transaction);
    return fresh(record) && record.file instanceof Blob ? record.file : undefined;
  } finally {
    database.close();
  }
}

/** Забирает PDF и удаляет запись: так мастер возвращает файл после перехода. */
export function takeProfilePDF() {
  return readProfilePDF(true);
}

/** Читает PDF, не удаляя запись: страница профиля освобождает её сама. */
export function peekProfilePDF() {
  return readProfilePDF(false);
}

/** Освобождает запись после успешного разбора. */
export async function clearProfilePDF() {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE, "readwrite");
    transaction.objectStore(STORE).delete(PDF_KEY);
    await transactionDone(transaction);
  } finally {
    database.close();
  }
}
