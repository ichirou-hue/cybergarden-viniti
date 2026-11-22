import { el, fill } from "./dom.js";
import { tracks } from "./tracks/index.js";
import { renderCatalog } from "./catalog.js";
import { renderDocument } from "./document.js";
import { openEvidence, dismissEvidence } from "./evidence.js";
import { uploadPDF, uploadPDFWithProgress, openDemo, getLLM, startParse } from "./api.js";
import { renderPassport, setPassport, setImportNotice } from "./passport.js";
import { stashProfilePDF, takeProfilePDF } from "./pending.js";
import { mountAuthScreen, currentAccount, isSignedIn, recordActivity, signOutAccount, restoreSession } from "./auth.js";
import { mountAccountMenu, renderProfile } from "./account.js";
import { renderParseJob } from "./parse.js";

const view = document.getElementById("view");
const statusLine = document.getElementById("status");
const form = document.getElementById("upload");
const fileInput = document.getElementById("file");
const uploadState = document.getElementById("uploadState");
const demoButton = document.getElementById("demo");
const passportInput = document.getElementById("passport");
const passportLink = document.getElementById("passportLink");
const trackBar = document.getElementById("tracks");
const evidenceDialog = document.getElementById("evidence");
const wizard = document.getElementById("wizard");
const dropZone = document.getElementById("dropZone");
const profileStep = document.getElementById("profileStep");
let selectedPDF = null;
let wizardTrack = null;

const authScreen = mountAuthScreen(document.getElementById("authScreen"), {
  onEnter: enterApp,
});
const accountMenu = mountAccountMenu({
  navigate: hash => { location.hash = hash; },
  onLogout: () => leaveAccount(),
});

const app = {descriptors: [], track: "rag", llm: "off", controller: null, started: false};
let booted = false;
let lastPath = "";
let pendingChecked = false;

function parse() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  return {path: path || "/", params: Object.fromEntries(new URLSearchParams(query))};
}

function compact(params) {
  const result = {};
  for (const [key, value] of Object.entries(params || {})) {
    if (value === "" || value === null || value === undefined) continue;
    result[key] = value;
  }
  return result;
}

function buildHash(path, params) {
  const query = new URLSearchParams(compact(params)).toString();
  return `#${path}${query ? "?" + query : ""}`;
}

function patch(changes) {
  const {path, params} = parse();
  const next = compact({...params, ...changes});
  history.replaceState(null, "", buildHash(path, next));
  return next;
}

function go(changes) {
  const {path, params} = parse();
  location.hash = buildHash(path, compact({...params, ...changes})).slice(1);
}

function say(message, isError = false) {
  statusLine.textContent = message || "";
  statusLine.className = isError ? "status error" : "status";
}

function renderTracks(active = app.track) {
  // Область больше не переключается в шапке: RAG/Solar выбираются только
  // на экране загрузки. Ссылка на паспорт остаётся отдельным пунктом.
  if (!trackBar) return;
  fill(trackBar, app.descriptors.map(item => el("a", {
    class: "track",
    href: buildHash("/catalog", {track: item.track}),
    "aria-current": item.track === active ? "page" : null,
    text: item.label || item.track,
  })));
}

function setUploadEnabled(enabled) {
  form.querySelectorAll("button, input").forEach(node => { node.disabled = !enabled; });
  form.classList.toggle("disabled", !enabled);
}

function message(title, detail, extra) {
  document.getElementById("bootError").hidden = true;
  document.title = `${title} · ВИНИТИ`;
  fill(view, el("div", {class: "empty"},
    el("h1", {class: "empty-title", text: title}),
    detail ? el("p", {class: "muted", text: detail}) : null,
    extra || el("p", {}, el("a", {href: "#/", text: "Добавить PDF"})),
  ));
}

function focusView() {
  try {
    view.focus({preventScroll: true});
  } catch {
    view.focus();
  }
}

function splitDocId(raw) {
  try {
    return decodeURIComponent(raw);
  } catch {
    return "";
  }
}

