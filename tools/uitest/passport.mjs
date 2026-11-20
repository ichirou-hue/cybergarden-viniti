import { chromium } from "playwright-core";

// Проверки экрана «Паспорт статьи» (#/passport) в реальном браузере.
// Требует запущенный мок: ../../.venv/bin/python ../../tools/mock_api.py --port 8799
// Запуск: node passport.mjs   (MOCK_URL=http://127.0.0.1:9000 node passport.mjs)

const base = process.env.MOCK_URL || "http://127.0.0.1:8799";
const results = [];
function check(name, ok, extra = "") {
  results.push({name, ok});
  console.log(`${ok ? "ok" : "FAIL"}  ${name}${ok ? "" : "  " + extra}`);
}
async function step(name, task) {
  try {
    await task();
  } catch (error) {
    check(name, false, error.message);
  }
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || chromium.executablePath(),
});

const deep = {
  title: "Паспорт статьи A",
  topic: "Тема A",
  results: [
    {
      id: "A1", metric: "Recall", value: 0.81, unit: "ratio",
      validation_status: "passed", extraction_status: "extracted", issues: [],
      source_page: 2, source_fragment: 'Recall was 0.81.\nСтрока "в кавычках"',
    },
    {
      id: "A2", metric: "Precision", value: 0.62, unit: "ratio",
      validation_status: "review", extraction_status: "extracted",
      issues: ["Расхождение значений"],
    },
  ],
};

const solar = {
  publications: [{publication_id: "P1", title: "Solar paper"}],
  materials: [{publication_id: "P1", material_id: "M1", material_name_raw: "MAPbI3"}],
  devices: [{publication_id: "P1", device_id: "D1", device_label: "Device A", architecture: "n-i-p", absorber_material_id: "M1"}],
  layers: [{publication_id: "P1", device_id: "D1", layer_order: 1, layer_material: "TiO2"}],
  measurements: [{publication_id: "P1", measurement_id: "MS1", spectrum: "AM1.5G", scan_direction: "forward"}],
  results: [
    {publication_id: "P1", result_id: "R1", device_id: "D1", measurement_id: "MS1", metric_name: "PCE",
      raw_value: "21.3", raw_unit: "%", normalized_value: "21.3", normalized_unit: "%",
      extraction_status: "extracted", validation_status: "passed", source_page: 5, source_fragment: "PCE 21.3%"},
    {publication_id: "P1", result_id: "R2", device_id: "D1", measurement_id: "MS1", metric_name: "Voc",
      raw_value: "1.10", raw_unit: "V", normalized_value: "1.10", normalized_unit: "V",
      extraction_status: "extracted", validation_status: "passed", source_page: 5, source_fragment: "Voc 1.10 V"},
    {publication_id: "P1", result_id: "R3", device_id: "D1", measurement_id: "MS1", metric_name: "FF",
      raw_value: "0.8", raw_unit: "", normalized_value: "0.8", normalized_unit: "",
      extraction_status: "extracted", validation_status: "passed", source_page: 6, source_fragment: "FF 0.8"},
  ],
  issues: [{publication_id: "P1", issue_id: "I1", check_name: "unit_mismatch",
    description: "Единица не совпадает", record_ids: "R1"}],
};

const context = await browser.newContext({viewport: {width: 1366, height: 900}, acceptDownloads: true});
const page = await context.newPage();
let alerted = false;
page.on("dialog", dialog => { alerted = true; dialog.dismiss(); });
page.on("pageerror", error => check("нет ошибок страницы", false, error.message));

async function open(path) {
  await page.goto(`${base}/${path}`, {waitUntil: "networkidle"});
  await page.waitForTimeout(250);
}
async function importJSON(data, name = "passport.json") {
  await page.setInputFiles("#passport", {
    name, mimeType: "application/json", buffer: Buffer.from(JSON.stringify(data)),
  });
  await page.waitForTimeout(400);
}
async function stats() {
  return (await page.locator("#view .stats").innerText()).replace(/\s+/g, " ").trim();
}
async function rows() {
  return page.locator("#view tbody tr").count();
}
function parseCSV(text) {
  text = text.replace(/^\uFEFF/, "");
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { cell += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(cell); cell = ""; }
    else if (char === "\n") {
      row.push(cell.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      cell = "";
    } else cell += char;
  }
  if (cell || row.length) { row.push(cell.replace(/\r$/, "")); rows.push(row); }
  return {header: rows[0] || [], rows: rows.slice(1).filter(item => item.some(Boolean))};
}

