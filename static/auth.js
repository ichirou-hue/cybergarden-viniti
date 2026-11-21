// Аккаунт исследователя без сервера: запись живёт в localStorage этого браузера.
// Пароль не сохраняется и никуда не отправляется, серверной проверки пока нет.
// Ключи продублированы в inline-скрипте static/index.html: он ставит класс
// auth-locked до первой отрисовки, чтобы не мелькал каталог.
//
// Если браузер запрещает localStorage, аккаунт всё равно работает в памяти
// модуля до перезагрузки страницы, поэтому вход и навигация не ломаются.

const STORE_KEY = "viniti.account.v1";
const SESSION_KEY = "viniti.session.v1";
const SESSION_OUT = "out";
const MAX_ACTIVITY = 12;

// Память процесса: страховка для приватного режима и запрета хранения.
// Гость не пишется в localStorage вовсе, иначе затирал бы профиль владельца.
let memoryAccount = null;
let memoryGuest = null;
let memorySignedOut = false;

const MONTHS = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];

function storage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

// Проверка записи, а не только доступа к объекту: приватный режим умеет
// бросать ошибку уже на setItem.
export function storageWorks() {
  const store = storage();
  if (!store) return false;
  const probe = "__viniti_probe__";
  try {
    store.setItem(probe, "1");
    store.removeItem(probe);
    return true;
  } catch {
    return false;
  }
}

function pad(value) {
  return String(value).padStart(2, "0");
}

export function initials(name) {
  const parts = String(name || "Гость").trim().split(/\s+/).filter(Boolean).slice(0, 2);
  const letters = parts.map(part => part.charAt(0).toUpperCase()).join("");
  return letters || "Г";
}

export function firstName(name) {
  return String(name || "Профиль").trim().split(/\s+/)[0] || "Профиль";
}

export function modeLabel(account) {
  if (!account) return "";
  if (account.guest) return "Гостевой доступ";
  return "Локальный аккаунт";
}

export function modeChip(account) {
  return account ? modeLabel(account).toLowerCase() : "";
}

