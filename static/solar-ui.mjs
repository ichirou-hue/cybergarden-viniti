import { solarCatalog, filterSolarCatalog } from "/solar.mjs";

const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(el => [el.id, el]));
const tables = ["publications", "materials", "devices", "layers", "measurements", "results", "issues"];
const columns = ("publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit "
  + "aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit "
  + "result_origin cited_publication_id source_page source_section source_table source_figure source_fragment "
  + "extraction_status duplicate_of note").split(" ");
const labels = {passed: "Пройдена", review: "На разборе", pending: "Ожидает"};

const sample = {
  publications: [{publication_id: "DEMO", title: "Синтетический паспорт перовскитного элемента"}],
  materials: [{publication_id: "DEMO", material_id: "M1", material_name_raw: "Перовскит"}],
  devices: [{publication_id: "DEMO", device_id: "D1", device_label: "Образец A", architecture: "p-i-n", absorber_material_id: "M1"}],
  layers: [],
  measurements: [{publication_id: "DEMO", measurement_id: "E1", spectrum: "AM1.5G", scan_direction: "reverse"}],
  results: [
    ["PCE", "18.2", "%", "passed", "Power conversion efficiency was 18.2%."],
    ["Voc", "1.08", "V", "passed", "Open-circuit voltage was 1.08 V."],
    ["FF", "112", "%", "review", "Fill factor was 112%."],
    ["Jsc", "22.4", "mA/cm²", "pending", "Short-circuit current density was 22.4 mA/cm²."],
  ].map(([metric_name, value, unit, validation_status, source_fragment], index) => ({
    publication_id: "DEMO", result_id: "R" + (index + 1), device_id: "D1", measurement_id: "E1",
    metric_name, raw_value: value, raw_unit: unit, normalized_value: value, normalized_unit: unit,
    extraction_status: "extracted", validation_status, source_fragment, source_page: 1,
  })),
  issues: [{publication_id: "DEMO", record_ids: "R3", check_name: "FF",
    description: "Коэффициент заполнения превышает 100%. Сверьте число и единицу с источником."}],
};

let rows = [];
let selected;
let demo = true;
let pdfs = new Map();

function validation(row) {
  if (row.issues.length || row.result.validation_status === "review") return "review";
  if (row.result.validation_status === "passed" && row.result.extraction_status === "extracted") return "passed";
  return "pending";
}

function node(tag, text, className) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}

function value(record, prefix) {
  return [record[prefix + "_value"], record[prefix + "_unit"]]
    .filter(part => part != null && part !== "").join(" ") || "—";
}

function detail() {
  ui.detail.replaceChildren();
  if (!selected) {
    ui.detail.textContent = "Выберите результат в таблице.";
    return;
  }
  const { result, source, device, material, measurement, publication, issues, layers } = selected;
  const status = validation(selected);
  ui.detail.append(node("p", result.metric_name + " · " + value(result, "normalized"), "value"),
    node("p", labels[status], "status " + status));
  const fields = document.createElement("dl");
  for (const [label, text] of [
    ["Публикация", publication?.title || result.publication_id],
    ["Исходное значение", value(result, "raw")],
    ["Прибор", [device?.device_label, device?.architecture].filter(Boolean).join(" · ")],
    ["Материал", material?.material_name_raw],
    ["Измерение", [measurement?.spectrum, measurement?.scan_direction].filter(Boolean).join(" · ")],
    ["Слои", layers.map(layer => layer.layer_material).filter(Boolean).join(" / ")],
  ]) fields.append(node("dt", label), node("dd", text || "—"));
  ui.detail.append(fields);
  const location = [source.page ? "стр. " + source.page : "", source.section, source.table, source.figure]
    .filter(Boolean).join(" · ");
  if (location) ui.detail.append(node("p", location));
  if (source.quote) ui.detail.append(node("blockquote", source.quote));
  const pdf = pdfs.get(result.publication_id);
  if (pdf && source.page && /^\d+$/.test(String(source.page))) {
    const link = node("a", "Открыть страницу PDF", "button");
    link.href = pdf + "#page=" + source.page;
    link.target = "_blank";
    link.rel = "noopener";
    ui.detail.append(link);
  }
  for (const issue of issues) ui.detail.append(node("p", issue.description || issue.check_name));
  if (status === "pending") ui.detail.append(node("p", "Результат проверки ещё не поступил."));
}

