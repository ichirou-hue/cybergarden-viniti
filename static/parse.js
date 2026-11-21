import { el, fill } from "./dom.js";
import { getJSON } from "./api.js";

const ACTIVE = new Set(["queued", "running"]);
const STATUS = {
  queued: ["В очереди", "idle"], running: ["Выполняется", "warn"],
  ok: ["Готово", "ok"], error: ["Ошибка", "bad"],
  rejected: ["Отклонено", "bad"], skipped: ["Пропущено", "idle"],
};
const STAGES = {sources: "Источники", interpret: "Интерпретация", passport: "Паспорт", audit: "Аудит", export: "Экспорт"};
const LABELS = {
  "result.json": "Результат JSON", "parser.json": "Отчёт парсера", "candidates.csv": "Кандидаты CSV",
  "sources.json": "Источники LLM", "request.json": "Запрос LLM", "interpretation.json": "Интерпретация",
  "audit.json": "Аудит", "manifest.json": "Манифест LLM",
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
  if (!rows?.length) return el("p", {class: "muted", text: job.llm === "off" ? "LLM-этап не запускался." : "Стадии появятся после обработки документа."});
  return el("div", {class: "parse-stages"}, rows.map(stage => el("div", {class: "parse-stage"},
    el("span", {text: STAGES[stage.name] || stage.name}), badge(stage.status),
    stage.error ? el("p", {class: "parse-stage-note error-hint", text: stage.error}) :
      stage.reason ? el("p", {class: "parse-stage-note muted", text: stage.reason}) : null)));
}

function auditCounts(manifest, job) {
  const counts = manifest?.counts || job.summary?.llm?.counts;
  if (!counts) return el("p", {class: "muted", text: "Сводка аудита пока недоступна."});
  return el("div", {class: "parse-counts"}, [
    ["Записей интерпретировано", counts.interpreted_records],
    ["Замечаний валидации", counts.validation_issues],
    ["Находок аудита", counts.audit_findings],
  ].map(([label, value]) => el("div", {}, el("strong", {text: Number.isFinite(Number(value)) ? String(value) : "-"}), el("span", {text: label}))));
}

function artifacts(job, manifest) {
  if (job.status !== "ok") return el("p", {class: "muted", text: "Артефакты станут доступны после успешного завершения."});
  const links = ["result.json"].map(name => artifactLink(job.id, name, LABELS[name]));
  if (manifest?.artifacts?.["candidates.csv"] || job.status === "ok")
    links.push(artifactLink(job.id, "candidates.csv", LABELS["candidates.csv"]));
  if (manifest?.artifacts?.["parser.json"] || job.parser === "docling")
    links.splice(1, 0, artifactLink(job.id, "parser.json", LABELS["parser.json"]));
  if (job.llm !== "off") {
    const names = new Set(Object.keys(manifest?.artifacts || {})); names.add("manifest.json");
    for (const name of names) links.push(artifactLink(job.id, `llm/${name}`, LABELS[name] || name));
  }
  return el("div", {class: "parse-artifacts"}, links);
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
        el("h3", {text: name}), el("span", {text: `${rows.length} ${rows.length === 1 ? "запись" : "записей"}`})),
      rows.length && columns.length ? el("div", {class: "table-wrap"}, el("table", {class: "compact"},
        el("thead", {}, el("tr", {}, columns.map(column => el("th", {text: column})))),
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

function render(container, job, manifest, manifestError, interpretation, interpretationError) {
  const active = ACTIVE.has(job.status), summary = job.summary || {};
  document.title = `Разбор ${job.filename || job.id} · ВИНИТИ`;
  fill(container,
    el("section", {class: "hero parse-hero"},
      el("div", {class: "doc-main"},
        el("div", {class: "crumbs"}, el("a", {href: "#/catalog", text: "Каталог"}), " / Разбор PDF"),
        el("p", {class: "eyebrow data", text: job.id || "Задание"}), el("h1", {text: job.filename || "Разбор документа"}),
        el("p", {class: "lede", text: active ? "Документ обрабатывается. Страница обновится автоматически." :
          job.status === "ok" ? "Обработка завершена. Результаты и технические артефакты готовы." : "Обработка не завершена. Проверьте сообщение ниже."})),
      el("div", {class: "parse-status"}, badge(job.status), active ? el("span", {class: "parse-pulse", "aria-hidden": "true"}) : null)),
    job.status === "error" ? el("div", {class: "notice bad", role: "alert"}, el("strong", {text: "Разбор завершился с ошибкой"}), el("p", {text: job.error || "Сервер не передал описание ошибки."})) : null,
    job.llm === "api" && job.status === "ok" ? el("div", {class: "notice good", role: "status"},
      el("strong", {text: "LLM включен · API-модель"}),
      el("p", {text: "Фрагменты документа отправлены в настроенную API-модель. Ниже отображается только интерпретация, прошедшая проверку."})) :
    job.llm === "api" && active ? el("div", {class: "notice", role: "status"},
      el("strong", {text: "LLM включен · обработка продолжается"}),
      el("p", {text: "Запрос к API ещё не подтверждён."})) : null,
    job.llm === "mock" ? el("div", {class: "notice good", role: "status"},
      el("strong", {text: "LLM mock · офлайн"}),
      el("p", {text: "Оболочка выполнила стадии поверх scripts-паспорта. Реальная модель не вызывалась, официальные CSV и паспорт не менялись."})) : null,
    el("div", {class: "parse-layout"},
      el("section", {class: "panel parse-panel"}, el("div", {class: "panel-head"}, el("h2", {text: "Ход обработки"})),
        el("dl", {class: "parse-meta"},
          el("dt", {text: "Профиль"}), el("dd", {text: job.profile || "Не указан"}), el("dt", {text: "Парсер"}), el("dd", {text: job.parser || "Не указан"}),
          el("dt", {text: "OCR"}), el("dd", {text: job.ocr || "off"}), el("dt", {text: "LLM"}), el("dd", {text: job.llm === "api" ? "включен · API" : (job.llm === "mock" ? "mock · офлайн" : "выключен")}),
          el("dt", {text: "Создано"}), el("dd", {text: dateTime(job.created_at)}), el("dt", {text: "Время работы"}), el("dd", {text: active ? "Идёт обработка" : seconds(job.elapsed_seconds)})),
        el("div", {class: "parse-summary"}, ["pages", "sources", "candidates", "warnings"].map(key => el("div", {},
          el("strong", {text: summary[key] ?? "-"}), el("span", {text: ({pages: "Страниц", sources: "Источников", candidates: "Кандидатов", warnings: "Предупреждений"})[key]}))))),
      el("section", {class: "panel parse-panel"}, el("div", {class: "panel-head"}, el("h2", {text: "Стадии LLM"})), stageList(manifest, job),
        el("h3", {text: "Аудит"}), auditCounts(manifest, job), manifestError ? el("p", {class: "parse-inline-error", text: manifestError}) : null)),
    el("section", {class: "panel parse-panel parse-interpretation"},
      el("div", {class: "panel-head"}, el("div", {}, el("p", {class: "eyebrow", text: "Проверенные данные"}), el("h2", {text: "Интерпретация"}))),
      interpretationResult(job, manifest, interpretation, interpretationError)),
    el("section", {class: "panel parse-panel parse-files"}, el("div", {class: "panel-head"}, el("h2", {text: "Артефакты"})), artifacts(job, manifest)));
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
