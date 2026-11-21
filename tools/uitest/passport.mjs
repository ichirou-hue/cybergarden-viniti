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

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || chromium.executablePath(),
});
const context = await browser.newContext({viewport: {width: 1366, height: 900}, acceptDownloads: true});
const page = await context.newPage();
let alerted = false;
page.on("dialog", dialog => { alerted = true; dialog.dismiss(); });
page.on("pageerror", error => check("нет ошибок страницы", false, error.message));

const general = {
  title: "Паспорт статьи A",
  topic: "Тема A",
  results: [
    {
      id: "A1", metric: "Recall", value: 0.81, unit: "ratio",
      validation_status: "passed", extraction_status: "extracted", issues: [],
      source_page: 2, source_fragment: 'Recall 0.81.\nСтрока "в кавычках"',
    },
    {
      id: "A2", metric: "Precision", value: 0.62, unit: "ratio",
      validation_status: "review", extraction_status: "extracted",
      issues: ["Расхождение значений"],
    },
    {
      id: "A3", metric: "MRR", value: 0.5, unit: "ratio",
      extraction_status: "extracted",
    },
    {
      id: "A4", metric: "=cmd", value: "@SUM(1)", unit: "x",
      validation_status: "passed", extraction_status: "extracted",
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

async function open(path) {
  await page.goto(`${base}/${path}`, {waitUntil: "networkidle"});
  await page.waitForTimeout(300);
}
async function importJSON(data, name = "passport.json") {
  await page.setInputFiles("#passport", {
    name, mimeType: "application/json", buffer: Buffer.from(JSON.stringify(data)),
  });
  await page.waitForTimeout(450);
}
const rows = () => page.locator("#view tbody tr").count();
const stats = async () => (await page.locator("#view .stats").innerText()).replace(/\s+/g, " ").trim();
const title = async () => (await page.locator("#view .lede").innerText()).trim();
const topic = async () => (await page.locator("#view .eyebrow.data").innerText()).trim();
const h1 = async () => (await page.locator("#view h1").innerText()).replace(/\s+/g, " ").trim();
const heroActions = async () => (await page.locator("#view .hero .actions > *").allInnerTexts()).map(text => text.trim());
const csvButton = () => page.locator('#view .panel-head button:has-text("Скачать CSV")');
const inferredNotice = async () => (await page.locator("#view .notice", {hasText: "Проверка не запускалась"}).innerText().catch(() => ""));

async function csvText() {
  const [download] = await Promise.all([page.waitForEvent("download"), csvButton().click()]);
  const target = `/tmp/viniti-passport-${Date.now()}.csv`;
  await download.saveAs(target);
  const {readFileSync} = await import("node:fs");
  return {name: download.suggestedFilename(), text: readFileSync(target, "utf8")};
}

/* 1. Демо */
await step("демо", async () => {
  await open("#/passport");
  check("демо: H1 «Паспорт публикации.»", (await h1()) === "Паспорт публикации.", await h1());
  check("демо: название в .lede", (await title()) === "Оценка воспроизводимости измерений", await title());
  check("демо: тема в .eyebrow.data", (await topic()) === "Демонстрационный пример", await topic());
  check("демо: 2 строки", await rows() === 2, String(await rows()));
  check("демо: сводка 2 / 1 / 1", (await stats()).includes("Извлечено результатов 2") &&
    (await stats()).includes("Прошли проверку 1") && (await stats()).includes("Требуют разбора 1"), await stats());
  check("демо: предупреждение", (await page.locator("#view").innerText()).includes("Демонстрационные значения"));
  check("демо: CSV в шапке панели", await csvButton().count() === 1);
  check("демо: CSV вторичный", (await csvButton().getAttribute("class") || "").includes("secondary"));
  check("демо: CSV выключен", await csvButton().isDisabled());
  check("демо: PDF выключен", await page.locator('#view input[type="file"]').isDisabled());
  check("демо: в герое нет CSV", !(await heroActions()).some(text => text.includes("Скачать CSV")), (await heroActions()).join(" | "));
  check("демо: в герое импорт и PDF", (await heroActions()).some(text => text.includes("Импорт паспорта JSON")) &&
    (await heroActions()).some(text => text.includes("Привязать исходный PDF")), (await heroActions()).join(" | "));
  check("демо: каталожные действия скрыты", !(await page.locator("#upload").isVisible()));
  check("демо: вкладка RAG на месте и не активна", await page.locator("#tracks .track").first().isVisible() &&
    await page.locator("#tracks .track[aria-current=\"page\"]").count() === 0);
  check("демо: «Паспорт» отмечен aria-current", await page.locator("#passportLink").getAttribute("aria-current") === "page");
  check("демо: таблица в .results", await page.locator("#view .workspace .results table").count() === 1);
  check("демо: .table-wrap больше нет", await page.locator("#view .table-wrap").count() === 0);
  check("демо: таблица — region", await page.locator("#view .workspace .results").getAttribute("role") === "region" &&
    await page.locator("#view .workspace .results").getAttribute("aria-label") === "Результаты паспорта");
  check("демо: значение в таблице", (await page.locator("#view tbody tr").first().innerText()).includes("1.2 с"));
  check("демо: карточка первой строки", (await page.locator("#detail").innerText()).includes("Среднее время"));
  check("демо: статус «Пройдена»", (await page.locator("#detail .badge").innerText()) === "Пройдена");
  check("демо: карточка aria-live", await page.locator("#detail").getAttribute("aria-live") === "polite");
});

/* 2. Фильтры и выбор строки */
await step("фильтры", async () => {
  await open("#/passport");
  await page.selectOption('#view select[aria-label="Проверка"]', "review");
  await page.waitForTimeout(150);
  check("фильтр по статусу: 1 строка", await rows() === 1, String(await rows()));
  check("фильтр по статусу: замечание в карточке", (await page.locator("#detail").innerText()).includes("не может быть отрицательным"));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "Эксперимент B");
  await page.waitForTimeout(300);
  check("поиск по контексту", (await page.locator("#view").innerText()).includes("Показано 1 из 1 · всего 2"));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "нет такого");
  await page.waitForTimeout(300);
  check("пустой поиск: сообщение", (await page.locator("#view").innerText()).includes("Результаты не найдены"));
  check("пустой поиск: пустая карточка", (await page.locator("#detail").innerText()).includes("Ничего не найдено по фильтрам"));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "");
  await page.selectOption('#view select[aria-label="Проверка"]', "");
  await page.waitForTimeout(200);
  await page.locator("#view tbody tr").nth(1).locator("button").click();
  check("выбор строки: aria-pressed", await page.locator("#view tbody tr").nth(1).locator("button").getAttribute("aria-pressed") === "true");
  check("выбор строки: подсветка", ((await page.locator("#view tbody tr").nth(1).getAttribute("class")) || "").includes("selected"));
  check("выбор строки: карточка", (await page.locator("#detail").innerText()).includes("Число наблюдений"));
  check("выбор строки: aria-controls", await page.locator("#view tbody tr").nth(1).locator("button").getAttribute("aria-controls") === "detail");
});