async function route() {
  let {path, params} = parse();
  if (!isSignedIn()) {
    wizard.hidden = true;
    authScreen.show();
    return;
  }
  const legacyTab = path.match(/^\/doc\/([^/]+)\/(issues|entities|sources)$/);
  if (legacyTab) {
    path = `/doc/${legacyTab[1]}`;
    params = {...params, tab: legacyTab[2]};
    history.replaceState(null, "", buildHash(path, params));
  }
  const match = path.match(/^\/doc\/(.+?)(?:\/source\/([0-9]+))?$/);
  const parseMatch = path.match(/^\/parse\/([^/]+)$/);
  const docId = match ? splitDocId(match[1]) : "";
  const pathChanged = path !== lastPath;
  lastPath = path;
  say("");
  const wizardActive = path === "/";
  wizard.hidden = !wizardActive;
  document.body.classList.toggle("wizard-active", wizardActive);
  if (wizardActive) {
    app.controller?.abort();
    setUploadEnabled(true);
    document.body.dataset.view = "";
    document.title = "ВИНИТИ · Каталог результатов";
    renderWizard();
    if (!pendingChecked) {
      pendingChecked = true;
      void restorePendingPDF();
    }
    booted = true;
    document.getElementById("bootError").hidden = true;
    return;
  }
  document.body.dataset.view = "catalog";
  passportLink.removeAttribute("aria-current");
  if (!(match && match[2])) dismissEvidence();
  if (match && !docId) {
    setUploadEnabled(false);
    message("Страница не найдена.", "Некорректный адрес документа.");
    return;
  }

  if (path === "/passport") {
    app.controller?.abort();
    document.body.dataset.view = "passport";
    passportLink.setAttribute("aria-current", "page");
    setUploadEnabled(true);
    renderTracks("");
    renderPassport(view, {say, go, patch, params});
    if (booted && pathChanged) focusView();
    booted = true;
    document.getElementById("bootError").hidden = true;
    return;
  }

  if (path === "/profile") {
    app.controller?.abort();
    document.body.dataset.view = "profile";
    passportLink.removeAttribute("aria-current");
    renderTracks("");
    setUploadEnabled(true);
    renderProfile(view, {
      account: currentAccount(),
      params,
      navigate: hash => { location.hash = hash; },
      onLogout: leaveAccount,
      onSave: updated => accountMenu.sync(updated),
    });
    if (booted && pathChanged) focusView();
    booted = true;
    document.getElementById("bootError").hidden = true;
    return;
  }

  if (parseMatch) {
    const jobId = splitDocId(parseMatch[1]);
    app.controller?.abort();
    document.body.dataset.view = "parse";
    setUploadEnabled(true);
    renderTracks("");
    if (!/^[0-9a-f]{16}$/.test(jobId)) {
      message("Разбор не найден.", "Некорректный идентификатор задания.");
      return;
    }
    const controller = new AbortController();
    app.controller = controller;
    try {
      await renderParseJob(view, {jobId, signal: controller.signal, say});
      if (!controller.signal.aborted && booted && pathChanged) focusView();
      booted = true;
      document.getElementById("bootError").hidden = true;
    } catch (error) {
      if (error.name === "AbortError" || controller.signal.aborted) return;
      fill(view, el("div", {class: "notice bad", role: "alert"},
        el("strong", {text: error.status === 404 ? "Разбор не найден" : "Не удалось загрузить задание"}),
        el("p", {text: error.message}),
        el("p", {}, el("a", {href: "#/", text: "Вернуться к загрузке"})),
      ));
      say(error.message, true);
    }
    return;
  }
  const wanted = params.track || app.track;
  const known = app.descriptors.some(item => item.track === wanted);
  const desired = docId
    ? (docId.includes(":") ? docId.split(":")[0] : (known ? wanted : app.track))
    : wanted;
  const descriptor = app.descriptors.find(item => item.track === desired);
  renderTracks(desired);
  if (!descriptor) {
    setUploadEnabled(false);
    const label = desired === "solar" ? "Solar" : desired;
      message(
        `Раздел «${label}» пока не подключён.`,
        "Обработчик области недоступен.",
      el("p", {}, el("a", {href: "#/", text: "Добавить PDF"})),
    );
    return;
  }
  app.track = descriptor.track;
  document.getElementById("demo").hidden = descriptor.demo === false;
  document.body.dataset.view = match ? "doc" : "catalog";
  setUploadEnabled(true);
  renderTracks();
  if (docId && params.track && params.track !== descriptor.track) {
    history.replaceState(null, "", buildHash(path, {...params, track: descriptor.track}));
  }

  app.controller?.abort();
  const controller = new AbortController();
  app.controller = controller;
  const {signal} = controller;
  const ctx = {descriptor, params, signal, patch, go, say};

  try {
    if (match) {
      const sourceId = match[2];
      await renderDocument(view, {...ctx, docId});
      if (signal.aborted) return;
      if (sourceId) {
        try {
          await openEvidence({docId, sourceId, signal});
        } catch (error) {
          if (error.name === "AbortError") return;
          dismissEvidence();
          history.replaceState(null, "", buildHash(`/doc/${match[1]}`, params));
          say(`Не удалось открыть источник: ${error.message}`, true);
        }
      }
    } else if (path === "/catalog") {
      await renderCatalog(view, ctx);
      if (signal.aborted) return;
    } else {
      setUploadEnabled(false);
      message("Страница не найдена.");
      return;
    }
    if (booted && pathChanged && !evidenceDialog.open) focusView();
    booted = true;
    document.getElementById("bootError").hidden = true;
  } catch (error) {
    if (error.name === "AbortError" || signal.aborted) return;
    fill(view, el("div", {class: "notice bad", role: "alert"},
      el("strong", {text: "Не удалось загрузить данные"}),
      el("p", {text: error.message}),
      el("p", {}, el("a", {href: "#/", text: "Добавить PDF"})),
    ));
    say(error.message, true);
  }
}

