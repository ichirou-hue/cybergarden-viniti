import { el, fill } from "./dom.js";
import {
  accountStats,
  currentAccount,
  describeWhen,
  firstName,
  initials,
  modeChip,
  modeLabel,
  updateAccount,
} from "./auth.js";

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

// Поповер в правом верхнем углу: аватар с инициалами и меню аккаунта.
export function mountAccountMenu({navigate, onLogout}) {
  const root = document.getElementById("account");
  const button = document.getElementById("accountButton");
  const menu = document.getElementById("accountMenu");
  if (!root || !button || !menu) {
    return {sync() {}, close() {}};
  }
  let open = false;

  function openMenu(moveFocus) {
    menu.hidden = false;
    open = true;
    button.setAttribute("aria-expanded", "true");
    if (moveFocus) menu.querySelector("[role=menuitem]")?.focus();
  }

  function closeMenu(returnFocus) {
    if (!open && menu.hidden) return;
    menu.hidden = true;
    open = false;
    button.setAttribute("aria-expanded", "false");
    if (returnFocus) button.focus();
  }

  function sync(account) {
    root.hidden = !account;
    if (!account) {
      closeMenu(false);
      return;
    }
    setText("accountAvatar", initials(account.name));
    setText("accountName", firstName(account.name));
    setText("accountMenuAvatar", initials(account.name));
    setText("accountMenuName", account.name);
    setText("accountMenuEmail", account.email || "Аккаунт без email");
    setText("accountMode", modeLabel(account));
    setText("accountLogout", account.guest ? "Завершить гостевой доступ" : "Выйти из аккаунта");
    button.setAttribute("aria-label", `Меню профиля: ${account.name}`);
  }

  button.addEventListener("click", event => {
    event.stopPropagation();
    if (open) closeMenu(false);
    else openMenu(true);
  });

  document.addEventListener("click", event => {
    if (open && !menu.contains(event.target) && !button.contains(event.target)) {
      closeMenu(false);
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      closeMenu(true);
    }
  });

  menu.addEventListener("keydown", event => {
    const items = [...menu.querySelectorAll("[role=menuitem]")];
    const index = items.indexOf(document.activeElement);
    let next = null;
    if (event.key === "ArrowDown") next = (index + 1) % items.length;
    if (event.key === "ArrowUp") next = (index - 1 + items.length) % items.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = items.length - 1;
    if (next !== null) {
      event.preventDefault();
      items[next]?.focus();
    }
    if (event.key === "Tab") closeMenu(false);
  });

  document.getElementById("accountCatalog")?.addEventListener("click", event => {
    event.preventDefault();
    closeMenu(false);
    navigate("#/catalog");
  });

  document.getElementById("accountProfileLink")?.addEventListener("click", event => {
    event.preventDefault();
    closeMenu(false);
    navigate("#/profile");
  });

  document.getElementById("accountSettings")?.addEventListener("click", () => {
    closeMenu(false);
    navigate("#/profile?section=settings");
  });

  document.getElementById("accountLogout")?.addEventListener("click", () => {
    closeMenu(false);
    onLogout();
  });

  return {sync, close: () => closeMenu(false)};
}

function statCard(label, value) {
  return el("div", {class: "profile-stat"},
    el("span", {text: label}),
    el("strong", {text: String(value)}));
}

function activityFeed(account) {
  if (!account.activity.length) {
    return el("div", {class: "profile-empty"},
      el("p", {text: "Пока нет действий. Разберите PDF из мастера, и запись появится здесь."}),
      el("a", {class: "button primary", href: "#/", text: "Добавить PDF"}));
  }
  return el("ol", {class: "profile-feed"},
    account.activity.map(item => el("li", {},
      el("span", {class: "profile-when", text: describeWhen(item.at)}),
      el("div", {},
        el("strong", {text: item.title}),
        item.detail ? el("p", {text: item.detail}) : null))));
}

function toggleRow({key, title, detail, checked}) {
  const input = el("input", {
    type: "checkbox",
    role: "switch",
    checked,
    "aria-label": title,
    dataset: {setting: key},
  });
  return {
    input,
    node: el("div", {class: "profile-setting"},
      el("div", {},
        el("strong", {text: title}),
        el("p", {text: detail})),
      el("label", {class: "profile-switch"},
        input,
        el("span", {class: "profile-switch-track", "aria-hidden": "true"}))),
  };
}