/* 3. Общий формат паспорта */
await step("общий формат", async () => {
  await open("#/passport");
  await importJSON(general);
  check("общий формат: H1 не меняется", (await h1()) === "Паспорт публикации.", await h1());
  check("общий формат: название в .lede", (await title()) === "Паспорт статьи A", await title());
  check("общий формат: тема в .eyebrow.data", (await topic()) === "Тема A", await topic());
  check("общий формат: success-notice", (await page.locator("#view .notice.good").innerText()).includes("Паспорт загружен. Записей: 4"),
    await page.locator("#view .notice.good").innerText().catch(() => "нет"));
  check("общий формат: 4 строки", await rows() === 4, String(await rows()));
  check("общий формат: сводка 4 / 3 / 1", (await stats()).includes("Прошли проверку 3") &&
    (await stats()).includes("Требуют разбора 1"), await stats());
  check("общий формат: value/unit видны", (await page.locator("#view tbody tr").first().innerText()).includes("0.81 ratio"),
    (await page.locator("#view tbody tr").first().innerText()).replace(/\s+/g, " "));
  check("общий формат: value в карточке", (await page.locator("#detail .value").innerText()).includes("0.81 ratio"), await page.locator("#detail .value").innerText());
  check("общий формат: page/quote", (await page.locator("#detail").innerText()).includes("стр. 2") &&
    (await page.locator("#detail").innerText()).includes('"в кавычках"'), (await page.locator("#detail").innerText()).slice(0, 200));
  check("общий формат: inferred notice", (await page.locator("#view").innerText()).includes("Проверка не запускалась") &&
    (await page.locator("#view").innerText()).includes("для 1 записи"), await inferredNotice());
  const badge = page.locator("#view tbody tr", {hasText: "MRR"}).locator(".badge");
  check("общий формат: inferred badge title", (await badge.getAttribute("title") || "").includes("Статус выведен"));
  const csv = await csvText();
  check("общий формат: CSV скачался", csv.name === "results.csv", csv.name);
  check("общий формат: CSV с BOM", csv.text.charCodeAt(0) === 0xFEFF, String(csv.text.charCodeAt(0)));
  const parsed = parseCSV(csv.text);
  check("общий формат: CSV 29 колонок", parsed.header.length === 29, String(parsed.header.length));
  check("общий формат: CSV строки той же ширины", parsed.rows.length > 0 &&
    parsed.rows.every(row => row.length === parsed.header.length), parsed.rows.map(row => row.length).join(","));
  const ids = parsed.rows.map(row => row[parsed.header.indexOf("result_id")]);
  check("общий формат: CSV только passed", ids.includes("A1") && ids.includes("A3") && ids.includes("A4") && !ids.includes("A2"), ids.join(","));
  const a1 = parsed.rows.find(row => row[parsed.header.indexOf("result_id")] === "A1") || [];
  check("общий формат: CSV содержит value", /0[.,]81/.test(a1[parsed.header.indexOf("normalized_value")] || ""),
    a1[parsed.header.indexOf("normalized_value")] || "empty");
  check("общий формат: CSV экранирует кавычки/перевод", (a1[parsed.header.indexOf("source_fragment")] || "").includes('"') &&
    (a1[parsed.header.indexOf("source_fragment")] || "").includes("\n"));
  const a4 = parsed.rows.find(row => row[parsed.header.indexOf("result_id")] === "A4") || [];
  check("общий формат: CSV защищает формулу", (a4[parsed.header.indexOf("metric_name")] || "").startsWith("'="),
    a4[parsed.header.indexOf("metric_name")] || "empty");
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "нет такого");
  await page.waitForTimeout(300);
  check("общий формат: CSV не зависит от пустого фильтра", !(await csvButton().isDisabled()));
  await page.fill('#view input[aria-label="Поиск по паспорту"]', "");
  await page.waitForTimeout(250);
});

