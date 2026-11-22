import { stashProfilePDF, takeProfilePDF } from "./pending.js";
import { apiFetch, loadSession } from "./api.js";

const byId = id => document.getElementById(id);
const upload = byId("profilePdf");
const form = byId("profileForm");
const entities = byId("profileEntities");
const matches = byId("profileMatches");
const status = byId("profileStatus");
const errorLine = byId("profileError");
let documentData;
let selection;
const presets = {
  solar: {title: "Солнечные элементы", types: [{name: "Устройство", aliases: "device, cell, ячейка", fields: ["Структура", "Материал", "Площадь"]}, {name: "Измерение", aliases: "efficiency, PCE, Voc, Jsc, FF, КПД", fields: ["Показатель", "Значение", "Единица", "Условия измерения"]}]},
  rag: {title: "RAG-система", types: [{name: "Система", aliases: "RAG, retrieval, model, модель", fields: ["Название", "Модель", "Метод поиска"]}, {name: "Результат", aliases: "accuracy, recall, precision, F1, benchmark, dataset", fields: ["Набор данных", "Метрика", "Значение", "Конфигурация"]}]},
  basic: {title: "Результаты статьи", types: [{name: "Результат", aliases: "result, results, experiment, результат, эксперимент", fields: ["Объект", "Показатель", "Значение", "Условия"]}]},
};

const value = (row, name) => row.querySelector('[name="' + name + '"]').value.trim();
const checked = (row, name) => row.querySelector('[name="' + name + '"]').checked;
const normalize = text => text.normalize("NFKC").toLowerCase();

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
  const types = entities.children.length;
  const fields = entities.querySelectorAll(".profile-field").length;
  status.textContent = types ? `Набор изменён: типов ${types}, полей ${fields}.` : "Типы сущностей удалены.";
  clearError();
}

function fieldMarkup() {
  return `
    <label class="field pf-name"><span>Название поля</span><input name="fieldName" required placeholder="Например, Значение"></label>
    <label class="field pf-type"><span>Тип</span><select name="type">
      <option value="text">Текст</option><option value="number">Число</option>
      <option value="boolean">Да / нет</option><option value="reference">Связь с типом</option></select></label>
    <label class="pf-required"><input type="checkbox" name="requiredField"> Обязательное</label>
    <button type="button" class="pf-remove" aria-label="Удалить поле" title="Удалить поле">×</button>
    <div class="pf-extra">
      <label class="field pf-unit" data-when="number"><span>Единица в статье</span><input name="unit" placeholder="например, мА/см²"></label>
      <label class="field pf-target-unit" data-when="number"><span>Нужная единица</span><input name="targetUnit" placeholder="например, %"></label>
      <label class="field pf-factor" data-when="number"><span>Коэффициент</span><input name="factor" type="number" step="any" placeholder="если единицы разные"></label>
      <label class="field pf-wide" data-when="reference"><span>Связанный тип</span><input name="target" list="profileTypes" placeholder="Название типа из списка выше"></label>
    </div>
    <label class="field pf-example"><span>Пример значения</span><input name="example" maxlength="160" placeholder="Из статьи или от помощника"></label>`;
}

function updateExtra(field) {
  const type = field.querySelector('[name="type"]').value;
  field.dataset.type = type;
  field.querySelectorAll("[data-when]").forEach(node => {
    node.hidden = node.dataset.when !== type;
  });
}

function addField(container, name = "", kind = "text", example = "") {
  const field = document.createElement("fieldset");
  field.className = "profile-field";
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
  type.value = kind;
  field.querySelector('[name="example"]').value = example;
  updateExtra(field);
  staleMatches();
}