async function csvText() {
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator('#view button:has-text("Скачать CSV")').click(),
  ]);
  const target = `/tmp/viniti-passport-${Date.now()}.csv`;
  await download.saveAs(target);
  const {readFileSync} = await import("node:fs");
  return {name: download.suggestedFilename(), text: readFileSync(target, "utf8")};
}

/* 1. Демо */
await step("демо", async () => {
  await open("#/passport");
  check("демо: 2 строки", await rows() === 2, String(await rows()));
  check("демо: сводка 2 / 1 / 1", (await stats()).includes("Извлечено результатов 2") &&
    (await stats()).includes("Прошли проверку 1") && (await stats()).includes("Требуют разбора 1"), await stats());
  check("демо: предупреждение", (await page.locator("#view").innerText()).includes("Демонстрационные значения"));
  check("демо: CSV выключен", await page.locator('#view button:has-text("Скачать CSV")').isDisabled());
  check("демо: PDF выключен", await page.locator('#view input[type="file"]').isDisabled());
  check("демо: значение в таблице", (await page.locator("#view tbody tr").first().innerText()).includes("1.2 с"));
  check("демо: карточка первой строки", (await page.locator("#detail").innerText()).includes("Среднее время"));
  check("демо: статус «Пройдена»", (await page.locator("#detail .badge").innerText()) === "Пройдена");
});

/* 2. Фильтры и выбор строки */
await step("фильтры", async () => {
  await open("#/passport");
  await page.selectOption('#view select[aria-label="Проверка"]', "review");
  await page.waitForTimeout(200);
  check("фильтр по статусу: 1 строка", await rows() === 1, String(await rows()));
  check("фильтр по статусу: замечание в карточке", (await page.locator("#detail").innerText()).includes("не может быть отрицательным"));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "Эксперимент B");
  await page.waitForTimeout(200);
  check("поиск по контексту", (await page.locator("#view").innerText()).includes("Показано 1 из 2"));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "нет такого");
  await page.waitForTimeout(200);
  check("пустой поиск: сообщение", (await page.locator("#view").innerText()).includes("Результаты не найдены"));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "");
  await page.selectOption('#view select[aria-label="Проверка"]', "");
  await page.waitForTimeout(200);
  await page.locator("#view tbody tr").nth(1).locator("button").click();
  check("выбор строки: aria-pressed", await page.locator("#view tbody tr").nth(1).locator("button").getAttribute("aria-pressed") === "true");
  check("выбор строки: подсветка", ((await page.locator("#view tbody tr").nth(1).getAttribute("class")) || "").includes("selected"));
  check("выбор строки: карточка", (await page.locator("#detail").innerText()).includes("Число наблюдений"));
});

/* 3. Общий формат паспорта */
await step("общий формат", async () => {
  await open("#/passport");
  await importJSON(deep);
  check("общий формат: 2 строки", await rows() === 2, String(await rows()));
  check("общий формат: сводка 2 / 1 / 1", (await stats()).includes("Прошли проверку 1") && (await stats()).includes("Требуют разбора 1"), await stats());
  check("общий формат: id в таблице", (await page.locator("#view tbody").innerText()).includes("Recall"));
  check("общий формат: value/unit видны", (await page.locator("#view tbody tr").first().innerText()).includes("0.81 ratio"),
    (await page.locator("#view tbody tr").first().innerText()).replace(/\s+/g, " "));
  check("общий формат: value в карточке", (await page.locator("#detail .value").innerText()).includes("0.81") &&
    !(await page.locator("#detail .value").innerText()).endsWith("—"),
    await page.locator("#detail .value").innerText());
  const csv = await csvText();
  check("общий формат: CSV скачался", csv.name === "results.csv", csv.name);
  const parsed = parseCSV(csv.text);
  check("общий формат: CSV только passed", parsed.rows.some(row => row[parsed.header.indexOf("result_id")] === "A1") &&
    !parsed.rows.some(row => row[parsed.header.indexOf("result_id")] === "A2"));
  check("общий формат: CSV 29 колонок", parsed.header.length === 29, String(parsed.header.length));
  check("общий формат: CSV строки той же ширины", parsed.rows.length > 0 &&
    parsed.rows.every(row => row.length === parsed.header.length), parsed.rows.map(row => row.length).join(","));
  const a1 = parsed.rows.find(row => row[parsed.header.indexOf("result_id")] === "A1") || [];
  const valueColumns = ["metric_value", "normalized_value"].map(name => a1[parsed.header.indexOf(name)] || "");
  check("общий формат: CSV содержит value в колонке значения", valueColumns.some(value => /0[.,]81/.test(value)),
    valueColumns.join(" | "));
  check("общий формат: CSV не ломается кавычками/переводом",
    (a1[parsed.header.indexOf("source_fragment")] || "").includes('"') &&
    (a1[parsed.header.indexOf("source_fragment")] || "").includes("\n"),
    a1[parsed.header.indexOf("source_fragment")] || "empty");
});