/* 4. Невалидный импорт */
await step("ошибки импорта", async () => {
  await open("#/passport");
  await importJSON(general);
  const beforeTitle = await title();
  const beforeRows = await rows();
  for (const [name, body, expected] of [
    ["empty", "{}", "В паспорте нужен массив results или facts."],
    ["array", "[]", "В паспорте нужен массив results или facts."],
    ["bad", "{ not json", "Файл не является корректным JSON."],
    ["results-string", '{"results":"x"}', "В паспорте нужен массив results или facts."],
    ["results-null", '{"results":[null]}', "В паспорте нужен массив results или facts."],
    ["null", "null", "Ожидается объект JSON."],
  ]) {
    await page.setInputFiles("#passport", {name: `${name}.json`, mimeType: "application/json", buffer: Buffer.from(body)});
    await page.waitForTimeout(350);
    const alert = page.locator('#view .notice.bad[role="alert"]');
    check(`ошибка ${name}: inline alert`, await alert.count() === 1, await alert.innerText().catch(() => "нет"));
    if (await alert.count()) check(`ошибка ${name}: текст`, (await alert.innerText()).includes(expected), await alert.innerText());
    check(`ошибка ${name}: #status не используется`, (await page.locator("#status").innerText()).trim() === "");
    check(`ошибка ${name}: данные не потеряны`, (await title()) === beforeTitle && await rows() === beforeRows);
  }
  await importJSON(general, "recover.json");
  check("после ошибок приложение работает", (await title()) === "Паспорт статьи A" &&
    (await page.locator("#view .notice.good").innerText()).includes("Паспорт загружен"));
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
  check("facts: page/quote", (await page.locator("#detail").innerText()).includes("стр. 4") &&
    (await page.locator("#detail").innerText()).includes("band gap"), (await page.locator("#detail").innerText()).slice(0, 160));
  check("facts: статус выведен", (await page.locator("#view").innerText()).includes("Проверка не запускалась") &&
    (await stats()).includes("Прошли проверку 2"), await stats());
  check("facts: CSV доступен", !(await csvButton().isDisabled()));
  const csv = await csvText();
  check("facts: CSV 2 passed", parseCSV(csv.text).rows.length === 2, String(parseCSV(csv.text).rows.length));
  check("facts: CSV с value", /1\.55|1,55/.test(csv.text));
});