async function busy(messageText, task) {
  const controls = [...form.querySelectorAll("button, input")];
  controls.forEach(node => { node.disabled = true; });
  form.classList.add("busy");
  uploadState.classList.remove("error");
  uploadState.textContent = messageText;
  say(messageText);
  try {
    return await task();
  } catch (error) {
    say(error.message, true);
    uploadState.textContent = error.message;
    uploadState.classList.add("error");
    return null;
  } finally {
    controls.forEach(node => { node.disabled = false; });
    form.classList.remove("busy");
    if (!uploadState.classList.contains("error")) uploadState.textContent = "";
  }
}

async function openPassport() {
  if (location.hash.startsWith("#/passport")) await route();
  else location.hash = "#/passport";
}

// «Паспорт» работает как переключатель: повторное нажатие возвращает
// к загрузке PDF, а не оставляет пользователя на том же экране.
passportLink.addEventListener("click", event => {
  if (!location.hash.startsWith("#/passport")) return;
  event.preventDefault();
  location.hash = "#/";
});

passportInput.addEventListener("change", async () => {
  const file = passportInput.files[0];
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch {
    setImportNotice("bad", "Файл не является корректным JSON.");
    await openPassport();
    passportInput.value = "";
    return;
  }
  try {
    const count = setPassport(data);
    setImportNotice("good",
      `Паспорт загружен. Записей: ${count}. CSV включает только прошедшие проверку строки.`);
  } catch (error) {
    setImportNotice("bad", `Не удалось разобрать паспорт. ${error.message}`);
  }
  await openPassport();
  passportInput.value = "";
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  uploadState.classList.remove("error");
  uploadState.textContent = "";
  if (file) selectPDF(file);
});

// Модель - необязательная надстройка над детерминированным разбором, поэтому
// переключатель живёт внутри мастера, рядом с выбором области, а не в шапке.
const llmStatus = {checked: false, available: false, configured: false};

function syncLLMNote() {
  const state = document.getElementById("wizardLLMState");
  const note = document.getElementById("wizardLLMNote");
  const input = document.getElementById("wizardLLM");
  if (!state || !note || !input) return;
  const on = app.llm === "api";
  state.textContent = on ? "С моделью" : "Без модели";
  input.checked = on;
  if (llmStatus.checked && !llmStatus.available) {
    note.textContent = llmStatus.configured
      ? "Модель отключена на сервере. Разбор пройдёт без неё."
      : "Модель не настроена на сервере. Разбор пройдёт без неё.";
    return;
  }
  note.textContent = on
    ? "Сервер дополнительно разберёт фрагменты моделью и покажет проверенную интерпретацию. Доступно для области Solar."
    : "Текст, таблицы и цитаты извлекаются без модели. Так результат воспроизводим.";
}