// Фрейм профиля: отдельный экран #/profile, доступен из меню и с главной.
export function renderProfile(view, ctx) {
  const account = ctx.account || currentAccount();
  document.title = "Профиль · ВИНИТИ";
  if (!account) {
    fill(view, el("div", {class: "empty"},
      el("h1", {class: "empty-title", text: "Профиль недоступен"}),
      el("p", {class: "muted", text: "Сессия не найдена. Войдите заново."}),
      el("p", {}, el("a", {href: "#/", text: "К регистрации"}))));
    return;
  }

  const stats = accountStats(account);
  const navigate = ctx.navigate || (hash => { location.hash = hash; });
  const onLogout = ctx.onLogout || (() => {});
  const onSave = ctx.onSave || (() => {});
  const flash = ctx.flash || "";

  const title = el("h1", {id: "profileTitle", tabindex: "-1", text: "Профиль"});
  const head = el("div", {class: "profile-head"},
    el("div", {},
      el("p", {class: "eyebrow", text: "Личный кабинет"}),
      title,
      el("p", {class: "lede", text: "Данные аккаунта, история действий и настройки извлечения."})),
    el("span", {class: `badge ${account.guest ? "idle" : "ok"}`, text: modeLabel(account)}));

  const settingsPanel = el("section", {
    class: "panel profile-panel profile-settings",
    id: "profileSettings",
    "aria-labelledby": "profileSettingsTitle",
  });
  const settingsTitle = el("h2", {id: "profileSettingsTitle", tabindex: "-1", text: "Настройки"});
  const settingsStatus = el("p", {class: "profile-saved", role: "status", "aria-live": "polite", text: ""});

  const saveSettings = changes => {
    const updated = updateAccount(changes);
    if (updated) settingsStatus.textContent = "Сохранено в этом браузере.";
  };

  const notify = toggleRow({
    key: "notifyIssues",
    title: "Уведомления о находках",
    detail: "Сообщать, когда в моих публикациях появляется новая находка.",
    checked: account.settings.notifyIssues,
  });
  const verified = toggleRow({
    key: "onlyVerified",
    title: "Только проверенные цитаты",
    detail: "Скрывать строки, у которых цитата ещё не подтверждена.",
    checked: account.settings.onlyVerified,
  });
  const compact = toggleRow({
    key: "compact",
    title: "Компактные таблицы",
    detail: "Показывать больше строк на экране ноутбука.",
    checked: account.settings.compact,
  });
  for (const row of [notify, verified, compact]) {
    row.input.addEventListener("change", () => {
      saveSettings({settings: {[row.input.dataset.setting]: row.input.checked}});
    });
  }

  const language = el("select", {"aria-label": "Язык интерфейса"},
    el("option", {value: "ru", selected: account.settings.language === "ru", text: "Русский"}),
    el("option", {value: "en", selected: account.settings.language === "en", text: "English (демо)"}));
  const area = el("select", {"aria-label": "Область по умолчанию"},
    el("option", {value: "solar", selected: account.settings.area === "solar", text: "solar"}),
    el("option", {value: "rag", selected: account.settings.area === "rag", text: "rag"}));
  language.addEventListener("change", () => saveSettings({settings: {language: language.value}}));
  area.addEventListener("change", () => saveSettings({settings: {area: area.value}}));

  const form = el("form", {class: "profile-form"},
    el("div", {class: "profile-settings-grid"},
      el("label", {}, "Имя и фамилия",
        el("input", {type: "text", name: "name", autocomplete: "name", value: account.name})),
      el("label", {}, "Организация",
        el("input", {type: "text", name: "organization", autocomplete: "organization", value: account.org})),
      el("label", {}, "Роль",
        el("input", {type: "text", name: "role", value: account.role})),
      el("label", {}, "Email",
        el("input", {type: "email", name: "email", value: account.email, readonly: true})),
      el("label", {class: "profile-form-wide"}, "Чем занимаюсь",
        el("textarea", {name: "bio", rows: "3"})),
      el("p", {class: "profile-form-status", role: "status", "aria-live": "polite", text: flash}),
      el("div", {class: "profile-form-actions"},
        el("button", {type: "submit", class: "primary", text: "Сохранить профиль"}))));
  form.elements.bio.value = account.bio || "";
  const formStatus = form.querySelector(".profile-form-status");

  form.addEventListener("submit", event => {
    event.preventDefault();
    const name = form.elements.name.value.trim();
    if (name.length < 2) {
      formStatus.textContent = "Укажите имя и фамилию.";
      formStatus.classList.add("bad");
      form.elements.name.focus();
      return;
    }
    const updated = updateAccount({
      name,
      org: form.elements.organization.value.trim() || "Без организации",
      role: form.elements.role.value.trim() || "Исследователь",
      bio: form.elements.bio.value.trim(),
    });
    onSave(updated);
    renderProfile(view, {
      ...ctx,
      account: updated,
      params: {...ctx.params, section: undefined},
      flash: "Изменения сохранены.",
    });
  });

  const identity = el("aside", {class: "panel profile-panel profile-identity", "aria-label": "Учётная запись"},
    el("span", {class: "profile-avatar", "aria-hidden": "true", text: initials(account.name)}),
    el("h2", {text: account.name}),
    el("p", {class: "profile-role", text: account.role}),
    el("dl", {},
      el("div", {}, el("dt", {text: "Email"}), el("dd", {text: account.email || "не указан"})),
      el("div", {}, el("dt", {text: "Организация"}), el("dd", {text: account.org})),
      el("div", {}, el("dt", {text: "В каталоге с"}), el("dd", {text: account.since}))),
    el("span", {class: "profile-chip", text: modeChip(account)}),
    el("nav", {class: "profile-side-nav", "aria-label": "Разделы профиля"},
      el("a", {href: "#/catalog", text: "Каталог"}),
      el("a", {href: "#/profile", "aria-current": "page", text: "Обзор"}),
      el("button", {
        type: "button",
        text: "Настройки",
        onclick: () => {
          const reduced = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
          settingsPanel.scrollIntoView?.({block: "start", behavior: reduced ? "auto" : "smooth"});
          settingsTitle.focus?.({preventScroll: true});
        },
      }),
      el("button", {type: "button", class: "danger-link", text: "Выйти", onclick: onLogout})));

  const about = el("section", {class: "panel profile-panel profile-about", "aria-labelledby": "profileAboutTitle"},
    el("h2", {id: "profileAboutTitle", text: "Чем занимаюсь"}),
    el("p", {class: "profile-sub", text: "Описание видно коллегам в карточках публикаций."}),
    el("p", {class: "profile-bio", text: account.bio || "Описание пока не заполнено."}));

  const statsBlock = el("section", {"aria-label": "Статистика"},
    el("div", {class: "profile-stats"},
      statCard("Документы", stats.docs),
      statCard("Действия", stats.actions),
      statCard("Последняя загрузка", describeWhen(stats.last)),
      statCard("В каталоге с", account.since)));

  const activity = el("section", {class: "panel profile-panel", "aria-labelledby": "profileActivityTitle"},
    el("h2", {id: "profileActivityTitle", text: "Активность"}),
    el("p", {class: "profile-sub", text: "Последние действия в этом браузере."}),
    activityFeed(account));

  fill(settingsPanel,
    settingsTitle,
    el("p", {class: "profile-sub", text: "Заглушки без сервера: значения сохраняются только в этом браузере."}),
    settingsStatus,
    notify.node,
    verified.node,
    compact.node,
    el("div", {class: "profile-settings-grid"},
      el("label", {}, "Язык интерфейса", language),
      el("label", {}, "Область по умолчанию", area)),
    el("h3", {class: "profile-settings-sub", text: "Данные аккаунта"}),
    form,
    el("div", {class: "profile-danger"},
      el("p", {
        text: account.guest
          ? "Завершение гостевого доступа вернёт экран регистрации."
          : "Выход завершает сессию в этом браузере. Профиль и настройки останутся, чтобы можно было войти снова.",
      }),
      el("button", {type: "button", text: "Выйти из аккаунта", onclick: onLogout})));

  const main = el("div", {class: "profile-main"},
    account.guest
      ? el("p", {class: "notice warn"},
        el("strong", {text: "Гостевой доступ."}),
        el("p", {text: "Каталог открыт, но данные профиля живут только в этой вкладке."}))
      : null,
    about,
    statsBlock,
    activity,
    settingsPanel);

  fill(view,
    head,
    el("div", {class: "profile-dash"}, identity, main));

  if (ctx.params?.section === "settings") {
    settingsPanel.scrollIntoView?.({block: "start"});
    settingsTitle.focus?.({preventScroll: true});
  }
}
