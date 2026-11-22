import { visibleWarnings } from "./ui.js";

const MAX_QUOTES = 80;
const CYRILLIC = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z",
  и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r",
  с: "s", т: "t", у: "u", ф: "f", х: "kh", ц: "ts", ч: "ch", ш: "sh",
  щ: "shch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
};

function yamlString(value) {
  return JSON.stringify(String(value ?? ""));
}

function yamlList(values) {
  const items = (Array.isArray(values) ? values : [values])
    .map(value => String(value ?? "").trim()).filter(Boolean);
  return `[${items.map(yamlString).join(", ")}]`;
}

function inline(value) {
  return String(value ?? "")
    .replace(/\\/g, "\\\\")
    .replace(/([`*_[\]<>#|])/g, "\\$1")
    .replace(/\r?\n/g, " ")
    .trim();
}

function tableCell(value) {
  const text = inline(value);
  return text || "-";
}

function quote(text) {
  return String(text || "").trim().split(/\r?\n/)
    .map(line => `> ${inline(line)}`).join("\n");
}

function valueOf(result) {
  const value = result.metric_value ?? result.normalized_value ?? result.raw_value ?? "";
  const unit = result.metric_unit || result.normalized_unit || result.raw_unit || "";
  return [value, unit].filter(part => part !== "" && part !== null && part !== undefined).join(" ");
}

function contextOf(result) {
  return [
    result.device_id, result.measurement_id, result.material_id,
    result.config_id, result.dataset_id, result.task_id,
    result.aggregation, result.time_point,
  ].filter(Boolean).join(" · ");
}

function pageOf(source) {
  const page = source && (source.page ?? source.source_page);
  return Number.isFinite(Number(page)) && Number(page) > 0 ? Number(page) : null;
}

function sourceQuote(source) {
  return source && (source.quote || source.fragment) || "";
}

function collectQuotes(envelope, results) {
  const seen = new Set();
  const items = [];
  const add = (source, label) => {
    const text = sourceQuote(source).trim();
    const page = pageOf(source);
    if (!text) return;
    const key = `${page || ""}|${text}`;
    if (seen.has(key)) return;
    seen.add(key);
    items.push({page, text, label: label || source.kind || "фрагмент"});
  };
  for (const result of results) {
    if (result.source) add(result.source, result.metric_name || "результат");
  }
  for (const source of Array.isArray(envelope.text_sources) ? envelope.text_sources : []) {
    add(source, source.kind || "текст");
  }
  return items.slice(0, MAX_QUOTES);
}

function scopeLine(envelope) {
  const publication = envelope.publication || {};
  const flag = String(publication.in_scope ?? "").trim().toLowerCase();
  if (flag === "true") return "в области";
  if (flag === "false") {
    const reason = String(publication.exclusion_reason ?? "").trim();
    return reason ? `вне области: ${reason}` : "вне области";
  }
  return "не определена";
}

function scopeFlag(envelope) {
  const flag = String(envelope.publication?.in_scope ?? "").trim().toLowerCase();
  return flag === "true" || flag === "false" ? flag : "unknown";
}

export function markdownFilename(envelope) {
  const publication = envelope.publication || {};
  const track = String(envelope.track || String(envelope.document_id || "").split(":")[0] || "document");
  const raw = String(publication.publication_id || envelope.publication_id || envelope.document_id || "document");
  const latin = raw.split("").map(char => CYRILLIC[char.toLowerCase()] ?? char).join("");
  const safe = latin.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 70);
  return ["viniti", track.replace(/[^A-Za-z0-9._-]+/g, "-"), safe || "document"]
    .filter(Boolean).join("-") + ".md";
}

export function passportMarkdown(envelope, {generated = new Date()} = {}) {
  const publication = envelope.publication || {};
  const title = publication.title || envelope.title || publication.publication_id || envelope.publication_id || "Публикация";
  const authors = publication.authors ?? envelope.authors ?? [];
  const year = publication.year ?? envelope.year ?? "";
  const doi = publication.doi ?? publication.DOI ?? "";
  const track = envelope.track || String(envelope.document_id || "").split(":")[0] || "";
  const results = Array.isArray(envelope.results) ? envelope.results : [];
  const issues = Array.isArray(envelope.issues) ? envelope.issues : [];
  const counts = envelope.counts || {};
  const publicationId = publication.publication_id || envelope.publication_id || "";
  const warnings = visibleWarnings(envelope.warnings);
  const quotes = collectQuotes(envelope, results);
  const date = generated instanceof Date && !Number.isNaN(generated.getTime())
    ? generated.toISOString().slice(0, 10) : String(generated).slice(0, 10);

  const frontmatter = [
    "---",
    `title: ${yamlString(title)}`,
    publicationId ? `publication_id: ${yamlString(publicationId)}` : null,
    envelope.document_id ? `document_id: ${yamlString(envelope.document_id)}` : null,
    track ? `area: ${yamlString(track)}` : null,
    `authors: ${yamlList(authors)}`,
    year !== "" ? `year: ${yamlString(year)}` : null,
    doi ? `doi: ${yamlString(doi)}` : null,
    Number.isFinite(Number(envelope.pages)) ? `pages: ${Number(envelope.pages)}` : null,
    envelope.status ? `status: ${yamlString(envelope.status)}` : null,
    `in_scope: ${yamlString(scopeFlag(envelope))}`,
    `results: ${results.length}`,
    `issues: ${issues.length}`,
    `generated: ${yamlString(date)}`,
    'source: "viniti"',
    `tags: ${yamlList(["viniti", track, "scientific-result"].filter(Boolean))}`,
    "---",
  ].filter(line => line !== null).join("\n");

  const bibliography = [
    `- **Название:** ${inline(title)}`,
    authors.length ? `- **Авторы:** ${(Array.isArray(authors) ? authors : [authors]).map(inline).filter(Boolean).join("; ")}` : null,
    year !== "" ? `- **Год:** ${inline(year)}` : null,
    doi ? `- **DOI:** [${inline(doi)}](https://doi.org/${encodeURI(String(doi))})` : null,
    publicationId ? `- **Идентификатор публикации:** ${inline(publicationId)}` : null,
    envelope.document_id ? `- **Документ ВИНИТИ:** ${inline(envelope.document_id)}` : null,
    track ? `- **Область:** ${inline(track)}` : null,
    Number.isFinite(Number(envelope.pages)) ? `- **Страниц:** ${Number(envelope.pages)}` : null,
  ].filter(Boolean).join("\n");

  const summary = [
    `- **Состояние обработки:** ${inline(envelope.status || "не указано")}`,
    `- **Область статьи:** ${inline(scopeLine(envelope))}`,
    `- **Результатов:** ${results.length}`,
    `- **Замечаний:** ${issues.length}`,
    counts && typeof counts === "object" && Number.isFinite(Number(counts.results))
      ? `- **Счётчик результатов в конверте:** ${Number(counts.results)}` : null,
    envelope.run?.handler ? `- **Обработчик:** ${inline(envelope.run.handler)}` : null,
  ].filter(Boolean).join("\n");

  const resultRows = results.map((result, index) => {
    const page = pageOf(result.source) ?? (Number(result.source_page) || null);
    return `| ${index + 1} | ${tableCell(result.metric_name || "результат")} | ${tableCell(valueOf(result))} | ${tableCell(contextOf(result))} | ${page || "-"} |`;
  });

  const resultQuotes = results
    .filter(result => result.source && sourceQuote(result.source))
    .map(result => {
      const page = pageOf(result.source);
      return `- **${inline(result.metric_name || "результат")}**${page ? ` (стр. ${page})` : ""}:\n\n  ${quote(sourceQuote(result.source)).replace(/\n/g, "\n  ")}`;
    });

  const issueRows = issues.map(issue => {
    const records = Array.isArray(issue.record_ids) ? issue.record_ids.filter(Boolean).join(", ") : "";
    return `- **${inline(issue.issue_id || "замечание")} · ${inline(issue.check_name || "проверка")}:** ${inline(issue.description || "описание не передано")}${records ? ` (записи: ${inline(records)})` : ""}`;
  });

  const quoteRows = quotes.map(item => `- **${inline(item.label)}**${item.page ? ` (стр. ${item.page})` : ""}:\n\n  ${quote(item.text).replace(/\n/g, "\n  ")}`);

  return [
    frontmatter,
    "",
    `# ${inline(title)}`,
    "",
    "## Библиография",
    bibliography || "- Данные библиографии не переданы.",
    "",
    "## Сводка статьи",
    summary,
    warnings.length ? "\n### Предупреждения\n" + warnings.map(text => `- ${inline(text)}`).join("\n") : null,
    "",
    "## Результаты",
    results.length
      ? ["| № | Метрика | Значение | Контекст | Стр. |", "| --- | --- | --- | --- | --- |", ...resultRows].join("\n")
      : "Подтверждённых результатов нет.",
    resultQuotes.length ? "\n### Цитаты по результатам\n\n" + resultQuotes.join("\n\n") : null,
    "",
    "## Замечания",
    issues.length ? issueRows.join("\n") : "Замечаний нет.",
    "",
    "## Цитаты со страницами",
    quoteRows.length ? quoteRows.join("\n\n") : "Цитаты не переданы.",
    "",
    "> Проверка источников буквальная: наличие цитат проверено, научная корректность связей и полнота извлечения не проверяются.",
    "",
  ].filter(part => part !== null).join("\n");
}