function setLLMMode(mode) {
  app.llm = mode === "api" ? "api" : "off";
  syncLLMNote();
}

document.getElementById("wizardLLM")?.addEventListener("change", event => {
  setLLMMode(event.target.checked ? "api" : "off");
  if (app.llm === "api" && wizardTrack === "rag") wizardTrack = "solar";
  renderWizard();
});

async function loadLLMStatus() {
  const input = document.getElementById("wizardLLM");
  if (!input) return;
  try {
    const status = await getLLM();
    llmStatus.checked = true;
    llmStatus.available = Boolean(status?.available);
    llmStatus.configured = Boolean(status?.configured);
    input.disabled = !llmStatus.available;
    if (!llmStatus.available && app.llm === "api") app.llm = "off";
    syncLLMNote();
  } catch {
    // Сервер остаётся последней инстанцией: разовый сбой проверки статуса
    // не должен отключать управление.
  }
}

function openWizard() {
  wizard.hidden = false;
  document.body.classList.add("wizard-active");
  renderWizard();
}

function renderWizard() {
  const ready = !!selectedPDF;
  const llmOn = app.llm === "api";
  dropZone.hidden = ready;
  profileStep.hidden = !ready;
  wizard.dataset.step = ready ? "2" : "1";
  document.getElementById("wizardStep").textContent = ready ? "Шаг 2 из 2 · Область" : "Шаг 1 из 2 · Документ";
  document.getElementById("wizardTitle").textContent = ready ? "Как разобрать статью?" : "Начните с PDF";
  document.getElementById("wizardHint").textContent = ready
    ? "Область задаёт поля и шаблон CSV. Модель можно включить ниже."
    : "RAG: до 20 МиБ, Solar: до 32 МиБ.";
  document.getElementById("selectedFile").textContent = ready ? selectedPDF.name : "";
  syncLLMNote();
  let preferred = wizardTrack === "rag" || wizardTrack === "solar" ? wizardTrack : app.track;
  if (llmOn) preferred = "solar";
  fill(document.getElementById("profileChoices"), ...["solar", "rag"].map(track => {
    const available = app.descriptors.some(item => item.track === track);
    const blockedByLLM = llmOn && track !== "solar";
    return el("label", {class: "profile-choice"},
      el("input", {type: "radio", name: "wizardProfile", value: track,
        disabled: !available || blockedByLLM,
        checked: available && !blockedByLLM && track === preferred}),
      el("span", {},
        el("strong", {text: `${track === "solar" ? "Solar" : "RAG"}${available ? "" : " · скоро"}`}),
        blockedByLLM ? el("small", {text: "Только без модели"}) : null));
  }));
  const checked = document.querySelector('input[name="wizardProfile"]:checked');
  if (checked) wizardTrack = checked.value;
}

document.getElementById("profileChoices").addEventListener("change", event => {
  if (event.target.matches('input[name="wizardProfile"]')) wizardTrack = event.target.value;
});

function selectPDF(file) {
  const error = document.getElementById("wizardError");
  error.hidden = true;
  openWizard();
  if (!file || !(file.name.toLowerCase().endsWith(".pdf") || file.type === "application/pdf")) {
    error.textContent = "Выберите файл PDF.";
    error.hidden = false;
    return;
  }
  if (file.size > 32 * 1024 * 1024) {
    error.textContent = "Файл больше 32 МиБ. Выберите PDF меньшего размера.";
    error.hidden = false;
    return;
  }
  selectedPDF = file;
  renderWizard();
}