/* 5b. Unit unspecified и окно строк */
await step("unit unspecified", async () => {
  await open("#/passport");
  await importJSON({title: "Unspecified", facts: [
    {id: "U1", metric: "Recall", value: "0.42", unit: "unspecified", evidence: {page: 3, quote: "Recall 0.42"}},
  ]});
  check("unit unspecified: значение в таблице", (await page.locator("#view tbody").innerText()).includes("0.42"),
    (await page.locator("#view tbody").innerText()).replace(/\s+/g, " "));
  check("unit unspecified: значение в карточке", (await page.locator("#detail .value").innerText()).trim() === "Recall · 0.42",
    (await page.locator("#detail .value").innerText()).trim());
});

await step("паспорт: окно строк", async () => {
  await open("#/passport");
  const response = await context.request.get(`${base}/api/documents/rag:d020/export.json`);
  check("экспорт: доступен", response.ok(), String(response.status()));
  await importJSON(await response.json());
  const buttons = () => page.locator("#view tbody button.row-button").count();
  check("окно: 200 строк из 408", await buttons() === 200, String(await buttons()));
  check("окно: счётчик", (await page.locator("#view .count").innerText()).includes("Показано 200 из 408"),
    await page.locator("#view .count").innerText());
  const more = page.locator('#view button:has-text("Показать ещё")');
  check("окно: кнопка «Показать ещё»", await more.count() === 1);
  const moreBox = await more.boundingBox();
  const tableBox = await page.locator("#view .workspace .results").boundingBox();
  check("окно: кнопка над таблицей", moreBox && tableBox && moreBox.y + moreBox.height <= tableBox.y,
    `${JSON.stringify(moreBox)} / ${JSON.stringify(tableBox)}`);
  await more.focus();
  await page.keyboard.press("Enter");
  await page.waitForTimeout(350);
  check("окно: после клика 400 строк", await buttons() === 400, String(await buttons()));
  const focusText = await page.evaluate(() => document.activeElement?.innerText?.trim() || "");
  check("окно: фокус остаётся на кнопке", focusText.startsWith("Показать ещё"), focusText);
  await page.locator("#view tbody button.row-button").nth(250).click();
  await page.selectOption('#view select[aria-label="Проверка"]', "passed");
  await page.waitForTimeout(300);
  check("окно: фильтр сбрасывает окно", await buttons() === 200 &&
    (await page.locator("#view .count").innerText()).includes("Показано 200 из 403"), await page.locator("#view .count").innerText());
  const selectedIndex = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("#view tbody tr")];
    return rows.findIndex(row => row.classList.contains("selected"));
  });
  check("окно: выбранная строка внутри окна", selectedIndex >= 0 && selectedIndex < 200, String(selectedIndex));
});

await step("CSV: формула после пробела", async () => {
  await open("#/passport");
  await importJSON({title: "CSV", results: [
    {id: "S1", metric: " =cmd", value: 1, unit: "u", validation_status: "passed", extraction_status: "extracted"},
  ]});
  const csv = await csvText();
  const parsed = parseCSV(csv.text);
  const cell = parsed.rows[0] ? parsed.rows[0][parsed.header.indexOf("metric_name")] : "";
  check("CSV: пробел перед = экранируется", String(cell).startsWith("' ="), String(cell));
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
  check("solar: сводка 3 / 2 / 1", (await stats()).includes("Прошли проверку 2") &&
    (await stats()).includes("Требуют разбора 1"), await stats());
  check("solar: заголовок публикации в .lede", (await title()) === "Solar paper", await title());
  check("solar: публикация в eyebrow", (await topic()) === "Solar paper", await topic());
  check("solar: полный набор без предупреждений", !(await page.locator("#view").innerText()).includes("Набор Solar неполный"));
  const csv = await csvText();
  check("solar: CSV с BOM", csv.text.charCodeAt(0) === 0xFEFF);
  const parsed = parseCSV(csv.text);
  check("solar: CSV 27 колонок", parsed.header.length === 27, String(parsed.header.length));
  const ids = parsed.rows.map(row => row[parsed.header.indexOf("result_id")]);
  check("solar: CSV только passed", ids.includes("R2") && ids.includes("R3") && !ids.includes("R1"), ids.join(","));
  check("solar: CSV со значениями", csv.text.includes("1.10") && csv.text.includes("0.8"));
});

