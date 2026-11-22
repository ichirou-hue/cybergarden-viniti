// Регрессия мастера для универсального режима модели и лимитов каталога.
// Проверяет то, что видно пользователю: какой профиль уходит на сервер,
// какие размеры PDF проходят, и что отклонённая замена файла не оставляет
// прежний PDF выбранным.
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const root = new URL("../../static/", import.meta.url);
const html = readFileSync(new URL("index.html", root), "utf8");
const MiB = 1024 * 1024;

const results = [];
function check(name, ok, extra = "") {
  results.push({name, ok: Boolean(ok)});
  console.log(`${ok ? "ok" : "FAIL"}  ${name}${ok ? "" : "  " + extra}`);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: {"Content-Type": "application/json; charset=utf-8"},
  });
}

function pdf(size, name = "paper.pdf") {
  const file = new File(["%PDF-1.4"], name, {type: "application/pdf"});
  // Настоящий буфер на 33 МиБ в jsdom не нужен: важен только size.
  Object.defineProperty(file, "size", {value: size, configurable: true});
  return file;
}

let scenario = 0;

// Каждый сценарий поднимает чистый документ и заново исполняет app.js:
// query-строка обходит кеш модуля, а зависимые модули без состояния.
async function openWizard({llm = {configured: true, enabled: true, available: true}} = {}) {
  scenario += 1;
  const dom = new JSDOM(html, {url: "http://localhost/#/", pretendToBeVisual: true});
  for (const key of ["window", "document", "location", "history", "HTMLElement",
    "Node", "Element", "Event", "File", "CustomEvent", "FormData"]) {
    Object.defineProperty(globalThis, key, {value: dom.window[key], configurable: true});
  }
  dom.window.localStorage.setItem("viniti.profile.v1:test-user", JSON.stringify({
    id: "test-user", name: "Тест", email: "test@viniti.ru", org: "", role: "",
    createdAt: 1763769600000, stats: {docs: 0, actions: 0}, activity: [],
  }));
  const requests = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    const method = (init.method || "GET").toUpperCase();
    if (url.includes("/api/auth/me")) {
      return json({user: {id: "test-user", username: "test@viniti.ru", created_at: 1763769600},
        csrf_token: "mock-csrf-token", expires_at: 4102444800});
    }
    if (url.includes("/api/llm")) return json(llm);
    if (url.includes("/api/documents/stream")) {
      requests.push({url, method, headers: new Headers(init.headers)});
      const events = [
        {completed: 4, total: 4, label: "Готово",
          result: {track: "solar", id: "D999", document_id: "solar:D999"}},
      ];
      return new Response(events.map(event => JSON.stringify(event) + "\n").join(""),
        {status: 200, headers: {"Content-Type": "application/x-ndjson; charset=utf-8"}});
    }
    requests.push({url, method, headers: new Headers(init.headers)});
    if (url.endsWith("/api/parse")) return json({id: "0123456789abcdef", status: "queued"}, 202);
    return json({error: "нет данных в проверке"}, 404);
  };
  await import(new URL(`app.js?scenario=${scenario}`, root).href);
  const $ = id => dom.window.document.getElementById(id);
  const choose = file => {
    Object.defineProperty($("file"), "files", {value: [file], configurable: true});
    $("file").dispatchEvent(new dom.window.Event("change"));
  };
  const setModel = on => {
    $("wizardLLM").checked = on;
    $("wizardLLM").dispatchEvent(new dom.window.Event("change"));
  };
  const setTrack = track => {
    const input = dom.window.document.querySelector(`[name="wizardProfile"][value="${track}"]`);
    input.checked = true;
    input.dispatchEvent(new dom.window.Event("change", {bubbles: true}));
  };
  const submit = async () => {
    $("wizardSubmit").click();
    await new Promise(resolve => setTimeout(resolve, 60));
  };
  const parseRequests = () => requests.filter(item => item.url.endsWith("/api/parse"));
  const uploadRequests = () => requests.filter(item => item.url.includes("/api/documents"));
  await new Promise(resolve => setTimeout(resolve, 20));
  return {dom, $, choose, setModel, setTrack, submit, parseRequests, uploadRequests};
}

// Сценарий 1: модель принимает 33 МиБ и отправляет универсальный профиль,
// не подменяя выбранную область каталога.
{
  const app = await openWizard();
  app.choose(pdf(33 * MiB, "big.pdf"));
  check("модель: PDF 33 МиБ принят на шаге выбора",
    app.$("profileStep").hidden === false && app.$("wizardError").hidden === true,
    app.$("wizardError").textContent);
  app.setModel(true);
  const rag = app.dom.window.document.querySelector('[name="wizardProfile"][value="rag"]');
  check("модель: RAG остаётся выбранным и доступным", rag.checked && !rag.disabled);
  check("модель: подпись говорит об универсальном профиле",
    /универсальн/.test(app.$("wizardLLMNote").textContent), app.$("wizardLLMNote").textContent);
  check("модель: подсказка про каталог без модели",
    /каталог без модели/.test(app.$("wizardHint").textContent), app.$("wizardHint").textContent);
  await app.submit();
  const [request] = app.parseRequests();
  check("модель: запрос /api/parse отправлен", Boolean(request), String(app.parseRequests().length));
  check("модель: X-Profile universal",
    request?.headers.get("X-Profile") === "universal", request?.headers.get("X-Profile"));
  check("модель: X-LLM api", request?.headers.get("X-LLM") === "api", request?.headers.get("X-LLM"));
  check("модель: загрузки в каталог не было", app.uploadRequests().length === 0);
  check("модель: ошибки лимита нет", app.$("wizardError").hidden === true, app.$("wizardError").textContent);
}

