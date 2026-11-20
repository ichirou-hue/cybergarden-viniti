import { el, fill } from "./dom.js";
import { solarCatalog } from "./solar.mjs";
import { number, plural } from "./ui.js";

const TABLES = ["publications", "materials", "devices", "layers", "measurements", "results", "issues"];
const PAGE_SIZE = 200;
const COLUMNS = ("publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit "
  + "aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit "
  + "result_origin cited_publication_id source_page source_section source_table source_figure source_fragment "
  + "extraction_status duplicate_of note").split(" ");
const LABELS = {passed: "Пройдена", review: "На разборе", pending: "Ожидает"};

const SAMPLE = {
  topic: "Демонстрационный пример",
  title: "Оценка воспроизводимости измерений",
  results: [
    {
      result_id: "R1", metric_name: "Среднее время", raw_value: "1200", raw_unit: "мс",
      normalized_value: "1.2", normalized_unit: "с", context: "Эксперимент A",
      extraction_status: "extracted", validation_status: "passed",
      source_page: 2, source_fragment: "Mean elapsed time was 1200 ms.",
    },
    {
      result_id: "R2", metric_name: "Число наблюдений", raw_value: "-5",
      context: "Эксперимент B", extraction_status: "extracted",
      source_page: 3, source_fragment: "The number of observations was -5.",
      issues: ["Число наблюдений не может быть отрицательным. Сверьте запись с источником."],
    },
  ],
};

let current = null;
let demoCache = null;
let pdfs = new Map();
let importNotice = null;

/** Сообщение о результате импорта; показывается один раз в ближайшем рендере. */
export function setImportNotice(tone, text) {
  importNotice = {tone, text};
}

function issueList(issues) {
  if (!Array.isArray(issues)) return [];
  return issues.map(issue => typeof issue === "string" ? {description: issue} : issue)
    .filter(issue => issue && typeof issue === "object");
}

function isSolar(data) {
  return Array.isArray(data.publications) && Array.isArray(data.results);
}

