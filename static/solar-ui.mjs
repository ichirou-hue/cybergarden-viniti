import { solarCatalog } from "/solar.mjs";

const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(el => [el.id, el]));
const tables = ["publications", "materials", "devices", "layers", "measurements", "results", "issues"];
const columns = ("publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit "
  + "aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit "
  + "result_origin cited_publication_id source_page source_section source_table source_figure source_fragment "
  + "extraction_status duplicate_of note").split(" ");
const labels = {passed: "Пройдена", review: "На разборе", pending: "Ожидает"};

const sample = {
  topic: "Демонстрационный пример",
  title: "Оценка воспроизводимости измерений",
  results: [
    {result_id: "R1", metric_name: "Среднее время", raw_value: "1200", raw_unit: "мс",
      normalized_value: "1.2", normalized_unit: "с", context: "Эксперимент A",
      extraction_status: "extracted", validation_status: "passed",
      source_page: 2, source_fragment: "Mean elapsed time was 1200 ms."},
    {result_id: "R2", metric_name: "Число наблюдений", raw_value: "-5",
      context: "Эксперимент B", extraction_status: "extracted",
      source_page: 3, source_fragment: "The number of observations was -5.",
      issues: ["Число наблюдений не может быть отрицательным. Сверьте запись с источником."]},
  ],
};

function issueList(issues) {
  if (!Array.isArray(issues)) return [];
  return issues.map(issue => typeof issue === "string" ? {description: issue} : issue)
    .filter(issue => issue && typeof issue === "object");
}

function passportRows(data) {
  if (tables.every(key => Array.isArray(data[key]))) {
    return solarCatalog(data).map(row => ({
      ...row, context: row.device?.device_label, fields: {
        "Прибор": [row.device?.device_label, row.device?.architecture].filter(Boolean).join(" · "),
        "Материал": row.material?.material_name_raw,
        "Измерение": [row.measurement?.spectrum, row.measurement?.scan_direction].filter(Boolean).join(" · "),
        "Слои": row.layers.map(layer => layer.layer_material).filter(Boolean).join(" / "),
      },
    }));
  }
  const entries = data.results ?? data.facts;
  if (!Array.isArray(entries) || entries.some(row => !row || typeof row !== "object" || Array.isArray(row))) {
    throw new Error("В паспорте нужен массив results или facts.");
  }
  return entries.map(entry => {
    const id = entry.publication_id || data.publication_id || data.id || data.title || "article";
    const rag = entries === data.facts;
    const result = {
      ...entry, publication_id: id, result_id: entry.result_id ?? entry.id,
      metric_name: entry.metric_name ?? [entry.metric, entry.cutoff ? "@" + entry.cutoff : ""].join(""),
      normalized_value: entry.normalized_value ?? (rag && entry.unit !== "unspecified" ? entry.value : undefined),
      normalized_unit: entry.normalized_unit ?? (rag && entry.unit !== "unspecified" ? entry.unit : undefined),
      extraction_status: entry.extraction_status ?? (rag ? "extracted" : undefined),
      source_page: entry.source_page ?? entry.evidence?.page,
      source_fragment: entry.source_fragment ?? entry.evidence?.quote,
    };
    return {
      result, context: entry.context, fields: entry.fields ?? {},
      publication: {title: data.title, topic: data.topic ?? data.track},
      source: {page: result.source_page, quote: result.source_fragment,
        section: entry.source_section, table: entry.source_table, figure: entry.source_figure},
      issues: issueList(entry.issues),
    };
  });
}

let rows = [];
let selected;
let demo = true;
let pdfs = new Map();
let exportColumns = columns;

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
  const { result, source, publication, issues, fields } = selected;
  const status = validation(selected);
  ui.detail.append(node("p", result.metric_name + " · " + value(result, "normalized"), "value"),
    node("p", labels[status], "status " + status));
  const description = document.createElement("dl");
  for (const [label, text] of [
    ["Публикация", publication?.title || result.publication_id],
    ["Исходное значение", value(result, "raw")],
    ["Тема", publication?.topic],
    ["Контекст", selected.context],
    ...Object.entries(fields).filter(([, field]) => ["string", "number", "boolean"].includes(typeof field)),
  ]) description.append(node("dt", label), node("dd", text == null || text === "" ? "—" : text));
  ui.detail.append(description);
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
  const terms = ui.query.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const filtered = rows.filter(row => {
    if (ui.metric.value && row.result.metric_name !== ui.metric.value) return false;
    if (ui.status.value && validation(row) !== ui.status.value) return false;
    const text = [row.publication?.title, row.publication?.topic, row.context,
      row.result.metric_name, row.result.raw_value, row.result.normalized_value,
      row.source.quote, ...Object.values(row.fields)].join(" ").toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  });
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
    tr.insertCell().textContent = row.context || "—";
    const status = validation(row);
    tr.insertCell().append(node("span", labels[status], "status " + status));
  }
  ui.count.textContent = filtered.length ? "Показано " + filtered.length + " из " + rows.length : "Результаты не найдены";
  ui.sourcePdf.disabled = demo || !selected;
  detail();
}

function load(tablesData, isDemo) {
  if (!tablesData || typeof tablesData !== "object") throw new Error("Ожидается объект JSON.");
  rows = passportRows(tablesData);
  exportColumns = tables.every(key => Array.isArray(tablesData[key]))
    ? columns : [...columns, "context", "fields"];
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
  ui.dataset.textContent = tablesData.topic || tablesData.title || "Импортированный паспорт";
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
  const lines = [exportColumns.join(","), ...accepted.map(({result, context, fields}) => {
    const record = {...result, context, fields: JSON.stringify(fields)};
    return exportColumns.map(column => csvCell(record[column])).join(",");
  })];
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