/* 4. Невалидный импорт */
await step("ошибки импорта", async () => {
  await open("#/passport");
  await importJSON(deep);
  const beforeTitle = "Паспорт статьи A";
  for (const [name, body] of [
    ["empty", "{}"], ["array", "[]"], ["bad", "{ not json"],
    ["results-string", '{"results":"x"}'], ["results-null", '{"results":[null]}'],
  ]) {
    await page.setInputFiles("#passport", {name: `${name}.json`, mimeType: "application/json", buffer: Buffer.from(body)});
    await page.waitForTimeout(300);
    const message = `${await page.locator("#status").innerText()} ${await page.locator("#view").innerText()}`;
    check(`ошибка ${name}: сообщение`, /Не удалось|нужен массив|Ожидается|JSON/i.test(message), message.trim().slice(0, 120));
    check(`ошибка ${name}: данные не потеряны`, (await page.locator("#view").innerText()).includes(beforeTitle));
  }
  await importJSON(deep, "recover.json");
  check("после ошибок приложение работает", (await page.locator("#view").innerText()).includes("Паспорт статьи A"));
});

/* 5. RAG-экспорт с facts */
await step("RAG facts", async () => {
  await open("#/passport");
  await importJSON({title: "RAG export", topic: "Track", facts: [
    {id: "F1", metric: "Band gap", value: "1.55", unit: "eV", evidence: {page: 4, quote: "The band gap was 1.55 eV."}},
    {id: "F2", metric: "Efficiency", value: "21.3", unit: "%", evidence: {page: 7, quote: "Efficiency 21.3%."}},
  ]});
  check("facts: 2 строки", await rows() === 2, String(await rows()));
  check("facts: значение и единица", (await page.locator("#view tbody").innerText()).includes("1.55 eV"),
    (await page.locator("#view tbody").innerText()).replace(/\s+/g, " "));
  check("facts: страница и цитата", (await page.locator("#detail").innerText()).includes("стр. 4") &&
    (await page.locator("#detail").innerText()).includes("band gap"), (await page.locator("#detail").innerText()).slice(0, 160));
});

/* 6. Семь таблиц Solar */
await step("solar", async () => {
  await open("#/passport");
  await importJSON(solar);
  check("solar: 3 строки", await rows() === 3, String(await rows()));
  check("solar: контекст прибора", (await page.locator("#view tbody").innerText()).includes("Device A"));
  check("solar: материалы и слои в карточке", (await page.locator("#detail").innerText()).includes("MAPbI3") &&
    (await page.locator("#detail").innerText()).includes("TiO2"), (await page.locator("#detail").innerText()).slice(0, 240));
  check("solar: замечание в статусе", (await page.locator("#view tbody").innerText()).includes("На разборе"));
  const csv = await csvText();
  check("solar: CSV только passed", csv.text.includes('"R2"') && csv.text.includes('"R3"') && !csv.text.includes('"R1"'));
  check("solar: CSV со значениями", csv.text.includes("1.10") && csv.text.includes("0.8"));
});