// Возврат со страницы ручного профиля не должен терять выбранный PDF:
// мастер сохраняет файл перед переходом и забирает его обратно при загрузке.
async function restorePendingPDF() {
  if (selectedPDF) return;
  // В средах без IndexedDB (jsdom, старый браузер) возврат просто не нужен.
  if (!("indexedDB" in globalThis)) return;
  try {
    const file = await takeProfilePDF();
    if (file) selectPDF(file);
  } catch (error) {
    // IndexedDB может быть недоступен: тогда мастер просто ждёт новый выбор.
    console.warn("Не удалось вернуть PDF из ручного профиля:", error.message);
  }
}

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", event => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); }
});
for (const name of ["dragenter", "dragover"]) dropZone.addEventListener(name, event => {
  event.preventDefault(); dropZone.classList.add("drag-over");
});
for (const name of ["dragleave", "drop"]) dropZone.addEventListener(name, event => {
  event.preventDefault(); dropZone.classList.remove("drag-over");
});
dropZone.addEventListener("drop", event => selectPDF(event.dataTransfer.files[0]));
document.getElementById("wizardBack").addEventListener("click", () => {
  selectedPDF = null;
  fileInput.value = "";
  document.getElementById("processingProgress").hidden = true;
  document.getElementById("wizardError").hidden = true;
  renderWizard();
});
document.getElementById("wizardCancel").addEventListener("click", () => {
  selectedPDF = null;
  fileInput.value = "";
  document.getElementById("processingProgress").hidden = true;
  document.getElementById("wizardError").hidden = true;
  renderWizard();
  location.hash = buildHash("/catalog", {track: app.track});
});
document.getElementById("createProfile").addEventListener("click", async event => {
  if (!selectedPDF) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const link = event.currentTarget;
  link.setAttribute("aria-busy", "true");
  try {
    await stashProfilePDF(selectedPDF);
  } catch (error) {
    // Переход не должен зависеть от хранилища: на странице профиля PDF
    // можно выбрать повторно.
    console.warn("Не удалось передать PDF в ручной профиль:", error.message);
  }
  location.assign(link.href);
});
document.getElementById("wizardSubmit").addEventListener("click", async () => {
  const track = wizardTrack;
  const llm = app.llm;
  if (!selectedPDF || !track) return;
  const error = document.getElementById("wizardError");
  if (llm === "api" && track !== "solar") {
    error.textContent = "С моделью разбирается только область Solar. Выберите Solar или выключите модель.";
    error.hidden = false;
    return;
  }
  if (track === "rag" && selectedPDF.size > 20 * 1024 * 1024) {
    error.textContent = "Для RAG нужен PDF размером до 20 МиБ. Выберите Solar или другой PDF.";
    error.hidden = false;
    return;
  }
  const button = document.getElementById("wizardSubmit");
  button.disabled = true;
  button.textContent = llm === "api" ? "Разбор с моделью…" : "Разбор PDF…";
  error.hidden = true;
  const progress = document.getElementById("processingProgress");
  const bar = document.getElementById("progressBar");
  const progressLabel = document.getElementById("progressLabel");
  const progressCount = document.getElementById("progressCount");
  progress.hidden = true;
  let result;
  if (llm === "api") {
    // LLM-разбор идёт через /api/parse: ключ остаётся на сервере, а результат
    // открывается на экране задания.
    progress.hidden = false;
    bar.removeAttribute("value");
    progressLabel.textContent = "Запускаем разбор с моделью…";
    progressCount.textContent = "Обработка идёт на сервере";
    result = await busy("Разбор с моделью…", () => startParse(selectedPDF, {profile: "solar", llm}));
    if (result && !result.id) {
      error.textContent = "Сервер не вернул идентификатор задания разбора.";
      error.hidden = false;
      result = null;
    }
  } else {
    progress.hidden = false;
    bar.max = 4;
    bar.value = 0;
    progressLabel.textContent = "Передача PDF…";
    progressCount.textContent = "0 из 4 этапов";
    result = await busy("Обработка PDF…", () => uploadPDFWithProgress(selectedPDF, track, event => {
      bar.value = event.completed;
      progressLabel.textContent = event.label;
      progressCount.textContent = `${event.completed} из ${event.total} этапов`;
    }));
  }
  button.disabled = false;
  button.textContent = "Разобрать PDF";
  if (!result) {
    progress.hidden = true;
    bar.max = 4;
    bar.value = 0;
    error.textContent = uploadState.textContent;
    error.hidden = false;
    return;
  }
  progress.hidden = true;
  selectedPDF = null;
  fileInput.value = "";
  if (llm === "api") {
    location.hash = `#/parse/${encodeURIComponent(result.id)}`;
  } else {
    openDocument(result);
  }
});

