import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const base = process.env.MOCK_URL || "http://127.0.0.1:8799";
const root = new URL("../../static/", import.meta.url);
const html = readFileSync(new URL("index.html", root), "utf8");
const dom = new JSDOM(html, { url: `${base}/#/`, pretendToBeVisual: true });

for (const key of [
  "window", "document", "location", "history", "HTMLElement", "Node",
  "Element", "Event", "MouseEvent", "CustomEvent", "FormData", "File",
]) {
  Object.defineProperty(globalThis, key, {value: dom.window[key], configurable: true, writable: true});
}
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = typeof input === "string" ? new URL(input, base) : input;
  return nativeFetch(url, init);
};
globalThis.dom = dom;

const dialog = document.getElementById("evidence");
dialog.showModal = function showModal() { this.open = true; };
dialog.close = function close() {
  this.open = false;
  this.dispatchEvent(new dom.window.Event("close"));
};

const results = [];
function check(name, condition, extra = "") {
  results.push({ name, ok: Boolean(condition), extra });
  console.log(`${condition ? "ok" : "FAIL"}  ${name}${condition ? "" : "  " + extra}`);
}

async function waitFor(predicate, label, timeout = 4000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return true;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error(`timeout: ${label}`);
}

const text = selector => (document.querySelector(selector)?.textContent || "").trim();
const rows = selector => [...document.querySelectorAll(selector)];

await import(new URL("app.js", root).href);

await waitFor(() => rows("#view tbody tr").length >= 8, "каталог загрузился");
await waitFor(() => text(".banner").includes("Обработано"), "баннер пакета");
check("каталог: не меньше 8 публикаций", rows("#view tbody tr").length >= 8, String(rows("#view tbody tr").length));
check("каталог: баннер пакета", text(".banner").includes("Обработано 4 из 90"), text(".banner"));
check("каталог: частично в баннере", /\d+ частично/.test(text(".banner")), text(".banner"));
check("каталог: ошибка видна", text(".banner").includes("ошибка"), text(".banner"));
check("каталог: статусы текстом", text("#view").includes("частично") && text("#view").includes("в очереди"));
check("каталог: переключатель области", document.querySelectorAll("#tracks .track").length === 1);

// Поиск по каталогу
const search = document.querySelector(".filters input[type=search]");
search.value = "D014";
search.dispatchEvent(new dom.window.Event("input"));
await waitFor(() => rows("#view tbody tr").length === 1, "поиск по каталогу");
check("каталог: поиск фильтрует", text("#view tbody").includes("D014"));

// Карточка D020
location.hash = "#/doc/rag:d020?track=rag";
await waitFor(() => text(".doc-main h1").includes("Web Retrieval"), "карточка открылась");
check("карточка: заголовок", text(".doc-main h1").includes("W-RAC"), text(".doc-main h1"));
check("карточка: вкладки", rows(".tabs .tab").length === 5);
check("карточка: ARIA вкладок", Boolean(document.querySelector('.tabs [role=tab][aria-controls="tabpanel"]')) && Boolean(document.querySelector('#tabpanel[role=tabpanel]')));
check("карточка: ссылка CSV", Boolean(document.querySelector('.doc-actions a[download$=".csv"]')));

await waitFor(() => rows("#view tbody tr.result").length > 0, "результаты загрузились");
const resultRows = rows("#view tbody tr.result");
check("результаты: страница 50 строк", resultRows.length === 50, String(resultRows.length));
check("результаты: метрика с k", text("#view tbody").includes("Recall @5"), text("#view tbody tr.result td.metric"));
check("результаты: две кнопки источника", resultRows[0].querySelectorAll("td.source-cell button").length === 2);
check("результаты: замечание текстом", text("#view tbody").includes("Шкала принята по заголовку"));
check("результаты: счётчик", text(".count").includes("408"), text(".count"));

// Раскрытие строки
const expander = resultRows[0].querySelector("button.ghost");
expander.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await new Promise(resolve => setTimeout(resolve, 50));
const detail = document.querySelector("tr.detail");
check("раскрытие: подробности появились", detail && !detail.hidden);
check("раскрытие: сырое значение", (detail?.textContent || "").includes("Сырое значение"));

// Доказательство
const evidenceButton = resultRows[0].querySelector("td.source-cell button");
evidenceButton.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => dialog.open === true, "диалог доказательства");
check("доказательство: цитата", text("#evidenceQuote").length > 0, text("#evidenceQuote"));
check("доказательство: PNG-адрес", (document.getElementById("preview").src || "").includes("/anchors/"));
check("доказательство: глубокий адрес", location.hash.includes("/source/"));

dialog.close();
await new Promise(resolve => setTimeout(resolve, 50));
check("доказательство: адрес восстановлен", !location.hash.includes("/source/"), location.hash);

