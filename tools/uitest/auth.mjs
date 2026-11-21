import { chromium } from "playwright-core";

// Проверки входа и профиля без бэкенда: аккаунт живёт в localStorage.
// Требует запущенный мок: ../../.venv/bin/python ../mock_api.py --port 8799

const base = process.env.MOCK_URL || "http://127.0.0.1:8799";
const out = [];
function check(name, ok, extra = "") {
  out.push({name, ok: Boolean(ok)});
  console.log(`${ok ? "ok" : "FAIL"}  ${name}${ok ? "" : "  " + extra}`);
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || chromium.executablePath(),
});

async function fresh(viewport = {width: 1440, height: 950}, setup) {
  const context = await browser.newContext({viewport});
  if (setup) await setup(context);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && !message.text().includes("404")) errors.push(message.text());
  });
  return {context, page, errors};
}

async function register(page, name = "Илья Сафонов", email = "i.safonov@viniti.ru") {
  await page.fill("#regName", name);
  await page.fill("#regOrg", "ЮФУ, ИКТ");
  await page.fill("#regEmail", email);
  await page.fill("#regPassword", "password123");
  await page.check("#regConsent");
  await page.locator("#registerForm button[type=submit]").click();
  await page.waitForSelector("#authScreen", {state: "hidden"});
  // Ждём, пока route отрисует целевой экран: класс снимается раньше.
  await page.waitForFunction(() => document.body.classList.contains("wizard-active")
    || Boolean(document.body.dataset.view));
}

// 1. Первый вход закрывает каталог
{
  const {context, page, errors} = await fresh();
  await page.goto(`${base}/#/catalog?track=rag`, {waitUntil: "networkidle"});
  check("вход: экран регистрации виден", await page.locator("#authScreen").isVisible());
  check("вход: каталог не отрисован", !(await page.locator("#view").isVisible()));
  check("вход: меню аккаунта скрыто", !(await page.locator("#account").isVisible()));
  check("вход: фокус в поле имени", await page.evaluate(() => document.activeElement?.id) === "regName");
  await page.locator("#registerForm button[type=submit]").click();
  check("вход: пустая форма ругается", (await page.locator("#authStatus").textContent()).includes("Проверьте"));
  check("вход: поле помечено", await page.locator("#regName").getAttribute("aria-invalid") === "true");

  await register(page);
  check("регистрация: каталог открылся", await page.locator("#view").isVisible());
  check("регистрация: имя в шапке", (await page.locator("#accountName").textContent()).trim() === "Илья");
  check("регистрация: фокус ушёл в приложение", await page.evaluate(() => document.activeElement?.id) === "view");

  await page.locator("#accountButton").click();
  check("поповер: раскрыт", await page.locator("#accountMenu").isVisible());
  check("поповер: aria-expanded", await page.locator("#accountButton").getAttribute("aria-expanded") === "true");
  check("поповер: имя и email", (await page.locator("#accountMenuName").textContent()).includes("Илья Сафонов")
    && (await page.locator("#accountMenuEmail").textContent()).includes("i.safonov@viniti.ru"));
  await page.keyboard.press("Escape");
  check("поповер: Escape закрывает", !(await page.locator("#accountMenu").isVisible()));
  check("поповер: фокус вернулся", await page.evaluate(() => document.activeElement?.id) === "accountButton");

  await page.locator("#accountButton").click();
  await page.locator("#accountProfileLink").click();
  await page.waitForSelector("#profileTitle");
  check("профиль: маршрут", page.url().includes("#/profile"), page.url());
  check("профиль: заголовок вкладки", (await page.title()).includes("Профиль"));
  check("профиль: статистика", await page.locator(".profile-stat").count() === 4);
  check("профиль: лента активности", (await page.locator(".profile-feed").textContent()).includes("Аккаунт создан"));
  check("профиль: настройки", await page.locator("#profileSettings").count() === 1);

  await page.locator('input[data-setting="compact"]').check();
  await page.fill('input[name="role"]', "Ведущий исследователь");
  await page.locator(".profile-form button[type=submit]").click();
  await page.waitForTimeout(200);
  check("профиль: роль сохранилась", (await page.locator(".profile-role").textContent()).includes("Ведущий исследователь"));
  await page.reload({waitUntil: "networkidle"});
  await page.waitForSelector(".profile-identity");
  check("профиль: данные после перезагрузки", (await page.locator(".profile-role").textContent()).includes("Ведущий исследователь"));
  check("профиль: настройка после перезагрузки", await page.locator('input[data-setting="compact"]').isChecked());
  check("профиль: сессия не сброшена", !(await page.locator("#authScreen").isVisible()));

  // Возврат в каталог из поповера
  await page.locator("#accountButton").click();
  await page.locator("#accountCatalog").click();
  await page.waitForTimeout(400);
  check("поповер: каталог открывается", page.url().includes("#/catalog"), page.url());

  // Выход: сессия завершается, профиль остаётся
  await page.locator("#accountButton").click();
  await page.locator("#accountLogout").click();
  await page.waitForSelector("#authScreen", {state: "visible"});
  check("выход: экран входа", await page.locator("#authScreen").isVisible());
  check("выход: заголовок вкладки", (await page.title()).includes("Вход"));
  check("выход: запись профиля сохранена", await page.evaluate(() => Boolean(localStorage.getItem("viniti.account.v1"))));

  // Повторный вход с тем же email возвращает профиль
  await page.locator("#tabLogin").click();
  await page.fill("#loginEmail", "i.safonov@viniti.ru");
  await page.fill("#loginPassword", "password123");
  await page.locator("#loginForm button[type=submit]").click();
  await page.waitForSelector("#authScreen", {state: "hidden"});
  await page.locator("#accountButton").click();
  await page.locator("#accountProfileLink").click();
  await page.waitForSelector(".profile-identity");
  check("повторный вход: профиль вернулся", (await page.locator(".profile-role").textContent()).includes("Ведущий исследователь"));
  check("повторный вход: история сохранилась", (await page.locator(".profile-feed").textContent()).includes("Вход в аккаунт"));
  check("десктоп: нет ошибок в консоли", errors.length === 0, errors.join(" | "));
  await context.close();
}

