import { el, fill } from "./dom.js";
import { getJSON } from "./api.js";

const ACTIVE = new Set(["queued", "running"]);
const STATUS = {
  queued: ["В очереди", "idle"], running: ["Выполняется", "warn"],
  ok: ["Готово", "ok"], error: ["Ошибка", "bad"],
  rejected: ["Отклонено", "bad"], skipped: ["Пропущено", "idle"],
};
// Статус задания описывает обработку файла, а не качество научного результата.
// Поэтому у завершённого задания своя подпись без зелёного «Готово».
const JOB_STATUS = {
  queued: ["В очереди", "idle"], running: ["Выполняется", "warn"],
  ok: ["Разбор завершён", "idle"], error: ["Ошибка", "bad"],
};
// Научные таблицы шаблона solar. Публикации и замечания - не научные записи.
const SCIENTIFIC_TABLES = ["materials", "devices", "layers", "measurements", "results"];
// Семантический слой (llm/semantic.json) - отдельное описание статьи моделью.
// Он не входит в научные таблицы и официальные CSV, поэтому статус говорит о
// сборке наблюдений, а не о проверке научной связи. `ok` здесь нейтральный.
const SEMANTIC_STATUS = {
  ok: ["Собран", "idle"], partial: ["Собран частично", "warn"],
  missing: ["Не сформирован", "idle"], rejected: ["Отклонён", "bad"],
};
const SEMANTIC_KINDS = ["layers", "conditions", "metrics"];
const SEMANTIC_KIND_TITLES = {layers: "Слои", conditions: "Условия", metrics: "Метрики"};
const SEMANTIC_FIELDS = {
  layers: ["name", "role", "material", "thickness", "thickness_unit"],
  conditions: ["name", "value", "unit"], metrics: ["name", "value", "unit"],
};
const SEMANTIC_FIELD_TITLES = {
  name: "Название", role: "Роль", material: "Материал",
  thickness: "Толщина", thickness_unit: "Единица толщины",
  value: "Значение", unit: "Единица",
};
const SEMANTIC_CLAIMS = [
  ["study_type", "Тип исследования"],
  ["data_provenance", "Происхождение данных"],
  ["thematic_relevance", "Тематика"],
];
// unknown и отсутствие значения не превращаются в false: это честная
// неопределённость, а не решение «не подходит».
const SEMANTIC_VALUES = {
  study_type: {experimental: "Эксперимент", simulation: "Расчёт", mixed: "Смешанное",
    review: "Обзор", unknown: "Не определено · требует проверки"},
  data_provenance: {measured: "Измерено", simulated: "Смоделировано",
    literature: "Из литературы", unknown: "Не определено · требует проверки"},
  thematic_relevance: {relevant: "По теме", partially_relevant: "Частично по теме",
    not_relevant: "Не по теме", unknown: "Не определено · требует проверки"},
};
const SEMANTIC_ROW_LIMIT = 100;
// Инвентарь привязки (attribution) - чувствительные поля процесса и размера
// выборки. Это предупреждение о ручной проверке, а не приговор строкам.
const ATTRIBUTION_FIELD_LIMIT = 12;
const ATTRIBUTION_FIELD_TITLES = {
  annealing_temperature: "Температура отжига", annealing_time: "Время отжига",
  atmosphere: "Атмосфера", deposition_method: "Метод осаждения",
  n_devices: "Число приборов", spread_value: "Разброс", aggregation: "Агрегация",
};
const STAGES = {
  sources: "Источники", interpret: "Интерпретация моделью",
  passport: "Сборка результата", audit: "Проверка цитат", export: "Экспорт",
};
const LABELS = {
  "result.json": "Результат JSON", "parser.json": "Отчёт парсера", "candidates.csv": "Кандидаты CSV",
  "sources.json": "Источники LLM", "request.json": "Запрос LLM", "interpretation.json": "Интерпретация",
  "audit.json": "Аудит", "manifest.json": "Манифест LLM",
  "response.json": "Исходный ответ модели", "draft.json": "Построчный черновик с причинами отказа",
};
// Таблицы и поля приходят из шаблона в машинном виде. Пользователю показываем
// подписи, а неизвестные имена оставляем как есть, чтобы ничего не потерять.
const TABLE_TITLES = {
  publications: "Публикации", materials: "Материалы", devices: "Приборы", layers: "Слои",
  measurements: "Измерения", results: "Результаты", issues: "Замечания",
};
const COLUMN_TITLES = {
  publication_id: "Публикация", result_id: "Запись", device_id: "Прибор",
  measurement_id: "Измерение", material_id: "Материал", layer_id: "Слой",
  metric_name: "Показатель", metric_value: "Значение", metric_unit: "Единица",
  metric_group: "Группа", aggregation: "Агрегация", spread_value: "Разброс",
  time_point: "Момент", time_unit: "Единица времени", raw_value: "Исходное значение",
  raw_unit: "Исходная единица", normalized_value: "Значение после пересчёта",
  normalized_unit: "Единица после пересчёта", result_origin: "Происхождение",
  extraction_status: "Извлечение", validation_status: "Проверка", duplicate_of: "Повтор",
  source_page: "Страница", source_section: "Раздел", source_table: "Таблица",
  source_figure: "Рисунок", source_fragment: "Цитата", note: "Примечание",
  issue_id: "Замечание", check_name: "Проверка", description: "Описание",
  severity: "Важность", status: "Статус", title: "Название", year: "Год",
  authors: "Авторы", doi: "DOI", topic: "Тема",
  in_scope: "Область", exclusion_reason: "Причина исключения",
};

function badge(status) {
  const [label, tone] = STATUS[status] || [status || "Неизвестно", "idle"];
  return el("span", {class: `badge ${tone}`, text: label});
}

function jobBadge(status) {
  const [label, tone] = JOB_STATUS[status] || STATUS[status] || [status || "Неизвестно", "idle"];
  return el("span", {class: `badge ${tone}`, text: label});
}

function tableRows(tables, name) {
  const rows = tables?.[name];
  return Array.isArray(rows) ? rows.filter(row => row && typeof row === "object" && !Array.isArray(row)) : [];
}

// У solar научные таблицы фиксированы шаблоном. У профилей с произвольным
// набором таблиц научными считаем все, кроме публикаций и замечаний.
function scientificNames(tables) {
  const names = Object.keys(tables || {});
  const solar = SCIENTIFIC_TABLES.filter(name => names.includes(name));
  if (solar.length) return solar;
  return names.filter(name => name !== "publications" && name !== "issues");
}