/* 6b. Solar без validation_status: статус выводится */
await step("solar без validation_status", async () => {
  await open("#/passport");
  const noValidation = {
    ...solar,
    results: [
      {...solar.results[0], validation_status: undefined},
      {...solar.results[1], validation_status: undefined},
    ],
    issues: [{publication_id: "P1", issue_id: "I1", check_name: "unit_mismatch",
      description: "Единица не совпадает", record_ids: "R1"}],
  };
  await importJSON(noValidation);
  check("inferred solar: 1 passed / 1 review", (await stats()).includes("Прошли проверку 1") &&
    (await stats()).includes("Требуют разбора 1"), await stats());
  check("inferred solar: notice", (await page.locator("#view").innerText()).includes("Проверка не запускалась") &&
    (await page.locator("#view").innerText()).includes("для 1 записи"), await inferredNotice());
  check("inferred solar: CSV доступен", !(await csvButton().isDisabled()));
});

/* 6c. Неполный набор Solar и строки CSV */
await step("solar: неполный набор", async () => {
  await open("#/passport");
  const partial = {...solar};
  delete partial.issues;
  await importJSON(partial);
  check("неполный solar: 3 строки", await rows() === 3, String(await rows()));
  check("неполный solar: контекст сохранён", (await page.locator("#view tbody").innerText()).includes("Device A") &&
    (await page.locator("#detail").innerText()).includes("MAPbI3"),
    (await page.locator("#detail").innerText()).slice(0, 180));
  check("неполный solar: предупреждение", (await page.locator("#view .notice.warn").innerText()).includes("нет таблиц issues"),
    await page.locator("#view .notice.warn").innerText().catch(() => "нет"));
  check("неполный solar: заголовок на месте", (await title()) === "Solar paper", await title());
});

await step("CSV: только строки со значением", async () => {
  await open("#/passport");
  await importJSON({title: "CSV", results: [
    {id: "P1", metric: "NoExtract", value: 0.5, unit: "u", validation_status: "passed", extraction_status: "present_not_extractable"},
    {id: "P2", metric: "Ok", value: 1.5, unit: "u", validation_status: "passed", extraction_status: "extracted"},
    {id: "P3", metric: "NoValue", validation_status: "passed", extraction_status: "extracted"},
  ]});
  check("CSV: явный passed не скрывается в статусе", (await stats()).includes("Прошли проверку 3"), await stats());
  check("CSV: подсказка кнопки", ((await csvButton().getAttribute("title")) || "").includes("извлечённым значением"));
  const csv = await csvText();
  const parsed = parseCSV(csv.text);
  const ids = parsed.rows.map(row => row[parsed.header.indexOf("result_id")]);
  check("CSV: только извлечённые строки", ids.length === 1 && ids[0] === "P2", ids.join(","));
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
  check("повторный импорт: новая шапка", (await title()) === "Паспорт B", await title());
  check("повторный импорт: одна строка", await rows() === 1, String(await rows()));
  check("повторный импорт: старая карточка не протекла", !(await page.locator("#detail").innerText()).includes("MetricB"));
  check("повторный импорт: старый PDF не протёк", await page.locator('#detail a:has-text("Открыть страницу PDF")').count() === 0);
});

