import { readFileSync } from "node:fs";
import { chromium } from "playwright-core";
import { seedContext } from "./account.mjs";

const base = process.env.MOCK_URL || "http://127.0.0.1:8799";
const results = [];
function check(name, ok, extra = "") {
  results.push({name, ok});
  console.log(`${ok ? "ok" : "FAIL"}  ${name}${ok ? "" : "  " + extra}`);
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || chromium.executablePath(),
});
const context = await browser.newContext({acceptDownloads: true});
// Корень сайта закрыт экраном первого входа: для проверки передачи PDF
// нужен готовый аккаунт в localStorage.
await seedContext(context);
const page = await context.newPage();
const pdf = {name: "article.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 mock")};

// Прямой вход: экран профиля просит PDF и не тянет чужую логику паспорта.
await page.goto(`${base}/manual-profile`, {waitUntil: "networkidle"});
check("профиль: заголовок экрана", (await page.locator("h1").first().textContent()).includes("Создать"));
check("профиль: стрелка назад ведёт на главную",
  await page.locator('a[aria-label="Назад на главную"]').getAttribute("href") === "/");
check("профиль: блока паспорта и его кнопок нет",
  await page.getByText("Паспорт публикации").count() === 0 &&
  await page.getByText("Импорт паспорта JSON").count() === 0 &&
  await page.getByText("Привязать исходный PDF").count() === 0);
check("профиль: панель скрыта до выбора PDF", await page.locator("#profilePanel").evaluate(node => node.hidden));

// Форма не показывается и не активна, пока PDF ещё читается.
await page.route("**/api/parse", async route => {
  await new Promise(resolve => setTimeout(resolve, 700));
  await route.continue();
});
await page.setInputFiles("#profilePdf", pdf);
await page.waitForTimeout(150);
check("профиль: форма скрыта во время чтения",
  await page.locator("#profileForm").evaluate(node => node.hidden && getComputedStyle(node).display === "none"));
await page.waitForSelector("#profileForm:not([hidden])", {timeout: 15000});
await page.unroute("**/api/parse");
const source = await page.locator("#profileSource").textContent();
check("профиль: PDF прочитан", source.includes("article.pdf") && source.includes("страниц: 2"), source);

await page.click('[data-preset="solar"]');
check("профиль: готовый набор Solar", await page.locator(".profile-entity").count() === 2,
  String(await page.locator(".profile-entity").count()));

// Лишние поля не показываются, пока тип их не требует.
const firstField = page.locator(".profile-field").first();
check("профиль: для текста нет единиц и связи",
  await firstField.locator('.pf-extra [data-when]:not([hidden])').count() === 0);
await firstField.locator('select[name="type"]').selectOption("number");
check("профиль: для числа видны единицы и коэффициент",
  await firstField.locator('.pf-extra [data-when]:not([hidden])').count() === 3);
await firstField.locator('select[name="type"]').selectOption("reference");
check("профиль: для связи виден связанный тип",
  await firstField.locator('.pf-extra [data-when]:not([hidden])').count() === 1);
await firstField.locator('select[name="type"]').selectOption("text");

await page.click("#scanProfile");
await page.waitForFunction(() => document.querySelectorAll("#profileMatches summary").length === 2);
const summaries = await page.locator("#profileMatches summary").allTextContents();
check("профиль: подсказки найдены", summaries.length === 2 && summaries[0].includes("подсказок:"),
  summaries.join(" | "));

await page.locator("#profileMatches details").first().evaluate(node => { node.open = true; });
await page.locator("#profileMatches input[type=checkbox]").first().check();
const [download] = await Promise.all([
  page.waitForEvent("download"),
  page.click("#profileForm button[type=submit]"),
]);
const payload = JSON.parse(readFileSync(await download.path(), "utf8"));
check("профиль: скачан JSON", download.suggestedFilename() === "extraction-profile.json",
  download.suggestedFilename());
check("профиль: типы и доказательства", payload.profile?.types?.length === 2 && payload.evidence?.length === 1,
  JSON.stringify({types: payload.profile?.types?.length, evidence: payload.evidence?.length}));

// Свои ошибки профиля видны рядом с кнопкой, а не только в шапке панели.
const entityNames = page.locator('.profile-entity [name="entityName"]');
await entityNames.nth(1).fill(await entityNames.first().inputValue());
await page.click("#profileForm button[type=submit]");
await page.waitForSelector("#profileError:not([hidden])");
const errorState = await page.locator("#profileError").evaluate(node => {
  const rect = node.getBoundingClientRect();
  return {text: node.textContent, visible: rect.height > 0 && rect.top >= 0 && rect.bottom <= innerHeight};
});
check("профиль: ошибка видна у кнопки", errorState.visible && errorState.text.includes("уникальные"), errorState.text);
check("профиль: пресет снят после правки",
  await page.locator('[data-preset="solar"]').getAttribute("aria-pressed") === "false");

// Правка после сканирования помечает подсказки устаревшими, а не стирает молча.
await entityNames.nth(1).fill("Измерение");
await page.click("#scanProfile");
await page.waitForFunction(() => document.querySelectorAll("#profileMatches summary").length === 2);
const fieldName = page.locator('.profile-field [name="fieldName"]').first();
await fieldName.fill((await fieldName.inputValue()) + " 2");
check("профиль: старые подсказки помечены",
  await page.locator("#profileMatches .profile-stale").count() === 1 &&
  await page.locator("#profileMatches input[type=checkbox]").first().isDisabled());

// Мастер на каталоге передаёт выбранный PDF на экран профиля без повторного выбора.
await page.goto(`${base}/`, {waitUntil: "networkidle"});
await page.setInputFiles("#file", {...pdf, name: "handoff.pdf"});
await page.waitForSelector("#profileStep:not([hidden])", {timeout: 10000});
await page.click("#createProfile");
await page.waitForURL("**/manual-profile", {timeout: 10000});
await page.waitForSelector("#profileForm:not([hidden])", {timeout: 15000});
const handoff = await page.locator("#profileSource").textContent();
check("мастер: PDF передан в профиль", handoff.includes("handoff.pdf") && handoff.includes("страниц: 2"), handoff);

await page.reload({waitUntil: "networkidle"});
check("профиль: PDF не подхватывается повторно",
  await page.locator("#profilePanel").evaluate(node => node.hidden));

await browser.close();
const failed = results.filter(item => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} проверок пройдено`);
process.exit(failed.length ? 1 : 0);