function sinceLabel(date) {
  return `${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
}

function text(value, fallback = "") {
  return String(value === null || value === undefined ? fallback : value).trim();
}

function normalize(raw) {
  if (!raw || typeof raw !== "object") return null;
  const name = text(raw.name);
  if (!name) return null;
  const createdAt = Number(raw.createdAt);
  return {
    name,
    email: text(raw.email),
    org: text(raw.org, "Без организации") || "Без организации",
    role: text(raw.role, "Исследователь"),
    bio: text(raw.bio),
    guest: Boolean(raw.guest),
    createdAt: Number.isFinite(createdAt) && createdAt > 0 ? createdAt : Date.now(),
    since: text(raw.since) || sinceLabel(new Date(Number.isFinite(createdAt) ? createdAt : Date.now())),
    settings: {
      notifyIssues: raw.settings?.notifyIssues !== false,
      onlyVerified: raw.settings?.onlyVerified !== false,
      compact: Boolean(raw.settings?.compact),
      language: text(raw.settings?.language, "ru") || "ru",
      area: text(raw.settings?.area, "solar") || "solar",
    },
    stats: {
      docs: Math.max(0, Number(raw.stats?.docs) || 0),
      actions: Math.max(0, Number(raw.stats?.actions) || 0),
    },
    activity: Array.isArray(raw.activity)
      ? raw.activity
        .filter(item => item && Number.isFinite(Number(item.at)) && text(item.title))
        .slice(0, MAX_ACTIVITY)
        .map(item => ({
          at: Number(item.at),
          title: text(item.title),
          detail: text(item.detail),
          kind: text(item.kind, "action") || "action",
        }))
      : [],
  };
}

export function readAccount() {
  // Гостевая сессия живёт только в памяти вкладки и не затирает чужой профиль.
  if (memoryGuest) return memoryGuest;
  const store = storage();
  if (!store) return memoryAccount;
  let raw;
  try {
    raw = store.getItem(STORE_KEY);
  } catch {
    return memoryAccount;
  }
  if (!raw) {
    memoryAccount = null;
    return null;
  }
  try {
    memoryAccount = normalize(JSON.parse(raw));
  } catch {
    memoryAccount = null;
  }
  return memoryAccount;
}

function signedOut() {
  const store = storage();
  if (!store) return memorySignedOut;
  try {
    return store.getItem(SESSION_KEY) === SESSION_OUT;
  } catch {
    return memorySignedOut;
  }
}

function setSignedOut(value) {
  memorySignedOut = value;
  const store = storage();
  if (!store) return;
  try {
    if (value) store.setItem(SESSION_KEY, SESSION_OUT);
    else store.removeItem(SESSION_KEY);
  } catch {
    // Запись не переживёт перезагрузку, но текущая вкладка уже знает ответ.
  }
}

export function isSignedIn() {
  return Boolean(readAccount()) && !signedOut();
}

export function currentAccount() {
  return isSignedIn() ? readAccount() : null;
}

export function saveAccount(account) {
  const normalized = normalize(account);
  if (!normalized) return null;
  if (normalized.guest) {
    memoryGuest = normalized;
    return normalized;
  }
  memoryGuest = null;
  memoryAccount = normalized;
  const store = storage();
  if (!store) return normalized;
  try {
    store.setItem(STORE_KEY, JSON.stringify(normalized));
  } catch {
    // Приватный режим или переполненное хранилище: экран продолжает работать,
    // запись не переживёт перезагрузку.
  }
  return normalized;
}

// Выход завершает сессию, но не стирает профиль: по тому же email можно
// вернуться и сохранить историю, настройки и описание.
export function signOutAccount() {
  memoryGuest = null;
  setSignedOut(true);
}

function baseAccount(fields) {
  const now = new Date();
  return {
    name: fields.name,
    email: fields.email || "",
    org: fields.org || "Без организации",
    role: fields.role || "Исследователь",
    bio: fields.bio || "",
    guest: Boolean(fields.guest),
    createdAt: now.getTime(),
    since: sinceLabel(now),
    settings: {
      notifyIssues: true,
      onlyVerified: true,
      compact: false,
      language: "ru",
      area: "solar",
    },
    stats: {docs: 0, actions: 0},
    activity: [],
  };
}

export function registerAccount({name, email, org}) {
  const account = baseAccount({
    name,
    email,
    org,
    role: "Исследователь",
    bio: "Заполните описание в настройках профиля, чтобы коллеги понимали, с какими данными вы работаете.",
  });
  memoryGuest = null;
  setSignedOut(false);
  return recordActivity(account, {
    title: "Аккаунт создан",
    detail: "Профиль сохранён в этом браузере.",
    kind: "account",
  });
}

export function loginAccount(email) {
  const clean = text(email);
  const normalized = clean.toLowerCase();
  const saved = readAccount();
  if (saved && !saved.guest) {
    if (saved.email.toLowerCase() !== normalized) {
      return {
        ok: false,
        error: `В этом браузере уже сохранён профиль ${saved.email}. Войдите с этим email или создайте новый аккаунт.`,
      };
    }
    memoryGuest = null;
    setSignedOut(false);
    return {
      ok: true,
      account: recordActivity(saved, {
        title: "Вход в аккаунт",
        detail: "Профиль найден в этом браузере.",
        kind: "account",
      }),
    };
  }
  const local = normalized.split("@")[0].replace(/[._-]+/g, " ").trim();
  const name = local
    ? local.split(/\s+/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ")
    : "Исследователь";
  const account = baseAccount({name, email: clean, org: "ВИНИТИ РАН", role: "Исследователь"});
  memoryGuest = null;
  setSignedOut(false);
  return {
    ok: true,
    account: recordActivity(account, {
      title: "Вход в каталог",
      detail: "Аккаунт создан локально при входе.",
      kind: "account",
    }),
  };
}

export function createGuest() {
  const account = baseAccount({
    name: "Гость",
    email: "",
    org: "Без организации",
    role: "Гостевой доступ",
    bio: "Гостевой доступ: каталог открыт, данные профиля остаются в этой вкладке.",
    guest: true,
  });
  setSignedOut(false);
  return recordActivity(account, {
    title: "Гостевой вход",
    detail: "Каталог открыт без регистрации.",
    kind: "account",
  });
}

export function updateAccount(patch) {
  const account = readAccount();
  if (!account) return null;
  const next = {
    ...account,
    ...patch,
    settings: {...account.settings, ...(patch.settings || {})},
  };
  return saveAccount(next);
}

export function recordActivity(account, {title, detail = "", kind = "action"}) {
  if (!account) return null;
  const activity = [
    {at: Date.now(), title, detail, kind},
    ...(account.activity || []),
  ].slice(0, MAX_ACTIVITY);
  const stats = {...account.stats};
  stats.actions = Number(stats.actions || 0) + 1;
  if (kind === "upload") stats.docs = Number(stats.docs || 0) + 1;
  return saveAccount({...account, activity, stats});
}

export function accountStats(account) {
  const activity = account?.activity || [];
  const lastUpload = activity.find(item => item.kind === "upload");
  return {
    docs: Number(account?.stats?.docs || 0),
    actions: Math.max(Number(account?.stats?.actions || 0), activity.length),
    last: lastUpload ? lastUpload.at : 0,
  };
}

export function describeWhen(timestamp) {
  if (!timestamp) return "-";
  const date = new Date(Number(timestamp));
  if (Number.isNaN(date.getTime())) return "-";
  const today = new Date();
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (date.toDateString() === today.toDateString()) return `Сегодня, ${time}`;
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `Вчера, ${time}`;
  return `${date.getDate()} ${MONTHS[date.getMonth()]}`;
}

function emailOk(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value);
}

// Экран первого входа: фон на всю страницу и карточка регистрации по центру.
// root - узел #authScreen из index.html, все поля и кнопки берутся из него.
export function mountAuthScreen(root, {onEnter}) {
  if (!root) {
    return {show() {}, hide() {}, reset() {}};
  }
  const byId = id => root.querySelector(`#${id}`);
  const tabsRoot = root.querySelector("[data-tabs]");
  const tabButtons = tabsRoot ? [...tabsRoot.querySelectorAll("[role=tab]")] : [];

  const FIELDS = ["regName", "regOrg", "regEmail", "regPassword", "loginEmail", "loginPassword"];

  const validators = {
    regName: () => {
      const value = byId("regName").value.trim();
      return value.length >= 2 ? "" : "Укажите имя и фамилию.";
    },
    regOrg: () => {
      const value = byId("regOrg").value.trim();
      return value.length >= 2 ? "" : "Укажите организацию.";
    },
    regEmail: () => {
      const value = byId("regEmail").value.trim();
      return emailOk(value) ? "" : "Введите email, например name@viniti.ru.";
    },
    regPassword: () => {
      const value = byId("regPassword").value;
      return value.length >= 8 ? "" : "Пароль должен быть не короче 8 символов.";
    },
    loginEmail: () => {
      const value = byId("loginEmail").value.trim();
      return emailOk(value) ? "" : "Введите email, например name@viniti.ru.";
    },
    loginPassword: () => {
      const value = byId("loginPassword").value;
      return value.length >= 1 ? "" : "Введите пароль.";
    },
  };

  function setStatus(message, isError = false) {
    const node = byId("authStatus");
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("is-error", Boolean(isError));
  }

  function checkField(id, message) {
    const input = byId(id);
    const error = byId(`${id}-error`);
    if (!input || !error) return false;
    if (message) {
      input.classList.add("is-invalid");
      input.setAttribute("aria-invalid", "true");
      error.textContent = message;
      error.hidden = false;
      input.setAttribute("aria-describedby", id === "regPassword"
        ? "regPassword-hint regPassword-error"
        : `${id}-error`);
      return true;
    }
    input.classList.remove("is-invalid");
    input.removeAttribute("aria-invalid");
    error.textContent = "";
    error.hidden = true;
    if (id === "regPassword") input.setAttribute("aria-describedby", "regPassword-hint");
    else input.removeAttribute("aria-describedby");
    return false;
  }

  function clearErrors() {
    for (const id of FIELDS) checkField(id, "");
    const consent = byId("regConsent");
    const consentError = byId("regConsent-error");
    if (consent) consent.removeAttribute("aria-invalid");
    if (consentError) consentError.hidden = true;
    setStatus("");
  }

  function resetForms() {
    byId("registerForm")?.reset();
    byId("loginForm")?.reset();
    clearErrors();
    selectTab(0, false);
    for (const button of root.querySelectorAll("[data-toggle-pass]")) {
      const input = byId(button.getAttribute("data-toggle-pass"));
      if (!input) continue;
      input.type = "password";
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", "Показать пароль");
      button.textContent = "Показать";
    }
  }

  function selectTab(index, moveFocus) {
    tabButtons.forEach((tab, i) => {
      const active = i === index;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      const panel = byId(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !active;
    });
    if (moveFocus) tabButtons[index]?.focus();
  }

  tabButtons.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(index, false));
    tab.addEventListener("keydown", event => {
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % tabButtons.length;
      if (event.key === "ArrowLeft") next = (index - 1 + tabButtons.length) % tabButtons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabButtons.length - 1;
      if (next !== null) {
        event.preventDefault();
        selectTab(next, true);
      }
    });
  });

  for (const id of FIELDS) {
    const input = byId(id);
    input?.addEventListener("input", () => {
      if (input.getAttribute("aria-invalid") === "true") checkField(id, validators[id]());
      if (root.querySelector("#authStatus.is-error")) setStatus("");
    });
  }

  byId("regConsent")?.addEventListener("change", event => {
    if (event.currentTarget.checked) {
      event.currentTarget.removeAttribute("aria-invalid");
      const error = byId("regConsent-error");
      if (error) error.hidden = true;
    }
  });

  byId("registerForm")?.addEventListener("submit", event => {
    event.preventDefault();
    let errors = 0;
    for (const id of ["regName", "regOrg", "regEmail", "regPassword"]) {
      if (checkField(id, validators[id]())) errors += 1;
    }
    const consent = byId("regConsent");
    const consentError = byId("regConsent-error");
    if (!consent.checked) {
      consent.setAttribute("aria-invalid", "true");
      consentError.textContent = "Подтвердите согласие, чтобы создать аккаунт.";
      consentError.hidden = false;
      errors += 1;
    } else {
      consent.removeAttribute("aria-invalid");
      consentError.hidden = true;
    }
    if (errors) {
      setStatus("Проверьте отмеченные поля.", true);
      (root.querySelector("#panelReg .is-invalid") || consent).focus();
      return;
    }
    setStatus("");
    const account = registerAccount({
      name: byId("regName").value.trim(),
      email: byId("regEmail").value.trim(),
      org: byId("regOrg").value.trim(),
    });
    hide();
    resetForms();
    onEnter(account, "Аккаунт создан. Добро пожаловать в каталог.");
  });

  byId("loginForm")?.addEventListener("submit", event => {
    event.preventDefault();
    let errors = 0;
    if (checkField("loginEmail", validators.loginEmail())) errors += 1;
    if (checkField("loginPassword", validators.loginPassword())) errors += 1;
    if (errors) {
      setStatus("Проверьте email и пароль.", true);
      (root.querySelector("#panelLogin .is-invalid") || byId("loginEmail")).focus();
      return;
    }
    setStatus("");
    const email = byId("loginEmail").value.trim();
    const result = loginAccount(email);
    if (!result.ok) {
      setStatus(result.error, true);
      byId("loginEmail").focus();
      return;
    }
    hide();
    resetForms();
    onEnter(result.account, "Вы вошли в каталог.");
  });

  byId("forgotBtn")?.addEventListener("click", () => {
    setStatus("В демо-режиме восстановление пароля не подключено: создайте новый аккаунт.", false);
  });

  byId("guestBtn")?.addEventListener("click", () => {
    const account = createGuest();
    hide();
    resetForms();
    onEnter(account, "Вы вошли как гость.");
  });

  for (const button of root.querySelectorAll("[data-toggle-pass]")) {
    button.addEventListener("click", () => {
      const input = byId(button.getAttribute("data-toggle-pass"));
      if (!input) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.setAttribute("aria-pressed", String(show));
      button.setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль");
      button.textContent = show ? "Скрыть" : "Показать";
    });
  }

  function show(message = "", isError = false) {
    document.documentElement.classList.add("auth-locked");
    document.body.classList.remove("wizard-active");
    document.body.dataset.view = "";
    document.title = "Вход · ВИНТИ";
    const bootError = document.getElementById("bootError");
    if (bootError) bootError.hidden = true;
    const storageNote = byId("authStorageNote");
    if (storageNote) storageNote.hidden = storageWorks();
    setStatus(message, isError);
    byId("regName")?.focus({preventScroll: true});
  }

  function hide() {
    document.documentElement.classList.remove("auth-locked");
    setStatus("");
  }

  return {show, hide, reset: resetForms};
}
