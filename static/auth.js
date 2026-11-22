// Аккаунт исследователя: вход и пароль проверяет сервер (`auth.py`), cookie
// сессии HttpOnly ставит он же. В браузере остаются только поля профиля
// (имя, организация, роль, описание, настройки и лента) - по одному набору
// на id пользователя. Пароль нигде в localStorage не хранится.

import { postJSON, loadSession, setCSRF } from "./api.js";

const PROFILE_PREFIX = "viniti.profile.v1:";
const LEGACY_KEY = "viniti.account.v1";
const MAX_ACTIVITY = 12;

const MONTHS = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];

// Память процесса: страховка для приватного режима и запрета хранения.
const memoryProfiles = new Map();
let serverUser = null;
let cachedAccount = null;

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
  const parts = String(name || "Профиль").trim().split(/\s+/).filter(Boolean).slice(0, 2);
  const letters = parts.map(part => part.charAt(0).toUpperCase()).join("");
  return letters || "Г";
}

export function firstName(name) {
  return String(name || "Профиль").trim().split(/\s+/)[0] || "Профиль";
}

// Логин на сервере: латиница, цифры и . _ + - @, от 3 до 254 символов.
// Тот же набор разрешает auth.USERNAME_RE, поэтому проверка здесь только
// повторяет контракт и не даёт отправить заведомо отклоняемое значение.
const LOGIN_RE = /^[A-Za-z0-9][A-Za-z0-9_.+@-]{2,253}$/;

export function loginShaped(value) {
  return LOGIN_RE.test(String(value || "").trim());
}