function render() {
  const filtered = filterSolarCatalog(rows, {query: ui.query.value, metric: ui.metric.value})
    .filter(row => !ui.status.value || validation(row) === ui.status.value);
  if (!filtered.includes(selected)) selected = filtered[0];
  ui.results.replaceChildren();
  for (const row of filtered) {
    const tr = ui.results.insertRow();
    tr.classList.toggle("selected", row === selected);
    const button = node("button", row.result.metric_name || "Показатель", "row-button");
    button.setAttribute("aria-pressed", String(row === selected));
    button.onclick = () => {
      selected = row;
      for (const other of ui.results.querySelectorAll("button")) {
        other.setAttribute("aria-pressed", String(other === button));
        other.closest("tr").classList.toggle("selected", other === button);
      }
      detail();
    };
    tr.insertCell().append(button);
    tr.insertCell().textContent = value(row.result, "normalized");
    tr.insertCell().textContent = row.device?.device_label || "—";
    const status = validation(row);
    tr.insertCell().append(node("span", labels[status], "status " + status));
  }
  ui.count.textContent = filtered.length ? "Показано " + filtered.length + " из " + rows.length : "Результаты не найдены";
  ui.sourcePdf.disabled = demo || !selected;
  detail();
}

function load(tablesData, isDemo) {
  if (!tablesData || !tables.every(key => Array.isArray(tablesData[key])
      && tablesData[key].every(record => record && typeof record === "object" && !Array.isArray(record)))) {
    throw new Error("В JSON нужны массивы publications, materials, devices, layers, measurements, results и issues.");
  }
  rows = solarCatalog(tablesData);
  demo = isDemo;
  for (const url of pdfs.values()) URL.revokeObjectURL(url);
  pdfs = new Map();
  selected = null;
  const metrics = [...new Set(rows.map(row => row.result.metric_name).filter(Boolean))].sort();
  ui.metric.replaceChildren(new Option("Все", ""), ...metrics.map(metric => new Option(metric, metric)));
  ui.status.value = "";
  ui.query.value = "";
  const passed = rows.filter(row => validation(row) === "passed").length;
  ui.total.textContent = rows.length;
  ui.passed.textContent = passed;
  ui.review.textContent = rows.filter(row => validation(row) === "review").length;
  ui.export.disabled = demo || !passed;
  ui.dataset.textContent = demo ? "Синтетический пример" : "Импортированный паспорт";
  ui.notice.textContent = demo ? "Демонстрационные значения. Импортируйте паспорт, чтобы работать со своими данными." : "Паспорт загружен. CSV включает только прошедшие проверку записи.";
  render();
}

function csvCell(value) {
  let text = String(value ?? "");
  if (/^[\s]*[=+\-@]/.test(text) && !/^[+-]?\d+(?:[.,]\d+)?(?:e[+-]?\d+)?$/i.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

ui.passport.onchange = async () => {
  const file = ui.passport.files[0];
  if (!file) return;
  try {
    load(JSON.parse(await file.text()), false);
  } catch (error) {
    ui.notice.textContent = "Не удалось загрузить паспорт. " + error.message;
  }
  ui.passport.value = "";
};
ui.sourcePdf.onchange = () => {
  const file = ui.sourcePdf.files[0];
  if (!file || !selected) return;
  const id = selected.result.publication_id;
  if (pdfs.has(id)) URL.revokeObjectURL(pdfs.get(id));
  pdfs.set(id, URL.createObjectURL(file));
  ui.sourcePdf.value = "";
  ui.notice.textContent = "PDF привязан к публикации " + id + ".";
  detail();
};
ui.export.onclick = () => {
  if (demo) return;
  const accepted = rows.filter(row => validation(row) === "passed");
  if (!accepted.length) return;
  const lines = [columns.join(","), ...accepted.map(({result}) =>
    columns.map(column => csvCell(result[column])).join(","))];
  const url = URL.createObjectURL(new Blob([lines.join("\r\n") + "\r\n"], {type: "text/csv;charset=utf-8"}));
  const link = node("a", "");
  link.href = url;
  link.download = "results.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
ui.query.oninput = render;
ui.metric.onchange = render;
ui.status.onchange = render;
load(sample, true);