function scientificRowCount(tables) {
  return scientificNames(tables).reduce((sum, name) => sum + tableRows(tables, name).length, 0);
}

// in_scope приходит строками "true"/"false" и решает, собирались ли научные строки.
// Область считается известной только по явному строковому значению: boolean, число,
// пустое или незнакомое поле - это неизвестность, а не «в области». String(true)
// и String(1) иначе молча превращались бы в решение, которого в данных нет.
function scopeValue(value) {
  if (typeof value !== "string") return "";
  const flag = value.trim().toLowerCase();
  return flag === "true" || flag === "false" ? flag : "";
}

function scopeFlag(row) {
  return scopeValue(row?.in_scope);
}

function scopeState(tables) {
  const rows = tableRows(tables, "publications");
  if (!rows.length) return {known: false, total: 0, inside: [], outside: [], unknown: [], reasons: []};
  const inside = rows.filter(row => scopeFlag(row) === "true");
  const outside = rows.filter(row => scopeFlag(row) === "false");
  const unknown = rows.filter(row => !scopeFlag(row));
  const reasons = [...new Set(outside.map(row => String(row.exclusion_reason ?? "").trim()).filter(Boolean))];
  return {
    known: inside.length + outside.length > 0,
    total: rows.length, inside, outside, unknown, reasons,
  };
}

// Подпись области не выводит «В области» из неполных данных: так можно только
// когда каждая публикация явно помечена true. Если часть строк вне области, а у
// части область не указана, подпись говорит об обоих фактах, а не только о false.
function scopeLabel(scope) {
  if (!scope.known) return "Не определена";
  if (scope.outside.length === scope.total) return "Вне области";
  if (scope.outside.length && scope.unknown.length) return "Часть вне области, определена не полностью";
  if (scope.outside.length) return "Часть вне области";
  if (scope.unknown.length) return "Определена не полностью";
  return "В области";
}

function candidateStats(result, job) {
  const list = Array.isArray(result?.candidates)
    ? result.candidates.filter(item => item && typeof item === "object") : null;
  if (!list) {
    const count = Number(job.summary?.candidates);
    const safe = Number.isFinite(count) && count > 0 ? count : 0;
    return {count: safe, unresolved: safe, known: false};
  }
  return {
    count: list.length,
    unresolved: list.filter(item => item.status === "candidate" || item.context_status === "unresolved").length,
    known: true,
  };
}

function coverageState(result) {
  const coverage = result?.coverage;
  if (!coverage || typeof coverage !== "object") return null;
  const completeness = coverage.completeness === "not_measured"
    ? "не измерена" : (coverage.completeness || "не указана");
  return {completeness, reason: typeof coverage.reason === "string" ? coverage.reason.trim() : ""};
}