function sinceLabel(date) {
  return `${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
}

function text(value, fallback = "") {
  return String(value === null || value === undefined ? fallback : value).trim();
}

// Mirrors auth.email_problem: the server stays authoritative, but the form must
// not send values the API will reject (for example a numeric-only "TLD" like
// name@example.45). The final domain label has to start with a letter.
const EMAIL_MAX = 254;
const PASSWORD_MIN = 8;
const PASSWORD_MAX = 256;

export function emailOk(value) {
  const email = String(value || "");
  if (!email || email.length > EMAIL_MAX) return false;
  const at = email.indexOf("@");
  if (at <= 0 || at !== email.lastIndexOf("@")) return false;
  const local = email.slice(0, at);
  const domain = email.slice(at + 1);
  if (local.length > 64) return false;
  if (local.includes("..")) return false;
  if (!/^[A-Za-z0-9](?:[A-Za-z0-9_.+-]*[A-Za-z0-9])?$/.test(local)) return false;
  const labels = domain.split(".");
  if (labels.length < 2) return false;
  const labelOk = label => label.length <= 63
    && /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(label);
  if (!labels.every(labelOk)) return false;
  return /^[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]$/.test(labels[labels.length - 1]);
}

function emailShaped(value) {
  return emailOk(value);
}

function nameFromUsername(username) {
  const local = String(username || "").split("@")[0].replace(/[._-]+/g, " ").trim();
  return local
    ? local.split(/\s+/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ")
    : "Исследователь";
}

function profileKey(userId) {
  return `${PROFILE_PREFIX}${userId}`;
}

function normalizeProfile(raw) {
  if (!raw || typeof raw !== "object") return null;
  const createdAt = Number(raw.createdAt);
  const stamp = Number.isFinite(createdAt) && createdAt > 0 ? createdAt : Date.now();
  return {
    name: text(raw.name),
    email: text(raw.email),
    org: text(raw.org, "Без организации") || "Без организации",
    role: text(raw.role, "Исследователь") || "Исследователь",
    bio: text(raw.bio),
    createdAt: stamp,
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

function readProfile(userId) {
  const store = storage();
  if (!store) return memoryProfiles.get(userId) || null;
  let raw;
  try {
    raw = store.getItem(profileKey(userId));
  } catch {
    return memoryProfiles.get(userId) || null;
  }
  if (!raw) return memoryProfiles.get(userId) || null;
  try {
    return normalizeProfile(JSON.parse(raw));
  } catch {
    return null;
  }
}

function saveProfile(userId, profile) {
  const normalized = normalizeProfile(profile);
  if (!normalized) return null;
  memoryProfiles.set(userId, normalized);
  const store = storage();
  if (!store) return normalized;
  try {
    store.setItem(profileKey(userId), JSON.stringify(normalized));
  } catch {
    // Приватный режим или переполненное хранилище: экран продолжает работать,
    // запись не переживёт перезагрузку.
  }
  return normalized;
}

// Профиль прошлой (локальной) версии переезжает к текущему пользователю,
// если там тот же email. Иначе он остаётся в старом ключе нетронутым.
function legacyProfile(userId) {
  const store = storage();
  if (!store) return null;
  let raw;
  try {
    raw = store.getItem(LEGACY_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const profile = normalizeProfile(parsed);
  if (!profile) return null;
  const email = profile.email.toLowerCase();
  // An empty email was the old guest record; it must not leak into the first
  // real account of this browser.
  if (!email || email !== String(serverUser?.username || "").toLowerCase()) return null;
  saveProfile(userId, profile);
  try {
    store.removeItem(LEGACY_KEY);
    store.removeItem("viniti.session.v1");
  } catch {
    // Ключи не мешают: профиль уже перенесён.
  }
  return profile;
}

function baseProfile(fields) {
  return {
    name: fields.name || "",
    email: fields.email || "",
    org: fields.org || "Без организации",
    role: fields.role || "Исследователь",
    bio: fields.bio || "",
    createdAt: Date.now(),
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

function composeAccount(user, profile) {
  const createdAt = profile.createdAt || Number(user.created_at || 0) * 1000 || Date.now();
  const username = String(user.username || "");
  return {
    id: user.id,
    username,
    name: profile.name || nameFromUsername(username),
    email: profile.email || (emailShaped(username) ? username : ""),
    org: profile.org,
    role: profile.role,
    bio: profile.bio,
    guest: false,
    createdAt,
    since: sinceLabel(new Date(createdAt)),
    settings: profile.settings,
    stats: profile.stats,
    activity: profile.activity,
  };
}

function applyUser(user) {
  serverUser = user || null;
  if (!serverUser) {
    cachedAccount = null;
    return null;
  }
  const username = String(serverUser.username || "");
  const profile = readProfile(serverUser.id) || legacyProfile(serverUser.id)
    || baseProfile({
      name: nameFromUsername(username),
      email: emailShaped(username) ? username : "",
      org: "ВИНИТИ РАН",
    });
  saveProfile(serverUser.id, profile);
  cachedAccount = composeAccount(serverUser, profile);
  return cachedAccount;
}

// Восстанавливает серверную сессию по cookie. {"user": null} - штатное
// «не вошёл»; сетевую ошибку пробрасываем, чтобы вход не притворялся.
export async function restoreSession(signal) {
  const data = await loadSession(signal);
  if (!data.user) {
    applyUser(null);
    return null;
  }
  return applyUser(data.user);
}

export function isSignedIn() {
  return Boolean(serverUser) && Boolean(cachedAccount);
}

export function currentAccount() {
  return isSignedIn() ? cachedAccount : null;
}

function updateStoredProfile(patch) {
  if (!serverUser) return null;
  const profile = readProfile(serverUser.id) || baseProfile({});
  const next = {
    ...profile,
    ...patch,
    settings: {...profile.settings, ...(patch.settings || {})},
  };
  const saved = saveProfile(serverUser.id, next);
  cachedAccount = composeAccount(serverUser, saved);
  return cachedAccount;
}

// Регистрация: логин и пароль проверяет сервер, cookie сессии ставит он же.
// Локально остаётся только описательная часть профиля.
export async function registerAccount({login, email, password}) {
  const data = await postJSON("/api/auth/register", {username: login, password});
  setCSRF(data.csrf_token);
  applyUser(data.user);
  updateStoredProfile({
    name: nameFromUsername(login),
    email: text(email),
    role: "Исследователь",
    bio: "",
  });
  return recordActivity(cachedAccount, {
    title: "Аккаунт создан",
    detail: "Добро пожаловать в каталог.",
    kind: "account",
  });
}

export async function loginAccount(login, password) {
  const data = await postJSON("/api/auth/login", {username: login, password});
  setCSRF(data.csrf_token);
  const account = applyUser(data.user);
  return recordActivity(cachedAccount || account, {
    title: "Вход в аккаунт",
    detail: "",
    kind: "account",
  });
}

// Выход стирает серверную сессию. Локальный профиль остаётся, чтобы
// повторный вход вернул настройки и ленту.
export function signOutAccount() {
  const request = postJSON("/api/auth/logout", {}).catch(error => {
    console.warn("Серверная сессия не завершилась:", error.message);
  });
  serverUser = null;
  cachedAccount = null;
  setCSRF("");
  return request;
}

export function updateAccount(patch) {
  if (!cachedAccount) return null;
  return updateStoredProfile(patch);
}

export function recordActivity(account, {title, detail = "", kind = "action"}) {
  if (!account || !serverUser) return account || null;
  const activity = [
    {at: Date.now(), title, detail, kind},
    ...(account.activity || []),
  ].slice(0, MAX_ACTIVITY);
  const stats = {...account.stats};
  stats.actions = Number(stats.actions || 0) + 1;
  if (kind === "upload") stats.docs = Number(stats.docs || 0) + 1;
  return updateStoredProfile({activity, stats});
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

// Экран первого входа: фон на всю страницу и карточка регистрации по центру.
// root - узел #authScreen из index.html, все поля и кнопки берутся из него.
export function mountAuthScreen(root, {onEnter}) {
  if (!root) {
    return {show() {}, hide() {}, reset() {}};
  }
  const byId = id => root.querySelector(`#${id}`);
  const tabsRoot = root.querySelector("[data-tabs]");
  const tabButtons = tabsRoot ? [...tabsRoot.querySelectorAll("[role=tab]")] : [];

  const FIELDS = ["regLogin", "regEmail", "regPassword", "loginUser", "loginPassword"];

  const validators = {
    regLogin: () => {
      const value = byId("regLogin").value.trim();
      if (!value) return "Придумайте логин.";
      return loginShaped(value)
        ? ""
        : "Логин: от 3 символов, латиница, цифры и знаки . _ + - @.";
    },
    regEmail: () => {
      const value = byId("regEmail").value.trim();
      // Email необязателен: вход выполняется по логину. Но если его указали,
      // сервер и клиент проверяют его одинаково строго.
      if (!value) return "";
      return emailOk(value) ? "" : "Проверьте email, например name@example.com.";
    },
    regPassword: () => {
      const value = byId("regPassword").value;
      if (value.length < PASSWORD_MIN) {
        return `Пароль должен быть не короче ${PASSWORD_MIN} символов.`;
      }
      if (value.length > PASSWORD_MAX) {
        return `Пароль должен быть не длиннее ${PASSWORD_MAX} символов.`;
      }
      return "";
    },
    loginUser: () => {
      const value = byId("loginUser").value.trim();
      if (!value) return "Введите логин или email.";
      return loginShaped(value) ? "" : "Логин: от 3 символов, без пробелов.";
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
      input.setAttribute("aria-describedby", `${id}-error`);
      return true;
    }
    input.classList.remove("is-invalid");
    input.removeAttribute("aria-invalid");
    error.textContent = "";
    error.hidden = true;
    input.removeAttribute("aria-describedby");
    return false;
  }

  function clearErrors() {
    for (const id of FIELDS) checkField(id, "");
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
    setStatus("");
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

  async function send(button, task) {
    if (button) button.disabled = true;
    try {
      await task();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  byId("registerForm")?.addEventListener("submit", event => {
    event.preventDefault();
    let errors = 0;
    for (const id of ["regLogin", "regEmail", "regPassword"]) {
      if (checkField(id, validators[id]())) errors += 1;
    }
    if (errors) {
      setStatus("Проверьте отмеченные поля.", true);
      (root.querySelector("#panelReg .is-invalid") || byId("regLogin")).focus();
      return;
    }
    setStatus("Создаём аккаунт…");
    const button = byId("registerForm").querySelector('button[type="submit"]');
    send(button, async () => {
      const account = await registerAccount({
        login: byId("regLogin").value.trim(),
        email: byId("regEmail").value.trim(),
        password: byId("regPassword").value,
      });
      hide();
      resetForms();
      onEnter(account, "Аккаунт создан. Загрузите первый PDF.");
    });
  });

  byId("loginForm")?.addEventListener("submit", event => {
    event.preventDefault();
    let errors = 0;
    if (checkField("loginUser", validators.loginUser())) errors += 1;
    if (checkField("loginPassword", validators.loginPassword())) errors += 1;
    if (errors) {
      setStatus("Проверьте логин и пароль.", true);
      (root.querySelector("#panelLogin .is-invalid") || byId("loginUser")).focus();
      return;
    }
    setStatus("Проверяем пароль…");
    const button = byId("loginForm").querySelector('button[type="submit"]');
    send(button, async () => {
      const account = await loginAccount(
        byId("loginUser").value.trim(),
        byId("loginPassword").value,
      );
      hide();
      resetForms();
      onEnter(account, "Вы вошли.");
    });
  });

  byId("forgotBtn")?.addEventListener("click", () => {
    setStatus("В прототипе восстановление пароля не подключено: зарегистрируйте новый аккаунт.", false);
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
    document.title = "Вход · ВИНИТИ";
    const bootError = document.getElementById("bootError");
    if (bootError) bootError.hidden = true;
    const storageNote = byId("authStorageNote");
    if (storageNote) storageNote.hidden = storageWorks();
    setStatus(message, isError);
    selectTab(0, false);
    byId("regLogin")?.focus({preventScroll: true});
  }

  function hide() {
    document.documentElement.classList.remove("auth-locked");
    setStatus("");
  }

  return {show, hide, reset: resetForms};
}
