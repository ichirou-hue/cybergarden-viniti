import { chromium } from "playwright-core";

// Проверки входа и профиля на серверной сессии: пароль проверяет мок-сервер,
// cookie сессии ставит он же, в localStorage остаётся только профиль.
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

async function register(page, login = "i.safonov", email = "i.safonov@viniti.ru") {
  await page.fill("#regLogin", login);
  await page.fill("#regEmail", email);
  await page.fill("#regPassword", "password123");
  await page.locator("#registerForm button[type=submit]").click();
  await page.waitForSelector("#authScreen", {state: "hidden"});
  // Ждём, пока route отрисует целевой экран: класс снимается раньше.
  await page.waitForFunction(() => document.body.classList.contains("wizard-active")
    || Boolean(document.body.dataset.view));
}

// Профиль хранится по id пользователя, а мок-сервер выдаёт id, равный логину.
const profileKey = login => `viniti.profile.v1:${login}`;

// 1. Первый вход закрывает каталог
{
  const {context, page, errors} = await fresh();
  await page.goto(`${base}/#/catalog?track=rag`, {waitUntil: "networkidle"});
  check("вход: экран регистрации виден", await page.locator("#authScreen").isVisible());
  check("вход: каталог не отрисован", !(await page.locator("#view").isVisible()));
  check("вход: меню аккаунта скрыто", !(await page.locator("#account").isVisible()));
  check("вход: фокус в поле логина", await page.evaluate(() => document.activeElement?.id) === "regLogin");
  await page.locator("#registerForm button[type=submit]").click();
  check("вход: пустая форма ругается", (await page.locator("#authStatus").textContent()).includes("Проверьте"));
  check("вход: поле помечено", await page.locator("#regLogin").getAttribute("aria-invalid") === "true");

  await register(page);
  check("регистрация: открылся мастер загрузки", await page.locator("#wizard").isVisible());
  check("регистрация: имя в шапке", (await page.locator("#accountName").textContent()).trim() === "I");
  check("регистрация: фокус ушёл в приложение", await page.evaluate(() => document.activeElement?.id) === "wizardTitle");

  await page.locator("#accountButton").click();
  check("поповер: раскрыт", await page.locator("#accountMenu").isVisible());
  check("поповер: aria-expanded", await page.locator("#accountButton").getAttribute("aria-expanded") === "true");
  check("поповер: имя и email", (await page.locator("#accountMenuName").textContent()).includes("I Safonov")
    && (await page.locator("#accountMenuEmail").textContent()).includes("i.safonov@viniti.ru"));
  await page.keyboard.press("Escape");
  check("поповер: Escape закрывает", !(await page.locator("#accountMenu").isVisible()));
  check("поповер: фокус вернулся", await page.evaluate(() => document.activeElement?.id) === "accountButton");

  await page.locator("#accountButton").click();
  await page.locator("#accountProfileLink").click();
  await page.waitForSelector("#profileTitle");
  check("профиль: маршрут", page.url().includes("#/profile"), page.url());
  check("профиль: заголовок вкладки", (await page.title()).includes("Профиль"));
  check("профиль: статистика", await page.locator(".profile-stat").count() === 3);
  check("профиль: дата в каталоге не дублируется",
    (await page.locator("#view").textContent()).split("В каталоге с").length - 1 === 1);
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

  // Выход: серверная сессия завершается, профиль остаётся
  await page.locator("#accountButton").click();
  await page.locator("#accountLogout").click();
  await page.waitForSelector("#authScreen", {state: "visible"});
  check("выход: экран входа", await page.locator("#authScreen").isVisible());
  check("выход: заголовок вкладки", (await page.title()).includes("Вход"));
  check("выход: профиль сохранён", await page.evaluate(key => Boolean(localStorage.getItem(key)),
    profileKey("i.safonov")));

  // Повторный вход с тем же email возвращает профиль
  await page.locator("#tabLogin").click();
  await page.fill("#loginUser", "i.safonov");
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

// 2. Неверный пароль и незарегистрированный email не пускают
{
  const {context, page} = await fresh();
  await page.goto(`${base}/`, {waitUntil: "networkidle"});
  await register(page, "second", "second@viniti.ru");
  await page.locator("#accountButton").click();
  await page.locator("#accountLogout").click();
  await page.waitForSelector("#authScreen", {state: "visible"});
  await page.locator("#tabLogin").click();
  await page.fill("#loginUser", "other");
  await page.fill("#loginPassword", "password123");
  await page.locator("#loginForm button[type=submit]").click();
  await page.waitForTimeout(300);
  check("вход: чужой логин отклонён", await page.locator("#authScreen").isVisible());
  check("вход: объяснение показано", (await page.locator("#authStatus").textContent()).includes("Неверное имя пользователя"));
  await page.fill("#loginUser", "second");
  await page.fill("#loginPassword", "wrong-password");
  await page.locator("#loginForm button[type=submit]").click();
  await page.waitForTimeout(300);
  check("вход: неверный пароль отклонён", await page.locator("#authScreen").isVisible());
  await context.close();
}

// 3. Недоступный localStorage: серверная сессия работает, профиль в памяти
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
  await register(page, "third", "storage@viniti.ru");
  check("хранилище: регистрация работает в памяти", await page.locator("#wizard").isVisible());
  check("хранилище: каталог доступен", (await page.locator("#accountName").textContent()).trim() === "Third");
  check("хранилище: нет ошибок в консоли", errors.length === 0, errors.join(" | "));
  await context.close();
}

// 4. Мобильная ширина
{
  const {context, page} = await fresh({width: 375, height: 812});
  await page.goto(`${base}/`, {waitUntil: "networkidle"});
  check("мобильный: карточка видна", await page.locator(".auth-card").isVisible());
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check("мобильный: нет горизонтального скролла", overflow <= 0, String(overflow));
  await page.screenshot({path: "/tmp/viniti-auth-mobile.png"});
  await register(page, "mobile", "mobile@viniti.ru");
  await page.locator("#accountButton").click();
  const box = await page.locator("#accountMenu").boundingBox();
  check("мобильный: поповер внутри экрана", box.x >= 0 && box.x + box.width <= 375, JSON.stringify(box));
  await context.close();
}

await browser.close();
const failed = out.filter(item => !item.ok);
console.log(`\n${out.length - failed.length}/${out.length} проверок пройдено`);
process.exit(failed.length ? 1 : 0);
