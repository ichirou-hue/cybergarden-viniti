"use strict";

/* Конструктор выгрузки: область -> таблица -> колонки -> фильтры -> сортировка -> CSV.
   Все данные из базы выводятся через textContent/createElement, без innerHTML. */

const $ = (id) => document.getElementById(id);

const REQUIRED = ["source_page", "source_fragment"];
const FRAGMENT_LIMIT = 200;
const DEFAULT_LIMIT = 500;

const OPS_NUM = [["=", "="], ["!=", "!="], [">", ">"], [">=", ">="], ["<", "<"], ["<=", "<="]];
const OPS_TXT = [["=", "="], ["!=", "!="], ["~", "~"]];
const OP_TITLE = { "=": "равно", "!=": "не равно", ">": "больше", ">=": "не меньше",
                   "<": "меньше", "<=": "не больше", "~": "содержит подстроку" };

let passport = null;

const state = {
  area: "solar",
  table: "results",
  cols: [],       // имена выбранных колонок (порядок - по схеме)
  filters: [],    // [{field, op, value}]
  order: "",      // "" или "поле" / "-поле"
  limit: DEFAULT_LIMIT,
};

function meta() { return passport.areas[state.area].tables[state.table]; }
function fields() { return meta().fields || {}; }
function isNumber(col) { return (fields()[col] || {}).type === "number"; }
function isEnum(col) { return (fields()[col] || {}).type === "enum"; }

function typeLabel(col) {
  const t = (fields()[col] || {}).type || "text";
  if (t === "number") return "число";
  if (t === "enum") return "список";
  if (t === "text") return "текст";
  return t;
}

function requiredCols(m) {
  return REQUIRED.filter((c) => m.columns.includes(c));
}

function orderedCols() {
  return meta().columns.filter((c) => state.cols.includes(c));
}

/* ---------- загрузка паспорта ---------- */

async function loadPassport() {
  const res = await fetch("/api/passport");
  if (!res.ok) throw new Error("Не удалось загрузить схему. Запущен ли сервер?");
  passport = await res.json();
}

function normalizeState() {
  const area = passport.areas[state.area] ? state.area : "solar";
  const tables = Object.keys(passport.areas[area].tables);
  const table = tables.includes(state.table) ? state.table : "results";
  const m = passport.areas[area].tables[table];
  const valid = new Set(m.columns);
  const req = requiredCols(m);
  const cols = (state.cols.length ? state.cols : m.columns).filter((c) => valid.has(c));
  for (const c of req) if (!cols.includes(c)) cols.push(c);
  const filters = state.filters
    .filter((f) => valid.has(f.field))
    .map((f) => ({ field: f.field, op: validOp(f.field, f.op), value: f.value }));
  const orderKey = state.order.replace(/^[-+]/, "");
  const order = valid.has(orderKey) ? state.order : "";
  const limit = Number.isFinite(state.limit) && state.limit >= 0 ? Math.floor(state.limit) : DEFAULT_LIMIT;
  Object.assign(state, { area, table, cols, filters, order, limit });
}

function validOp(field, op) {
  const ops = opsFor(field).map(([value]) => value);
  return ops.includes(op) ? op : (isNumber(field) ? ">" : "=");
}

function opsFor(field) { return isNumber(field) ? OPS_NUM : OPS_TXT; }

/* ---------- построение интерфейса ---------- */

function fillTableSelect() {
  const sel = $("tableSelect");
  sel.replaceChildren();
  for (const name of Object.keys(passport.areas[state.area].tables)) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    sel.append(o);
  }
  sel.value = state.table;
}

function renderColumns() {
  const m = meta();
  const req = new Set(requiredCols(m));
  const box = $("columns");
  box.replaceChildren();
  for (const col of m.columns) {
    const label = document.createElement("label");
    label.className = "col";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = col;
    input.checked = req.has(col) ? true : state.cols.includes(col);
    input.disabled = req.has(col);
    input.addEventListener("change", () => {
      input.disabled || syncColsFromUI();
    });
    const name = document.createElement("span");
    name.className = "col-name";
    name.textContent = col;
    if (req.has(col)) name.classList.add("req");
    const badge = document.createElement("span");
    badge.className = "type-badge";
    badge.textContent = typeLabel(col);
    label.append(input, name, badge);
    if (req.has(col)) {
      const mark = document.createElement("span");
      mark.className = "col-req";
      mark.textContent = "обяз.";
      label.append(mark);
    }
    box.append(label);
  }
}

function syncColsFromUI() {
  state.cols = [];
  const inputs = $("columns").querySelectorAll("input");
  for (const input of inputs) if (input.checked) state.cols.push(input.value);
  syncURL();
}

function setAllCols(onlyRequired) {
  const m = meta();
  const req = new Set(requiredCols(m));
  state.cols = m.columns.filter((c) => (onlyRequired ? req.has(c) : true));
  renderColumns();
  syncURL();
}

