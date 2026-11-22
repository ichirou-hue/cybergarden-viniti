import { el, fill } from "./dom.js";
import { getJSON } from "./api.js";

const ACTIVE = new Set(["queued", "running"]);
const STATUS = {
  queued: ["В очереди", "idle"], running: ["Выполняется", "warn"],
  ok: ["Готово", "ok"], error: ["Ошибка", "bad"],
  rejected: ["Отклонено", "bad"], skipped: ["Пропущено", "idle"],
};
const STAGES = {
  sources: "Источники", interpret: "Интерпретация моделью",
  passport: "Сборка результата", audit: "Проверка цитат", export: "Экспорт",
};
const LABELS = {
  "result.json": "Результат JSON", "parser.json": "Отчёт парсера", "candidates.csv": "Кандидаты CSV",
  "sources.json": "Источники LLM", "request.json": "Запрос LLM", "interpretation.json": "Интерпретация",
  "audit.json": "Аудит", "manifest.json": "Манифест LLM",
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
};

function badge(status) {
  const [label, tone] = STATUS[status] || [status || "Неизвестно", "idle"];
  return el("span", {class: `badge ${tone}`, text: label});
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

function auditCounts(manifest, job) {
  const counts = manifest?.counts || job.summary?.llm?.counts;
  if (!counts) return el("p", {class: "muted", text: "Сводка проверки пока недоступна."});
  return el("div", {class: "parse-counts"}, [
    ["Записей интерпретировано", counts.interpreted_records],
    ["Замечаний структуры", counts.validation_issues],
    ["Находок проверки", counts.audit_findings],
  ].map(([label, value]) => el("div", {}, el("strong", {text: Number.isFinite(Number(value)) ? String(value) : "-"}), el("span", {text: label}))));
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
    const names = new Set(Object.keys(manifest?.artifacts || {}));
    names.add("manifest.json");
    for (const name of names) technical.push(artifactLink(job.id, `llm/${name}`, LABELS[name] || name));
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

function interpretationTables(interpretation) {
  const tables = interpretation?.tables;
  if (!tables || typeof tables !== "object" || Array.isArray(tables)) return null;
  const sections = [];
  for (const [name, rawRows] of Object.entries(tables)) {
    const rows = Array.isArray(rawRows) ? rawRows.filter(row => row && typeof row === "object" && !Array.isArray(row)) : [];
    const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
    const shown = rows.slice(0, 100);
    sections.push(el("section", {class: "interpretation-table"},
      el("div", {class: "interpretation-table-head"},
        el("h3", {text: TABLE_TITLES[name] || name}), el("span", {text: `${rows.length} ${rows.length === 1 ? "запись" : "записей"}`})),
      rows.length && columns.length ? el("div", {class: "table-wrap"}, el("table", {class: "compact"},
        el("thead", {}, el("tr", {}, columns.map(column => el("th", {text: COLUMN_TITLES[column] || column,
          title: COLUMN_TITLES[column] ? column : null})))),
        el("tbody", {}, shown.map(row => el("tr", {}, columns.map(column => el("td", {text: printable(row[column])}))))))) :
        el("p", {class: "muted", text: "Проверенных записей в этой таблице нет."}),
      rows.length > shown.length ? el("p", {class: "muted interpretation-limit", text: `Показаны первые ${shown.length} записей. Полный результат доступен в interpretation.json.`}) : null));
  }
  return sections.length ? el("div", {class: "interpretation-tables"}, sections) : null;
}

function interpretationResult(job, manifest, interpretation, interpretationError) {
  if (ACTIVE.has(job.status)) return el("p", {class: "muted", text: "Результат появится после проверки ответа модели."});
  if (job.llm === "off") return el("div", {class: "interpretation-empty"},
    el("strong", {text: "LLM выключен"}),
    el("p", {class: "muted", text: "Интерпретация не запускалась, фрагменты документа модели не передавались."}));
  const stage = (manifest?.stages || []).find(item => item.name === "interpret");
  if (interpretation?.status === "ok") {
    const tables = interpretationTables(interpretation);
    return el("div", {},
      el("div", {class: "interpretation-summary"}, badge("ok"), el("span", {text: "Ответ модели прошёл проверку источников и структуры."})),
      tables || el("p", {class: "muted", text: "Проверенная интерпретация не содержит таблиц для отображения."}));
  }
  if (stage?.status === "skipped") return el("div", {class: "interpretation-empty"},
    el("strong", {text: "Интерпретация пропущена"}),
    el("p", {class: "muted", text: stage.reason || "Этот профиль не формирует интерпретацию с помощью выбранной модели."}));
  if (stage?.status === "rejected" || interpretation?.status === "rejected") return el("div", {class: "notice warn"},
    el("strong", {text: "Интерпретация не прошла проверку"}),
    el("p", {text: interpretation?.error || stage?.error || "Ответ модели отклонён проверкой структуры или источников."}));
  return el("div", {class: "interpretation-empty"},
    el("strong", {text: "Интерпретация недоступна"}),
    el("p", {class: "muted", text: interpretationError || "Сервер не предоставил проверенный результат для отображения."}));
}

function areaLabel(value) {
  if (value === "solar") return "Solar";
  if (value === "rag") return "RAG";
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

function render(container, job, manifest, manifestError, interpretation, interpretationError) {
  const active = ACTIVE.has(job.status), summary = job.summary || {};
  const llmOn = job.llm !== "off";
  document.title = job.filename ? `Разбор ${job.filename} · ВИНИТИ` : "Разбор PDF · ВИНИТИ";
  const lede = active ? "Документ обрабатывается. Страница обновится автоматически."
    : job.status === "ok"
      ? (llmOn ? "Разбор завершён. Результат и проверенная интерпретация готовы."
        : "Разбор завершён. Таблицы, значения и цитаты готовы.")
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
        el("div", {class: "parse-status"}, badge(job.status), active ? el("span", {class: "parse-pulse", "aria-hidden": "true"}) : null),
        el("div", {class: "bar"},
          el("a", {class: "button secondary", href: "#/catalog", text: "Каталог"}),
          job.status === "ok" ? el("a", {
            class: "button", href: `/api/parse/${encodeURIComponent(job.id)}/file/result.json`,
            download: "result.json", text: "Скачать результат",
          }) : null))),
    job.status === "error" ? el("div", {class: "notice bad", role: "alert"}, el("strong", {text: "Разбор завершился с ошибкой"}), el("p", {text: job.error || "Сервер не передал описание ошибки."})) : null,
    job.llm === "api" && job.status === "ok" ? el("div", {class: "notice good", role: "status"},
      el("strong", {text: "Модель включена · API"}),
      el("p", {text: "Фрагменты документа отправлены в настроенную API-модель. Ниже отображается только интерпретация, прошедшая проверку."})) :
    job.llm === "api" && active ? el("div", {class: "notice", role: "status"},
      el("strong", {text: "Модель включена · обработка продолжается"}),
      el("p", {text: "Разбор идёт на сервере. Эта страница обновится сама, когда появится проверенный результат."})) : null,
    job.llm === "mock" ? el("div", {class: "notice good", role: "status"},
      el("strong", {text: "Модель в режиме mock · офлайн"}),
      el("p", {text: "Стадии выполнены поверх готового результата. Реальная модель не вызывалась, официальные CSV и результат не менялись."})) : null,
    el("div", {class: "parse-layout" + (stagesPanel ? "" : " single")}, paramsPanel, stagesPanel),
    llmOn ? el("section", {class: "panel parse-panel parse-interpretation"},
      el("div", {class: "panel-head"}, el("div", {}, el("p", {class: "eyebrow", text: "Проверенные данные"}), el("h2", {text: "Интерпретация"}))),
      interpretationResult(job, manifest, interpretation, interpretationError)) : null,
    el("section", {class: "panel parse-panel parse-files"},
      el("div", {class: "panel-head"}, el("h2", {text: "Файлы результата"})), artifacts(job, manifest)));
}

export async function renderParseJob(container, {jobId, signal, say}) {
  while (true) {
    const job = await getJSON(`/api/parse/${encodeURIComponent(jobId)}`, {}, signal);
    job.llm = job.llm === "api" || job.llm === "mock" ? job.llm : "off";
    let manifest = null, manifestError = "", interpretation = null, interpretationError = "";
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
    render(container, job, manifest, manifestError, interpretation, interpretationError);
    say(ACTIVE.has(job.status) ? "Обработка PDF продолжается…" : "");
    if (!ACTIVE.has(job.status)) return;
    await wait(signal);
  }
}