/* 7. Повторный импорт и локальный PDF */
await step("повторный импорт и PDF", async () => {
  await open("#/passport");
  const first = {
    title: "Паспорт A", topic: "A",
    results: [
      {id: "A1", publication_id: "PUB-A", metric: "MetricA", value: 1, unit: "u", validation_status: "passed", extraction_status: "extracted"},
      {id: "A2", publication_id: "PUB-B", metric: "MetricB", value: 2, unit: "u", validation_status: "passed", extraction_status: "extracted", source_page: 9},
    ],
  };
  await importJSON(first, "a.json");
  await page.locator("#view tbody tr").nth(1).locator("button").click();
  const pdfResponse = await context.request.get(`${base}/api/documents/rag%3Ad020/pdf`);
  const pdf = await pdfResponse.body();
  await page.setInputFiles('#view input[type="file"]', {name: "a.pdf", mimeType: "application/pdf", buffer: pdf});
  await page.waitForTimeout(300);
  const link = page.locator('#detail a:has-text("Открыть страницу PDF")');
  const href = await link.getAttribute("href");
  check("PDF: ссылка на страницу", /^blob:.*#page=9$/.test(href || ""), String(href));
  const popupPromise = page.waitForEvent("popup", {timeout: 3000}).catch(() => null);
  await link.click();
  const popup = await popupPromise;
  check("PDF: страница открывается", Boolean(popup));
  if (popup) await popup.close();
  await page.setInputFiles('#view input[type="file"]', {name: "b.pdf", mimeType: "application/pdf", buffer: pdf});
  await page.waitForTimeout(300);
  const href2 = await page.locator('#detail a:has-text("Открыть страницу PDF")').getAttribute("href");
  check("PDF: повторная привязка меняет ссылку", href2 !== href, String(href2));
  const oldAlive = await page.evaluate(async url => {
    try { return (await fetch(url.replace(/#page=9$/, ""))).ok; } catch { return false; }
  }, href);
  check("PDF: старая ссылка отозвана", !oldAlive);
  await importJSON({title: "Паспорт B", results: [
    {id: "B1", publication_id: "PUB-A", metric: "MetricC", value: 3, unit: "u", validation_status: "passed", extraction_status: "extracted"},
  ]}, "b.json");
  check("повторный импорт: новая шапка", (await page.locator("#view").innerText()).includes("Паспорт B"));
  check("повторный импорт: одна строка", await rows() === 1, String(await rows()));
  check("повторный импорт: старая карточка не протекла", !(await page.locator("#detail").innerText()).includes("MetricB"));
  check("повторный импорт: старый PDF не протёк", await page.locator('#detail a:has-text("Открыть страницу PDF")').count() === 0);
});

/* 8. Навигация и hash-роутинг */
await step("навигация", async () => {
  await open("#/?track=rag&q=D014");
  await page.click('a.nav-link:has-text("Паспорт")');
  await page.waitForTimeout(350);
  check("навигация: каталог → паспорт", page.url().endsWith("#/passport"), page.url());
  await page.goBack();
  await page.waitForTimeout(500);
  check("навигация: назад в каталог", page.url().includes("q=D014"), page.url());
  check("навигация: фильтр восстановлен", await page.inputValue('input[aria-label="Поиск публикации"]') === "D014");
  await open("#/doc/rag:d020?track=rag");
  check("навигация: прямая ссылка с двоеточием", (await page.locator("#view h1").innerText()).includes("Web Retrieval-Aware"),
    (await page.locator("#view h1").innerText()).slice(0, 80));
  await page.click('a.nav-link:has-text("Паспорт")');
  await page.waitForTimeout(350);
  check("навигация: карточка → паспорт", page.url().endsWith("#/passport"), page.url());
  await page.setInputFiles("#passport", {name: "from-catalog.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(deep))});
  await page.waitForTimeout(500);
  check("навигация: импорт с любой страницы ведёт в паспорт", page.url().endsWith("#/passport") &&
    (await page.locator("#view").innerText()).includes("Паспорт статьи A"), page.url());
});

/* 9. XSS в данных паспорта */
await step("xss", async () => {
  await open("#/passport");
  await importJSON({
    title: '<img src=x onerror=alert(1)>',
    results: [{id: "<b>R</b>", metric: "<img src=y onerror=alert(2)>", value: 1, unit: "u",
      validation_status: "passed", extraction_status: "extracted", context: "<script>alert(3)</script>"}],
  });
  await page.waitForTimeout(200);
  check("xss: alert не сработал", !alerted);
  check("xss: тег не создан", await page.locator("#view img").count() === 0 && await page.locator("#view script").count() === 0);
});

/* 10. Мобильная ширина */
await step("мобильная ширина", async () => {
  const mobile = await browser.newContext({viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
  const mp = await mobile.newPage();
  await mp.goto(`${base}/#/passport`, {waitUntil: "networkidle"});
  await mp.waitForTimeout(400);
  const overflow = await mp.evaluate(() => ({page: document.documentElement.scrollWidth, viewport: innerWidth}));
  check("мобилка: нет горизонтального overflow", overflow.page <= overflow.viewport, JSON.stringify(overflow));
  const table = await mp.locator("#view .table-wrap, #view .results").first().evaluate(el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth}));
  check("мобилка: таблица скроллится внутри", table.scrollWidth > table.clientWidth, JSON.stringify(table));
  const actions = await mp.locator("#view .hero .actions").evaluate(el => [...el.children].map(node => node.getBoundingClientRect().right));
  check("мобилка: кнопки в экране", actions.every(right => right <= overflow.viewport), actions.join(","));
  await mobile.close();
});

await browser.close();
const failed = results.filter(item => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} проверок пройдено`);
if (failed.length) {
  console.log("Провалены:");
  for (const item of failed) console.log(`  - ${item.name}`);
}
process.exit(failed.length ? 1 : 0);