// Фильтр замечаний
const issuesBox = document.querySelector(".check input");
issuesBox.checked = true;
issuesBox.dispatchEvent(new dom.window.Event("change"));
await waitFor(() => text(".count").includes("Найдено 3"), "фильтр замечаний");
check("фильтр: только с замечаниями", text(".count").includes("3"), text(".count"));
check("фильтр: в адресе issues=1", location.hash.includes("issues=1"), location.hash);
const resultsSearch = document.querySelector('input[aria-label="Поиск по результатам"]');
resultsSearch.value = "zzzz";
resultsSearch.dispatchEvent(new dom.window.Event("input"));
await waitFor(() => text("#view").includes("По этому запросу ничего не найдено"), "пустой результат");
const resetInEmpty = [...document.querySelectorAll("#view .empty button")].find(node => node.textContent === "Сбросить фильтры");
check("пустой результат: сброс фильтров", Boolean(resetInEmpty));
resetInEmpty?.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => text(".count").includes("Найдено 408"), "сброс фильтров результатов");
check("сброс фильтров: снова 408", text(".count").includes("Найдено 408"), text(".count"));

// Совместимость со старым параметром view=
location.hash = "#/doc/rag:d020?track=rag&view=issues";
await waitFor(() => text("#view").includes("Замечаний: 3"), "view=issues");
check("view=issues открывает замечания", text("#view").includes("Замечаний: 3"));

// Вкладка замечаний
[...document.querySelectorAll(".tabs .tab")].find(node => node.textContent === "Замечания")
  .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => text("#view").includes("Замечаний: 3"), "вкладка замечаний");
check("замечания: причина", text("#view").includes("Recall@10"), text("#view"));

// Вкладка сущностей
[...document.querySelectorAll(".tabs .tab")].find(node => node.textContent === "Сущности")
  .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => text("#view").includes("Записей: 3"), "вкладка сущностей");
check("сущности: конфигурации", text("#view").includes("C03"));

// Вкладка фрагментов
[...document.querySelectorAll(".tabs .tab")].find(node => node.textContent === "Фрагменты PDF")
  .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => text("#view").includes("Фрагментов:"), "вкладка фрагментов");
check("фрагменты: строки", rows("#view tbody tr").length > 0);

// Скан без OCR
location.hash = "#/doc/rag:d013?track=rag";
await waitFor(() => text("#view").includes("Скан без текстового слоя"), "карточка скана");
check("скан: предупреждение", text("#view").includes("OCR не выполнялся"));

// 28k
location.hash = "#/doc/rag:d014?track=rag";
await waitFor(() => text(".count").replace(/\s/g, "").includes("28649"), "статья с 28k");
check("28k: пагинация", text(".count").replace(/\s/g, "").includes("28649") && rows("#view tbody tr.result").length === 50);

// Глубокая ссылка на источник
location.hash = "#/doc/rag:d020/source/1001?track=rag";
await waitFor(() => dialog.open === true && text("#evidenceQuote").length > 0, "глубокая ссылка");
check("глубокая ссылка: цитата", text("#evidenceQuote").includes("Recall") || text("#evidenceQuote").length > 0, text("#evidenceQuote"));
dialog.close();
await new Promise(resolve => setTimeout(resolve, 50));
check("глубокая ссылка: track сохраняется", location.hash.includes("track=rag"), location.hash);
check("глубокая ссылка: /source/ убран", !location.hash.includes("/source/"), location.hash);

// Клик по записи из замечаний: фильтры сброшены, строка подсвечена
location.hash = "#/doc/rag:d020?track=rag";
await waitFor(() => text(".doc-main h1").includes("W-RAC"), "карточка перед замечаниями");
[...document.querySelectorAll(".tabs .tab")].find(node => node.textContent === "Замечания")
  .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => text("#view").includes("Замечаний: 3"), "замечания снова");
document.querySelector("#view .link-button").dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
await waitFor(() => Boolean(document.querySelector("#view tr.target")), "переход к записи");
check("запись: фильтр по result в URL", location.hash.includes("result=R001"), location.hash);
check("запись: результат найден", text("#view tbody").includes("R001"));
check("запись: строка подсвечена", Boolean(document.querySelector("#view tr.target")));
check("запись: подробности раскрыты", !document.querySelector("#view tr.detail").hidden);

// Зажим страницы за пределами диапазона
location.hash = "#/doc/rag:d020?track=rag&page=99";
await waitFor(() => text(".count").includes("страница 9 из 9"), "зажим страницы");
check("пагинация: страница зажата", text(".count").includes("страница 9 из 9"), text(".count"));

// Область: неизвестная и несовпадение track с document_id
location.hash = "#/?track=solar";
await waitFor(() => text("#view").includes("не подключён"), "неизвестная область");
check("область: явная ошибка", text("#view").includes("Раздел «Solar» пока не подключён"), text("#view"));
location.hash = "#/doc/rag:d020?track=solar";
await waitFor(() => text(".doc-main h1").includes("W-RAC"), "track из document_id");
check("область: document_id приоритетнее", text(".doc-main h1").includes("W-RAC"));

// Несуществующий документ
location.hash = "#/doc/rag:nope?track=rag";
await waitFor(() => text("#view").includes("Не удалось загрузить данные"), "404 документа");
check("404: ссылка в каталог", text("#view").includes("Вернуться в каталог"), text("#view"));

const failed = results.filter(item => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} проверок пройдено`);
process.exit(failed.length ? 1 : 0);