function plural(count, one, few, many) {
  const mod100 = count % 100, mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

function seconds(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 1})} с` : "Нет данных";
}

function dateTime(value) {
  if (!value) return "Нет данных";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? "Нет данных" : date.toLocaleString("ru-RU");
}

function wait(signal, delay = 1500) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(done, delay);
    function done() { signal?.removeEventListener("abort", abort); resolve(); }
    function abort() {
      clearTimeout(timer);
      const error = new Error("Запрос отменён"); error.name = "AbortError"; reject(error);
    }
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, {once: true});
  });
}

function artifactLink(id, path, label) {
  return el("a", {class: "parse-artifact", href: `/api/parse/${encodeURIComponent(id)}/file/${path}`, target: "_blank", rel: "noopener"},
    el("span", {text: label}), el("small", {text: path.split("/").at(-1)}));
}

function stageList(manifest, job) {
  let rows = Array.isArray(manifest?.stages) ? manifest.stages : null;
  if (!rows && job.summary?.llm?.stages) rows = Object.entries(job.summary.llm.stages).map(([name, status]) => ({name, status}));
  if (!rows?.length) return el("p", {class: "muted", text: "Стадии появятся после обработки документа."});
  return el("div", {class: "parse-stages"}, rows.map(stage => el("div", {class: "parse-stage"},
    el("span", {text: STAGES[stage.name] || stage.name}), badge(stage.status),
    stage.error ? el("p", {class: "parse-stage-note error-hint", text: stage.error}) :
      stage.reason ? el("p", {class: "parse-stage-note muted", text: stage.reason}) : null)));
}

// Манифест может прийти с незаполненным counts (пустой объект), хотя задание уже
// знает сводку из summary.llm. Пустой объект не должен затирать её и давать «-».
function countMap(value) {
  return value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length ? value : null;
}

function auditCounts(manifest, job) {
  const counts = countMap(manifest?.counts) || countMap(job?.summary?.llm?.counts);
  if (!counts) return el("p", {class: "muted", text: "Сводка проверки пока недоступна."});
  return el("div", {class: "parse-counts"}, [
    ["Записей интерпретировано", counts.interpreted_records],
    ["Замечаний структуры", counts.validation_issues],
    ["Находок проверки", counts.audit_findings],
  ].map(([label, value]) => el("div", {}, el("strong", {text: Number.isFinite(Number(value)) ? String(value) : "-"}), el("span", {text: label}))));
}

// Ссылки на технические файлы берутся из artifacts манифеста. Запись со значением
// false/null - явное «файла нет». manifest.json не перечисляет сам себя, но он есть
// ровно тогда, когда манифест загрузился; без загруженного манифеста ссылку не
// выдумываем, иначе она вела бы в 404.
function artifactNames(manifest) {
  const artifacts = manifest?.artifacts;
  const known = artifacts && typeof artifacts === "object" && !Array.isArray(artifacts) ? artifacts : null;
  const present = name => !known || !Object.hasOwn(known, name) || Boolean(known[name]);
  const names = known ? Object.keys(known).filter(present) : [];
  if (manifest && present("manifest.json") && !names.includes("manifest.json")) names.push("manifest.json");
  return names;
}

function artifacts(job, manifest) {
  if (job.status !== "ok") return el("p", {class: "muted", text: "Файлы появятся после завершения разбора."});
  const main = [
    artifactLink(job.id, "result.json", LABELS["result.json"]),
    artifactLink(job.id, "candidates.csv", LABELS["candidates.csv"]),
  ];
  if (manifest?.artifacts?.["parser.json"] || job.parser === "docling")
    main.push(artifactLink(job.id, "parser.json", LABELS["parser.json"]));
  const technical = [];
  if (job.llm !== "off") {
    for (const name of artifactNames(manifest))
      technical.push(artifactLink(job.id, `llm/${name}`, LABELS[name] || name));
  }
  return el("div", {},
    el("div", {class: "parse-artifacts"}, main),
    technical.length ? el("details", {class: "parse-technical"},
      el("summary", {text: "Технические файлы модели"}),
      el("div", {class: "parse-artifacts"}, technical)) : null);
}

function printable(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") {
    try { return JSON.stringify(value); } catch { return String(value); }
  }
  return String(value);
}

function cellText(column, value) {
  if (column === "in_scope") {
    const flag = scopeValue(value);
    if (flag === "true") return "В области";
    if (flag === "false") return "Вне области Solar";
    return "Не указана";
  }
  return printable(value);
}

function emptyTableText(name, scientific) {
  if (scientific.has(name))
    return "Научных записей нет. Подтверждённых строк для этой таблицы в документе не нашлось.";
  return "Записей нет.";
}

function interpretationTables(interpretation) {
  const tables = interpretation?.tables;
  if (!tables || typeof tables !== "object" || Array.isArray(tables)) return null;
  const scientific = new Set(scientificNames(tables));
  const sections = [];
  for (const [name, rawRows] of Object.entries(tables)) {
    const rows = Array.isArray(rawRows) ? rawRows.filter(row => row && typeof row === "object" && !Array.isArray(row)) : [];
    const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
    const shown = rows.slice(0, 100);
    sections.push(el("section", {class: "interpretation-table"},
      el("div", {class: "interpretation-table-head"},
        el("h3", {text: TABLE_TITLES[name] || name}),
        el("span", {text: `${rows.length} ${plural(rows.length, "запись", "записи", "записей")}`})),
      rows.length && columns.length ? el("div", {class: "table-wrap"}, el("table", {class: "compact"},
        el("thead", {}, el("tr", {}, columns.map(column => el("th", {text: COLUMN_TITLES[column] || column,
          title: COLUMN_TITLES[column] ? column : null})))),
        el("tbody", {}, shown.map(row => el("tr", {}, columns.map(column => el("td", {text: cellText(column, row[column])}))))))) :
        el("p", {class: "muted", text: emptyTableText(name, scientific)}),
      rows.length > shown.length ? el("p", {class: "muted interpretation-limit", text: `Показаны первые ${shown.length} записей. Полный результат доступен в interpretation.json.`}) : null));
  }
  return sections.length ? el("div", {class: "interpretation-tables"}, sections) : null;
}

function scopeNotice(job, scope) {
  if (!scope.known) return null;
  const area = areaLabel(job.profile);
  const unknownNote = scope.unknown.length
    ? `Для ${scope.unknown.length} ${plural(scope.unknown.length, "публикации", "публикаций", "публикаций")} область в данных не указана, поэтому она не считается подтверждённой.`
    : "";
  if (scope.outside.length) {
    const all = scope.outside.length === scope.total;
    return el("div", {class: "notice warn", role: "status"},
      el("strong", {text: all ? `Вне области ${area}` : `Часть публикаций вне области ${area}`}),
      el("p", {text: scope.reasons.length
        ? scope.reasons.join(" ")
        : "Причина исключения не указана в извлечённых данных."}),
      unknownNote ? el("p", {class: "muted", text: unknownNote}) : null);
  }
  if (!scope.unknown.length) return null;
  return el("div", {class: "notice", role: "status"},
    el("strong", {text: `Область ${area} определена не полностью`}),
    el("p", {class: "muted", text: unknownNote}));
}

function emptyScientificNotice(job, interpretation) {
  const tables = interpretation?.tables;
  if (!tables || typeof tables !== "object" || Array.isArray(tables)) return null;
  if (scientificRowCount(tables) > 0) return null;
  const scope = scopeState(tables);
  const outside = scope.known && scope.outside.length === scope.total;
  const text = outside && job.profile === "solar"
    ? "Документ вне области Solar: научные строки по нему не собирались."
    : "В строгой интерпретации нет строк научных таблиц. Это не значит, что в документе нет данных: полнота не измерена, часть сведений может быть в прозе или на рисунках.";
  return el("div", {class: "notice", role: "status"},
    el("strong", {text: "Научные записи отсутствуют"}), el("p", {text}));
}

// Инвентарь привязки: чувствительные поля процесса и размера выборки. Это
// предупреждение о ручной проверке, а не приговор строкам. Строки не
// отклоняются и не меняются, поэтому "пусто" не значит "наука проверена".
function attributionState(job, manifest, interpretation) {
  const solar = manifest?.solar && typeof manifest.solar === "object" && !Array.isArray(manifest.solar)
    ? manifest.solar
    : (job.summary?.llm?.solar && typeof job.summary.llm.solar === "object"
      ? job.summary.llm.solar : null);
  const attribution = interpretation?.attribution && typeof interpretation.attribution === "object"
    && !Array.isArray(interpretation.attribution) ? interpretation.attribution : null;
  const number = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : 0;
  const flags = {
    binding: typeof solar?.scientific_binding_verification === "string"
      ? solar.scientific_binding_verification : "",
    completeness: typeof solar?.scientific_completeness_verification === "string"
      ? solar.scientific_completeness_verification : "",
  };
  if (attribution) {
    const counts = attribution.counts && typeof attribution.counts === "object"
      && !Array.isArray(attribution.counts) ? attribution.counts : {};
    const byField = counts.by_field && typeof counts.by_field === "object"
      && !Array.isArray(counts.by_field) ? counts.by_field : {};
    const fields = Object.entries(byField)
      .map(([name, count]) => ({name, count: number(count)}))
      .filter(item => item.count > 0)
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
    const totalRows = number(counts.rows);
    const totalFields = number(counts.fields);
    return {
      known: true, source: "file", ...flags,
      state: typeof attribution.state === "string" ? attribution.state : "unverified",
      requiresReview: attribution.requires_review === true,
      truncated: attribution.truncated === true, fields, totalRows, totalFields,
      // requires_review всегда true, поэтому предупреждение решает наличие
      // чувствительных полей, а не сам флаг.
      hasSensitive: totalFields > 0 || totalRows > 0 || fields.length > 0,
    };
  }
  if (solar && typeof solar.attribution_state === "string") {
    const counts = solar.attribution_counts && typeof solar.attribution_counts === "object"
      && !Array.isArray(solar.attribution_counts) ? solar.attribution_counts : {};
    const totalRows = number(counts.rows);
    const totalFields = number(counts.fields);
    return {
      known: true, source: "manifest", ...flags,
      state: solar.attribution_state, requiresReview: solar.requires_review === true,
      truncated: false, fields: [], totalRows, totalFields,
      hasSensitive: totalFields > 0 || totalRows > 0,
    };
  }
  // Старый разбор без метаданных: привязка неизвестна, а не подтверждена.
  return {
    known: false, source: "legacy", ...flags, state: "unknown",
    requiresReview: false, truncated: false, fields: [], totalRows: 0, totalFields: 0,
    hasSensitive: false,
  };
}

function attributionFieldLine(item) {
  const dot = item.name.indexOf(".");
  const table = dot > 0 ? item.name.slice(0, dot) : "";
  const field = dot > 0 ? item.name.slice(dot + 1) : item.name;
  const label = ATTRIBUTION_FIELD_TITLES[field] || field || item.name;
  const prefix = table && TABLE_TITLES[table] ? `${TABLE_TITLES[table]}: ` : "";
  return el("li", {text: `${prefix}${label} - ${item.count} ${plural(item.count, "строка", "строки", "строк")}`});
}

function attributionNotice(attribution) {
  if (!attribution?.known) return el("div", {class: "notice", role: "status"},
    el("strong", {text: "Сведения о привязке отсутствуют"}),
    el("p", {text: "Этот разбор создан без инвентаря привязки. Научные связи условий и размера выборки не подтверждены; отсутствие сведений не означает проверку."}));
  // requires_review в контракте всегда true, поэтому решает наличие
  // чувствительных полей. Пустой инвентарь не значит ни "наука проверена",
  // ни "проверка не нужна", и предупреждения о несуществующих полях нет.
  if (!attribution.hasSensitive) return el("div", {class: "notice", role: "status"},
    el("strong", {text: "Чувствительных полей привязки не найдено"}),
    el("p", {text: "Это только пустой инвентарь: научные связи и полнота не подтверждены, общая проверка результата остаётся за человеком."}));
  const shown = attribution.fields.slice(0, ATTRIBUTION_FIELD_LIMIT);
  const rows = attribution.totalRows;
  const fields = attribution.totalFields;
  const head = rows || fields
    ? `Требуют проверки привязки: ${rows} ${plural(rows, "строка", "строки", "строк")}, ${fields} ${plural(fields, "поле", "поля", "полей")}`
    : "Требуют проверки привязки";
  return el("div", {class: "notice warn", role: "status"},
    el("strong", {text: head}),
    el("p", {text: "Условия процесса и размер выборки нужно проверить вручную: автоматическая проверка научной привязки не выполнялась. Строки не отклонены и не изменены."}),
    shown.length ? el("ul", {}, shown.map(attributionFieldLine)) : null,
    shown.length && shown.length < attribution.fields.length
      ? el("p", {class: "muted", text: `Показаны первые ${shown.length} из ${attribution.fields.length} полей. Полный инвентарь в interpretation.json.`})
      : null,
    !shown.length && fields
      ? el("p", {class: "muted", text: "Список полей доступен в interpretation.json; в манифесте только счётчики."})
      : null);
}

function interpretationResult(job, manifest, interpretation, interpretationError) {
  if (ACTIVE.has(job.status)) return el("p", {class: "muted", text: "Результат появится после проверки ответа модели."});
  if (job.llm === "off") return el("div", {class: "interpretation-empty"},
    el("strong", {text: "LLM выключен"}),
    el("p", {class: "muted", text: "Интерпретация не запускалась: собраны источники, кандидаты и паспорт разбора. Научных записей в таком результате нет."}));
  const stage = (manifest?.stages || []).find(item => item.name === "interpret");
  if (interpretation?.status === "ok") {
    const tables = interpretationTables(interpretation);
    return el("div", {},
      el("div", {class: "interpretation-summary"}, el("span", {class: "badge idle", text: "Формальные проверки пройдены"}),
        el("span", {text: "Структура и цитаты прошли формальную проверку. Научные связи и полнота автоматически не подтверждены."})),
      attributionNotice(attributionState(job, manifest, interpretation)),
      scopeNotice(job, scopeState(interpretation.tables)),
      emptyScientificNotice(job, interpretation),
      tables || el("p", {class: "muted", text: "Интерпретация не содержит таблиц для отображения."}));
  }
  if (stage?.status === "skipped") return el("div", {class: "interpretation-empty"},
    el("strong", {text: "Интерпретация пропущена"}),
    el("p", {class: "muted", text: stage.reason || "Этот профиль не формирует интерпретацию с помощью выбранной модели."}));
  if (stage?.status === "rejected" || interpretation?.status === "rejected") {
    const draft = draftState(job, manifest);
    return el("div", {},
      draftNotice(job, draft, true),
      el("div", {class: "notice warn"},
        el("strong", {text: "Интерпретация не прошла проверку"}),
        el("p", {text: interpretation?.error || stage?.error || "Ответ модели отклонён проверкой структуры или источников."})));
  }
  return el("div", {class: "interpretation-empty"},
    el("strong", {text: "Интерпретация недоступна"}),
    el("p", {class: "muted", text: interpretationError || "Сервер не предоставил проверенный результат для отображения."}));
}

function areaLabel(value) {
  if (value === "solar") return "Solar";
  if (value === "rag") return "RAG";
  // Универсальный профиль не является областью каталога, поэтому в подписи
  // стоит его собственное название, а не латинский идентификатор.
  if (value === "universal") return "универсальный профиль";
  return value || "Не указана";
}

function parserLabel(value) {
  if (value === "docling") return "Docling";
  if (value === "pdfplumber") return "pdfplumber";
  return value || "Не указан";
}

function ocrLabel(value) {
  if (!value || value === "off") return "выключен";
  if (value === "auto") return "авто";
  return value;
}

function resultStatePanel(job, manifest, result, interpretation, interpretationError, resultError, candidates) {
  const coverage = coverageState(result);
  const tables = interpretation?.tables;
  const tablesKnown = Boolean(tables && typeof tables === "object" && !Array.isArray(tables));
  const scientific = tablesKnown ? scientificRowCount(tables) : 0;
  const draft = draftState(job, manifest);
  const scientificLabel = tablesKnown ? (scientific ? String(scientific) : "Нет")
    : ((interpretationError || resultError) && job.llm !== "off" ? "Нет данных" : "Нет");
  const scope = scopeState(tables);
  const cards = [
    // Строгие научные строки и принятые строки черновика - разные вещи.
    // Подпись говорит, что это именно строгие записи, а не вся добыча модели.
    [scientificLabel, draft.accepted ? "Научные записи (строгие)" : "Научные записи"],
    [candidates.count ? String(candidates.count) : "Нет", "Неподтверждённые кандидаты"],
    [scopeLabel(scope), "Область документа"],
    [coverage ? coverage.completeness : (resultError ? "Нет данных" : "не измерена"), "Полнота"],
  ];
  const notes = [
    coverage?.reason ? el("p", {class: "muted", text: coverage.reason}) : null,
    !interpretation && job.llm === "off"
      ? el("p", {class: "muted", text: "Интерпретация не запускалась: собраны источники, кандидаты и паспорт разбора."})
      : null,
    // Пустая или несобранная строгая таблица не означает, что модель ничего не
    // вернула: принятые строки черновика лежат в draft.json и не являются CSV.
    !draft.accepted || (tablesKnown && scientific)
      ? null
      : el("p", {class: "muted", text: `${tablesKnown ? "Строгих научных строк нет" : "Строгие научные строки не собраны"}, но построчный черновик сохранил ${draft.accepted} принятых и ${draft.quarantined} отклонённых строк. Это не официальные CSV и не подтверждённые научные записи; см. draft.json.`}),
    resultError ? el("p", {class: "parse-inline-error", text: resultError}) : null,
  ];
  return el("section", {class: "panel parse-panel parse-state"},
    el("div", {class: "panel-head"}, el("h2", {text: "Что получено"})),
    el("p", {class: "parse-state-lede", text: "Обработка файла завершена. Это технический статус разбора, а не подтверждение полноты и точности научных данных."}),
    el("div", {class: "parse-summary parse-state-grid"}, cards.map(([value, label]) => el("div", {},
      el("strong", {text: value}), el("span", {text: label})))),
    notes.filter(Boolean));
}

function candidateNotice(candidates) {
  if (!candidates.count) return null;
  const tail = "Кандидаты не входят в официальную выгрузку: candidates.csv - рабочий черновик, а не сдача.";
  if (!candidates.known) return el("div", {class: "notice warn", role: "status"},
    el("strong", {text: `Кандидаты не подтверждены: ${candidates.count}`}),
    el("p", {text: `Сервер сообщает число кандидатов, но result.json не загрузился, поэтому статусы записей не проверены. ${tail}`}));
  const detail = candidates.unresolved === candidates.count
    ? "У записей статус candidate и контекст unresolved: они не привязаны к прибору или условию."
    : `Из ${candidates.count} ${plural(candidates.count, "записи", "записей", "записей")} ${candidates.unresolved} имеют статус candidate и контекст unresolved.`;
  return el("div", {class: "notice warn", role: "status"},
    el("strong", {text: `Кандидаты не подтверждены: ${candidates.count}`}),
    el("p", {text: `${detail} ${tail}`}));
}

function semanticBadge(status) {
  const [label, tone] = SEMANTIC_STATUS[status] || ["Неизвестно", "idle"];
  return el("span", {class: `badge ${tone}`, text: label});
}

// Построчный черновик (llm/draft.json) - диагностический вид ответа модели.
// Манифест отдаёт только статус и счётчики; строки лежат в самом артефакте.
// Принятая строка черновика не считается подтверждённым научным результатом.
function draftState(job, manifest) {
  const solar = manifest?.solar && typeof manifest.solar === "object" && !Array.isArray(manifest.solar)
    ? manifest.solar
    : (job.summary?.llm?.solar && typeof job.summary.llm.solar === "object"
      ? job.summary.llm.solar : null);
  const counts = solar?.draft_counts && typeof solar.draft_counts === "object"
    && !Array.isArray(solar.draft_counts) ? solar.draft_counts : null;
  const status = typeof solar?.draft_status === "string" ? solar.draft_status : "";
  const number = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : 0;
  const known = Boolean(status) || Boolean(counts);
  return {
    known, status, accepted: number(counts?.accepted),
    quarantined: number(counts?.quarantined), rows: number(counts?.rows),
  };
}

function draftLink(job) {
  return artifactLink(job.id, "llm/draft.json", LABELS["draft.json"]);
}

// Если строгая интерпретация отклонена, нельзя писать «научных данных нет»:
// построчный черновик мог сохранить независимые строки. Показываем счётчики и
// ссылку на артефакт, но не называем принятые строки подтверждёнными.
function draftNotice(job, draft, rejected) {
  if (!draft?.known || !draft.accepted) return null;
  const counts = `${draft.accepted} принятых и ${draft.quarantined} отклонённых строк`;
  if (rejected) return el("div", {class: "notice warn", role: "status"},
    el("strong", {text: "Строгая интерпретация отклонена"}),
    el("p", {text: `Построчный черновик сохранил ${counts}. Не официальные CSV.`}),
    el("p", {class: "muted", text: "Это диагностический черновик с причинами отказа, а не подтверждённые научные записи. Независимые строки сохранены в draft.json."}),
    el("div", {class: "parse-artifacts"}, draftLink(job)));
  return el("div", {class: "notice", role: "status"},
    el("strong", {text: "Построчный черновик"}),
    el("p", {text: `Черновик сохранил ${counts}. Не официальные CSV; это не подтверждённые научные записи.`}),
    el("div", {class: "parse-artifacts"}, draftLink(job)));
}

// Постоянные подписи: слой всегда остаётся наблюдением модели, а не выгрузкой.
function semanticPermanentBadges() {
  return [
    el("span", {class: "badge idle", text: "semantic_only"}),
    el("span", {class: "badge idle", text: "Не для официальных CSV"}),
  ];
}

function semanticClaimText(field, value) {
  if (value === null || value === undefined || value === "") return "Нет данных";
  const known = SEMANTIC_VALUES[field] || {};
  return known[value] || "Не определено · требует проверки";
}

// Ссылка считается доказательством поля, только если у неё есть непустая
// цитата. Одна цитата на строку не подставляется: каждое поле берёт свою.
function semanticEvidence(anchor) {
  if (!anchor || typeof anchor !== "object" || Array.isArray(anchor)) return null;
  const quote = typeof anchor.quote === "string" ? anchor.quote.trim() : "";
  if (!quote) return null;
  const page = Number.isFinite(Number(anchor.page)) ? Number(anchor.page) : null;
  return {page, quote};
}

function semanticEvidenceLine(anchor) {
  const evidence = semanticEvidence(anchor);
  if (!evidence) return el("span", {class: "muted", text: "Нет подтверждающей цитаты"});
  return el("span", {class: "muted", text: `стр. ${evidence.page ?? "?"}: «${evidence.quote}»`});
}

function semanticFieldCell(record, field) {
  const raw = record?.[field];
  const value = typeof raw === "string" ? raw.trim() : "";
  if (!value) return el("td", {class: "muted", text: "-"});
  const sources = record?.field_sources && typeof record.field_sources === "object"
    && !Array.isArray(record.field_sources) ? record.field_sources : {};
  return el("td", {}, el("span", {text: value}), el("br"), semanticEvidenceLine(sources[field]));
}

function semanticRecordTable(kind, rows) {
  const fields = SEMANTIC_FIELDS[kind] || [];
  const shown = rows.slice(0, SEMANTIC_ROW_LIMIT);
  return el("section", {class: "interpretation-table"},
    el("div", {class: "interpretation-table-head"},
      el("h3", {text: SEMANTIC_KIND_TITLES[kind] || kind}),
      el("span", {text: `${rows.length} ${plural(rows.length, "запись", "записи", "записей")}`})),
    shown.length ? el("div", {class: "table-wrap"}, el("table", {class: "compact"},
      el("thead", {}, el("tr", {},
        el("th", {text: "Стр."}),
        fields.map(field => el("th", {text: SEMANTIC_FIELD_TITLES[field] || field, title: field})))),
      el("tbody", {}, shown.map(record => el("tr", {},
        el("td", {text: printable(record?.page)}),
        fields.map(field => semanticFieldCell(record, field)))))))
      : el("p", {class: "muted", text: "Принятых записей этого вида нет."}),
    rows.length > shown.length
      ? el("p", {class: "muted interpretation-limit",
        text: `Показаны первые ${shown.length} из ${rows.length}. Полный набор в semantic.json.`})
      : null);
}

// Принятые записи считаются по semantic.records, а `errors` - это замечания
// проверки, а не число отклонённых записей: одна запись может дать несколько
// замечаний, а часть замечаний относится к признакам статьи.
function semanticCountState(semantic, solar) {
  const records = semantic?.records && typeof semantic.records === "object"
    && !Array.isArray(semantic.records) ? semantic.records : null;
  const counts = solar?.semantic_counts && typeof solar.semantic_counts === "object"
    && !Array.isArray(solar.semantic_counts) ? solar.semantic_counts : null;
  const accepted = kind => {
    if (records && Array.isArray(records[kind])) return records[kind].length;
    const value = Number(counts?.[kind]);
    return Number.isFinite(value) && value >= 0 ? value : null;
  };
  let errors = null;
  if (Array.isArray(semantic?.errors)) errors = semantic.errors.length;
  else if (Number.isFinite(Number(counts?.errors))) errors = Number(counts.errors);
  return {layers: accepted("layers"), conditions: accepted("conditions"),
    metrics: accepted("metrics"), errors};
}

// Статус берём из файла, а при его отсутствии - из манифеста. Если нет ни
// файла, ни блока solar, это старый разбор без слоя: показываем пометку, а не
// ошибку. Так legacy-манифесты не выглядят сломанными.
function semanticState(job, manifest, semantic, semanticError) {
  const solar = manifest?.solar && typeof manifest.solar === "object"
    ? manifest.solar
    : (job.summary?.llm?.solar && typeof job.summary.llm.solar === "object"
      ? job.summary.llm.solar : null);
  const status = semantic?.status
    || (typeof solar?.semantic_status === "string" ? solar.semantic_status : null);
  const legacy = !semantic && !solar && !status;
  return {solar, status, legacy};
}

function semanticNotice(status, semantic, counts, semanticError) {
  if (semanticError && !semantic) {
    return el("div", {class: "notice warn", role: "status"},
      el("strong", {text: "Семантический слой недоступен"}),
      el("p", {text: status
        ? "Файл semantic.json недоступен. Статус и счётчики взяты из манифеста; значения и цитаты не показаны, чтобы не выдать неполные данные за полные."
        : "Файл semantic.json недоступен, и манифест не сообщает статус слоя. Значения и цитаты не показаны."}));
  }
  if (status === "ok") return el("div", {class: "notice", role: "status"},
    el("p", {text: "Для всех показанных значений нашлась буквальная цитата. Это подтверждает только присутствие значения в тексте, а не научную связь или полноту."}));
  if (status === "partial") {
    const accepted = SEMANTIC_KINDS.reduce((sum, kind) => sum + (counts[kind] || 0), 0);
    const errors = counts.errors === null ? "нет данных" : counts.errors;
    return el("div", {class: "notice warn", role: "status"},
      el("strong", {text: "Описание статьи собрано частично"}),
      el("p", {text: `Принято ${accepted} ${plural(accepted, "запись", "записи", "записей")}, замечаний проверки: ${errors}. Ниже только записи с буквальной цитатой; отклонённые и неподтверждённые причины перечислены отдельно.`}));
  }
  if (status === "missing") return el("div", {class: "notice", role: "status"},
    el("strong", {text: "Описание статьи не сформировано"}),
    el("p", {text: "Модель не вернула описание статьи. Это не ошибка разбора: официальные таблицы и CSV собираются отдельно. Тип исследования и происхождение данных не определены; пустое значение не означает, что статья не подходит."}));
  if (status === "rejected") {
    const first = Array.isArray(semantic?.errors) && semantic.errors.length
      ? String(semantic.errors[0]?.error || "").trim() : "";
    return el("div", {class: "notice bad", role: "status"},
      el("strong", {text: "Описание статьи отклонено"}),
      el("p", {text: first
        ? `Описание статьи нельзя использовать: ${first}. Признаки и записи не показываются, чтобы не выдать непроверенное за результат.`
        : "Описание статьи нельзя использовать. Признаки и записи не показываются, чтобы не выдать непроверенное за результат."}));
  }
  return null;
}

function semanticClaimsTable(semantic) {
  const evidence = semantic?.metadata_evidence
    && typeof semantic.metadata_evidence === "object"
    && !Array.isArray(semantic.metadata_evidence) ? semantic.metadata_evidence : {};
  const rows = SEMANTIC_CLAIMS.map(([field, title]) => {
    const value = semantic?.[field];
    const unknown = value === null || value === undefined || value === "unknown";
    return el("tr", {},
      el("td", {text: title}),
      el("td", {class: unknown ? "muted" : "", text: semanticClaimText(field, value)}),
      el("td", {}, semanticEvidenceLine(evidence[field])));
  });
  // scope_reason - пересказ модели. Цитата рядом это основание, а не факт:
  // подпись прямо говорит, что формулировка не является буквальным фактом.
  const reason = typeof semantic?.scope_reason === "string" ? semantic.scope_reason.trim() : "";
  rows.push(reason
    ? el("tr", {},
      el("td", {text: "Причина области"}),
      el("td", {}, el("span", {text: reason}), el("br"),
        el("span", {class: "muted", text: "Это пересказ модели, не буквальный факт документа."})),
      el("td", {}, semanticEvidenceLine(evidence.scope_reason)))
    : el("tr", {},
      el("td", {text: "Причина области"}),
      el("td", {class: "muted", text: "Не указана моделью"}),
      el("td", {class: "muted", text: "-"})));
  return el("div", {class: "table-wrap"}, el("table", {class: "compact"},
    el("thead", {}, el("tr", {},
      el("th", {text: "Признак"}), el("th", {text: "Значение"}), el("th", {text: "Цитата"}))),
    el("tbody", {}, rows)));
}

function semanticErrors(semantic) {
  const errors = Array.isArray(semantic?.errors)
    ? semantic.errors.filter(item => item && typeof item === "object" && !Array.isArray(item)) : [];
  if (!errors.length) return null;
  const shown = errors.slice(0, SEMANTIC_ROW_LIMIT);
  return el("details", {class: "parse-technical"},
    el("summary", {text: `Замечания проверки: ${errors.length}`}),
    el("ul", {}, shown.map(item => {
      const where = [];
      if (item.record && SEMANTIC_KIND_TITLES[item.record]) where.push(SEMANTIC_KIND_TITLES[item.record]);
      if (Number.isFinite(Number(item.index))) where.push(`запись ${Number(item.index) + 1}`);
      const prefix = where.length ? where.join(", ") + ": " : "";
      const text = typeof item.error === "string" && item.error.trim() ? item.error.trim() : "Без описания";
      return el("li", {text: prefix + text});
    })),
    errors.length > shown.length
      ? el("p", {class: "muted interpretation-limit", text: `Показаны первые ${shown.length} из ${errors.length}.`})
      : null);
}

function semanticCountsPanel(counts) {
  const value = item => item === null || item === undefined ? "-" : String(item);
  return el("div", {class: "parse-summary"},
    el("div", {}, el("strong", {text: value(counts.layers)}), el("span", {text: "Слои, принято"})),
    el("div", {}, el("strong", {text: value(counts.conditions)}), el("span", {text: "Условия, принято"})),
    el("div", {}, el("strong", {text: value(counts.metrics)}), el("span", {text: "Метрики, принято"})),
    el("div", {}, el("strong", {text: value(counts.errors)}), el("span", {text: "Замечания проверки"})));
}

function semanticPanel(job, manifest, semantic, semanticError) {
  const state = semanticState(job, manifest, semantic, semanticError);
  const head = el("div", {class: "panel-head"},
    el("div", {}, el("p", {class: "eyebrow", text: "Наблюдения модели"}),
      el("h2", {text: "Описание статьи моделью"})));
  if (state.legacy) return el("section", {class: "panel parse-panel parse-semantic"}, head,
    el("p", {class: "muted", text: "Этот разбор создан без семантического слоя: отдельного описания статьи в нём нет."}));

  const counts = semanticCountState(semantic, state.solar);
  const status = state.status;
  const rejected = status === "rejected";
  const records = semantic?.records && typeof semantic.records === "object"
    && !Array.isArray(semantic.records) ? semantic.records : null;
  const blocks = [
    el("div", {class: "parse-status"}, ...semanticPermanentBadges(), semanticBadge(status || "missing")),
    el("p", {class: "parse-state-lede", text: "Модель отдельно описала тип исследования, происхождение данных и часть наблюдений. Эти строки не входят в официальные таблицы и CSV. Буквальная цитата подтверждает только то, что значение напечатано в документе; она не доказывает научную связь, полноту или правильность вывода."}),
  ];
  const notice = semanticNotice(status, semantic, counts, semanticError);
  if (notice) blocks.push(notice);
  if (!rejected && (semantic || status)) blocks.push(semanticCountsPanel(counts));
  if (!rejected && semantic) {
    blocks.push(el("h3", {text: "Признаки статьи"}));
    blocks.push(semanticClaimsTable(semantic));
    for (const kind of SEMANTIC_KINDS) {
      const rows = Array.isArray(records?.[kind])
        ? records[kind].filter(row => row && typeof row === "object" && !Array.isArray(row)) : [];
      if (rows.length) blocks.push(semanticRecordTable(kind, rows));
    }
    const errors = semanticErrors(semantic);
    if (errors) blocks.push(errors);
  }
  return el("section", {class: "panel parse-panel parse-semantic"}, head, blocks);
}

function render(container, job, manifest, manifestError, interpretation, interpretationError, result, resultError, semantic, semanticError) {
  const active = ACTIVE.has(job.status), summary = job.summary || {};
  const llmOn = job.llm !== "off";
  const candidates = candidateStats(result, job);
  document.title = job.filename ? `Разбор ${job.filename} · ВИНИТИ` : "Разбор PDF · ВИНИТИ";
  const lede = active ? "Документ обрабатывается. Страница обновится автоматически."
    : job.status === "ok"
      ? "Файл обработан. Завершение обработки не подтверждает полноту и точность научных данных."
      : "Обработка не завершена. Проверьте сообщение ниже.";
  const paramsPanel = el("section", {class: "panel parse-panel"},
    el("div", {class: "panel-head"}, el("h2", {text: "Параметры разбора"})),
    el("dl", {class: "parse-meta"},
      el("dt", {text: "Область"}), el("dd", {text: areaLabel(job.profile)}),
      el("dt", {text: "Парсер"}), el("dd", {text: parserLabel(job.parser)}),
      el("dt", {text: "OCR"}), el("dd", {text: ocrLabel(job.ocr)}),
      el("dt", {text: "Модель"}), el("dd", {text: job.llm === "api" ? "включена · API" : (job.llm === "mock" ? "mock · офлайн" : "выключена")}),
      el("dt", {text: "Создано"}), el("dd", {text: dateTime(job.created_at)}),
      el("dt", {text: "Время работы"}), el("dd", {text: active ? "Идёт обработка" : seconds(job.elapsed_seconds)})),
    el("div", {class: "parse-summary"}, ["pages", "sources", "candidates", "warnings"].map(key => el("div", {},
      el("strong", {text: summary[key] ?? "-"}),
      el("span", {text: ({pages: "Страниц", sources: "Источников", candidates: "Кандидатов", warnings: "Предупреждений"})[key]})))));
  const stagesPanel = llmOn ? el("section", {class: "panel parse-panel"},
    el("div", {class: "panel-head"}, el("h2", {text: "Работа модели"})),
    stageList(manifest, job),
    el("h3", {text: "Проверка ответа"}), auditCounts(manifest, job),
    manifestError ? el("p", {class: "parse-inline-error", text: manifestError}) : null) : null;

  fill(container,
    el("section", {class: "hero parse-hero"},
      el("div", {class: "doc-main"},
        el("div", {class: "crumbs"}, el("a", {href: "#/catalog", text: "Каталог"}), " / Разбор PDF"),
        el("p", {class: "eyebrow data", title: job.filename || "", text: job.filename || "Документ"}),
        el("h1", {text: "Разбор документа"}),
        el("p", {class: "lede", text: lede}),
        active ? el("div", {class: "parse-progress"},
          el("progress", {"aria-label": "Ход разбора"}),
          el("span", {text: "Этапы выполняются на сервере. Страница обновится сама."})) : null),
      el("div", {class: "parse-hero-side"},
        el("div", {class: "parse-status"}, jobBadge(job.status), active ? el("span", {class: "parse-pulse", "aria-hidden": "true"}) : null),
        el("div", {class: "bar"},
          el("a", {class: "button secondary", href: "#/catalog", text: "Каталог"}),
          job.status === "ok" ? el("a", {
            class: "button", href: `/api/parse/${encodeURIComponent(job.id)}/file/result.json`,
            download: "result.json", text: "Скачать файл разбора",
          }) : null))),
    job.status === "error" ? el("div", {class: "notice bad", role: "alert"}, el("strong", {text: "Разбор завершился с ошибкой"}), el("p", {text: job.error || "Сервер не передал описание ошибки."})) : null,
    job.llm === "api" && job.status === "ok" ? el("div", {class: "notice good", role: "status"},
      el("strong", {text: "Модель включена · API"}),
      el("p", {text: "Фрагменты документа отправлены в настроенную API-модель. Ниже отображается только интерпретация, прошедшая проверку. Полнота научных данных при этом не подтверждена."})) :
    job.llm === "api" && active ? el("div", {class: "notice", role: "status"},
      el("strong", {text: "Модель включена · обработка продолжается"}),
      el("p", {text: "Разбор идёт на сервере. Эта страница обновится сама, когда появится проверенный результат."})) : null,
    job.llm === "mock" ? el("div", {class: "notice good", role: "status"},
      el("strong", {text: "Модель в режиме mock · офлайн"}),
      el("p", {text: "Стадии выполнены поверх готового результата. Реальная модель не вызывалась, официальные CSV и результат не менялись."})) : null,
    job.status === "ok" ? resultStatePanel(job, manifest, result, interpretation, interpretationError, resultError, candidates) : null,
    job.status === "ok" ? candidateNotice(candidates) : null,
    el("div", {class: "parse-layout" + (stagesPanel ? "" : " single")}, paramsPanel, stagesPanel),
    job.status === "ok" && llmOn
      && (job.profile === "solar" || manifest?.profile?.adapter === "solar_csv")
      ? semanticPanel(job, manifest, semantic, semanticError) : null,
    llmOn ? el("section", {class: "panel parse-panel parse-interpretation"},
      el("div", {class: "panel-head"}, el("div", {}, el("p", {class: "eyebrow", text: "Черновик модели"}), el("h2", {text: "Интерпретация"}))),
      interpretationResult(job, manifest, interpretation, interpretationError)) : null,
    el("section", {class: "panel parse-panel parse-files"},
      el("div", {class: "panel-head"}, el("h2", {text: "Файлы результата"})), artifacts(job, manifest)));
}

export async function renderParseJob(container, {jobId, signal, say}) {
  while (true) {
    const job = await getJSON(`/api/parse/${encodeURIComponent(jobId)}`, {}, signal);
    job.llm = job.llm === "api" || job.llm === "mock" ? job.llm : "off";
    let manifest = null, manifestError = "", interpretation = null, interpretationError = "";
    let result = null, resultError = "";
    let semantic = null, semanticError = "";
    if (job.status === "ok") {
      try { result = await getJSON(`/api/parse/${encodeURIComponent(jobId)}/file/result.json`, {}, signal); }
      catch (error) {
        if (error.name === "AbortError") throw error;
        resultError = "Файл result.json недоступен. Показаны только счётчики задания, покрытие и статусы кандидатов не проверены.";
      }
    }
    if (!ACTIVE.has(job.status) && job.llm !== "off") {
      try { manifest = await getJSON(`/api/parse/${encodeURIComponent(jobId)}/llm`, {}, signal); }
      catch (error) {
        if (error.name === "AbortError") throw error;
        manifestError = "Манифест LLM недоступен. Основной статус задания показан без него.";
      }
    }
    const interpretStage = (manifest?.stages || []).find(stage => stage.name === "interpret");
    const hasInterpretation = Object.hasOwn(manifest?.artifacts || {}, "interpretation.json") || interpretStage?.status === "ok" || interpretStage?.status === "rejected";
    if (!ACTIVE.has(job.status) && job.llm !== "off" && hasInterpretation) {
      try { interpretation = await getJSON(`/api/parse/${encodeURIComponent(jobId)}/file/llm/interpretation.json`, {}, signal); }
      catch (error) {
        if (error.name === "AbortError") throw error;
        interpretationError = "Файл проверенной интерпретации недоступен.";
      }
    }
    // Семантический слой есть только у области solar. Если файла нет у старого
    // разбора, панель покажет честную пометку, а не ошибку разбора.
    if (!ACTIVE.has(job.status) && job.llm !== "off"
        && (job.profile === "solar" || manifest?.profile?.adapter === "solar_csv")) {
      try { semantic = await getJSON(`/api/parse/${encodeURIComponent(jobId)}/file/llm/semantic.json`, {}, signal); }
      catch (error) {
        if (error.name === "AbortError") throw error;
        semanticError = "Файл semantic.json недоступен.";
      }
    }
    render(container, job, manifest, manifestError, interpretation, interpretationError, result, resultError, semantic, semanticError);
    say(ACTIVE.has(job.status) ? "Обработка PDF продолжается…" : "");
    if (!ACTIVE.has(job.status)) return;
    await wait(signal);
  }
}