function fieldOptions(selected) {
  const frag = document.createDocumentFragment();
  for (const col of meta().columns) {
    const o = document.createElement("option");
    o.value = col;
    o.textContent = col;
    if (col === selected) o.selected = true;
    frag.append(o);
  }
  return frag;
}

function renderFilters() {
  const box = $("filters");
  box.replaceChildren();
  state.filters.forEach((filter, index) => {
    const row = document.createElement("div");
    row.className = "filter-row";

    const fieldSel = document.createElement("select");
    fieldSel.setAttribute("aria-label", "Поле фильтра");
    fieldSel.append(fieldOptions(filter.field));
    fieldSel.addEventListener("change", () => {
      state.filters[index].field = fieldSel.value;
      state.filters[index].op = validOp(fieldSel.value, state.filters[index].op);
      renderFilters();
      syncURL();
    });

    const opSel = document.createElement("select");
    opSel.setAttribute("aria-label", "Оператор");
    for (const [value, label] of opsFor(filter.field)) {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      o.title = OP_TITLE[value];
      if (value === filter.op) o.selected = true;
      opSel.append(o);
    }
    opSel.addEventListener("change", () => {
      state.filters[index].op = opSel.value;
      syncURL();
    });

    const valueInput = document.createElement("input");
    valueInput.type = "text";
    valueInput.value = filter.value;
    valueInput.placeholder = isNumber(filter.field) ? "число" : "значение";
    valueInput.setAttribute("inputmode", isNumber(filter.field) ? "decimal" : "text");
    valueInput.addEventListener("input", () => {
      state.filters[index].value = valueInput.value;
      syncURL();
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "×";
    remove.title = "Убрать фильтр";
    remove.setAttribute("aria-label", "Убрать фильтр");
    remove.addEventListener("click", () => {
      state.filters.splice(index, 1);
      renderFilters();
      syncURL();
    });

    row.append(fieldSel, opSel, valueInput, remove);
    box.append(row);
  });

  const hint = document.createElement("p");
  hint.className = "filter-hint";
  hint.textContent = ["=", "!=", ">", ">=", "<", "<="].some((o) =>
    state.filters.some((f) => isNumber(f.field) && o === f.op))
    ? "Сравнение по числу, не по тексту." : "";
  if (hint.textContent) box.append(hint);
}

function renderSort() {
  const sel = $("sortField");
  sel.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "—";
  sel.append(empty);
  sel.append(fieldOptions(state.order.replace(/^[-+]/, "")));
  sel.value = state.order.replace(/^[-+]/, "");
  $("sortDir").value = state.order.startsWith("-") ? "desc" : "asc";
}

function renderAreaSwitch() {
  $("areaSolar").setAttribute("aria-selected", String(state.area === "solar"));
  $("areaRag").setAttribute("aria-selected", String(state.area === "rag"));
}

function renderAll() {
  fillTableSelect();
  renderColumns();
  renderFilters();
  renderSort();
  renderAreaSwitch();
  $("limit").value = String(state.limit);
  updateLinks();
}

/* ---------- адресная строка ---------- */

function currentQuery(extraLimit) {
  const p = new URLSearchParams();
  p.set("area", state.area);
  p.set("table", state.table);
  for (const c of orderedCols()) p.append("col", c);
  for (const f of state.filters) if (f.field) p.append("where", f.field + f.op + f.value);
  if (state.order) p.set("order", state.order);
  if (state.limit && extraLimit !== false) p.set("limit", String(state.limit));
  return p;
}

function syncURL() {
  const p = currentQuery();
  const qs = p.toString();
  history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  updateLinks();
}

function sliceURL() {
  return "/api/results.csv?" + currentQuery().toString();
}

function fullURL() {
  const p = new URLSearchParams();
  p.set("area", state.area);
  p.set("table", state.table);
  return "/api/results.csv?" + p.toString();
}

function updateLinks() {
  $("download").href = sliceURL();
  $("downloadFull").href = fullURL();
  $("exportHint").textContent =
    "«Скачать CSV» - только выбранные колонки и фильтры, это рабочий срез. " +
    "«Полная сдача» - все колонки схемы таблицы без фильтров, ровно такой файл " +
    "принимает валидатор организаторов; срез он не примет.";
}

function readURL() {
  const p = new URLSearchParams(location.search);
  state.area = p.get("area") || "solar";
  state.table = p.get("table") || "results";
  state.cols = p.getAll("col");
  state.filters = p.getAll("where").map(parseWhere).filter(Boolean);
  state.order = p.get("order") || "";
  const limit = parseInt(p.get("limit"), 10);
  state.limit = Number.isFinite(limit) ? limit : DEFAULT_LIMIT;
}

function parseWhere(clause) {
  const ops = [">=", "<=", "!=", "=", ">", "<", "~"];
  for (const op of ops) {
    const i = clause.indexOf(op);
    if (i > 0) {
      return { field: clause.slice(0, i).trim(), op, value: clause.slice(i + op.length).trim() };
    }
  }
  return null;
}

/* ---------- запрос и рендер результата ---------- */

function showError(message) {
  const status = $("status");
  status.textContent = message;
  status.className = "status error";
  $("count").textContent = "";
  $("thead").replaceChildren();
  $("tbody").replaceChildren();
  showEmpty(message);
}

function showEmpty(message) {
  const empty = $("empty");
  empty.hidden = false;
  empty.textContent = message;
}

function hideEmpty() { $("empty").hidden = true; }

async function apply() {
  const cols = orderedCols();
  if (!cols.length) {
    showError("Выберите хотя бы одну колонку.");
    return;
  }
  try {
    const res = await fetch("/api/results?" + currentQuery().toString());
    if (!res.ok) {
      let message = "HTTP " + res.status;
      try {
        const body = await res.json();
        if (body && body.error) message = body.error;
      } catch (e) { /* не JSON */ }
      throw new Error(message);
    }
    const data = await res.json();
    renderTable(data);
  } catch (error) {
    showError(error && error.message ? error.message : "Сеть недоступна. Сервер запущен?");
  }
}

function renderTable(data) {
  const status = $("status");
  status.textContent = "";
  status.className = "status";
  $("count").textContent = "Строк: " + data.count;

  const numeric = new Set(data.columns.filter((c) => isNumber(c)));

  const theadRow = document.createElement("tr");
  for (const col of data.columns) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = col;
    th.title = (fields()[col] || {}).description || col;
    theadRow.append(th);
  }
  $("thead").replaceChildren(theadRow);

  const tbody = $("tbody");
  tbody.replaceChildren();
  if (!data.count) {
    showEmpty("По этому запросу ничего не найдено. Измените фильтры или колонки.");
    return;
  }
  hideEmpty();
  for (const row of data.rows) {
    const tr = document.createElement("tr");
    for (const col of data.columns) {
      const td = document.createElement("td");
      if (numeric.has(col)) td.className = "num";
      fillCell(td, row[col], col);
      tr.append(td);
    }
    tbody.append(tr);
  }
}

function fillCell(td, value, col) {
  const text = value === null || value === undefined ? "" : String(value);
  if (col === "source_fragment" && text.length > FRAGMENT_LIMIT) {
    fillFragment(td, text);
    return;
  }
  td.textContent = text;
}

function fillFragment(td, full) {
  td.replaceChildren();
  td.className = "frag";
  const expanded = td.dataset.expanded === "1";
  const span = document.createElement("span");
  span.textContent = expanded ? full : full.slice(0, FRAGMENT_LIMIT) + "…";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "frag-toggle";
  btn.textContent = expanded ? "свернуть" : "развернуть";
  btn.addEventListener("click", () => {
    td.dataset.expanded = expanded ? "0" : "1";
    fillFragment(td, full);
  });
  td.append(span);
  if (!expanded) td.append(btn);
}

/* ---------- события ---------- */

function bindEvents() {
  $("areaSolar").addEventListener("click", () => switchArea("solar"));
  $("areaRag").addEventListener("click", () => switchArea("rag"));

  $("tableSelect").addEventListener("change", () => {
    const m = passport.areas[state.area].tables[$("tableSelect").value];
    state.table = $("tableSelect").value;
    state.cols = m.columns.slice();
    state.filters = [];
    state.order = "";
    renderAll();
    syncURL();
    apply();
  });

  $("allCols").addEventListener("click", () => setAllCols(false));
  $("reqCols").addEventListener("click", () => setAllCols(true));
  $("resetCols").addEventListener("click", () => setAllCols(false));

  $("addFilter").addEventListener("click", () => {
    const m = meta();
    const numeric = m.columns.find((c) => isNumber(c));
    const first = numeric || m.columns[0];
    state.filters.push({ field: first, op: isNumber(first) ? ">" : "=", value: "" });
    renderFilters();
    syncURL();
  });

  $("sortField").addEventListener("change", () => {
    const key = $("sortField").value;
    state.order = key ? ($("sortDir").value === "desc" ? "-" + key : key) : "";
    syncURL();
  });
  $("sortDir").addEventListener("change", () => {
    const key = $("sortField").value;
    state.order = key ? ($("sortDir").value === "desc" ? "-" + key : key) : "";
    syncURL();
  });

  $("limit").addEventListener("change", () => {
    const value = parseInt($("limit").value, 10);
    state.limit = Number.isFinite(value) && value >= 0 ? value : 0;
    syncURL();
  });

  $("apply").addEventListener("click", apply);
}

function switchArea(area) {
  if (area === state.area) return;
  state.area = area;
  const tables = Object.keys(passport.areas[area].tables);
  state.table = tables.includes("results") ? "results" : tables[0];
  state.cols = passport.areas[area].tables[state.table].columns.slice();
  state.filters = [];
  state.order = "";
  renderAll();
  syncURL();
  apply();
}

/* ---------- старт ---------- */

async function init() {
  try {
    await loadPassport();
    readURL();
    normalizeState();
    renderAll();
    syncURL();
    await apply();
  } catch (error) {
    showError(error && error.message ? error.message : "Ошибка запуска.");
  }
}

document.addEventListener("DOMContentLoaded", init);