// 2. Чужой email не затирает профиль молча
{
  const {context, page} = await fresh();
  await page.goto(`${base}/`, {waitUntil: "networkidle"});
  await register(page);
  await page.locator("#accountButton").click();
  await page.locator("#accountLogout").click();
  await page.waitForSelector("#authScreen", {state: "visible"});
  await page.locator("#tabLogin").click();
  await page.fill("#loginEmail", "other@viniti.ru");
  await page.fill("#loginPassword", "password123");
  await page.locator("#loginForm button[type=submit]").click();
  await page.waitForTimeout(200);
  check("вход: чужой email отклонён", await page.locator("#authScreen").isVisible());
  check("вход: объяснение показано", (await page.locator("#authStatus").textContent()).includes("уже сохранён профиль"));
  await context.close();
}

// 3. Гость не затирает профиль владельца
{
  const {context, page} = await fresh();
  await page.goto(`${base}/`, {waitUntil: "networkidle"});
  await page.locator("#guestBtn").click();
  await page.waitForSelector("#authScreen", {state: "hidden"});
  await page.waitForSelector("#wizard", {state: "visible"});
  check("гость: каталог открыт", await page.locator("#wizard").isVisible());
  check("гость: шапка", (await page.locator("#accountName").textContent()).trim() === "Гость");
  await page.locator("#accountButton").click();
  check("гость: режим", (await page.locator("#accountMode").textContent()).includes("Гостевой доступ"));
  await page.locator("#accountProfileLink").click();
  await page.waitForSelector(".profile-identity");
  check("гость: предупреждение", (await page.locator(".notice.warn").textContent()).includes("Гостевой доступ"));
  await page.locator(".profile-side-nav .danger-link").click();
  await page.waitForSelector("#authScreen", {state: "visible"});
  check("гость: запись не попала в профиль", await page.evaluate(() => localStorage.getItem("viniti.account.v1")) === null);
  await context.close();
}

// 4. Недоступный localStorage: вход работает в памяти
{
  const {context, page, errors} = await fresh({width: 1280, height: 800}, async ctx => {
    await ctx.addInitScript(() => {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        get() { throw new DOMException("Storage is disabled", "SecurityError"); },
      });
    });
  });
  await page.goto(`${base}/`, {waitUntil: "networkidle"});
  check("хранилище: экран входа", await page.locator("#authScreen").isVisible());
  check("хранилище: честное предупреждение", await page.locator("#authStorageNote").isVisible());
  await register(page);
  check("хранилище: регистрация работает в памяти", await page.locator("#wizard").isVisible());
  check("хранилище: каталог доступен", (await page.locator("#accountName").textContent()).trim() === "Илья");
  check("хранилище: нет ошибок в консоли", errors.length === 0, errors.join(" | "));
  await context.close();
}

// 5. Мобильная ширина
{
  const {context, page} = await fresh({width: 375, height: 812});
  await page.goto(`${base}/`, {waitUntil: "networkidle"});
  check("мобильный: карточка видна", await page.locator(".auth-card").isVisible());
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check("мобильный: нет горизонтального скролла", overflow <= 0, String(overflow));
  await page.screenshot({path: "/tmp/viniti-auth-mobile.png"});
  await register(page);
  await page.locator("#accountButton").click();
  const box = await page.locator("#accountMenu").boundingBox();
  check("мобильный: поповер внутри экрана", box.x >= 0 && box.x + box.width <= 375, JSON.stringify(box));
  await context.close();
}

await browser.close();
const failed = out.filter(item => !item.ok);
console.log(`\n${out.length - failed.length}/${out.length} проверок пройдено`);
process.exit(failed.length ? 1 : 0);