// Сценарий 2: без модели действуют лимиты каталога, а обычная загрузка цела.
{
  const app = await openWizard();
  app.choose(pdf(33 * MiB, "big.pdf"));
  check("каталог: 33 МиБ проходят выбор файла без модели", app.$("profileStep").hidden === false);
  check("каталог: по умолчанию выбран RAG",
    app.dom.window.document.querySelector('[name="wizardProfile"]:checked')?.value === "rag");
  await app.submit();
  check("каталог: RAG 33 МиБ отклонён с лимитом 20 МиБ",
    app.$("wizardError").hidden === false && /20 МиБ/.test(app.$("wizardError").textContent),
    app.$("wizardError").textContent);
  check("каталог: после отказа RAG запроса не было",
    app.parseRequests().length === 0 && app.uploadRequests().length === 0);
  app.setTrack("solar");
  await app.submit();
  check("каталог: Solar 33 МиБ отклонён с лимитом 32 МиБ",
    /32 МиБ/.test(app.$("wizardError").textContent), app.$("wizardError").textContent);
  check("каталог: после отказа Solar запроса не было",
    app.parseRequests().length === 0 && app.uploadRequests().length === 0);
  app.choose(pdf(19 * MiB, "small.pdf"));
  await app.submit();
  const [upload] = app.uploadRequests();
  check("каталог: Solar 19 МиБ уходит обычной загрузкой",
    Boolean(upload) && upload.headers.get("X-Track") === "solar",
    upload?.headers.get("X-Track"));
  check("каталог: модель не включалась", app.parseRequests().length === 0);
}

// Сценарий 3: отклонённая замена файла сбрасывает прежний PDF, а не оставляет
// его готовым к отправке.
{
  const app = await openWizard();
  app.choose(pdf(1 * MiB, "valid.pdf"));
  check("сброс: корректный файл выбран", app.$("profileStep").hidden === false);
  app.choose(new File(["text"], "notes.txt", {type: "text/plain"}));
  check("сброс: не-PDF отклонён", /PDF/.test(app.$("wizardError").textContent),
    app.$("wizardError").textContent);
  check("сброс: прежний файл снят, шаг вернулся к выбору",
    app.$("profileStep").hidden === true && app.$("selectedFile").textContent === "");
  await app.submit();
  check("сброс: после не-PDF запроса нет",
    app.parseRequests().length === 0 && app.uploadRequests().length === 0);
  app.choose(pdf(41 * MiB, "huge.pdf"));
  check("сброс: файл больше 40 МиБ отклонён",
    /40 МиБ/.test(app.$("wizardError").textContent), app.$("wizardError").textContent);
  await app.submit();
  check("сброс: после 41 МиБ запроса нет",
    app.parseRequests().length === 0 && app.uploadRequests().length === 0);
  app.setModel(true);
  app.choose(pdf(41 * MiB, "huge.pdf"));
  await app.submit();
  check("сброс: 41 МиБ не проходит и с моделью",
    app.parseRequests().length === 0 && app.$("profileStep").hidden === true);
  app.choose(pdf(38 * MiB, "model.pdf"));
  check("сброс: 38 МиБ снова принимается", app.$("profileStep").hidden === false);
  await app.submit();
  check("сброс: 38 МиБ с моделью уходит в /api/parse",
    app.parseRequests().length === 1
      && app.parseRequests()[0].headers.get("X-Profile") === "universal");
}

// Сценарий 4: подпись универсального профиля на экране задания.
{
  scenario += 1;
  const dom = new JSDOM(html, {url: "http://localhost/#/parse/0123456789abcdef", pretendToBeVisual: true});
  for (const key of ["window", "document", "location", "history", "HTMLElement", "Node",
    "Element", "Event", "File", "CustomEvent"]) {
    Object.defineProperty(globalThis, key, {value: dom.window[key], configurable: true});
  }
  globalThis.fetch = async input => {
    const url = String(input);
    if (url.endsWith("/api/parse/0123456789abcdef")) return json({
      id: "0123456789abcdef", status: "ok", profile: "universal", parser: "pdfplumber",
      ocr: "off", llm: "off", filename: "article.pdf", created_at: 1763769600,
      updated_at: 1763769605, elapsed_seconds: 5,
      summary: {pages: 1, sources: 0, candidates: 0, warnings: 0},
    });
    if (url.endsWith("/file/result.json")) {
      return json({document: {pages: 1}, sources: [], candidates: []});
    }
    return json({error: "нет файла"}, 404);
  };
  const {renderParseJob} = await import(new URL(`parse.js?scenario=${scenario}`, root).href);
  const view = dom.window.document.getElementById("view");
  await renderParseJob(view, {jobId: "0123456789abcdef", signal: new AbortController().signal, say: () => {}});
  const terms = [...view.querySelectorAll("dt")];
  const area = terms.find(node => node.textContent.trim() === "Область");
  const value = area?.nextElementSibling?.textContent.trim();
  check("экран задания: подпись области по-русски", value === "универсальный профиль", value);
  check("экран задания: латинский идентификатор не показан",
    !view.textContent.includes("Областьuniversal") && value !== "universal", value);
}

const failed = results.filter(item => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} проверок пройдено`);
process.exit(failed.length ? 1 : 0);