/* 7b. Вложенный source (собственный export.json) */
await step("вложенный source", async () => {
  await open("#/passport");
  await importJSON({
    title: "Собственный экспорт",
    results: [{
      id: "S1", metric: "Recall", value: 0.81, unit: "ratio",
      validation_status: "passed", extraction_status: "extracted",
      source: {page: 7, section: "Results", table: "Table 3", fragment: "Recall was 0.81"},
    }],
  });
  check("nested source: страница", (await page.locator("#detail").innerText()).includes("стр. 7"),
    (await page.locator("#detail").innerText()).slice(0, 160));
  check("nested source: цитата", (await page.locator("#detail").innerText()).includes("Recall was 0.81"));
  const pdfResponse = await context.request.get(`${base}/api/documents/rag%3Ad020/pdf`);
  await page.setInputFiles('#view input[type="file"]', {name: "s.pdf", mimeType: "application/pdf", buffer: await pdfResponse.body()});
  await page.waitForTimeout(300);
  check("nested source: PDF-ссылка", /#page=7$/.test(await page.locator('#detail a:has-text("Открыть страницу PDF")').getAttribute("href") || ""));
});

/* 8. Навигация, hidden-контролы и hash-роутинг */
await step("навигация", async () => {
  await open("#/?track=rag&q=D014");
  check("каталог: контролы видны", await page.locator("#upload").isVisible() && await page.locator("#tracks").isVisible());
  await page.click("#passportLink");
  await page.waitForTimeout(400);
  check("навигация: каталог → паспорт", page.url().endsWith("#/passport"), page.url());
  check("паспорт: H1", (await h1()) === "Паспорт публикации.");
  check("паспорт: каталожные действия скрыты", !(await page.locator("#upload").isVisible()));
  check("паспорт: вкладка RAG осталась", await page.locator("#tracks .track").first().isVisible() &&
    await page.locator("#tracks .track[aria-current=\"page\"]").count() === 0);
  check("паспорт: aria-current", await page.locator("#passportLink").getAttribute("aria-current") === "page");
  await page.goBack();
  await page.waitForTimeout(500);
  check("навигация: назад в каталог", page.url().includes("q=D014"), page.url());
  check("навигация: фильтр восстановлен", await page.inputValue('input[aria-label="Поиск публикации"]') === "D014");
  check("каталог: aria-current снят", await page.locator("#passportLink").getAttribute("aria-current") === null);
  await open("#/doc/rag:d020?track=rag");
  check("навигация: прямая ссылка с двоеточием", (await page.locator("#view h1").innerText()).includes("Web Retrieval-Aware"),
    (await page.locator("#view h1").innerText()).slice(0, 80));
  await page.click("#passportLink");
  await page.waitForTimeout(350);
  check("навигация: карточка → паспорт", page.url().endsWith("#/passport"), page.url());
  await page.setInputFiles("#passport", {name: "from-doc.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(general))});
  await page.waitForTimeout(500);
  check("навигация: импорт с любой страницы ведёт в паспорт", page.url().endsWith("#/passport") &&
    (await title()) === "Паспорт статьи A", page.url());
  await page.goBack();
  await page.waitForTimeout(700);
  check("навигация: назад в карточку", page.url().includes("/doc/rag") && page.url().includes("d020"), page.url());
  await open("#/passport?track=solar");
  check("навигация: ?track=solar не блокирует паспорт", (await h1()) === "Паспорт публикации." &&
    !(await page.locator("#view").innerText()).includes("Раздел «Solar»"), (await page.locator("#view").innerText()).slice(0, 120));
  await open("#/passport");
  await page.click('#tracks .track:has-text("RAG")');
  await page.waitForTimeout(400);
  check("паспорт → каталог по вкладке RAG", page.url().includes("#/?track=rag"), page.url());
  check("каталог: RAG снова активен", (await page.locator("#tracks .track[aria-current=\"page\"]").innerText()).trim() === "RAG");
  await open("#/nope");
  check("навигация: неизвестный путь", (await page.locator("#view").innerText()).includes("Страница не найдена"));
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
  check("мобилка: нет горизонтального overflow страницы", overflow.page <= overflow.viewport, JSON.stringify(overflow));
  const actions = await mp.locator("#view .hero .actions").evaluate(el => [...el.children].map(node => node.getBoundingClientRect().right));
  check("мобилка: кнопки в экране", actions.every(right => right <= overflow.viewport), actions.join(","));
  const csv = await mp.locator('#view .panel-head button:has-text("Скачать CSV")').boundingBox();
  check("мобилка: CSV в экране", csv && csv.x + csv.width <= overflow.viewport, JSON.stringify(csv));
  // Дизайн-решение: на 390px таблица паспорта сжимается по колонкам.
  const table = await mp.locator("#view .workspace .results").evaluate(el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth}));
  check("мобилка: паспортная таблица не скроллится внутри", table.scrollWidth <= table.clientWidth + 1, JSON.stringify(table));
  await mp.goto(`${base}/#/`, {waitUntil: "networkidle"});
  await mp.waitForTimeout(500);
  const catalog = await mp.locator("#view .results").evaluate(el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth}));
  check("мобилка: каталог скроллится внутри", catalog.scrollWidth > catalog.clientWidth, JSON.stringify(catalog));

  const exportData = await (await context.request.get(`${base}/api/documents/rag:d020/export.json`)).json();
  await mp.goto(`${base}/#/passport`, {waitUntil: "networkidle"});
  await mp.setInputFiles("#passport", {name: "export.json", mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(exportData))});
  await mp.waitForTimeout(800);
  check("мобилка: «Контекст» скрыт, если его нет",
    !(await mp.locator("#view .passport-table th", {hasText: "Контекст"}).isVisible()));
  const exportBox = await mp.locator("#view .workspace .results").evaluate(el => ({sw: el.scrollWidth, cw: el.clientWidth}));
  check("мобилка: экспорт без скролла", exportBox.sw <= exportBox.cw + 1, JSON.stringify(exportBox));
  const justify = await mp.locator("#view .row-button").first().evaluate(node => getComputedStyle(node).justifyContent);
  check("мобилка: метрика по левому краю", ["flex-start", "flow-start", "start"].includes(justify), justify);

  await mp.goto(`${base}/#/doc/rag:d020?track=rag&tab=sources`, {waitUntil: "networkidle"});
  await mp.waitForTimeout(700);
  const frag = await mp.locator("#view .results").evaluate(el => ({sw: el.scrollWidth, cw: el.clientWidth}));
  check("мобилка: фрагменты без скролла", frag.sw <= frag.cw + 1, JSON.stringify(frag));
  const fragButton = await mp.locator("#view .sources-table tbody button").first().boundingBox();
  check("мобилка: «Стр.» в экране", fragButton && fragButton.x + fragButton.width <= 390,
    JSON.stringify(fragButton));
  const tabRows = await mp.evaluate(() => {
    const tabs = [...document.querySelectorAll("#view .tabs .tab")];
    return new Set(tabs.map(tab => Math.round(tab.getBoundingClientRect().top))).size;
  });
  check("мобилка: вкладки в две строки", tabRows === 2, String(tabRows));
  await mobile.close();
});

