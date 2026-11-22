import { clearProfilePDF, peekProfilePDF, stashProfilePDF } from "./pending.js";
import { apiFetch, deleteJSON, getJSON, loadSession, putJSON } from "./api.js";
import { visibleWarnings } from "./ui.js";

const byId = id => document.getElementById(id);
const upload = byId("profilePdf");
const form = byId("profileForm");
const entities = byId("profileEntities");
const matches = byId("profileMatches");
const status = byId("profileStatus");
const errorLine = byId("profileError");
let documentData;
let selection;
let sourceProfile = null;
let profileLocked = false;
let profileDirty = false;
let profileList = [];
let profileRequest = 0;
const PROFILE_NAME = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const TRANSLIT = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z",
  и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r",
  с: "s", т: "t", у: "u", ф: "f", х: "kh", ц: "ts", ч: "ch", ш: "sh",
  щ: "shch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
};
const presets = {
  solar: {title: "Солнечные элементы", types: [{name: "Устройство", aliases: "device, cell, ячейка", fields: ["Структура", "Материал", "Площадь"]}, {name: "Измерение", aliases: "efficiency, PCE, Voc, Jsc, FF, КПД", fields: ["Показатель", "Значение", "Единица", "Условия измерения"]}]},
  rag: {title: "RAG-система", types: [{name: "Система", aliases: "RAG, retrieval, model, модель", fields: ["Название", "Модель", "Метод поиска"]}, {name: "Результат", aliases: "accuracy, recall, precision, F1, benchmark, dataset", fields: ["Набор данных", "Метрика", "Значение", "Конфигурация"]}]},
  basic: {title: "Результаты статьи", types: [{name: "Результат", aliases: "result, results, experiment, результат, эксперимент", fields: ["Объект", "Показатель", "Значение", "Условия"]}]},
};

const value = (row, name) => row.querySelector('[name="' + name + '"]').value.trim();
const checked = (row, name) => row.querySelector('[name="' + name + '"]').checked;
const normalize = text => text.normalize("NFKC").toLowerCase();

function latinName(text) {
  return String(text || "").trim().split("").map(char => TRANSLIT[char.toLowerCase()] ?? char).join("");
}

function safeName(text, fallback = "field") {
  const base = latinName(text).replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
  const withLetter = /^[a-z]/.test(base) ? base : `${fallback}_${base}`.replace(/_+$/g, "");
  return withLetter.slice(0, 64) || fallback;
}

function uniqueName(base, used) {
  let name = base;
  let index = 2;
  while (used.has(name)) {
    const suffix = `_${index}`;
    name = base.slice(0, 64 - suffix.length) + suffix;
    index += 1;
  }
  used.add(name);
  return name;
}