function openDocument(item, kind = "upload") {
  const track = item.track || app.track;
  const id = item.document_id || `${track}:${item.id}`;
  const account = currentAccount();
  if (account) {
    const updated = recordActivity(account, {
      title: kind === "demo" ? "Открыт демо-документ" : "Разобран PDF",
      detail: [id, track ? `область ${track}` : ""].filter(Boolean).join(" · "),
      kind: kind === "demo" ? "account" : "upload",
    });
    accountMenu.sync(updated || account);
  }
  location.hash = `#/doc/${encodeURIComponent(id)}?track=${encodeURIComponent(track)}`;
}

// Кнопка в тулбаре открывает мастер: шаг 1 с областью перетаскивания.
// Отдельного «загрузить» рядом больше нет, чтобы не было двух входов в один флоу.
document.getElementById("startWizard").addEventListener("click", () => {
  const error = document.getElementById("wizardError");
  error.hidden = true;
  openWizard();
});

demoButton.addEventListener("click", async () => {
  const result = await busy("Открываю демо…", () => openDemo(app.track));
  if (!result) return;
  say("");
  openDocument(result, "demo");
});

async function startApp() {
  if (app.started) return;
  app.started = true;
  app.descriptors = await tracks();
  if (!app.descriptors.length) {
    setUploadEnabled(false);
    message(
      "Не удалось загрузить интерфейс.",
      "Модули областей недоступны. Проверьте, что сервер запущен, и обновите страницу.",
    );
    return;
  }
  await loadLLMStatus();
  if (!location.hash) history.replaceState(null, "", buildHash("/", {track: app.track}));
  window.addEventListener("hashchange", route);
  await route();
}

let toastTimer = null;

function hideToast() {
  const node = document.getElementById("toast");
  clearTimeout(toastTimer);
  if (node) node.hidden = true;
}

function toast(message) {
  const node = document.getElementById("toast");
  if (!node || !message) return;
  node.replaceChildren(
    el("span", {text: message}),
    el("button", {type: "button", class: "toast-close", "aria-label": "Скрыть сообщение", text: "×",
      onclick: hideToast}),
  );
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 6000);
}

function focusAfterAuth() {
  if (document.body.classList.contains("wizard-active")) {
    document.getElementById("wizardTitle")?.focus?.({preventScroll: true});
  } else {
    focusView();
  }
}

async function enterApp(account, notice = "") {
  accountMenu.sync(account);
  authScreen.hide();
  lastPath = "";
  // Вход ведёт на мастер загрузки, а не на каталог. Прямые ссылки на
  // документ и его источник сохраняются.
  const {path} = parse();
  if (!path.startsWith("/doc/") && !path.startsWith("/parse/")) {
    history.replaceState(null, "", buildHash("/", {track: app.track}));
  }
  // Адрес не трогаем сверх этого: глубокая ссылка должна открыться и при
  // первом, и при повторном входе.
  if (!app.started) await startApp();
  else await route();
  focusAfterAuth();
  if (notice) toast(notice);
}

function leaveAccount() {
  signOutAccount();
  app.controller?.abort();
  accountMenu.sync(null);
  selectedPDF = null;
  fileInput.value = "";
  lastPath = "";
  booted = false;
  hideToast();
  fill(view);
  say("");
  setUploadEnabled(false);
  authScreen.reset();
  authScreen.show("Вы вышли из аккаунта.", false);
}

async function boot() {
  let account = null;
  try {
    account = await restoreSession();
  } catch (error) {
    // Сервер недоступен: показываем вход, а не пустой каталог.
    authScreen.show("Не удалось проверить сессию. Проверьте, что сервер запущен.", true);
    return;
  }
  if (!account) {
    document.getElementById("bootError").hidden = true;
    authScreen.show();
    window.__vinitiBooted = true;
    return;
  }
  authScreen.hide();
  accountMenu.sync(account);
  await startApp();
  window.__vinitiBooted = true;
}

// Истёкшая cookie превращает любой запрос в 401: возвращаем экран входа,
// не оставляя пользователя с пустыми таблицами.
window.addEventListener("viniti:unauthorized", () => {
  if (!isSignedIn()) return;
  signOutAccount();
  app.controller?.abort();
  accountMenu.sync(null);
  lastPath = "";
  booted = false;
  setUploadEnabled(false);
  authScreen.show("Сессия истекла. Войдите снова.", true);
});

await boot();