/* 11. Доступность */
await step("доступность", async () => {
  await open("#/passport");
  await importJSON(general);
  const trace = [];
  await page.evaluate(() => document.body.focus());
  for (let index = 0; index < 24; index += 1) {
    await page.keyboard.press("Tab");
    trace.push(await page.evaluate(() => {
      const node = document.activeElement;
      return `${node.tagName}|${String(node.getAttribute("aria-label") || node.innerText || node.value || node.id).replace(/\s+/g, " ").slice(0, 50)}`;
    }));
  }
  check("a11y: CSV доступен по Tab", trace.some(item => item.includes("Скачать CSV")), trace.join("; "));
  check("a11y: PDF доступен по Tab", trace.some(item => item.startsWith("INPUT|")), trace.join("; "));
  check("a11y: поиск доступен по Tab", trace.some(item => item.includes("Поиск по паспорту")), trace.join("; "));
  check("a11y: строки доступны по Tab", trace.some(item => item.includes("Recall") || item.includes("Precision")), trace.join("; "));
  const row = page.locator("#view tbody tr").first().locator("button");
  check("a11y: aria-controls detail", await row.getAttribute("aria-controls") === "detail");
  check("a11y: detail aria-live", await page.locator("#detail").getAttribute("aria-live") === "polite");
  check("a11y: таблица — region", await page.locator("#view .workspace .results").getAttribute("role") === "region");
});

await browser.close();
const failed = results.filter(item => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} проверок пройдено`);
if (failed.length) {
  console.log("Провалены:");
  for (const item of failed) console.log(`  - ${item.name}`);
}
process.exit(failed.length ? 1 : 0);
