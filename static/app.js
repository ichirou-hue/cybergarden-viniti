import { el, fill } from "./dom.js";
import { tracks } from "./tracks/index.js";
import { renderCatalog } from "./catalog.js";
import { renderDocument } from "./document.js";
import { openEvidence, dismissEvidence } from "./evidence.js";
import { openDemo, getProfiles, getLLM, startParse } from "./api.js";
import { renderPassport, setPassport, setImportNotice } from "./passport.js";
import { renderParseJob } from "./parse.js";

const view = document.getElementById("view");
const statusLine = document.getElementById("status");
const form = document.getElementById("upload");
const fileInput = document.getElementById("file");
const fileName = document.getElementById("fileName");
const uploadState = document.getElementById("uploadState");
const demoButton = document.getElementById("demo");
const passportInput = document.getElementById("passport");
const passportLink = document.getElementById("passportLink");
const contextLine = document.getElementById("context");
const trackBar = document.getElementById("tracks");
const evidenceDialog = document.getElementById("evidence");
const wizard = document.getElementById("wizard");
const dropZone = document.getElementById("dropZone");
const profileStep = document.getElementById("profileStep");
let selectedPDF = null;

const FALLBACK_PROFILES = [{
  id: "solar",
  description: "Тонкоплёночные солнечные элементы. Семь таблиц шаблона участников.",
}];

const app = {descriptors: [], profiles: [], track: "rag", llm: "off", controller: null};
let booted = false;
let lastPath = "";

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

function setContext(text) {
  if (contextLine) contextLine.textContent = `Проверка научных данных · ${text}`;
}

