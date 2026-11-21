import { takeProfilePDF } from "./pending.js";

const byId = id => document.getElementById(id);
const upload = byId("profilePdf");
const form = byId("profileForm");
const entities = byId("profileEntities");
const matches = byId("profileMatches");
const status = byId("profileStatus");
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

function clearMatches() {
  selection = undefined;
  matches.replaceChildren();
}

function addField(container, name = "") {
  const field = document.createElement("fieldset");
  field.className = "profile-field";
  field.innerHTML = `
    <label><input type="checkbox" name="includeField" checked> Извлекать поле</label>
    <label>Название поля <input name="fieldName" required></label>
    <label>Тип <select name="type"><option value="text">Текст</option><option value="number">Число</option>
      <option value="boolean">Да / нет</option><option value="reference">Связь</option></select></label>
    <label><input type="checkbox" name="requiredField"> Обязательное поле</label>
    <label>Исходная единица <input name="unit"></label>
    <label>Целевая единица <input name="targetUnit"></label>
    <label>Коэффициент пересчёта <input name="factor" type="number" step="any"></label>
    <label>Тип связанной сущности <input name="target"></label>
    <button type="button" class="button">Удалить поле</button>`;
  field.querySelector("button").onclick = () => {
    field.remove();
    clearMatches();
  };
  container.append(field);
  field.querySelector('[name="fieldName"]').value = name;
  clearMatches();
}

function addEntity(preset) {
  const entity = document.createElement("fieldset");
  entity.className = "profile-entity";
  entity.innerHTML = `<legend>Тип сущности</legend>
    <label><input type="checkbox" name="includeEntity" checked> Извлекать этот тип</label>
    <label>Название типа <input name="entityName" required></label>
    <label>Подсказки для поиска через запятую <input name="aliases" placeholder="Названия и сокращения из статьи"></label>
    <div class="profile-fields"></div>
    <button type="button" class="button">Добавить поле</button>
    <button type="button" class="button">Удалить тип</button>`;
  const fields = entity.querySelector(".profile-fields");
  const [add, remove] = entity.querySelectorAll("button");
  add.onclick = () => addField(fields);
  remove.onclick = () => {
    entity.remove();
    clearMatches();
  };
  entities.append(entity);
  if (preset) {
    entity.querySelector('[name="entityName"]').value = preset.name;
    entity.querySelector('[name="aliases"]').value = preset.aliases;
    preset.fields.forEach(name => addField(fields, name));
  } else addField(fields);
}
function applyPreset(key) {
  const preset = presets[key];
  entities.replaceChildren();
  form.elements.title.value = preset.title;
  preset.types.forEach(addEntity);
  document.querySelectorAll('[data-preset]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.preset === key)));
  status.textContent = "Готовый набор добавлен. Можно изменить названия и поля под статью.";
}
document.querySelectorAll('[data-preset]').forEach(button => button.onclick = () => applyPreset(button.dataset.preset));

function readProfile() {
  const types = [...entities.children].map(entity => ({
    name: value(entity, "entityName"), selected: checked(entity, "includeEntity"),
    aliases: value(entity, "aliases").split(",").map(term => term.trim()).filter(Boolean),
    fields: [...entity.querySelectorAll(".profile-field")].map(field => {
      const type = value(field, "type");
      const factor = value(field, "factor");
      return {
        name: value(field, "fieldName"), type, selected: checked(field, "includeField"),
        required: checked(field, "requiredField"),
        ...(type === "number" && {unit: value(field, "unit"), targetUnit: value(field, "targetUnit"),
          factor: factor === "" ? null : Number(factor)}),
        ...(type === "reference" && {target: value(field, "target")}),
      };
    }),
  }));
  const unique = names => new Set(names.map(normalize)).size === names.length;
  if (!types.some(type => type.selected)) throw new Error("Отметьте хотя бы один тип для извлечения.");
  if (!types.every(type => type.name) || !unique(types.map(type => type.name))) {
    throw new Error("Укажите уникальные названия типов.");
  }
  for (const type of types) {
    if (!type.fields.every(field => field.name) || !unique(type.fields.map(field => field.name))) {
      throw new Error("Укажите уникальные названия полей в типе «" + type.name + "».");
    }
    for (const field of type.fields.filter(field => type.selected && field.selected)) {
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
      label.append(input, " Стр. " + source.page + " · " + source.quote);
      panel.append(label);
      selection.push({type: type.name, source, input});
    }
    if (!candidates.length) panel.append("Проверьте названия и добавьте сокращения из статьи. Тип можно оставить выбранным.");
    matches.append(panel);
  }
  status.textContent = browseAll ? "Отметьте подходящие фрагменты для каждого типа."
    : "Отметьте подходящие фрагменты. Подсказки основаны на совпадении слов.";
}

byId("addEntity").onclick = () => addEntity();
form.addEventListener("input", event => {
  if (!matches.contains(event.target)) clearMatches();
});
function scan(browseAll) {
  if (!documentData || !form.reportValidity()) return;
  try {
    showMatches(readProfile(), browseAll);
  } catch (error) {
    status.textContent = error.message;
  }
}
byId("scanProfile").onclick = () => scan(false);
byId("browseProfile").onclick = () => scan(true);

let loading = false;
async function loadFile(file) {
  if (!file || loading) return;
  loading = true;
  upload.disabled = true;
  form.hidden = true;
  documentData = undefined;
  clearMatches();
  byId("profilePanel").hidden = false;
  status.textContent = "Читаем PDF…";
  try {
    const response = await fetch("/api/profile/source", {
      method: "POST", headers: {"Content-Type": "application/pdf"}, body: file,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    documentData = {...data, filename: file.name};
    const sourceLine = byId("profileSource");
    if (sourceLine) {
      sourceLine.textContent = file.name + " · страниц: " + data.pages + " · фрагментов: " + data.sources.length
        + (data.warnings.length ? " · " + data.warnings.join(" ") : "");
    }
    form.hidden = false;
    if (!entities.children.length) applyPreset("basic");
    status.textContent = "PDF прочитан. Выберите готовый набор или опишите поля вручную.";
  } catch (error) {
    status.textContent = error.message;
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

takeProfilePDF().then(file => {
  if (file) loadFile(file);
}).catch(error => {
  // Без IndexedDB страница работает как обычно: PDF выбирается вручную.
  console.warn("Не удалось забрать PDF из мастер-потока:", error.message);
});

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
    status.textContent = "Профиль сохранён. Выбрано фрагментов: " + evidence.length + ".";
  } catch (error) {
    status.textContent = error.message;
  }
};

document.body.dataset.profile = "ready";