function jsonValue(text, fallback) {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

export function matchingSources(sources, names) {
  const terms = names.map(normalize).filter(Boolean);
  return sources.filter(source => {
    const quote = normalize(source.quote);
    return terms.some(term => quote.includes(term));
  });
}

let matchesStale = false;
function clearMatches() {
  selection = undefined;
  matches.replaceChildren();
  matchesStale = false;
}

function staleMatches() {
  if (!matches.children.length || matchesStale) return;
  matchesStale = true;
  matches.querySelectorAll('input[type="checkbox"]').forEach(input => {
    input.checked = false;
    input.disabled = true;
  });
  const note = document.createElement("p");
  note.className = "profile-help profile-stale";
  note.textContent = "Поля изменились. Нажмите «Найти фрагменты», чтобы обновить подсказки.";
  matches.append(note);
  status.textContent = note.textContent;
}

function clearError() {
  if (!errorLine) return;
  errorLine.hidden = true;
  errorLine.textContent = "";
}

function showError(message) {
  if (errorLine) {
    errorLine.textContent = message;
    errorLine.hidden = false;
    if (typeof errorLine.scrollIntoView === "function") errorLine.scrollIntoView({block: "nearest"});
    try {
      errorLine.focus({preventScroll: true});
    } catch {
      errorLine.focus();
    }
  }
  status.textContent = message;
}

// Ручной профиль доступен только вошедшему пользователю: /api/profile/source
// и /api/profile/assist требуют серверную сессию и CSRF-токен.
const sessionReady = loadSession().then(data => {
  if (!data.user) {
    location.replace("/");
    return null;
  }
  return data.user;
}).catch(error => {
  showError(error.message);
  return null;
});

async function authorized(response) {
  if (response.status === 401) {
    location.replace("/");
    throw new Error("Требуется вход.");
  }
  return response;
}

function clearPreset() {
  document.querySelectorAll("[data-preset]").forEach(button => button.setAttribute("aria-pressed", "false"));
}

function markEdited() {
  clearPreset();
  profileDirty = true;
  const types = entities.children.length;
  const fields = entities.querySelectorAll(".profile-field").length;
  status.textContent = types ? `Набор изменён: типов ${types}, полей ${fields}.` : "Типы сущностей удалены.";
  const libraryStatus = byId("profileLibraryStatus");
  if (libraryStatus) libraryStatus.textContent = "Есть несохранённые изменения в форме.";
  clearError();
}

function fieldMarkup() {
  return `
    <label class="field pf-name"><span>Название поля</span><input name="fieldName" required placeholder="Например, Значение"></label>
    <label class="field pf-type"><span>Тип</span><select name="type">
      <option value="text">Текст</option><option value="number">Число</option>
      <option value="boolean">Да / нет</option><option value="reference">Связь с типом</option>
      <option value="id">Идентификатор</option></select></label>
    <label class="pf-required"><input type="checkbox" name="requiredField"> Обязательное</label>
    <button type="button" class="pf-remove" aria-label="Удалить поле" title="Удалить поле">×</button>
    <div class="pf-extra">
      <label class="field pf-unit" data-when="number"><span>Единица в статье</span><input name="unit" placeholder="например, мА/см²"></label>
      <label class="field pf-target-unit" data-when="number"><span>Нужная единица</span><input name="targetUnit" placeholder="например, %"></label>
      <label class="field pf-factor" data-when="number"><span>Коэффициент</span><input name="factor" type="number" step="any" placeholder="если единицы разные"></label>
      <label class="field pf-wide" data-when="reference"><span>Связанный тип</span><input name="target" list="profileTypes" placeholder="Название типа из списка выше"></label>
    </div>
    <label class="field pf-description"><span>Описание для модели</span><input name="description" maxlength="300" placeholder="Что именно извлекать из статьи"></label>
    <label class="field pf-example"><span>Пример значения</span><input name="example" maxlength="160" placeholder="Из статьи или от помощника"></label>`;
}

function updateExtra(field) {
  const type = field.querySelector('[name="type"]').value;
  field.dataset.type = type;
  field.querySelectorAll("[data-when]").forEach(node => {
    node.hidden = node.dataset.when !== type;
  });
}

function addField(container, name = "", kind = "text", example = "", meta = {}) {
  const info = meta && typeof meta === "object" ? meta : {};
  const field = document.createElement("fieldset");
  field.className = "profile-field";
  field.dataset.field = info.field || "";
  field.dataset.units = JSON.stringify(info.units && typeof info.units === "object" ? info.units : {});
  field.dataset.aliases = JSON.stringify(Array.isArray(info.aliases) ? info.aliases : []);
  field.dataset.minimum = info.minimum ?? "";
  field.dataset.maximum = info.maximum ?? "";
  field.innerHTML = fieldMarkup();
  const type = field.querySelector('[name="type"]');
  type.addEventListener("change", () => updateExtra(field));
  field.querySelector(".pf-remove").onclick = () => {
    field.remove();
    staleMatches();
    markEdited();
  };
  container.append(field);
  field.querySelector('[name="fieldName"]').value = name;
  type.value = kind === "id" ? "id" : kind;
  field.querySelector('[name="example"]').value = example || info.example || "";
  field.querySelector('[name="description"]').value = info.description || "";
  if (info.required) field.querySelector('[name="requiredField"]').checked = true;
  if (kind === "number") {
    field.querySelector('[name="unit"]').value = info.unit || "";
    field.querySelector('[name="targetUnit"]').value = info.targetUnit || "";
    if (info.factor !== undefined && info.factor !== null) {
      field.querySelector('[name="factor"]').value = info.factor;
    }
  }
  if (kind === "reference" && info.target) {
    field.querySelector('[name="target"]').value = info.target;
  }
  updateExtra(field);
  staleMatches();
}

function addEntity(preset, meta = {}) {
  const info = meta && typeof meta === "object" ? meta : {};
  const entity = document.createElement("fieldset");
  entity.className = "profile-entity";
  entity.dataset.table = info.table || "";
  entity.dataset.key = info.key || "";
  entity.dataset.description = info.description || "";
  entity.innerHTML = `<legend>Тип сущности</legend>
    <div class="entity-head">
      <label class="field pe-name"><span>Название типа</span><input name="entityName" required placeholder="Например, Результат"></label>
      <label class="field pe-aliases"><span>Слова-подсказки</span><input name="aliases" placeholder="Через запятую: result, experiment…"></label>
      <button type="button" class="pf-remove pe-remove" aria-label="Удалить тип" title="Удалить тип">×</button>
    </div>
    <p class="profile-entity-description" hidden></p>
    <div class="profile-fields"></div>
    <div class="entity-actions"><button type="button" class="button">Добавить поле</button></div>`;
  const fields = entity.querySelector(".profile-fields");
  entity.querySelector(".entity-actions button").onclick = () => {
    addField(fields);
    markEdited();
  };
  entity.querySelector(".pf-remove").onclick = () => {
    entity.remove();
    refreshTypes();
    staleMatches();
    markEdited();
  };
  entities.append(entity);
  const descriptionLine = entity.querySelector(".profile-entity-description");
  if (descriptionLine && info.description) {
    descriptionLine.textContent = info.description;
    descriptionLine.hidden = false;
  }
  if (preset) {
    entity.querySelector('[name="entityName"]').value = preset.name || "";
    entity.querySelector('[name="aliases"]').value = preset.aliases || "";
  }
  if (Array.isArray(info.fields) && info.fields.length) {
    info.fields.forEach(field => addField(
      fields, field.name || field.field || "", field.type || "text",
      field.example || "", field,
    ));
  } else if (preset && Array.isArray(preset.fields)) {
    preset.fields.forEach(field => {
      if (typeof field === "string") addField(fields, field);
      else addField(fields, field.name || "", field.type || "text", field.example || "", field);
    });
  } else {
    addField(fields);
  }
  refreshTypes();
}

function refreshTypes() {
  const list = byId("profileTypes");
  if (!list) return;
  const names = [...entities.querySelectorAll('[name="entityName"]')]
    .map(input => input.value.trim()).filter(Boolean);
  list.replaceChildren(...names.map(name => {
    const option = document.createElement("option");
    option.value = name;
    return option;
  }));
}
function applyPreset(key) {
  if (tablesLocked()) {
    warnLocked();
    return;
  }
  const preset = presets[key];
  clearError();
  clearMatches();
  entities.replaceChildren();
  form.elements.title.value = preset.title;
  preset.types.forEach(addEntity);
  refreshTypes();
  profileDirty = true;
  const libraryStatus = byId("profileLibraryStatus");
  if (libraryStatus) libraryStatus.textContent = "Готовый набор добавлен в форму. Сохраните его как мой профиль, если он нужен на сервере.";
  document.querySelectorAll('[data-preset]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.preset === key)));
  status.textContent = "Готовый набор добавлен. Можно изменить названия и поля под статью.";
}
document.querySelectorAll('[data-preset]').forEach(button => button.onclick = () => applyPreset(button.dataset.preset));

function readProfile() {
  const usedTables = new Set();
  const types = [...entities.children].map(entity => {
    const name = value(entity, "entityName");
    let table = entity.dataset.table || "";
    if (!table) {
      table = uniqueName(safeName(name, "table"), usedTables);
    } else {
      if (usedTables.has(table)) throw new Error("Повторяется идентификатор таблицы «" + table + "».");
      usedTables.add(table);
    }
    if (!PROFILE_NAME.test(table)) throw new Error("Идентификатор таблицы «" + table + "» должен начинаться с латинской буквы.");
    const usedFields = new Set();
    const fields = [...entity.querySelectorAll(".profile-field")].map(field => {
      const fieldName = value(field, "fieldName");
      const type = value(field, "type");
      let id = field.dataset.field || "";
      if (!id) {
        id = uniqueName(safeName(fieldName, "field"), usedFields);
      } else {
        if (usedFields.has(id)) throw new Error("Повторяется идентификатор поля «" + id + "» в типе «" + name + "».");
        usedFields.add(id);
      }
      if (!PROFILE_NAME.test(id) || id === "field_sources") {
        throw new Error("Идентификатор поля «" + id + "» недопустим.");
      }
      const factor = value(field, "factor");
      return {
        name: fieldName, field: id, type, selected: true,
        description: value(field, "description"),
        example: value(field, "example"),
        required: checked(field, "requiredField"),
        units: jsonValue(field.dataset.units || "{}", {}),
        aliases: jsonValue(field.dataset.aliases || "[]", []),
        minimum: field.dataset.minimum || "",
        maximum: field.dataset.maximum || "",
        ...(type === "number" && {
          unit: value(field, "unit"), targetUnit: value(field, "targetUnit"),
          factor: factor === "" ? null : Number(factor),
        }),
        ...(type === "reference" && {targetName: value(field, "target")}),
      };
    });
    return {
      name, table, key: entity.dataset.key || "", selected: true,
      description: entity.dataset.description || "",
      aliases: value(entity, "aliases").split(",").map(term => term.trim()).filter(Boolean),
      fields,
    };
  });
  const unique = names => new Set(names.map(normalize)).size === names.length;
  if (!types.length) throw new Error("Добавьте хотя бы один тип сущности.");
  if (!types.every(type => type.name) || !unique(types.map(type => type.name))) {
    throw new Error("Укажите уникальные названия типов.");
  }
  if (!unique(types.map(type => type.table))) {
    throw new Error("Укажите уникальные идентификаторы таблиц.");
  }
  for (const type of types) {
    if (!type.fields.length) throw new Error("Добавьте хотя бы одно поле в тип «" + type.name + "».");
    if (!type.fields.every(field => field.name) || !unique(type.fields.map(field => field.name))) {
      throw new Error("Укажите уникальные названия полей в типе «" + type.name + "».");
    }
    if (!unique(type.fields.map(field => field.field))) {
      throw new Error("Идентификаторы полей в типе «" + type.name + "» повторяются.");
    }
    for (const field of type.fields) {
      if (field.type === "reference") {
        const target = types.find(other => normalize(other.name) === normalize(field.targetName || "")
          || normalize(other.table) === normalize(field.targetName || ""));
        if (!target) throw new Error("Для связи «" + field.name + "» укажите название выбранного типа.");
        field.targetTable = target.table;
      }
      if (field.type === "number" && field.targetUnit && field.targetUnit !== field.unit
          && (!field.unit || !Number.isFinite(field.factor) || field.factor <= 0)) {
        throw new Error("Для пересчёта «" + field.name + "» укажите исходную единицу и положительный коэффициент.");
      }
    }
  }
  return {title: value(form, "title"), types};
}

/* ------------------------------------------------------- профили и сохранение */

function profileMetaValue(id, fallback = "") {
  const node = byId(id);
  return node ? node.value.trim() : fallback;
}

function suggestedProfileId() {
  return safeName(value(form, "title") || "profile", "profile");
}

function fillFormFromProfile(profile) {
  entities.replaceChildren();
  form.elements.title.value = profile.description || profile.id || "";
  if (profile.adapter !== "solar_csv") {
    for (const [table, spec] of Object.entries(profile.tables || {})) {
      const fields = Object.entries(spec.fields || {}).map(([name, settings]) => {
        const units = settings.units && typeof settings.units === "object"
          ? Object.entries(settings.units) : [];
        const sourceUnit = units.length ? units[0][0] : (settings.unit || "");
        const factor = units.length ? units[0][1] : null;
        return {
          field: name, name, type: settings.type || "text",
          required: Boolean(settings.required),
          description: settings.description || "",
          example: "",
          units: settings.units && typeof settings.units === "object" ? settings.units : {},
          aliases: Array.isArray(settings.aliases) ? settings.aliases : [],
          minimum: settings.minimum ?? "",
          maximum: settings.maximum ?? "",
          unit: settings.type === "number" ? sourceUnit : "",
          targetUnit: settings.type === "number" ? (settings.unit || "") : "",
          factor: settings.type === "number" ? factor : null,
          target: settings.type === "reference" ? settings.reference || "" : "",
        };
      });
      const displayName = spec.description && spec.description.length <= 60 ? spec.description : table;
      addEntity({name: displayName, aliases: "", fields: []}, {
        table, key: spec.key || "", description: spec.description || "", fields,
      });
    }
    // Ссылки в форме задаются названием типа, а в профиле хранится id таблицы.
    const nameByTable = new Map([...entities.children].map(row => [row.dataset.table, value(row, "entityName")]));
    for (const field of entities.querySelectorAll(".profile-field")) {
      if (value(field, "type") !== "reference") continue;
      const input = field.querySelector('[name="target"]');
      const translated = nameByTable.get(input.value);
      if (translated) input.value = translated;
    }
    refreshTypes();
  }
}

function applyProfileMeta(profile, source) {
  const description = byId("profileDescription");
  const instructions = byId("profileInstructions");
  const profileId = byId("profileId");
  if (description) description.value = profile?.description || "";
  if (instructions) instructions.value = profile?.instructions || "";
  if (profileId) profileId.value = profile?.id || "";
  const locked = source === "builtin";
  if (description) {
    description.disabled = locked;
    byId("profileDescriptionWrap").hidden = !profile || profile.adapter !== "solar_csv";
  }
  if (instructions) {
    instructions.disabled = locked;
    byId("profileInstructionsWrap").hidden = !profile;
  }
  if (profileId) {
    profileId.readOnly = source === "user" || locked;
    byId("profileIdWrap").hidden = !profile || locked;
  }
  const save = byId("profileSave");
  const copy = byId("profileCopy");
  const remove = byId("profileDelete");
  if (save) {
    save.hidden = locked;
    save.textContent = source === "user" ? "Сохранить профиль" : "Сохранить как мой профиль";
  }
  if (copy) copy.hidden = !locked;
  if (remove) remove.hidden = source !== "user";
}

function setProfileLocked(locked) {
  profileLocked = Boolean(locked);
  const editor = document.querySelector(".profile-editor");
  if (editor) editor.querySelectorAll("input, select, textarea, button").forEach(node => {
    node.disabled = profileLocked;
  });
  const apply = byId("assistantApply");
  if (apply) apply.disabled = profileLocked;
  const lock = byId("assistantLock");
  if (lock) lock.hidden = !profileLocked;
  syncPdfActions();
}

function syncPdfActions() {
  const hasPdf = Boolean(documentData);
  for (const id of ["scanProfile", "browseProfile", "profileDownload"]) {
    const node = byId(id);
    if (node) node.disabled = !hasPdf || profileLocked;
  }
  const ask = byId("assistantAsk");
  if (ask) ask.disabled = !hasPdf;
  document.querySelectorAll("[data-assistant-prompt]").forEach(node => {
    node.disabled = !hasPdf;
  });
}

function refreshProfileContext() {
  const label = sourceProfile
    ? `${sourceProfile.id} · ${sourceProfile.source === "builtin" ? "встроенный" : "мой"}`
    : "черновик профиля";
  const context = byId("profileContext");
  if (context) {
    context.textContent = "Контекст: " + label
      + (documentData ? " · PDF «" + documentData.filename + "»" : " · PDF не загружен");
  }
  const assistant = byId("assistantContext");
  if (assistant) {
    assistant.textContent = "Контекст: " + label
      + (documentData ? " · цитаты из PDF" : " · загрузите PDF для цитат");
  }
  const entry = byId("assistantEntry");
  if (entry) entry.hidden = Boolean(documentData);
}

function profileSummaryNode(profile, source) {
  const summary = profileList.find(item => item.id === profile.id);
  const tables = summary?.tables || [];
  const fields = tables.reduce((total, table) => total + (table.fields?.length || 0), 0);
  const list = document.createElement("dl");
  list.className = "profile-summary-grid";
  const pairs = [
    ["Источник", source === "builtin" ? "встроенный (только чтение)" : "мой профиль"],
    ["ID", profile.id],
    ["Адаптер", profile.adapter || "fields"],
    ["Таблиц", String(tables.length)],
    ["Полей", String(fields)],
  ];
  for (const [term, text] of pairs) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = text;
    list.append(dt, dd);
  }
  if (profile.adapter === "solar_csv") {
    const note = document.createElement("p");
    note.className = "profile-help";
    note.textContent = "Официальный шаблон solar: колонки задаются адаптером, редактор полей не применяется. "
      + "В копии можно изменить описание и инструкции.";
    const description = document.createElement("p");
    description.className = "profile-help";
    description.textContent = profile.description || "";
    return [list, description, note, ...tables.slice(0, 7).map(table => {
      const item = document.createElement("p");
      item.className = "profile-table-line";
      item.textContent = `${table.name}: ${table.fields?.length || 0} полей`;
      return item;
    })];
  }
  return [list];
}

function resetProfileMeta() {
  sourceProfile = null;
  applyProfileMeta(null, "");
  const id = byId("profileId");
  if (id) id.value = suggestedProfileId();
  byId("profileDescriptionWrap").hidden = true;
  byId("profileInstructionsWrap").hidden = true;
  byId("profileIdWrap").hidden = false;
  byId("profileSave").hidden = false;
  byId("profileCopy").hidden = true;
  byId("profileDelete").hidden = true;
  setProfileLocked(false);
  refreshProfileContext();
}

function startDraft() {
  profileRequest += 1;
  const select = byId("profileSelect");
  if (select) select.value = "";
  resetProfileMeta();
  clearMatches();
  entities.replaceChildren();
  applyPreset("basic");
  profileDirty = false;
  const summary = byId("profileSummary");
  if (summary) summary.replaceChildren();
  proposals = [];
  assistantHistory.length = 0;
  const suggestions = byId("assistantSuggestions");
  if (suggestions) suggestions.replaceChildren();
  const apply = byId("assistantApply");
  if (apply) apply.hidden = true;
  const id = byId("profileId");
  if (id) {
    id.value = suggestedProfileId();
    delete id.dataset.touched;
  }
  const editor = document.querySelector(".profile-editor");
  if (editor) editor.hidden = false;
  byId("profilePanel").hidden = false;
  form.hidden = false;
  const statusLine = byId("profileLibraryStatus");
  if (statusLine) statusLine.textContent = "Новый черновик. Сохраните его, чтобы получить пользовательский профиль.";
  const source = byId("profileSource");
  if (source) source.textContent = documentData
    ? `${documentData.filename} · страниц: ${documentData.pages}`
    : "Профиль без PDF: поля можно править, подсказки появятся после загрузки PDF.";
  syncPdfActions();
}

async function selectProfile(id) {
  if (!id) {
    startDraft();
    return;
  }
  const request = ++profileRequest;
  try {
    const data = await getJSON(`/api/profiles/${encodeURIComponent(id)}`, {_t: Date.now()});
    if (request !== profileRequest) return;
    const profile = data.profile;
    const source = data.source;
    sourceProfile = {profile, source, id: profile.id, summary: data.summary};
    profileDirty = false;
    proposals = [];
    assistantHistory.length = 0;
    byId("assistantSuggestions")?.replaceChildren();
    if (byId("assistantApply")) byId("assistantApply").hidden = true;
    fillFormFromProfile(profile);
    applyProfileMeta(profile, source);
    setProfileLocked(source === "builtin");
    byId("profilePanel").hidden = false;
    form.hidden = false;
    const editor = document.querySelector(".profile-editor");
    if (editor) editor.hidden = profile.adapter === "solar_csv";
    const select = byId("profileSelect");
    if (select) select.value = profile.id;
    const libraryStatus = byId("profileLibraryStatus");
    if (libraryStatus) {
      libraryStatus.textContent = source === "builtin"
        ? "Встроенный профиль открыт только для чтения. Создайте копию, чтобы менять поля."
        : "Мой профиль загружен. Изменения не сохраняются, пока вы не нажмёте «Сохранить профиль».";
    }
    const summary = byId("profileSummary");
    if (summary) summary.replaceChildren(...(profileSummaryNode(profile, source) || []));
    const sourceLine = byId("profileSource");
    if (sourceLine) {
      sourceLine.textContent = documentData
        ? `${documentData.filename} · страниц: ${documentData.pages}`
        : "Профиль без PDF: поля можно править, подсказки появятся после загрузки PDF.";
    }
    refreshProfileContext();
    clearMatches();
    syncPdfActions();
    renderSuggestions();
  } catch (error) {
    if (request !== profileRequest) return;
    if (error.status === 401) { location.replace("/"); return; }
    const select = byId("profileSelect");
    if (select) select.value = sourceProfile?.id || "";
    const statusLine = byId("profileLibraryStatus");
    if (statusLine) statusLine.textContent = "Не удалось открыть профиль: " + error.message;
    showError(error.message);
  }
}

async function loadProfileLibrary(selected = "") {
  const box = byId("profileLibrary");
  const select = byId("profileSelect");
  if (!box || !select) return;
  try {
    const data = await getJSON("/api/profiles", {_t: Date.now()});
    profileList = Array.isArray(data?.profiles) ? data.profiles : [];
    const options = [document.createElement("option")];
    options[0].value = "";
    options[0].textContent = "Черновик в браузере";
    for (const item of profileList) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.id} · ${item.source === "builtin" ? "встроенный" : "мой"}`
        + (item.error ? " · ошибка" : "");
      option.disabled = Boolean(item.error);
      options.push(option);
    }
    select.replaceChildren(...options);
    select.value = selected;
    box.hidden = false;
    const statusLine = byId("profileLibraryStatus");
    if (statusLine && !selected) {
      statusLine.textContent = "Можно открыть готовый шаблон или свой профиль. Встроенный профиль меняется только через копию.";
    }
  } catch (error) {
    if (error.status === 401) { location.replace("/"); return; }
    box.hidden = false;
    const statusLine = byId("profileLibraryStatus");
    if (statusLine) statusLine.textContent = "Профили недоступны: " + error.message;
  }
}

function buildProfile(id) {
  const base = sourceProfile?.profile || {};
  const adapter = base.adapter || "fields";
  const description = adapter === "solar_csv"
    ? (profileMetaValue("profileDescription") || value(form, "title") || id)
    : (value(form, "title") || profileMetaValue("profileDescription") || id);
  const instructions = profileMetaValue("profileInstructions") || base.instructions || "";
  if (adapter === "solar_csv") {
    const copy = {...base, id, description, instructions, adapter};
    delete copy.tables;
    return copy;
  }
  const {types} = readProfile();
  const tables = {};
  for (const type of types) {
    const idField = type.fields.find(field => field.type === "id");
    const key = type.key || idField?.field || `${type.table}_id`;
    const fields = {};
    if (!type.fields.some(field => field.field === key)) {
      fields[key] = {type: "id", description: `Технический идентификатор записи «${type.name}».`};
    }
    for (const field of type.fields) {
      if (field.type === "id") {
        fields[field.field] = {
          type: "id",
          description: field.description || `Технический идентификатор записи «${type.name}».`,
        };
        continue;
      }
      const spec = {
        type: field.type,
        description: field.description || field.example || `Поле «${field.name}».`,
      };
      if (field.required) spec.required = true;
      if (Array.isArray(field.aliases) && field.aliases.length) spec.aliases = field.aliases;
      if (field.minimum !== "") spec.minimum = field.minimum;
      if (field.maximum !== "") spec.maximum = field.maximum;
      if (field.type === "number") {
        spec.unit = field.targetUnit || field.unit || "";
        const units = field.units && typeof field.units === "object" && !Array.isArray(field.units)
          ? {...field.units} : {};
        if (field.unit && field.targetUnit && field.unit !== field.targetUnit) {
          units[field.unit] = String(field.factor);
        }
        if (Object.keys(units).length) spec.units = units;
        else if (spec.unit) spec.units = {[spec.unit]: "1"};
      }
      if (field.type === "reference") spec.reference = field.targetTable;
      fields[field.field] = spec;
    }
    if (!fields[key] || fields[key].type !== "id") {
      fields[key] = {type: "id", description: `Технический идентификатор записи «${type.name}».`};
    }
    tables[type.table] = {key, description: type.description || type.name, fields};
  }
  const profile = {
    id, version: base.version || "1", description,
    adapter: "fields", tables,
  };
  if (base.metrics && typeof base.metrics === "object" && Object.keys(base.metrics).length) {
    profile.metrics = base.metrics;
  }
  if (instructions) profile.instructions = instructions;
  return profile;
}

async function saveProfile() {
  if (profileLocked) {
    showError("Встроенный профиль нельзя сохранить поверх. Создайте копию.");
    return;
  }
  const id = sourceProfile?.source === "user"
    ? sourceProfile.id : (byId("profileId")?.value || "").trim();
  if (!PROFILE_NAME.test(id)) {
    showError("ID профиля: латинская буква, затем латинские буквы, цифры или подчёркивание.");
    byId("profileId")?.focus();
    return;
  }
  const existing = profileList.find(item => item.id === id);
  if (!sourceProfile && existing) {
    if (existing.source === "builtin") {
      showError("Этот ID занят встроенным профилем. Укажите другой ID для копии.");
      byId("profileId")?.focus();
      return;
    }
    if (!window.confirm(`Профиль «${id}» уже существует. Перезаписать его?`)) return;
  }
  if ((sourceProfile?.profile?.adapter || "fields") !== "solar_csv" && !value(form, "title")) {
    showError("Укажите название профиля.");
    form.elements.title?.focus();
    return;
  }
  try {
    const profile = buildProfile(id);
    await putJSON(`/api/profiles/${encodeURIComponent(id)}`, profile);
    profileDirty = false;
    const statusLine = byId("profileLibraryStatus");
    if (statusLine) statusLine.textContent = "Профиль сохранён.";
    await loadProfileLibrary(id);
    await selectProfile(id);
    const savedLine = byId("profileLibraryStatus");
    if (savedLine) savedLine.textContent = "Профиль сохранён.";
    status.textContent = "Профиль сохранён. Извлечение не запускалось.";
  } catch (error) {
    if (error.status === 401) { location.replace("/"); return; }
    showError(error.message);
  }
}

async function copyProfile() {
  if (!sourceProfile?.profile) return;
  const used = new Set(profileList.map(item => item.id));
  const id = uniqueName(safeName(`${sourceProfile.id}_copy`, "copy"), used);
  const copy = JSON.parse(JSON.stringify(sourceProfile.profile));
  copy.id = id;
  if (copy.adapter === "solar_csv") delete copy.tables;
  try {
    await putJSON(`/api/profiles/${encodeURIComponent(id)}`, copy);
    await loadProfileLibrary(id);
    await selectProfile(id);
    const statusLine = byId("profileLibraryStatus");
    if (statusLine) statusLine.textContent = "Создана пользовательская копия. Теперь её можно менять.";
    status.textContent = "Создана копия профиля: " + id + ".";
  } catch (error) {
    if (error.status === 401) { location.replace("/"); return; }
    showError(error.message);
  }
}

async function deleteProfile() {
  if (!sourceProfile || sourceProfile.source !== "user") return;
  if (!window.confirm(`Удалить пользовательский профиль «${sourceProfile.id}»?`)) return;
  try {
    await deleteJSON(`/api/profiles/${encodeURIComponent(sourceProfile.id)}`);
    await loadProfileLibrary();
    startDraft();
    status.textContent = "Профиль удалён.";
  } catch (error) {
    if (error.status === 401) { location.replace("/"); return; }
    showError(error.message);
  }
}

function checkProfile(quiet = false) {
  const findings = [];
  const add = (tone, text) => findings.push({tone, text});
  const adapter = sourceProfile?.profile?.adapter || "fields";
  const rows = [...entities.children];
  if (adapter === "solar_csv") {
    add("info", "Официальный шаблон solar: колонки задаются адаптером, проверка полей формы не применяется.");
  }
  if (!rows.length && adapter !== "solar_csv") add("bad", "Нет ни одного типа сущности.");
  const names = rows.map(row => value(row, "entityName")).filter(Boolean).map(normalize);
  if (new Set(names).size !== names.length) add("bad", "Названия типов повторяются.");
  const tables = rows.map(row => row.dataset.table || safeName(value(row, "entityName"), "table"));
  if (new Set(tables).size !== tables.length) add("bad", "Идентификаторы таблиц повторяются.");
  for (const row of rows) {
    const name = value(row, "entityName");
    if (!name) add("bad", "У типа нет названия.");
    const fields = [...row.querySelectorAll(".profile-field")];
    if (!fields.length) add("bad", `В типе «${name || "без названия"}» нет полей.`);
    const fieldNames = fields.map(field => value(field, "fieldName")).filter(Boolean).map(normalize);
    if (new Set(fieldNames).size !== fieldNames.length) add("bad", `В типе «${name}» повторяются названия полей.`);
    for (const field of fields) {
      const fieldName = value(field, "fieldName");
      const type = value(field, "type");
      if (!fieldName) add("bad", `В типе «${name}» есть поле без названия.`);
      if (!value(field, "description") && !value(field, "example")) {
        add("info", `Поле «${fieldName || "без названия"}» без описания и примера.`);
      }
      if (type === "number") {
        const unit = value(field, "unit");
        const target = value(field, "targetUnit");
        const factor = Number(value(field, "factor"));
        if (target && target !== unit && (!unit || !Number.isFinite(factor) || factor <= 0)) {
          add("bad", `Для «${fieldName}» нужна исходная единица и положительный коэффициент.`);
        }
      }
      if (type === "reference") {
        const target = value(field, "target");
        const found = rows.some(other => normalize(value(other, "entityName")) === normalize(target)
          || normalize(other.dataset.table || "") === normalize(target));
        if (!target || !found) add("bad", `Для связи «${fieldName}» не найден связанный тип.`);
      }
    }
  }
  const id = (byId("profileId")?.value || "").trim();
  if (!profileLocked && !PROFILE_NAME.test(id) && !sourceProfile) {
    add("warn", "ID профиля нужен для сохранения: латинская буква, цифры и подчёркивание.");
  }
  const box = byId("profileChecks");
  const problems = findings.filter(finding => finding.tone !== "info").length;
  if (box) {
    box.hidden = false;
    box.replaceChildren();
    const title = document.createElement("p");
    title.className = "profile-checks-title";
    title.textContent = problems
      ? `Найдено замечаний: ${problems}`
      : findings.length ? "Структурных проблем нет, есть примечание." : "Структурных проблем не найдено.";
    box.append(title);
    if (findings.length) {
      const list = document.createElement("ul");
      for (const finding of findings) {
        const item = document.createElement("li");
        item.className = "check-" + finding.tone;
        item.textContent = finding.text;
        list.append(item);
      }
      box.append(list);
    }
  }
  const summary = problems
    ? `Проверка профиля: ${problems} замечаний. ` + findings.filter(item => item.tone !== "info").slice(0, 4).map(item => item.text).join(" ")
    : findings.length
      ? "Проверка профиля: структурных проблем нет, есть примечание. " + findings.map(item => item.text).join(" ")
      : "Проверка профиля: структурных проблем не найдено.";
  if (!quiet) status.textContent = summary;
  return summary;
}

function showMatches(profile, browseAll) {
  clearError();
  clearMatches();
  selection = [];
  for (const type of profile.types.filter(type => type.selected)) {
    const panel = document.createElement("details");
    const candidates = browseAll ? documentData.sources : matchingSources(documentData.sources, [type.name, ...type.aliases,
      ...type.fields.filter(field => field.selected).map(field => field.name)]);
    const title = document.createElement("summary");
    title.textContent = type.name + " · подсказок: " + candidates.length;
    panel.append(title);
    for (const source of candidates) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      label.append(input, " Стр. " + source.page + " · " + (source.display_text || source.quote));
      panel.append(label);
      selection.push({type: type.name, source, input});
    }
    if (!candidates.length) panel.append("Проверьте названия и добавьте сокращения из статьи. Тип можно оставить выбранным.");
    matches.append(panel);
  }
  status.textContent = browseAll ? "Отметьте подходящие фрагменты для каждого типа."
    : "Отметьте подходящие фрагменты. Подсказки основаны на совпадении слов.";
}

byId("addEntity").onclick = () => {
  addEntity();
  staleMatches();
  markEdited();
};
form.addEventListener("input", event => {
  if (matches.contains(event.target)) return;
  if (event.target.closest(".assistant-panel")) return;
  if (event.target.closest("#profileChecks")) return;
  if (event.target.name === "title" && !sourceProfile) {
    const id = byId("profileId");
    if (id && !id.dataset.touched) id.value = suggestedProfileId();
  }
  refreshTypes();
  staleMatches();
  markEdited();
});

for (const id of ["profileDescription", "profileInstructions", "profileId"]) {
  const node = byId(id);
  if (!node) continue;
  node.addEventListener("input", () => {
    if (id === "profileId") node.dataset.touched = "1";
    profileDirty = true;
    const libraryStatus = byId("profileLibraryStatus");
    if (libraryStatus) libraryStatus.textContent = "Есть несохранённые изменения.";
  });
}
function scan(browseAll) {
  if (!documentData) {
    status.textContent = "Сначала загрузите PDF: подсказки строятся по фрагментам статьи.";
    return;
  }
  if (!form.reportValidity()) return;
  try {
    showMatches(readProfile(), browseAll);
  } catch (error) {
    showError(error.message);
  }
}
byId("scanProfile").onclick = () => scan(false);
byId("browseProfile").onclick = () => scan(true);

async function parseResponse(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось прочитать PDF.");
  return data;
}

// Разбор по полям идёт на сервере, поэтому номер задания запоминается на
// время вкладки: после перехода на другую страницу и возврата прогресс и
// результат возвращаются, а не пропадают вместе со страницей.
const JOB_KEY = "viniti.profile.job";
// Сервер держит разбор до 30 минут, поэтому запись живёт чуть дольше:
// иначе возврат на страницу потерял бы ещё идущее задание.
const JOB_TTL_MS = 45 * 60 * 1000;
const HANDOFF_KEY = "viniti.profile.handoff";

function rememberJob(job) {
  try {
    sessionStorage.setItem(JOB_KEY, JSON.stringify({
      id: job.id, filename: job.filename || "", savedAt: Date.now(),
    }));
  } catch {
    // Приватный режим: разбор просто не возобновится после перехода.
  }
}

function forgetJob() {
  try { sessionStorage.removeItem(JOB_KEY); } catch { /* см. rememberJob */ }
}

function rememberedJob() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(JOB_KEY) || "null");
    if (!saved || typeof saved.id !== "string" || !saved.id) return null;
    if (Date.now() - Number(saved.savedAt) > JOB_TTL_MS) return null;
    return saved;
  } catch {
    return null;
  }
}

function jobPath(jobId) {
  return "/api/parse/" + encodeURIComponent(jobId);
}

// null значит, что задание уже удалено на сервере: возобновлять нечего.
async function readJob(jobId) {
  const response = await authorized(await apiFetch(jobPath(jobId), {cache: "no-store"}));
  if (response.status === 404) return null;
  return parseResponse(response);
}

function missingJob() {
  const error = new Error("Разбор больше не доступен. Выберите PDF снова.");
  error.missing = true;
  return error;
}

async function awaitJob(job) {
  const deadline = Date.now() + 35 * 60 * 1000;
  let current = job;
  while (current.status === "queued" || current.status === "running") {
    status.textContent = current.status === "queued" ? "PDF ожидает своей очереди…"
      : "Читаем текст, таблицы и подписи страниц. Это может занять несколько минут…";
    await new Promise(resolve => setTimeout(resolve, 1000));
    if (Date.now() >= deadline) throw new Error("Разбор PDF превысил лимит ожидания. Проверьте статус задачи и журнал сервера.");
    const next = await readJob(current.id);
    if (!next) throw missingJob();
    current = next;
  }
  if (current.status !== "ok") {
    forgetJob();
    throw new Error(current.error || "Разбор PDF завершился с ошибкой.");
  }
  const result = await parseResponse(await authorized(
    await apiFetch(jobPath(current.id) + "/result", {cache: "no-store"})));
  return {job: current, data: {pages: result.document.pages, sources: result.sources, warnings: result.warnings || []}};
}

async function startJob(file) {
  const job = await parseResponse(await authorized(await apiFetch("/api/parse", {
    method: "POST",
    headers: {"Content-Type": "application/pdf", "X-Parser": "auto", "X-OCR": "auto", "X-Profile": "solar",
      "X-Filename": encodeURIComponent(file.name)},
    body: file,
  })));
  rememberJob(job);
  return job;
}

function showParsed(data, filename) {
  documentData = {...data, filename};
  clearHandoff();
  const sourceLine = byId("profileSource");
  if (sourceLine) {
    const warnings = visibleWarnings(data.warnings);
    sourceLine.textContent = filename + " · страниц: " + data.pages + " · фрагментов: " + data.sources.length
      + (warnings.length ? " · " + warnings.join(" ") : "");
  }
  form.hidden = false;
  byId("profilePanel").hidden = false;
  byId("assistantEntry")?.setAttribute("hidden", "");
  if (!entities.children.length && !sourceProfile) {
    if (byId("profileLibrary")) startDraft();
    else applyPreset("basic");
  }
  const messages = byId("assistantMessages");
  if (messages && !messages.children.length) {
    chatMessage(sourceProfile
      ? "PDF прочитан. Профиль уже загружен; помощник может предложить поля по цитатам."
      : "PDF прочитан. Спросите, какие поля искать в статье, или отметьте фрагменты слева.");
  }
  status.textContent = "PDF прочитан. Выберите готовый набор или опишите поля вручную.";
  refreshProfileContext();
  syncPdfActions();
}

async function releasePending() {
  try {
    await clearProfilePDF();
  } catch (error) {
    console.warn("Не удалось освободить переданный PDF:", error.message);
  }
}

let proposals = [];
const assistantHistory = [];
const TYPE_LABELS = {text: "Текст", number: "Число", boolean: "Да / нет", reference: "Связь", id: "ID"};

function chatMessage(text, side = "assistant") {
  const messages = byId("assistantMessages");
  if (!messages) return;
  const bubble = document.createElement("p");
  bubble.className = `chat-bubble ${side === "user" ? "user-bubble" : "assistant-bubble"}`;
  bubble.textContent = text;
  messages.append(bubble);
  messages.scrollTop = messages.scrollHeight;
}

function warnLocked() {
  const message = sourceProfile?.profile?.adapter === "solar_csv"
    ? "Колонки официального шаблона solar задаются адаптером. Меняйте описание и инструкции, поля формы к нему не применяются."
    : "Встроенный профиль нельзя изменить. Нажмите «Создать копию» в блоке профилей.";
  status.textContent = message;
  const assistantStatus = byId("assistantStatus");
  if (assistantStatus) {
    assistantStatus.hidden = false;
    assistantStatus.textContent = message;
  }
  chatMessage(message);
}

function tablesLocked() {
  return profileLocked || sourceProfile?.profile?.adapter === "solar_csv";
}

function existingField(entity, fieldName) {
  return [...entity.querySelectorAll(".profile-field")]
    .find(row => normalize(value(row, "fieldName")) === normalize(fieldName));
}

function existingEntity(entityName) {
  return [...entities.children]
    .find(row => normalize(value(row, "entityName")) === normalize(entityName));
}

function applySuggestion(index) {
  if (tablesLocked()) {
    warnLocked();
    return false;
  }
  const item = proposals[index];
  if (!item || item.applied || item.rejected) return false;
  let entity = existingEntity(item.entity);
  if (!entity) {
    const used = new Set([...entities.children].map(row => row.dataset.table).filter(Boolean));
    addEntity({name: item.entity, aliases: "", fields: []}, {
      table: uniqueName(safeName(item.entity, "entity"), used),
    });
    entity = entities.lastElementChild;
  }
  const container = entity.querySelector(".profile-fields");
  const field = existingField(entity, item.field);
  const kind = TYPE_LABELS[item.type] ? item.type : "text";
  if (field) {
    field.querySelector('[name="type"]').value = kind;
    field.querySelector('[name="example"]').value = item.value || "";
    if (item.explanation) field.querySelector('[name="description"]').value = item.explanation;
    updateExtra(field);
    item.applied = "updated";
    status.textContent = `Обновлено поле «${item.entity} · ${item.field}».`;
  } else {
    const used = new Set([...container.querySelectorAll(".profile-field")]
      .map(row => row.dataset.field).filter(Boolean));
    addField(container, item.field, kind, item.value || "", {
      field: uniqueName(safeName(item.field, "field"), used),
      description: item.explanation || "",
    });
    item.applied = "added";
    status.textContent = `Добавлено поле «${item.entity} · ${item.field}».`;
  }
  markEdited();
  return true;
}

function renderSuggestions() {
  const list = byId("assistantSuggestions");
  if (!list) return;
  list.replaceChildren();
  proposals.forEach((item, index) => {
    const card = document.createElement("article");
    card.className = "assistant-card"
      + (item.applied ? " applied" : "") + (item.rejected ? " rejected" : "");
    card.dataset.index = String(index);

    const head = document.createElement("div");
    head.className = "assistant-card-head";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "suggestion-check";
    checkbox.value = String(index);
    checkbox.disabled = Boolean(item.applied || item.rejected);
    checkbox.setAttribute("aria-label", `Выбрать предложение ${item.entity} · ${item.field}`);
    const title = document.createElement("div");
    title.className = "assistant-card-title";
    const strong = document.createElement("strong");
    strong.textContent = `${item.entity} · ${item.field}`;
    title.append(strong);
    const badges = document.createElement("span");
    badges.className = "assistant-card-badges";
    const typeBadge = document.createElement("span");
    typeBadge.className = "badge idle";
    typeBadge.textContent = TYPE_LABELS[item.type] || item.type;
    badges.append(typeBadge);
    if (item.quote) {
      const evidenceBadge = document.createElement("span");
      evidenceBadge.className = "badge ok";
      evidenceBadge.textContent = "из PDF";
      badges.append(evidenceBadge);
    }
    if (item.entity && existingEntity(item.entity) && existingField(existingEntity(item.entity), item.field)) {
      const updateBadge = document.createElement("span");
      updateBadge.className = "badge warn";
      updateBadge.textContent = "обновит поле";
      badges.append(updateBadge);
    }
    title.append(badges);
    head.append(checkbox, title);
    card.append(head);

    if (item.value) {
      const example = document.createElement("p");
      example.className = "assistant-card-example";
      example.textContent = "Пример: " + item.value;
      card.append(example);
    } else {
      const example = document.createElement("p");
      example.className = "assistant-card-example muted";
      example.textContent = "Пример значения не передан.";
      card.append(example);
    }
    const explanation = document.createElement("p");
    explanation.className = "assistant-card-explanation";
    explanation.textContent = item.explanation || item.reason
      || (item.quote
        ? `Предложение опирается на цитату${item.page ? ` со страницы ${item.page}` : ""}. Проверьте поле вручную.`
        : "Пояснение не передано помощником. Проверьте поле и цитату вручную.");
    card.append(explanation);

    if (item.quote) {
      const evidence = document.createElement("div");
      evidence.className = "assistant-card-evidence";
      const quote = document.createElement("blockquote");
      quote.textContent = item.quote;
      evidence.append(quote);
      if (item.page) {
        const page = document.createElement("span");
        page.className = "badge idle";
        page.textContent = "Стр. " + item.page;
        page.title = "Страница из проверенной цитаты";
        evidence.append(page);
      }
      card.append(evidence);
    }

    const actions = document.createElement("div");
    actions.className = "assistant-card-actions";
    if (item.applied) {
      const done = document.createElement("span");
      done.className = "badge ok";
      done.textContent = item.applied === "updated" ? "Обновлено" : "Применено";
      actions.append(done);
    } else if (item.rejected) {
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "button";
      undo.textContent = "Вернуть";
      undo.onclick = () => {
        item.rejected = false;
        renderSuggestions();
      };
      actions.append(undo);
    } else {
      const apply = document.createElement("button");
      apply.type = "button";
      apply.className = "button primary";
      apply.textContent = "Применить";
      apply.disabled = tablesLocked();
      apply.title = tablesLocked() ? "Поля этой формы не сохраняются в выбранном профиле" : "";
      apply.onclick = () => {
        if (applySuggestion(index)) {
          renderSuggestions();
          const assistantStatus = byId("assistantStatus");
          if (assistantStatus) assistantStatus.textContent = status.textContent;
        }
      };
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "button";
      reject.textContent = "Отклонить";
      reject.onclick = () => {
        item.rejected = true;
        renderSuggestions();
        const assistantStatus = byId("assistantStatus");
        if (assistantStatus) assistantStatus.textContent = `Предложение «${item.field}» отклонено. Профиль не изменён.`;
      };
      actions.append(apply, reject);
    }
    card.append(actions);
    list.append(card);
  });
  const apply = byId("assistantApply");
  if (apply) {
    apply.hidden = !proposals.length;
    apply.disabled = tablesLocked();
    apply.textContent = "Применить выбранные";
  }
}

const assistantAsk = byId("assistantAsk");
if (assistantAsk) assistantAsk.onclick = async () => {
  const question = byId("assistantQuestion").value.trim();
  const assistantStatus = byId("assistantStatus");
  if (!question) return;
  if (!documentData) {
    chatMessage("Сначала загрузите PDF: помощник опирается только на цитаты статьи.");
    return;
  }
  const profileToken = profileRequest;
  const profile = {types: [...entities.children].map(entity => ({
    name: value(entity, "entityName"),
    fields: [...entity.querySelectorAll(".profile-field")].map(field => ({name: value(field, "fieldName")})),
  }))};
  const button = byId("assistantAsk");
  button.disabled = true;
  assistantStatus.hidden = false;
  assistantStatus.textContent = "Ищу поля в статье…";
  byId("assistantApply").hidden = true;
  byId("assistantSuggestions").replaceChildren();
  chatMessage(question, "user");
  byId("assistantQuestion").value = "";
  try {
    const response = await authorized(await apiFetch("/api/profile/assist", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question, profile, sources: documentData.sources.slice(0, 24),
        history: assistantHistory.slice(-4)}),
    }));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Помощник не ответил.");
    if (profileToken !== profileRequest) return;
    proposals = (Array.isArray(data.suggestions) ? data.suggestions : []).map(item => ({
      ...item, applied: false, rejected: false,
    }));
    assistantHistory.push({role: "user", content: question},
      {role: "assistant", content: JSON.stringify({suggestions: proposals})});
    assistantStatus.textContent = proposals.length
      ? `Предложено полей: ${proposals.length}. Примените карточки по одной или отметьте и примените выбранные.`
      : "По этим фрагментам нет подтверждённых предложений. Уточните вопрос.";
    chatMessage(proposals.length
      ? `Нашёл ${proposals.length} полей. Карточки показывают тип, пример и цитату со страницей.`
      : "В доступных фрагментах нет подтверждённых полей. Уточните вопрос или проверьте PDF.");
    renderSuggestions();
  } catch (error) {
    if (profileToken !== profileRequest) return;
    assistantStatus.textContent = error.message;
    chatMessage(error.message);
  } finally {
    button.disabled = false;
    syncPdfActions();
  }
};

const assistantApply = byId("assistantApply");
if (assistantApply) assistantApply.onclick = () => {
  if (tablesLocked()) {
    warnLocked();
    return;
  }
  const chosen = [...byId("assistantSuggestions").querySelectorAll(".suggestion-check:checked")]
    .map(input => Number(input.value));
  if (!chosen.length) {
    byId("assistantStatus").textContent = "Отметьте хотя бы одну карточку.";
    return;
  }
  let changed = 0;
  for (const index of chosen) {
    if (applySuggestion(index)) changed += 1;
  }
  renderSuggestions();
  const applyButton = byId("assistantApply");
  if (applyButton) applyButton.hidden = true;
  const message = changed
    ? `Применено предложений: ${changed}. Профиль изменён только в форме, сохранение запускается отдельно.`
    : "Выбранные предложения уже применены или отклонены.";
  byId("assistantStatus").textContent = message;
  chatMessage(message);
};

document.querySelectorAll("[data-assistant-prompt]").forEach(button => {
  button.onclick = () => {
    if (!documentData) {
      chatMessage("Сначала загрузите PDF: помощник опирается только на цитаты статьи.");
      return;
    }
    byId("assistantQuestion").value = button.dataset.assistantPrompt;
    byId("assistantAsk").click();
  };
});

const assistantCheck = byId("assistantCheck");
if (assistantCheck) assistantCheck.onclick = () => {
  chatMessage(checkProfile());
};

/* --------------------------------------------------------- библиотека профилей */

const profileSelect = byId("profileSelect");
if (profileSelect) profileSelect.onchange = () => {
  if (profileDirty && !window.confirm("В форме есть несохранённые изменения. Переключить профиль и потерять их?")) {
    profileSelect.value = sourceProfile?.id || "";
    return;
  }
  if (profileSelect.value) selectProfile(profileSelect.value);
  else startDraft();
};
const profileSave = byId("profileSave");
if (profileSave) profileSave.onclick = saveProfile;
const profileCopy = byId("profileCopy");
if (profileCopy) profileCopy.onclick = copyProfile;
const profileDelete = byId("profileDelete");
if (profileDelete) profileDelete.onclick = deleteProfile;
const profileNew = byId("profileNew");
if (profileNew) profileNew.onclick = () => {
  if (profileDirty && !window.confirm("В форме есть несохранённые изменения. Начать новый профиль и потерять их?")) return;
  startDraft();
};
const checkButton = byId("checkProfile");
if (checkButton) checkButton.onclick = () => checkProfile();

let loading = false;
let currentFile = null;
let handoffStored = false;
async function loadFile(file) {
  if (!file || loading) return;
  if (!(await sessionReady)) return;
  currentFile = file;
  handoffStored = false;
  clearHandoff();
  assistantHistory.length = 0;
  proposals = [];
  byId("assistantMessages")?.replaceChildren();
  byId("assistantSuggestions")?.replaceChildren();
  if (byId("assistantApply")) byId("assistantApply").hidden = true;
  if (byId("assistantStatus")) byId("assistantStatus").hidden = true;
  loading = true;
  upload.disabled = true;
  form.hidden = true;
  documentData = undefined;
  syncPdfActions();
  clearError();
  clearMatches();
  byId("profilePanel").hidden = false;
  status.textContent = "Читаем текст, таблицы и подписи страниц. Это может занять несколько минут…";
  try {
    const {data, job} = await awaitJob(await startJob(file));
    showParsed(data, job.filename || file.name);
    await releasePending();
  } catch (error) {
    // Файл, который сервер не принял, не возвращаем в мастер: иначе каждый
    // вход на страницу повторял бы тот же отказ. Выбрать другой PDF можно
    // кнопкой «Загрузить PDF» в шапке.
    currentFile = null;
    await releasePending();
    showError(error.message);
  } finally {
    upload.value = "";
    upload.disabled = false;
    loading = false;
  }
}

// Возврат на страницу профиля: активный разбор и его результат показываются
// снова, вместо пустого экрана с просьбой выбрать PDF.
async function resumeJob(saved, file = null) {
  // Файл уже выбран вручную: возобновление не должно перебивать его разбор.
  if (loading) return false;
  loading = true;
  upload.disabled = true;
  form.hidden = true;
  currentFile = file || currentFile;
  if (file) handoffStored = false;
  clearError();
  clearMatches();
  syncPdfActions();
  byId("profilePanel").hidden = false;
  const entryLine = byId("assistantEntry");
  if (entryLine) entryLine.hidden = true;
  status.textContent = "Возвращаемся к разбору PDF. Это может занять несколько минут…";
  try {
    const current = await readJob(saved.id);
    if (!current) throw missingJob();
    const wasActive = current.status === "queued" || current.status === "running";
    const {data, job} = await awaitJob(current);
    showParsed(data, job.filename || saved.filename || "Документ");
    if (!wasActive) {
      // Поля профиля живут только в форме, поэтому после возврата их
      // приходится заполнять заново: об этом честно сообщаем.
      status.textContent = "Разбор восстановлен. Поля профиля нужно проверить заново.";
    }
    await releasePending();
    return true;
  } catch (error) {
    forgetJob();
    if (error.missing) {
      // Задание исчезло на сервере: возвращаем экран к выбору PDF.
      byId("profilePanel").hidden = true;
      const entryLine = byId("assistantEntry");
      if (entryLine) entryLine.hidden = false;
      status.textContent = "";
      return false;
    }
    showError(error.message);
    return true;
  } finally {
    upload.disabled = false;
    loading = false;
  }
}

function handoffFailed() {
  try {
    if (sessionStorage.getItem(HANDOFF_KEY) !== "failed") return false;
    sessionStorage.removeItem(HANDOFF_KEY);
  } catch {
    return false;
  }
  const entry = byId("assistantEntry");
  if (!entry) return false;
  entry.textContent = "PDF из мастера не передался. Выберите файл снова: "
    + "поля, фрагменты и помощник появятся после чтения.";
  entry.hidden = false;
  return true;
}

function clearHandoff() {
  try { sessionStorage.removeItem(HANDOFF_KEY); } catch { /* хранилище недоступно */ }
}

// Что показать при входе: переданный из мастера PDF, продолжение активного
// разбора или просьбу выбрать файл.
async function restoreEntry() {
  // Без сессии страница уже уходит на вход: переданный PDF трогать нельзя.
  if (!(await sessionReady)) return;
  await loadProfileLibrary();
  refreshProfileContext();
  syncPdfActions();
  let file;
  if ("indexedDB" in globalThis) {
    try {
      file = await peekProfilePDF();
    } catch (error) {
      console.warn("Не удалось прочитать переданный PDF:", error.message);
    }
  }
  const saved = rememberedJob();
  if (file) {
    if (saved && saved.filename && saved.filename === file.name) {
      // Этот файл уже разбирается: продолжаем задание, а не запускаем второе.
      if (await resumeJob(saved, file)) return;
    }
    await loadFile(file);
    return;
  }
  if (saved) {
    const restored = await resumeJob(saved);
    // Задание истекло на сервере: если PDF из мастера тоже не передался,
    // об этом надо сказать, а не оставлять общую подсказку.
    if (!restored) handoffFailed();
    return;
  }
  handoffFailed();
}

upload.onchange = () => {
  const file = upload.files[0];
  if (file) loadFile(file);
};

// Страница сама решает, что показать при входе; без IndexedDB (jsdom, старый
// браузер) остаётся обычный выбор PDF.
void restoreEntry();

// Браузерная кнопка «назад» не проходит через ссылку, поэтому PDF, который
// ещё разбирается, сохраняется и на уходе со страницы. Если файл уже отдан
// стрелкой «назад», второй записи не нужно.
window.addEventListener("pagehide", () => {
  if (!currentFile || handoffStored) return;
  stashProfilePDF(currentFile)
    .catch(error => console.warn("Не удалось вернуть PDF в мастер:", error.message));
});

// Стрелка «назад» возвращает в мастер выбранный PDF: без этого файл
// пришлось бы выбирать заново, а мастер терял бы шаг с профилем.
const backLink = document.querySelector('a[aria-label="Назад на главную"]');
if (backLink) {
  backLink.addEventListener("click", async event => {
    if (!currentFile) return;
    event.preventDefault();
    try {
      await stashProfilePDF(currentFile);
      handoffStored = true;
    } catch (error) {
      console.warn("Не удалось вернуть PDF в мастер:", error.message);
    }
    location.href = backLink.href;
  });
}

form.onsubmit = event => {
  event.preventDefault();
  if (!documentData) return;
  try {
    const profile = readProfile();
    const evidence = (selection ?? []).filter(item => item.input.checked)
      .map(({type, source}) => ({type, source}));
    const payload = {schema_version: 1, document: {filename: documentData.filename,
      pages: documentData.pages, warnings: documentData.warnings}, profile, evidence};
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = "extraction-profile.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    clearError();
    status.textContent = "Профиль сохранён. Выбрано фрагментов: " + evidence.length + ".";
  } catch (error) {
    showError(error.message);
  }
};

window.addEventListener("beforeunload", event => {
  if (!profileDirty) return;
  event.preventDefault();
  event.returnValue = "";
});

document.body.dataset.profile = "ready";
refreshProfileContext();
syncPdfActions();
