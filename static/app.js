import { el, fill } from "./dom.js";
import { tracks } from "./tracks/index.js";
import { renderCatalog } from "./catalog.js";
import { renderDocument } from "./document.js";
import { openEvidence, dismissEvidence } from "./evidence.js";
import { uploadPDF, openDemo } from "./api.js";
import { renderPassport, setPassport, setImportNotice } from "./passport.js";

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

const app = {descriptors: [], track: "rag", controller: null};
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
  const docId = match ? splitDocId(match[1]) : "";
  const pathChanged = path !== lastPath;
  lastPath = path;
  say("");
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
      "Обработчик области ещё не добавлен. Каталог RAG работает; Solar появится отдельным дескриптором.",
      el("p", {}, el("a", {href: "#/?track=rag", text: "Перейти в каталог RAG"})),
    );
    return;
  }
  app.track = descriptor.track;
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
    } else if (path === "/") {
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
    uploadState.textContent = "Сначала выберите PDF";
    return;
  }
  const result = await busy("Обработка PDF…", () => uploadPDF(file, app.track));
  if (!result) return;
  fileInput.value = "";
  fileName.textContent = "Загрузить PDF";
  fileName.title = "";
  say("");
  openDocument(result);
});

demoButton.addEventListener("click", async () => {
  const result = await busy("Открываю демо…", () => openDemo(app.track));
  if (!result) return;
  say("");
  openDocument(result);
});

app.descriptors = await tracks();
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
