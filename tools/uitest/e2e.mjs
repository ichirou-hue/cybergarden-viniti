import { chromium } from "playwright-core";
import { writeFileSync } from "node:fs";
import { seedContext } from "./account.mjs";

const base = process.env.MOCK_URL || "http://127.0.0.1:8799";
const out = [];
function check(name, ok, extra = "") {
  out.push({name, ok});
  console.log(`${ok ? "ok" : "FAIL"}  ${name}${ok ? "" : "  " + extra}`);
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || chromium.executablePath(),
});
const context = await browser.newContext({viewport: {width: 1366, height: 900}, acceptDownloads: true});
// Экран первого входа обязателен: сразу кладём готовый аккаунт, иначе
// каталог закрыт регистрацией.
await seedContext(context);
const page = await context.newPage();
let alertFired = false;
page.on("dialog", dialog => { alertFired = true; dialog.dismiss(); });

// Каталог и сортировка по замечаниям.
// Корень "/" открывает мастер загрузки, поэтому каталог проверяем на /catalog.
await page.goto(`${base}/#/catalog?track=rag`, {waitUntil: "networkidle"});
await page.selectOption('.filters select[aria-label="Сортировка"]', "issues");
await page.waitForTimeout(400);
const firstPub = await page.locator("#view tbody tr td .pub strong").first().textContent();
check("сортировка по замечаниям", firstPub === "D020", firstPub);

// Поиск с XSS-строкой
await page.fill('.filters input[type="search"]', '<img src=x onerror=alert(1)>');
await page.waitForTimeout(500);
check("XSS не исполняется", !alertFired);
check("XSS: пустой результат", (await page.locator("#view").textContent()).includes("Ничего не найдено"));

// Сброс фильтров
await page.click('button:has-text("Сбросить фильтры")');
await page.waitForTimeout(400);
check("сброс фильтров", /Публикаций: \d+/.test(await page.locator(".count").textContent()));

// D014: пагинация
await page.goto(`${base}/#/doc/rag:d014?track=rag`, {waitUntil: "networkidle"});
await page.click('.pager button:has-text("Далее")');
await page.waitForTimeout(500);
check("пагинация: страница 2", (await page.locator(".count").textContent()).includes("страница 2 из"));
check("пагинация: 50 строк", (await page.locator("tr.result").count()) === 50);

// Фильтр метрики
await page.goto(`${base}/#/doc/rag:d020?track=rag`, {waitUntil: "networkidle"});
await page.selectOption('select[aria-label="Метрика"]', "Recall");
await page.waitForTimeout(500);
const metrics = await page.locator("tr.result td.metric strong").allTextContents();
check("фильтр метрики", metrics.length > 0 && metrics.every(text => text === "Recall"), metrics.slice(0, 3).join(","));
check("фильтр метрики: URL", page.url().includes("metric=Recall"), page.url());

// Заголовок открывает диалог с подписью
await page.goto(`${base}/#/doc/rag:d020?track=rag`, {waitUntil: "networkidle"});
await page.locator("tr.result").first().locator("button:has-text('Заголовок')").click();
await page.waitForTimeout(500);
check("диалог: заголовок", (await page.locator("#evidenceTitle").textContent()).includes("Заголовок таблицы"));
check("диалог: цитата заголовка", (await page.locator("#evidenceQuote").textContent()).length > 0);
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
check("диалог: Esc закрывает", !page.url().includes("/source/"), page.url());

// Поиск по результатам с @k
await page.fill('input[aria-label="Поиск по результатам"]', "Recall@5");
await page.waitForTimeout(500);
check("поиск Recall@5", (await page.locator(".count").textContent()).includes("Найдено"));
check("поиск Recall@5: строки", (await page.locator("tr.result").count()) > 0);

// Фрагменты PDF: без молчаливой обрезки
await page.goto(`${base}/#/doc/rag:d020?track=rag&tab=sources`, {waitUntil: "networkidle"});
await page.waitForTimeout(600);
check("фрагменты: счётчик 411", (await page.locator("#view .count").textContent()).includes("Фрагментов: 411"),
  await page.locator("#view .count").textContent());