function passportRows(data) {
  if (isSolar(data)) {
    const tables = Object.fromEntries(
      TABLES.map(key => [key, Array.isArray(data[key]) ? data[key] : []]));
    return solarCatalog(tables).map(row => ({
      ...row,
      context: row.device?.device_label,
      fields: {
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
    const nested = entry.source && typeof entry.source === "object" ? entry.source : {};
    const legacy = entry.evidence && typeof entry.evidence === "object" ? entry.evidence : {};
    const result = {
      ...entry,
      publication_id: id,
      result_id: entry.result_id ?? entry.id,
      metric_name: entry.metric_name ?? [entry.metric, entry.cutoff ? "@" + entry.cutoff : ""].join(""),
      normalized_value: entry.normalized_value ?? entry.value,
      normalized_unit: entry.normalized_unit === "unspecified" ? undefined
        : (entry.normalized_unit ?? (entry.unit === "unspecified" ? undefined : entry.unit)),
      extraction_status: entry.extraction_status ?? (rag ? "extracted" : undefined),
      source_page: entry.source_page ?? nested.page ?? legacy.page,
      source_fragment: entry.source_fragment ?? nested.fragment ?? nested.quote ?? legacy.quote,
    };
    return {
      result,
      context: entry.context,
      fields: entry.fields ?? {},
      publication: {title: data.title, topic: data.topic ?? data.track},
      source: {
        page: result.source_page,
        quote: result.source_fragment,
        section: entry.source_section ?? nested.section,
        table: entry.source_table ?? nested.table,
        figure: entry.source_figure ?? nested.figure,
      },
      issues: issueList(entry.issues),
    };
  });
}

function demoState() {
  if (!demoCache) {
    demoCache = {
      rows: passportRows(SAMPLE), demo: true, source: SAMPLE.topic,
      title: SAMPLE.title, columns: [...COLUMNS, "context", "fields"], warnings: [],
    };
  }
  return demoCache;
}

/** Возвращает число загруженных записей. */
export function setPassport(data) {
  if (!data || typeof data !== "object") throw new Error("Ожидается объект JSON.");
  const rows = passportRows(data);
  const solar = isSolar(data);
  const publication = solar ? (data.publications[0] || {}) : {};
  const missing = solar ? TABLES.filter(key => !Array.isArray(data[key])) : [];
  for (const url of pdfs.values()) URL.revokeObjectURL(url);
  pdfs = new Map();
  current = {
    rows,
    demo: false,
    source: data.topic || publication.topic || data.title || publication.title
      || "Импортированный паспорт",
    title: data.title || publication.title || data.topic || "Паспорт статьи",
    columns: solar ? COLUMNS : [...COLUMNS, "context", "fields"],
    warnings: missing.length
      ? [`Набор Solar неполный: нет таблиц ${missing.join(", ")}. `
        + "Контекст прибора, материала и слоёв может быть неполным."]
      : [],
  };
  return rows.length;
}

function validation(row) {
  const explicit = row.result.validation_status;
  if (row.issues.length || explicit === "review") return "review";
  if (explicit === "passed") return "passed";
  if (!explicit && row.result.extraction_status === "extracted") return "passed";
  return "pending";
}

function hasValue(value) {
  return value !== undefined && value !== null && String(value).trim() !== "";
}

function exportable(row) {
  if (validation(row) !== "passed") return false;
  const status = row.result.extraction_status;
  if (status === "present_not_extractable" || status === "absent") return false;
  return hasValue(row.result.normalized_value) || hasValue(row.result.raw_value);
}

function inferred(row) {
  return !row.result.validation_status && !row.issues.length
    && row.result.extraction_status === "extracted";
}

function badge(status, isInferred = false) {
  const tone = status === "passed" ? "ok" : status === "review" ? "warn" : "idle";
  return el("span", {
    class: `badge ${tone}`,
    text: LABELS[status],
    title: isInferred ? "Статус выведен из извлечения: запись не проходила валидацию" : null,
  });
}

function value(record, prefix) {
  return [record[prefix + "_value"], record[prefix + "_unit"]]
    .filter(part => part != null && part !== "").join(" ") || "—";
}

function csvCell(cellValue) {
  let text = String(cellValue ?? "");
  const risky = /^\s*[=+@-]/.test(text) || /^[\t\r]/.test(text);
  if (risky && !/^\s*[+-]?\d+(?:[.,]\d+)?(?:e[+-]?\d+)?\s*$/i.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

function download(rows, columns) {
  const accepted = rows.filter(exportable);
  if (!accepted.length) return;
  const lines = [columns.join(","), ...accepted.map(({result, context, fields}) => {
    const record = {...result, context, fields: JSON.stringify(fields)};
    return columns.map(column => csvCell(record[column])).join(",");
  })];
  const blob = new Blob(["\ufeff" + lines.join("\r\n") + "\r\n"], {type: "text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const link = el("a", {href: url, download: "results.csv"});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function renderPassport(root, ctx) {
  document.title = "Паспорт статьи · ВИНИТИ";
  const source = current || demoState();
  const rows = source.rows;
  const notice = importNotice;
  importNotice = null;
  const inferredCount = source.demo ? 0 : rows.filter(inferred).length;

  const total = el("strong", {text: number(rows.length)});
  const passedCount = el("strong", {text: number(rows.filter(row => validation(row) === "passed").length)});
  const reviewCount = el("strong", {text: number(rows.filter(row => validation(row) === "review").length)});

  const body = el("tbody");
  const detail = el("div", {id: "detail", "aria-live": "polite"});
  const count = el("p", {class: "count", "aria-live": "polite"});

  const query = el("input", {
    type: "search", "aria-label": "Поиск по паспорту",
    placeholder: "Показатель, контекст, цитата…",
  });
  const metric = el("select", {"aria-label": "Метрика"},
    el("option", {value: "", text: "Все"}),
    ...[...new Set(rows.map(row => row.result.metric_name).filter(Boolean))].sort()
      .map(name => el("option", {value: name, text: name})),
  );
  const status = el("select", {"aria-label": "Проверка"},
    el("option", {value: "", text: "Все"}),
    el("option", {value: "passed", text: LABELS.passed}),
    el("option", {value: "review", text: LABELS.review}),
    el("option", {value: "pending", text: LABELS.pending}),
  );

  const csvButton = el("button", {
    type: "button", class: "secondary", text: "Скачать CSV",
    title: "Строки со статусом «Пройдена» и извлечённым значением",
    onclick: () => download(rows, source.columns),
  });

  const pdfInput = el("input", {type: "file", accept: ".pdf,application/pdf"});
  const pdfLabel = el("label", {class: "file"}, "Привязать исходный PDF", pdfInput);
  pdfInput.addEventListener("change", () => {
    const file = pdfInput.files[0];
    if (!file || !selected) return;
    const id = selected.result.publication_id;
    if (pdfs.has(id)) URL.revokeObjectURL(pdfs.get(id));
    pdfs.set(id, URL.createObjectURL(file));
    pdfInput.value = "";
    ctx.say(`PDF привязан к публикации ${id}.`);
    drawDetail();
  });

  const importButton = el("button", {
    type: "button", class: "primary", text: "Импорт паспорта JSON",
    onclick: () => document.getElementById("passport")?.click(),
  });

  let selected = null;
  let shown = PAGE_SIZE;

  function drawDetail() {
    detail.replaceChildren();
    if (!rows.length) {
      detail.append(el("p", {class: "muted", text: "В паспорте нет результатов."}));
      return;
    }
    if (!selected) {
      detail.append(el("p", {class: "muted", text: "Ничего не найдено по фильтрам."}));
      return;
    }
    const {result, publication, source: evidence, issues, fields} = selected;
    const state = validation(selected);
    const title = [result.metric_name, value(result, "normalized")].filter(part => part && part !== "—").join(" · ");
    detail.append(
      el("p", {class: "value", text: title || "Результат"}),
      badge(state, inferred(selected)),
    );
    const list = el("dl", {class: "detail-grid"});
    const terms = [
      ["Публикация", publication?.title || result.publication_id],
      ["Исходное значение", value(result, "raw")],
      ["Тема", publication?.topic],
      ["Контекст", selected.context],
      ...Object.entries(fields).filter(([, field]) => ["string", "number", "boolean"].includes(typeof field)),
    ];
    for (const [term, text] of terms) {
      list.append(el("dt", {text: term}),
        el("dd", {text: text == null || text === "" ? "—" : text}));
    }
    detail.append(list);

    const location = [evidence.page ? "стр. " + evidence.page : "", evidence.section, evidence.table, evidence.figure]
      .filter(Boolean).join(" · ");
    if (location) detail.append(el("p", {class: "muted", text: location}));
    if (evidence.quote) detail.append(el("blockquote", {text: evidence.quote}));

    const pdf = pdfs.get(result.publication_id);
    if (pdf && evidence.page && /^\d+$/.test(String(evidence.page))) {
      detail.append(el("a", {
        class: "button secondary", href: pdf + "#page=" + evidence.page,
        target: "_blank", rel: "noopener", text: "Открыть страницу PDF",
      }));
    }
    for (const issue of issues) {
      detail.append(el("p", {class: "notice warn", text: issue.description || issue.check_name}));
    }
    if (state === "pending") {
      detail.append(el("p", {class: "muted", text: "Результат проверки ещё не поступил."}));
    }
  }

  function drawRows() {
    const terms = query.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const filtered = rows.filter(row => {
      if (metric.value && row.result.metric_name !== metric.value) return false;
      if (status.value && validation(row) !== status.value) return false;
      const text = [
        row.publication?.title, row.publication?.topic, row.context,
        row.result.metric_name, row.result.raw_value, row.result.normalized_value,
        row.source.quote, ...Object.values(row.fields),
      ].join(" ").toLocaleLowerCase();
      return terms.every(term => text.includes(term));
    });
    const visible = filtered.slice(0, shown);
    if (!visible.includes(selected)) selected = visible[0] || null;
    body.replaceChildren();
    for (const row of visible) {
      const button = el("button", {
        type: "button", class: "row-button",
        "aria-pressed": String(row === selected),
        "aria-controls": "detail",
        text: row.result.metric_name || "Показатель",
      });
      const tr = el("tr", {class: row === selected ? "selected" : ""},
        el("td", {}, button),
        el("td", {text: value(row.result, "normalized")}),
        el("td", {text: row.context || "—"}),
        el("td", {}, badge(validation(row), inferred(row))),
      );
      button.addEventListener("click", () => {
        selected = row;
        for (const other of body.querySelectorAll("button.row-button")) {
          const target = other === button;
          other.setAttribute("aria-pressed", String(target));
          other.closest("tr").classList.toggle("selected", target);
        }
        drawDetail();
        syncActions();
      });
      body.append(tr);
    }
    if (!filtered.length) {
      body.append(el("tr", {}, el("td", {
        colspan: 4, class: "empty",
        text: rows.length ? "Результаты не найдены" : "В паспорте нет результатов",
      })));
    } else if (filtered.length > visible.length) {
      const next = Math.min(PAGE_SIZE, filtered.length - visible.length);
      body.append(el("tr", {class: "more"}, el("td", {colspan: 4},
        el("button", {
          type: "button", class: "secondary", text: `Показать ещё ${number(next)}`,
          onclick: () => {
            const firstNew = visible.length;
            shown += PAGE_SIZE;
            drawRows();
            const buttons = body.querySelectorAll("button.row-button");
            if (!buttons[firstNew]) return;
            try {
              buttons[firstNew].focus({preventScroll: true});
            } catch {
              buttons[firstNew].focus();
            }
          },
        }),
      )));
    }
    count.textContent = filtered.length
      ? `Показано ${number(Math.min(shown, filtered.length))} из ${number(filtered.length)}`
        + (filtered.length < rows.length ? ` · всего ${number(rows.length)}` : "")
        + (filtered.length > shown ? ` · ещё ${number(filtered.length - shown)}` : "")
      : rows.length ? "Результаты не найдены" : "Результатов нет";
  }

  function syncActions() {
    csvButton.disabled = source.demo || !rows.some(exportable);
    pdfInput.disabled = source.demo || !selected;
    pdfLabel.classList.toggle("disabled", pdfInput.disabled);
  }

  function paint() {
    drawRows();
    drawDetail();
    syncActions();
  }

  let timer = 0;
  const repaint = () => {
    shown = PAGE_SIZE;
    paint();
  };
  query.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(repaint, 180);
  });
  metric.addEventListener("change", repaint);
  status.addEventListener("change", repaint);

  fill(root,
    el("section", {class: "hero passport-hero"},
      el("div", {},
        el("p", {class: "eyebrow", text: "Локальный паспорт"}),
        el("h1", {}, "Паспорт", el("br"), "публикации", el("span", {class: "dot", text: "."})),
        el("p", {class: "lede", text: source.title}),
      ),
      el("div", {class: "actions"}, importButton, pdfLabel),
    ),
    notice ? el("p", {class: `notice ${notice.tone}`, role: notice.tone === "bad" ? "alert" : "status", text: notice.text}) : null,
    el("section", {class: "stats", "aria-label": "Сводка паспорта"},
      el("div", {}, el("span", {text: "Извлечено результатов"}), total),
      el("div", {}, el("span", {text: "Прошли проверку"}), passedCount),
      el("div", {}, el("span", {text: "Требуют разбора"}), reviewCount),
    ),
    source.demo ? el("p", {class: "notice warn", text:
      "Демонстрационные значения. Импортируйте паспорт, чтобы работать со своими данными."}) : null,
    ...(source.warnings || []).map(text => el("p", {class: "notice warn", text})),
    inferredCount ? el("p", {class: "notice", text:
      `Проверка не запускалась: для ${number(inferredCount)} `
      + `${plural(inferredCount, "записи", "записей", "записей")} статус «Пройдена» выведен `
      + "из успешного извлечения и отсутствия замечаний."}) : null,
    el("div", {class: "workspace"},
      el("section", {class: "panel", "aria-labelledby": "passport-results-title"},
        el("header", {class: "panel-head"},
          el("div", {},
            el("p", {class: "eyebrow data", title: source.source, text: source.source}),
            el("h2", {id: "passport-results-title", text: "Результаты"}),
          ),
          csvButton,
        ),
        el("div", {class: "bar filters"},
          el("label", {class: "grow"}, "Поиск", query),
          el("label", {}, "Метрика", metric),
          el("label", {}, "Проверка", status),
        ),
        count,
        el("div", {class: "results", tabindex: "0", role: "region", "aria-label": "Результаты паспорта"},
          el("table", {class: "passport-table"},
            el("caption", {class: "sr-only", text: "Результаты паспорта"}),
            el("thead", {}, el("tr", {},
              el("th", {scope: "col", text: "Метрика"}),
              el("th", {scope: "col", text: "Значение"}),
              el("th", {scope: "col", text: "Контекст"}),
              el("th", {scope: "col", text: "Проверка"}),
            )),
            body,
          ),
        ),
      ),
      el("aside", {class: "panel detail", "aria-labelledby": "passport-detail-title"},
        el("header", {class: "panel-head"},
          el("div", {},
            el("p", {class: "eyebrow", text: "Карточка результата"}),
            el("h2", {id: "passport-detail-title", text: "Источник и проверка"}),
          ),
        ),
        detail,
      ),
    ),
  );

  paint();
}