function addEntity(preset) {
  const entity = document.createElement("fieldset");
  entity.className = "profile-entity";
  entity.innerHTML = `<legend>Тип сущности</legend>
    <div class="entity-head">
      <label class="field pe-name"><span>Название типа</span><input name="entityName" required placeholder="Например, Результат"></label>
      <label class="field pe-aliases"><span>Слова-подсказки</span><input name="aliases" placeholder="Через запятую: result, experiment…"></label>
      <button type="button" class="pf-remove pe-remove" aria-label="Удалить тип" title="Удалить тип">×</button>
    </div>
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
  if (preset) {
    entity.querySelector('[name="entityName"]').value = preset.name;
    entity.querySelector('[name="aliases"]').value = preset.aliases;
    preset.fields.forEach(name => addField(fields, name));
  } else addField(fields);
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
  const preset = presets[key];
  clearError();
  clearMatches();
  entities.replaceChildren();
  form.elements.title.value = preset.title;
  preset.types.forEach(addEntity);
  refreshTypes();
  document.querySelectorAll('[data-preset]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.preset === key)));
  status.textContent = "Готовый набор добавлен. Можно изменить названия и поля под статью.";
}
document.querySelectorAll('[data-preset]').forEach(button => button.onclick = () => applyPreset(button.dataset.preset));

function readProfile() {
  const types = [...entities.children].map(entity => ({
    name: value(entity, "entityName"), selected: true,
    aliases: value(entity, "aliases").split(",").map(term => term.trim()).filter(Boolean),
    fields: [...entity.querySelectorAll(".profile-field")].map(field => {
      const type = value(field, "type");
      const factor = value(field, "factor");
      return {
        name: value(field, "fieldName"), type, selected: true,
        example: value(field, "example"),
        required: checked(field, "requiredField"),
        ...(type === "number" && {unit: value(field, "unit"), targetUnit: value(field, "targetUnit"),
          factor: factor === "" ? null : Number(factor)}),
        ...(type === "reference" && {target: value(field, "target")}),
      };
    }),
  }));
  const unique = names => new Set(names.map(normalize)).size === names.length;
  if (!types.length) throw new Error("Добавьте хотя бы один тип сущности.");
  if (!types.every(type => type.name) || !unique(types.map(type => type.name))) {
    throw new Error("Укажите уникальные названия типов.");
  }
  for (const type of types) {
    if (!type.fields.length) throw new Error("Добавьте хотя бы одно поле в тип «" + type.name + "».");
    if (!type.fields.every(field => field.name) || !unique(type.fields.map(field => field.name))) {
      throw new Error("Укажите уникальные названия полей в типе «" + type.name + "».");
    }
    for (const field of type.fields) {
      if (field.type === "reference" && !types.some(other => other.selected && normalize(other.name) === normalize(field.target))) {
        throw new Error("Для связи «" + field.name + "» укажите название выбранного типа.");
      }
      if (field.type === "number" && field.targetUnit && field.targetUnit !== field.unit
          && (!field.unit || !Number.isFinite(field.factor) || field.factor <= 0)) {
        throw new Error("Для пересчёта «" + field.name + "» укажите исходную единицу и положительный коэффициент.");
      }
    }
  }
  return {title: value(form, "title"), types};
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
  refreshTypes();
  staleMatches();
  markEdited();
});
function scan(browseAll) {
  if (!documentData || !form.reportValidity()) return;
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

async function readPdf(file) {
  let job = await parseResponse(await authorized(await apiFetch("/api/parse", {
    method: "POST",
    headers: {"Content-Type": "application/pdf", "X-Parser": "auto", "X-OCR": "auto", "X-Profile": "solar",
      "X-Filename": encodeURIComponent(file.name)},
    body: file,
  })));
  const path = "/api/parse/" + encodeURIComponent(job.id);
  while (job.status === "queued" || job.status === "running") {
    status.textContent = job.status === "queued" ? "PDF ожидает своей очереди…"
      : "Читаем текст, таблицы и подписи страниц. Это может занять несколько минут…";
    await new Promise(resolve => setTimeout(resolve, 1000));
    job = await parseResponse(await fetch(path, {cache: "no-store"}));
  }
  if (job.status !== "ok") throw new Error(job.error || "Разбор PDF завершился с ошибкой.");
  const result = await parseResponse(await fetch(path + "/result", {cache: "no-store"}));
  return {pages: result.document.pages, sources: result.sources, warnings: result.warnings || []};
}

let proposals = [];
const assistantHistory = [];
function chatMessage(text, side = "assistant") {
  const bubble = document.createElement("p");
  bubble.className = `chat-bubble ${side === "user" ? "user-bubble" : "assistant-bubble"}`;
  bubble.textContent = text;
  const messages = byId("assistantMessages");
  messages.append(bubble);
  messages.scrollTop = messages.scrollHeight;
}
byId("assistantAsk").onclick = async () => {
  const question = byId("assistantQuestion").value.trim();
  const assistantStatus = byId("assistantStatus");
  if (!question || !documentData) { chatMessage("Сначала загрузите PDF и напишите вопрос по статье."); return; }
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
    proposals = data.suggestions || [];
    assistantHistory.push({role: "user", content: question},
      {role: "assistant", content: JSON.stringify({suggestions: proposals})});
    assistantStatus.textContent = proposals.length ? `Предложено полей: ${proposals.length}. Отметьте нужные.`
      : "По этим фрагментам нет подтверждённых предложений. Уточните вопрос.";
    chatMessage(proposals.length ? `Нашёл ${proposals.length} полей. Отметьте подходящие карточки ниже.`
      : "В доступных фрагментах нет подтверждённых полей. Уточните вопрос или проверьте PDF.");
    const list = byId("assistantSuggestions");
    proposals.forEach((item, index) => {
      const label = document.createElement("label");
      label.className = "assistant-suggestion";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = String(index);
      label.append(checkbox, ` ${item.entity} · ${item.field} · ${item.type}`);
      if (item.value) label.append(Object.assign(document.createElement("small"), {textContent: `Значение: ${item.value}`}));
      if (item.quote) label.append(Object.assign(document.createElement("small"), {textContent: `Стр. ${item.page}: ${item.quote}`}));
      list.append(label);
    });
    byId("assistantApply").hidden = !proposals.length;
  } catch (error) {
    assistantStatus.textContent = error.message;
    chatMessage(error.message);
  } finally { button.disabled = false; }
};

byId("assistantApply").onclick = () => {
  const selected = [...byId("assistantSuggestions").querySelectorAll('input:checked')]
    .map(input => proposals[Number(input.value)]);
  let added = 0;
  for (const item of selected) {
    let entity = [...entities.children].find(row => normalize(value(row, "entityName")) === normalize(item.entity));
    if (!entity) {
      addEntity({name: item.entity, aliases: "", fields: []});
      entity = entities.lastElementChild;
    }
    if ([...entity.querySelectorAll('.profile-field')].some(row => normalize(value(row, "fieldName")) === normalize(item.field))) continue;
    addField(entity.querySelector(".profile-fields"), item.field, item.type, item.value || "");
    added++;
  }
  byId("assistantStatus").textContent = added ? `Добавлено полей: ${added}. Проверьте их перед сохранением.`
    : "Новые поля не выбраны или уже есть в профиле.";
  chatMessage(byId("assistantStatus").textContent);
  byId("assistantApply").hidden = true;
  byId("assistantSuggestions").replaceChildren();
  proposals = [];
};

let loading = false;
let currentFile = null;
async function loadFile(file) {
  if (!file || loading) return;
  if (!(await sessionReady)) return;
  currentFile = file;
  assistantHistory.length = 0;
  byId("assistantMessages").replaceChildren();
  byId("assistantSuggestions").replaceChildren();
  byId("assistantApply").hidden = true;
  byId("assistantStatus").hidden = true;
  loading = true;
  upload.disabled = true;
  form.hidden = true;
  documentData = undefined;
  clearError();
  clearMatches();
  byId("profilePanel").hidden = false;
  status.textContent = "Читаем текст, таблицы и подписи страниц. Это может занять несколько минут…";
  try {
    const data = await readPdf(file);
    documentData = {...data, filename: file.name};
    const sourceLine = byId("profileSource");
    if (sourceLine) {
      sourceLine.textContent = file.name + " · страниц: " + data.pages + " · фрагментов: " + data.sources.length
        + (data.warnings.length ? " · " + data.warnings.join(" ") : "");
    }
    form.hidden = false;
    byId("assistantEntry").hidden = true;
    if (!entities.children.length) applyPreset("basic");
    if (!byId("assistantMessages").children.length) {
      chatMessage("PDF прочитан. Спросите, какие поля искать в статье, или отметьте фрагменты слева.");
    }
    status.textContent = "PDF прочитан. Выберите готовый набор или опишите поля вручную.";
  } catch (error) {
    showError(error.message);
  } finally {
    upload.value = "";
    upload.disabled = false;
    loading = false;
  }
}

upload.onchange = () => {
  const file = upload.files[0];
  if (file) loadFile(file);
};

// Без IndexedDB (jsdom, старый браузер) страница работает как обычно:
// PDF выбирается вручную.
if ("indexedDB" in globalThis) {
  takeProfilePDF().then(file => {
    if (file) loadFile(file);
  }).catch(error => {
    console.warn("Не удалось забрать PDF из мастер-потока:", error.message);
  });
}

// Стрелка «назад» возвращает в мастер выбранный PDF: без этого файл
// пришлось бы выбирать заново, а мастер терял бы шаг с профилем.
const backLink = document.querySelector('a[aria-label="Назад на главную"]');
if (backLink) {
  backLink.addEventListener("click", async event => {
    if (!currentFile) return;
    event.preventDefault();
    try {
      await stashProfilePDF(currentFile);
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

document.body.dataset.profile = "ready";