check("фрагменты: все строки", await page.locator("#view .sources-table tbody tr").count() === 411,
  String(await page.locator("#view .sources-table tbody tr").count()));

// Фрагменты PDF: пометка об обрезке на большом документе
await page.goto(`${base}/#/doc/rag:d014?track=rag&tab=sources`, {waitUntil: "networkidle"});
await page.waitForTimeout(900);
check("фрагменты: пометка об обрезке", (await page.locator("#view .count").textContent()).includes("показаны первые 500"),
  await page.locator("#view .count").textContent());
check("фрагменты: 500 строк", await page.locator("#view .sources-table tbody tr").count() === 500,
  String(await page.locator("#view .sources-table tbody tr").count()));

// result= вне первой страницы
await page.goto(`${base}/#/doc/rag:d020?track=rag&result=R060`, {waitUntil: "networkidle"});
await page.waitForTimeout(1400);
check("result= на второй странице", await page.locator("#view tr.target").count() === 1 &&
  (await page.locator("#view .count").textContent()).includes("страница 2"),
  `${await page.locator("#view tr.target").count()} · ${await page.locator("#view .count").textContent()}`);

// Битый source: старый диалог не остаётся
await page.goto(`${base}/#/doc/rag:d020/source/1001?track=rag`, {waitUntil: "networkidle"});
await page.waitForTimeout(700);
check("source: диалог открыт", await page.locator("#evidence").evaluate(dialog => dialog.open));
await page.evaluate(() => { location.hash = "#/doc/rag:d020/source/999999?track=rag"; });
await page.waitForTimeout(1400);
check("битый source: диалог закрыт", !(await page.locator("#evidence").evaluate(dialog => dialog.open)));
check("битый source: сообщение", (await page.locator("#status").textContent()).includes("Не удалось открыть источник"));
check("битый source: URL очищен", !page.url().includes("/source/999999"), page.url());

// Документ без префикса области
await page.goto(`${base}/#/doc/foo`, {waitUntil: "networkidle"});
await page.waitForTimeout(700);
const docFoo = (await page.locator("#view").textContent()).replace(/\s+/g, " ");
check("#/doc/foo: документ не найден", docFoo.includes("Документ не найден") && !docFoo.includes("Раздел"), docFoo.slice(0, 120));

await page.goto(`${base}/#/doc/foo?track=solar`, {waitUntil: "networkidle"});
await page.waitForTimeout(700);
const docFooSolar = (await page.locator("#view").textContent()).replace(/\s+/g, " ");
check("#/doc/foo?track=solar: область нормализована", docFooSolar.includes("Документ не найден") &&
  !docFooSolar.includes("Раздел Solar"), docFooSolar.slice(0, 120));

// result= не найден
await page.goto(`${base}/#/doc/rag:d020?track=rag&result=R99999`, {waitUntil: "networkidle"});
await page.waitForTimeout(1500);
check("result=: сообщение о ненайденной записи",
  (await page.locator("#status").textContent()).includes("не найдена среди первых"), await page.locator("#status").textContent());

// Загрузка PDF
const response = await context.request.get(`${base}/api/documents/rag%3Ad020/pdf`);
writeFileSync("/tmp/viniti-uitest-upload.pdf", await response.body());
// Загрузка идёт из тулбара каталога: корень "/" открывает мастер.
await page.goto(`${base}/#/catalog?track=rag`, {waitUntil: "networkidle"});
await page.setInputFiles("#file", "/tmp/viniti-uitest-upload.pdf");
await page.click('#upload button[type="submit"]');
await page.waitForTimeout(800);
check("загрузка: карточка открылась", page.url().includes("#/doc/rag%3A"), page.url());
check("загрузка: статус частично", (await page.locator("#view").textContent()).includes("Обработано частично"));

const failed = out.filter(item => !item.ok);
console.log(`\n${out.length - failed.length}/${out.length} проверок пройдено`);
await browser.close();
process.exit(failed.length ? 1 : 0);