function renderTracks(active = app.track) {
  fill(trackBar, app.descriptors.map(item => el("a", {
    class: "track",
    href: buildHash("/", {track: item.track}),
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
    extra || el("p", {}, el("a", {href: "#/", text: "К каталогу"})),
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
  // Мастер разбора - наложение поверх каталога, а не отдельный маршрут:
  // его открывает кнопка загрузки из любого места, а смена hash его закрывает.
  wizard.hidden = true;
  document.body.classList.remove("wizard-active");
  selectedPDF = null;
  fileInput.value = "";
  fileName.textContent = "Загрузить PDF";
  fileName.title = "";
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
    setContext("паспорт статьи");
    setUploadEnabled(true);
    renderTracks("");
    renderPassport(view, {say, go, patch, params});
    if (booted && pathChanged) focusView();
    booted = true;
    document.getElementById("bootError").hidden = true;
    return;
  }

  if (parseMatch) {
    const jobId = splitDocId(parseMatch[1]);
    app.controller?.abort();
    document.body.dataset.view = "parse";
    setContext("ход разбора PDF");
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
      el("p", {}, el("a", {href: "#/?track=rag", text: "Перейти в каталог RAG"})),
    );
    return;
  }
  app.track = descriptor.track;
  document.getElementById("demo").hidden = descriptor.demo === false;
  document.body.dataset.view = match ? "doc" : "catalog";
  setContext(match ? "карточка статьи" : "каталог результатов");
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
    } else if (path === "/catalog" || path === "/") {
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
      el("p", {}, el("a", {href: "#/", text: "Вернуться в каталог"})),
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
  fileName.textContent = file ? file.name : "Загрузить PDF";
  fileName.title = file ? file.name : "";
  uploadState.classList.remove("error");
  uploadState.textContent = "";
  if (file) selectPDF(file);
});

function parseProfiles() {
  return app.profiles.length ? app.profiles : FALLBACK_PROFILES;
}

function profileLabel(profile) {
  if (profile.id === "solar") return "Solar";
  if (profile.id === "custom-example") return "Custom example";
  return profile.id;
}

function profileDescription(profile) {
  return profile.error || profile.description || "Пользовательский профиль извлечения.";
}

function maxMbFor(profileId) {
  // Та же предметная граница, что и у загрузки в каталог: solar принимает 32 МиБ,
  // остальные профили - 20 МиБ.
  return profileId === "solar" ? 32 : 20;
}

function selectedProfileId() {
  return document.querySelector('input[name="wizardProfile"]:checked')?.value
    || parseProfiles()[0]?.id || "solar";
}

function setLLMMode(mode) {
  app.llm = mode === "api" ? "api" : "off";
  document.querySelectorAll('input[name="toolbarLLM"]').forEach(input => {
    input.checked = input.value === app.llm;
  });
  const state = document.getElementById("wizardLLMState");
  const note = document.getElementById("wizardLLMNote");
  if (state) state.textContent = app.llm === "api" ? "LLM включен" : "LLM выключен";
  if (note) note.textContent = app.llm === "api"
    ? "Фрагменты документа будут отправлены в настроенную API-модель. Режим задан единым переключателем вверху страницы."
    : "Разбор выполняется без передачи фрагментов документа модели. Режим задан единым переключателем вверху страницы.";
}

document.getElementById("toolbarLLM").addEventListener("change", event => {
  if (event.target.matches('input[name="toolbarLLM"]')) setLLMMode(event.target.value);
});
let profilesState = "loading";
let profilesError = "";

async function loadProfiles() {
  try {
    const data = await getProfiles();
    app.profiles = Array.isArray(data?.profiles) ? data.profiles : [];
    profilesState = app.profiles.length ? "ok" : "fallback";
    profilesError = app.profiles.length ? "" : "сервер вернул пустой список профилей";
  } catch (error) {
    app.profiles = [];
    profilesState = "fallback";
    profilesError = error.message;
  }
  if (!wizard.hidden) renderWizard();
}

async function loadLLMStatus() {
  const input = document.querySelector('input[name="toolbarLLM"][value="api"]');
  if (!input) return;
  try {
    const status = await getLLM();
    input.disabled = !status?.available;
    if (!status?.available && app.llm === "api") setLLMMode("off");
    const note = document.getElementById("wizardLLMNote");
    if (note && !status?.available) {
      note.textContent = status?.configured
        ? "LLM отключён администратором сервера."
        : "LLM не настроен: нужны OPENAI_API_KEY и OPENAI_MODEL.";
    }
  } catch {
    // The server remains the final authority; do not disable the control on
    // a transient status-probe failure.
  }
}

function renderProfileNotice() {
  const notice = document.getElementById("profileNotice");
  if (profilesState !== "fallback") {
    notice.hidden = true;
    notice.replaceChildren();
    return;
  }
  notice.hidden = false;
  fill(notice,
    el("strong", {text: "Профили не загрузились."}),
    el("span", {text: ` ${profilesError || "Сервер не вернул список"}. Пока доступен только встроенный профиль Solar.`}),
    el("button", {type: "button", class: "link-button", text: "Повторить", onclick: () => loadProfiles()}));
}

function openWizard() {
  wizard.hidden = false;
  document.body.classList.add("wizard-active");
  renderWizard();
}

function closeWizard() {
  wizard.hidden = true;
  document.body.classList.remove("wizard-active");
  selectedPDF = null;
  fileInput.value = "";
  fileName.textContent = "Загрузить PDF";
  fileName.title = "";
  renderWizard();
}

function renderWizard() {
  const ready = !!selectedPDF;
  const profiles = parseProfiles();
  const known = profiles.some(profile => profile.id === app.track);
  const current = known ? app.track : profiles[0]?.id;
  dropZone.hidden = ready;
  profileStep.hidden = !ready;
  document.getElementById("wizardStep").textContent = ready ? "Шаг 2 из 2 · Профиль" : "Шаг 1 из 2 · Документ";
  document.getElementById("wizardTitle").textContent = ready ? "Как обработать статью?" : "Загрузите научную статью";
  document.getElementById("wizardHint").textContent = ready ? "Выберите профиль извлечения. Настройка LLM наследуется из переключателя вверху страницы." : "Перетащите PDF в область ниже или выберите файл на устройстве.";
  document.getElementById("selectedFile").textContent = ready ? selectedPDF.name : "";
  renderProfileNotice();
  setLLMMode(app.llm);
  fill(document.getElementById("profileChoices"), ...profiles.map(profile => el("label", {class: "profile-choice"},
    el("input", {type: "radio", name: "wizardProfile", value: profile.id, checked: profile.id === current, disabled: !!profile.error}),
    el("span", {},
      el("strong", {text: profileLabel(profile)}),
      el("small", {text: profileDescription(profile)})))));
}

function selectPDF(file) {
  const error = document.getElementById("wizardError");
  error.hidden = true;
  openWizard();
  if (!file || !(file.name.toLowerCase().endsWith(".pdf") || file.type === "application/pdf")) {
    error.textContent = "Выберите файл PDF.";
    error.hidden = false;
    return;
  }
  const maxMb = maxMbFor(selectedProfileId());
  if (file.size > maxMb * 1024 * 1024) {
    error.textContent = `Файл больше ${maxMb} МиБ. Выберите PDF меньшего размера.`;
    error.hidden = false;
    return;
  }
  selectedPDF = file;
  renderWizard();
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
  selectedPDF = null; fileInput.value = ""; renderWizard();
});
document.getElementById("wizardCancel").addEventListener("click", closeWizard);
document.getElementById("wizardSubmit").addEventListener("click", async () => {
  const profile = document.querySelector('input[name="wizardProfile"]:checked')?.value;
  const llm = app.llm;
  if (!selectedPDF || !profile) return;
  const button = document.getElementById("wizardSubmit");
  button.disabled = true;
  button.textContent = llm === "api" ? "Разбор с LLM…" : "Разбор PDF…";
  const error = document.getElementById("wizardError"); error.hidden = true;
  const maxMb = maxMbFor(profile);
  if (selectedPDF.size > maxMb * 1024 * 1024) {
    error.textContent = `Файл больше ${maxMb} МиБ. Выберите PDF меньшего размера.`;
    error.hidden = false;
    button.disabled = false;
    button.textContent = "Разобрать PDF";
    return;
  }
  const result = await busy(llm === "api" ? "Разбор с LLM…" : "Разбор PDF…",
    () => startParse(selectedPDF, {profile, llm}));
  button.disabled = false;
  button.textContent = "Разобрать PDF";
  if (!result) { error.textContent = uploadState.textContent; error.hidden = false; return; }
  if (!result.id) { error.textContent = "Сервер не вернул идентификатор задания разбора."; error.hidden = false; return; }
  selectedPDF = null;
  fileInput.value = "";
  fileName.textContent = "Загрузить PDF";
  fileName.title = "";
  location.hash = `#/parse/${encodeURIComponent(result.id)}`;
});

function openDocument(item) {
  const track = item.track || app.track;
  const id = item.document_id || `${track}:${item.id}`;
  location.hash = `#/doc/${encodeURIComponent(id)}?track=${encodeURIComponent(track)}`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    // Кнопка в тулбаре открывает тот же мастер: шаг 1 с областью перетаскивания.
    openWizard();
    const error = document.getElementById("wizardError");
    error.textContent = "Выберите PDF, чтобы продолжить.";
    error.hidden = false;
    return;
  }
  selectPDF(file);
});

demoButton.addEventListener("click", async () => {
  const result = await busy("Открываю демо…", () => openDemo(app.track));
  if (!result) return;
  say("");
  openDocument(result);
});

  app.descriptors = await tracks();
await loadLLMStatus();
await loadProfiles();
if (!app.descriptors.length) {
  setUploadEnabled(false);
  message(
    "Не удалось загрузить интерфейс.",
    "Модули областей недоступны. Запустите сервер: .venv/bin/python tools/mock_api.py",
  );
} else {
  if (!location.hash) history.replaceState(null, "", buildHash("/", {track: app.track}));
  window.addEventListener("hashchange", route);
  await route();
}
