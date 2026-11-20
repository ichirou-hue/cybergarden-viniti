import { getJSON } from "./api.js";
import { el, fill } from "./dom.js";
import { docStatus, extractStatus, fieldLabel, issueLabel, number, plural, seconds, unitLabel } from "./ui.js";
import { openEvidence } from "./evidence.js";

const PAGE = 50;

const TABS = [
  ["results", "Результаты"],
  ["entities", "Сущности"],
  ["issues", "Замечания"],
  ["sources", "Фрагменты PDF"],
  ["summary", "Сводка"],
];

const KIND_LABELS = {text: "Текст", table: "Таблица", caption: "Подпись", equation: "Формула"};

let keepTabFocus = false;

export async function renderDocument(root, ctx) {
  const docId = ctx.docId;
  const base = `/api/documents/${encodeURIComponent(docId)}`;
  const [summary, facets] = await Promise.all([
    getJSON(base, {}, ctx.signal),
    getJSON(`${base}/facets`, {}, ctx.signal)
      .then(data => ({ok: true, data}))
      .catch(error => {
        if (error.name === "AbortError") throw error;
        return {ok: false, data: {metrics: [], ks: [], datasets: [], configs: [], statuses: []}};
      }),
  ]);
  if (ctx.signal.aborted) return;
  const active = TABS.some(([key]) => key === (ctx.params.tab || ctx.params.view))
    ? (ctx.params.tab || ctx.params.view)
    : "results";
  const publication = summary.publication || {};
  const heading = publication.title || summary.title || summary.publication_id;
  document.title = `${publication.publication_id || summary.publication_id || docId}${heading ? ` · ${String(heading).slice(0, 60)}` : ""} · ВИНИТИ`;

  const counts = summary.counts || {};
  const header = el("section", {class: "hero doc-hero"},
    el("div", {class: "doc-main"},
      el("nav", {class: "crumbs", "aria-label": "Хлебные крошки"},
        el("a", {href: `#/?track=${encodeURIComponent(ctx.descriptor.track)}`, text: "Каталог"}),
        el("span", {"aria-hidden": "true", text: "/"}),
        el("span", {text: ctx.descriptor.label || ctx.descriptor.track}),
        el("span", {"aria-hidden": "true", text: "/"}),
        el("span", {text: publication.publication_id || summary.publication_id || docId}),
      ),
      el("h1", {text: publication.title || summary.title || summary.publication_id}),
      metaLine(summary, publication),
    ),
    el("div", {class: "doc-actions"},
      docStatus(summary.status),
      scopeBadge(publication),
      exportLink(summary, base, "pdf", "PDF", summary.status),
      exportLink(summary, base, "json", "JSON", summary.status),
      exportLink(summary, base, "csv", "CSV", summary.status),
    ),
  );

  const notices = noticeBlock(summary);
  const stats = el("section", {class: "stats", "aria-label": "Сводка документа"},
    el("div", {}, el("span", {text: "Результатов"}), el("strong", {text: number(counts.results ?? 0)})),
    el("div", {}, el("span", {text: "Замечаний"}), el("strong", {text: number(counts.issues ?? 0)})),
    el("div", {}, el("span", {text: "Обработка"}), el("strong", {text: seconds(summary.run && summary.run.duration_ms)})),
  );
  const body = el("section", {class: "tab-body", id: "tabpanel", role: "tabpanel", "aria-labelledby": `tab-${active}`, tabindex: "0"});

  const tabs = el("nav", {class: "tabs", role: "tablist", "aria-label": "Разделы документа"},
    TABS.map(([key, label]) => el("button", {
      type: "button", role: "tab", id: `tab-${key}`, "aria-controls": "tabpanel",
      "aria-selected": key === active ? "true" : "false",
      tabindex: key === active ? "0" : "-1",
      "data-count": key === "results" && counts.results !== undefined ? String(counts.results)
        : key === "issues" && counts.issues ? String(counts.issues) : null,
      class: "tab" + (key === active ? " active" : ""),
      onclick: () => {
        if (key !== active) keepTabFocus = true;
        ctx.go({
          tab: key, page: "", result: "",
          q: key === "results" ? ctx.params.q : "",
        });
      },
      text: label,
    })),
  );

  tabs.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = [...tabs.querySelectorAll(".tab")];
    let index = items.indexOf(document.activeElement);
    if (index < 0) index = Math.max(0, items.findIndex(node => node.getAttribute("aria-selected") === "true"));
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = items.length - 1;
    else index = (index + (event.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
    keepTabFocus = true;
    items[index].focus();
    items[index].click();
  });

  fill(root, header, stats, notices, el("section", {class: "doc-panel"},
    el("h2", {class: "sr-only", text: "Разделы документа"}),
    tabs, body,
  ));
  if (keepTabFocus) {
    keepTabFocus = false;
    const frame = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (task) => setTimeout(task, 0);
    frame(() => tabs.querySelector(".tab.active")?.focus());
  }
  try {
    await renderTab(body, ctx, summary, facets.data, facets.ok, active);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    fill(body, el("div", {class: "notice bad", role: "alert"},
      el("strong", {text: "Не удалось загрузить раздел"}),
      el("p", {text: error.message}),
      el("p", {}, el("button", {
        type: "button", class: "secondary", text: "Повторить",
        onclick: () => ctx.go({tab: active}),
      })),
    ));
  }
}

function metaLine(summary, publication) {
  const authors = Array.isArray(publication.authors) ? publication.authors.join(", ") : publication.authors || "";
  const counts = summary.counts || {};
  const run = summary.run || {};
  const parts = [
    publication.publication_id || summary.publication_id,
    publication.year || summary.year,
    summary.pages ? `${summary.pages} стр.` : "",
    authors,
    `${counts.results ?? 0} ${plural(counts.results ?? 0, "результат", "результата", "результатов")}`,
    `${counts.issues ?? 0} ${plural(counts.issues ?? 0, "замечание", "замечания", "замечаний")}`,
    run.duration_ms ? seconds(run.duration_ms) : "",
  ].filter(Boolean);
  const line = el("p", {class: "meta"});
  parts.forEach((part, index) => {
    if (index) line.append(" · ");
    line.append(part);
  });
  const doi = String(publication.doi || "").trim();
  if (/^10\./.test(doi)) {
    line.append(" · ", el("a", {
      href: `https://doi.org/${encodeURI(doi)}`,
      rel: "noopener", target: "_blank", text: `doi:${doi}`,
    }));
  }
  return line;
}

function scopeBadge(publication) {
  if (publication.in_scope === undefined || publication.in_scope === null) return null;
  const inScope = Boolean(publication.in_scope);
  return el("span", {
    class: `badge ${inScope ? "idle" : "warn"}`,
    title: publication.exclusion_reason || "",
    text: inScope ? "в области" : `вне области${publication.exclusion_reason ? `: ${publication.exclusion_reason}` : ""}`,
  });
}

const EXPORT_PATHS = {
  pdf: {path: "/pdf", label: "PDF", download: ""},
  json: {path: "/export.json", label: "JSON", download: "analysis.json"},
  csv: {path: "/export.csv", label: "CSV (область)", download: ""},
};

function exportLink(summary, base, kind, label, status) {
  if (status === "running" || status === "queued" || status === "failed") return null;
  const item = EXPORT_PATHS[kind];
  const remote = kind === "json" ? summary.export?.json : kind === "csv" ? summary.export?.csv : "";
  const href = typeof remote === "string" && remote.startsWith("/api/") ? remote : `${base}${item.path}`;
  const download = kind === "json" ? item.download
    : kind === "csv" ? `${summary.publication_id || "results"}.csv`
      : "";
  return el("a", {href, download, text: label});
}

function noticeBlock(summary) {
  const blocks = [];
  if (summary.status === "failed" && summary.error) {
    blocks.push(el("div", {class: "notice bad"},
      el("strong", {text: "Обработка не удалась"}),
      el("p", {text: summary.error}),
    ));
  }
  if (summary.status === "running") {
    blocks.push(el("div", {class: "notice warn"},
      el("strong", {text: "Идёт обработка"}),
      el("p", {text: "Результаты и экспорт появятся после завершения. Страницу можно обновить позже."}),
    ));
  }
  if (summary.status === "queued") {
    blocks.push(el("div", {class: "notice warn"},
      el("strong", {text: "Документ в очереди"}),
      el("p", {text: "Обработка ещё не начиналась: результатов и экспорта пока нет."}),
    ));
  }
  if (summary.status === "partial" && !(summary.scanned && !summary.ocr_used)) {
    blocks.push(el("div", {class: "notice warn"},
      el("strong", {text: "Обработано частично"}),
      el("p", {text: "Часть страниц не разобрана. Результаты ниже относятся только к прочитанным страницам."}),
    ));
  }
  if (summary.scanned && !summary.ocr_used) {
    blocks.push(el("div", {class: "notice warn"},
      el("strong", {text: "Скан без текстового слоя"}),
      el("p", {text: "Текст со страниц не извлекался, OCR не выполнялся. Результатов нет — это не отсутствие данных в статье."}),
    ));
  }
  if (Array.isArray(summary.warnings) && summary.warnings.length) {
    blocks.push(el("ul", {class: "warnings"}, summary.warnings.map(warning => el("li", {text: warning}))));
  }
  return blocks;
}

async function renderTab(body, ctx, summary, facets, facetsOk, tab) {
  if (tab !== "summary" && (summary.status === "queued" || summary.status === "running")) {
    fill(body, emptyState(
      "Документ ещё не обработан.",
      "Результаты, сущности и проверки появятся после завершения обработки; обновите страницу позже.",
    ));
    return;
  }
  if (tab !== "summary" && summary.status === "failed") {
    fill(body, emptyState("Данных нет.", "Обработка завершилась ошибкой; причина указана выше."));
    return;
  }
  if (tab === "entities") return renderEntities(body, ctx);
  if (tab === "issues") return renderIssues(body, ctx, summary);
  if (tab === "sources") return renderSources(body, ctx, summary);
  if (tab === "summary") return renderSummary(body, ctx, summary, facets);
  return renderResults(body, ctx, summary, facets, facetsOk);
}

function emptyState(message, detail, role) {
  return el("div", {class: "empty", role: role || null}, el("p", {text: message}), detail ? el("p", {class: "muted", text: detail}) : null);
}

/* ---------------------------------------------------------------- результаты */

const CELLS = {
  metric: (result) => el("td", {class: "metric"},
    el("strong", {text: result.metric_name}),
    result.metric_k ? el("span", {class: "cutoff", text: ` @${result.metric_k}`}) : null,
  ),
  value: (result, ctx) => {
    if (result.extraction_status !== "extracted") {
      return el("td", {class: "muted", text: "значение не извлечено"});
    }
    const unit = unitLabel(ctx.descriptor, result.metric_unit);
    const raw = [result.raw_value, result.raw_unit].filter(Boolean).join(" ");
    return el("td", {},
      el("strong", {text: number(result.metric_value)}),
      unit ? el("span", {class: "unit", text: ` ${unit}`}) : null,
      raw ? el("small", {class: "raw", text: ` · raw ${raw}`}) : null,
    );
  },
  config: (result) => el("td", {text: result.config_id || "—"}),
  dataset: (result) => el("td", {text: [result.dataset_id, result.task_id].filter(Boolean).join(" / ") || "—"}),
  group: (result) => el("td", {class: "group", text: result.metric_group || "—"}),
  status: (result) => el("td", {}, extractStatus(result.extraction_status)),
  check: (result, ctx) => {
    const issues = result.issues || [];
    if (!issues.length) return el("td", {class: "muted", text: "—"});
    return el("td", {class: "flag"}, issues.map(issue => issueLabel(ctx.descriptor, issue.check_name)).join(" · "));
  },
  source: (result, ctx) => sourceCell(result, ctx),
};

function cell(column, result, ctx) {
  const override = ctx.descriptor.cells && ctx.descriptor.cells[column.key];
  if (override) {
    const value = override(result, ctx);
    if (value instanceof HTMLElement) return value.tagName === "TD" ? value : el("td", {}, value);
    return el("td", {text: value === undefined || value === null || value === "" ? "—" : String(value)});
  }
  const renderer = CELLS[column.key];
  if (renderer) return renderer(result, ctx);
  const value = result[column.key];
  return el("td", {text: value === undefined || value === null || value === "" ? "—" : String(value)});
}

function evidenceAction(ctx, options) {
  return () => {
    openEvidence({...options, signal: ctx.signal}).catch(error => {
      if (error.name === "AbortError") return;
      ctx.say(`Не удалось открыть источник: ${error.message}`, true);
    });
  };
}

function sourceCell(result, ctx) {
  const buttons = [];
  if (result.source) {
    buttons.push(el("button", {
      type: "button", class: "secondary", text: `Стр. ${result.source.page}`,
      onclick: evidenceAction(ctx, {
        docId: ctx.docId, source: result.source, label: result.metric_name,
      }),
    }));
  }
  if (result.header_source) {
    buttons.push(el("button", {
      type: "button", class: "secondary", text: "Заголовок",
      onclick: evidenceAction(ctx, {
        docId: ctx.docId, source: result.header_source, label: "Заголовок таблицы",
      }),
    }));
  }
  return el("td", {class: "source-cell"}, buttons.length ? buttons : el("span", {class: "muted", text: "—"}));
}

function detailRow(result, ctx, columns) {
  const pairs = [
    ["Идентификатор", result.result_id],
    ["Сырое значение", [result.raw_value, result.raw_unit].filter(Boolean).join(" ") || "—"],
    ["Нормализовано", [number(result.normalized_value), unitLabel(ctx.descriptor, result.normalized_unit)].filter(Boolean).join(" ") || "—"],
    ["Происхождение", result.result_origin === "cited"
      ? `cited · ${result.cited_publication_id || "без ссылки"}`
      : "primary"],
    ["Агрегация", result.aggregation || "—"],
    ["Выборка", result.sample_size ? String(result.sample_size) : "—"],
    ["Дубль", result.duplicate_of || "—"],
    ["Заметка", result.note || "—"],
  ];
  const list = el("dl", {class: "detail-grid"});
  for (const [term, value] of pairs) list.append(el("dt", {text: term}), el("dd", {text: value}));

  const issues = (result.issues || []).length
    ? el("div", {class: "detail-issues"},
      el("p", {class: "detail-title", text: "Замечания"}),
      el("ul", {}, result.issues.map(issue => el("li", {},
        el("strong", {text: issueLabel(ctx.descriptor, issue.check_name)}),
        " — ",
        issue.description || issue.check_name,
      ))),
    )
    : null;

  const fragment = result.source && result.source.fragment
    ? el("p", {class: "fragment"},
      el("span", {class: "muted", text: "Цитата: "}),
      el("q", {text: result.source.fragment}),
    )
    : null;

  const row = el("tr", {class: "detail"},
    el("td", {colspan: columns.length + 1}, list, issues, fragment));
  row.hidden = true;
  return row;
}

async function renderResults(body, ctx, summary, facets, facetsOk) {
  const descriptor = ctx.descriptor;
  const params = ctx.params;
  const base = `/api/documents/${encodeURIComponent(ctx.docId)}`;
  const state = {
    q: params.q || "",
    metric: params.metric || "",
    k: params.k || "",
    dataset: params.dataset || "",
    config: params.config || "",
    status: params.status || "",
    issues: params.issues === "1",
    result: params.result || "",
    page: Math.max(1, Number(params.page) || 1),
  };

  const search = el("input", {
    type: "search", value: state.q, "aria-label": "Поиск по результатам",
    placeholder: descriptor.searchHint || "Метрика, значение, набор, цитата",
  });
  const issuesBox = el("input", {type: "checkbox"});
  issuesBox.checked = state.issues;

  const controls = [];
  for (const filter of descriptor.filters || []) {
    const options = facets[filter.facet] || [];
    if (!options.length) continue;
    const select = el("select", {"aria-label": filter.label},
      el("option", {value: "", text: filter.allLabel || "Все"}),
      ...options.map(option => el("option", {
        value: option.value,
        text: `${option.label}${option.count !== undefined ? ` (${option.count})` : ""}`,
      })),
    );
    select.value = state[filter.key] || "";
    select.addEventListener("change", () => {
      state[filter.key] = select.value;
      state.result = "";
      state.page = 1;
      load();
    });
    controls.push(el("label", {}, filter.label, select));
  }

  const head = el("thead", {}, el("tr", {},
    el("th", {scope: "col", class: "expander"}),
    ...descriptor.columns.map(column => el("th", {scope: "col", text: column.label})),
  ));
  const widths = {metric: 120, value: 190, config: 120, dataset: 130, group: 140, status: 105, check: 160, source: 180};
  const cols = el("colgroup", {},
    el("col", {style: "width: 28px"}),
    ...descriptor.columns.map(column => el("col", {style: `width: ${widths[column.key] || 120}px`})),
  );
  const rows = el("tbody");
  const count = el("p", {class: "count", "aria-live": "polite"});
  const pager = el("nav", {class: "bar pager", "aria-label": "Страницы результатов"});
  const placeholder = el("div", {class: "placeholder"});
  const box = el("div", {class: "results"}, el("table", {class: "results-table"},
    el("caption", {class: "sr-only", text: "Результаты извлечения"}),
    cols, head, rows,
  ));

  fill(body,
    el("div", {class: "bar filters"},
      el("label", {class: "grow"}, "Поиск", search),
      ...controls,
      el("label", {class: "check"}, issuesBox, " Только с замечаниями"),
    ),
    facetsOk ? null : el("p", {class: "notice warn", role: "status",
      text: "Фильтры по значениям недоступны: не удалось загрузить фасеты. Поиск и отметка «только с замечаниями» работают."}),
    count,
    placeholder,
    box,
    pager,
  );

  let timer = 0;
  let request = 0;
  let pageJump = false;
  ctx.signal.addEventListener("abort", () => clearTimeout(timer), {once: true});

  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.q = search.value.trim();
      state.result = "";
      state.page = 1;
      load();
    }, 250);
  });
  issuesBox.addEventListener("change", () => {
    state.issues = issuesBox.checked;
    state.result = "";
    state.page = 1;
    load();
  });

  async function load() {
    if (ctx.signal.aborted) return;
    const id = ++request;
    ctx.patch({
      q: state.q, metric: state.metric, k: state.k, dataset: state.dataset,
      config: state.config, status: state.status, issues: state.issues ? "1" : "",
      result: state.result,
      page: state.page > 1 ? state.page : "",
    });
    count.textContent = "Загрузка…";
    let data;
    try {
      data = await getJSON(`${base}/results`, {
        q: state.q, metric: state.metric, k: state.k, dataset: state.dataset,
        config: state.config, status: state.status, issues: state.issues ? "1" : "",
        limit: PAGE, offset: (state.page - 1) * PAGE,
      }, ctx.signal);
    } catch (error) {
      if (error.name === "AbortError" || id !== request || ctx.signal.aborted) return;
      box.hidden = true;
      fill(placeholder, el("div", {class: "empty", role: "alert"},
        el("p", {text: error.message}),
        el("p", {}, el("button", {
          type: "button", class: "secondary", text: "Повторить",
          onclick: () => load(),
        })),
      ));
      count.textContent = "";
      pager.hidden = true;
      fill(pager);
      return;
    }
    if (id !== request || ctx.signal.aborted) return;

    const pages = Math.max(1, Math.ceil(data.total / PAGE));
    if (state.page > pages) {
      state.page = pages;
      return load();
    }
    drawResultRows(data, rows, ctx, descriptor, state.result);
    fill(placeholder);
    box.hidden = data.total === 0;
    if (!data.total) {
      const hasResults = Boolean(summary.counts && summary.counts.results);
      const empty = emptyState(
        hasResults ? "По этому запросу ничего не найдено." : (descriptor.emptyHint || "Результатов нет."),
        hasResults ? "Измените фильтры или очистите поиск." : null,
        "status",
      );
      if (hasResults) {
        empty.append(el("button", {
          type: "button", class: "secondary", text: "Сбросить фильтры",
          onclick: () => ctx.go({
            tab: "results", q: "", metric: "", k: "", dataset: "", config: "",
            status: "", issues: "", result: "", page: "",
          }),
        }));
      }
      fill(placeholder, empty);
    }
    count.textContent = data.total
      ? `Найдено ${number(data.total)}${data.total > PAGE ? ` · страница ${state.page} из ${pages}` : ""}`
      : "Найдено 0";
    pager.hidden = pages <= 1;
    fill(pager,
      el("button", {
        type: "button", class: "secondary", text: "Назад",
        disabled: state.page <= 1 ? "" : null,
        onclick: () => { state.result = ""; state.page -= 1; pageJump = true; load(); },
      }),
      el("span", {text: `${state.page} / ${pages}`}),
      el("button", {
        type: "button", class: "secondary", text: "Далее",
        disabled: state.page >= pages ? "" : null,
        onclick: () => { state.result = ""; state.page += 1; pageJump = true; load(); },
      }),
    );
    if (pageJump) {
      pageJump = false;
      box.scrollTop = 0;
      if (typeof box.scrollIntoView === "function") box.scrollIntoView({block: "start", behavior: "smooth"});
      count.setAttribute("tabindex", "-1");
      try {
        count.focus({preventScroll: true});
      } catch {
        count.focus();
      }
    }
    const target = rows.querySelector("tr.target");
    if (target) {
      if (typeof target.scrollIntoView === "function") target.scrollIntoView({block: "center"});
      const expander = target.querySelector("button.ghost");
      if (expander) {
        try {
          expander.focus({preventScroll: true});
        } catch {
          expander.focus();
        }
      }
    }
  }

  await load();
}

function drawResultRows(data, rows, ctx, descriptor, targetId) {
  rows.replaceChildren();
  if (!data.items.length) return;
  for (const result of data.items) {
    const expander = el("button", {
      type: "button", class: "ghost", "aria-expanded": "false",
      "aria-label": `Подробности ${result.result_id}`, text: "▸",
    });
    const detail = detailRow(result, ctx, descriptor.columns);
    detail.id = `detail-${result.result_id}`;
    expander.setAttribute("aria-controls", detail.id);
    const row = el("tr", {class: "result"}, el("td", {class: "expander"}, expander));
    for (const column of descriptor.columns) row.append(cell(column, result, ctx));

    expander.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
      expander.setAttribute("aria-expanded", detail.hidden ? "false" : "true");
      expander.textContent = detail.hidden ? "▸" : "▾";
    });
    if (targetId && result.result_id === targetId) {
      row.classList.add("target");
      row.setAttribute("aria-current", "true");
      detail.hidden = false;
      expander.setAttribute("aria-expanded", "true");
      expander.textContent = "▾";
    }
    rows.append(row, detail);
  }
}

/* ------------------------------------------------------------------ сущности */

async function renderEntities(body, ctx) {
  const entities = ctx.descriptor.entities || [];
  if (!entities.length) {
    fill(body, emptyState("Для этой области сущности не описаны."));
    return;
  }
  const active = entities.find(item => item.key === ctx.params.entity) || entities[0];
  const select = el("select", {"aria-label": "Тип сущности"},
    entities.map(item => el("option", {value: item.key, text: item.label})));
  select.value = active.key;
  const wrap = el("div", {class: "results"});
  const count = el("p", {class: "count", "aria-live": "polite"});

  fill(body,
    el("div", {class: "bar filters"}, el("label", {}, "Тип", select)),
    count, wrap);
  select.addEventListener("change", () => ctx.go({tab: "entities", entity: select.value}));

  const data = await getJSON(
    `/api/documents/${encodeURIComponent(ctx.docId)}/entities`, {type: active.key}, ctx.signal);
  if (ctx.signal.aborted) return;
  count.textContent = `Записей: ${number(data.total)}`;
  if (!data.items.length) {
    fill(wrap, emptyState("В этой статье таких сущностей не найдено."));
    return;
  }
  const keys = active.columns ? active.columns.map(column => column.key) : Object.keys(data.items[0]);
  const labels = Object.fromEntries((active.columns || []).map(column => [column.key, column.label]));
  const links = ctx.descriptor.entityFilters || {};
  const linkKey = keys.find(key => links[key]);

  fill(wrap, el("table", {class: "entities-table"},
    el("caption", {class: "sr-only", text: `Сущности: ${active.label}`}),
    el("thead", {}, el("tr", {},
      keys.map(key => el("th", {scope: "col", text: labels[key] || fieldLabel(ctx.descriptor, key)})),
      linkKey ? el("th", {scope: "col", text: "Действие"}) : null,
    )),
    el("tbody", {}, data.items.map(item => el("tr", {},
      keys.map(key => {
        const value = item[key];
        return el("td", {text: Array.isArray(value) ? value.join(", ") : value === "" || value === null || value === undefined ? "—" : String(value)});
      }),
      linkKey ? el("td", {}, el("button", {
        type: "button", class: "link-button", text: "Показать результаты",
        "aria-label": `Показать результаты для ${item[linkKey]}`,
        onclick: () => ctx.go({
          tab: "results", result: "", page: "",
          q: "", metric: "", k: "", dataset: "", config: "", status: "", issues: "",
          [links[linkKey]]: item[linkKey],
        }),
      })) : null,
    ))),
  ));
}

/* ----------------------------------------------------------------- замечания */

async function renderIssues(body, ctx, summary) {
  const data = await getJSON(
    `/api/documents/${encodeURIComponent(ctx.docId)}/issues`, {limit: 200}, ctx.signal);
  if (ctx.signal.aborted) return;
  fill(body, el("p", {class: "count", text: `Замечаний: ${number(data.total)}`}));
  if (!data.items.length) {
    body.append(emptyState("Замечаний нет.", `Проверки пройдены для ${summary.counts?.results || 0} записей.`));
    return;
  }
  body.append(el("div", {class: "results"}, el("table", {class: "issues-table"},
    el("caption", {class: "sr-only", text: "Замечания проверок"}),
    el("thead", {}, el("tr", {},
      el("th", {scope: "col", text: "Код"}),
      el("th", {scope: "col", text: "Проверка"}),
      el("th", {scope: "col", text: "Причина"}),
      el("th", {scope: "col", text: "Записи"}),
    )),
    el("tbody", {}, data.items.map(issue => el("tr", {},
      el("td", {text: issue.issue_id}),
      el("td", {},
        el("strong", {text: issueLabel(ctx.descriptor, issue.check_name)}),
        el("div", {class: "muted", text: issue.check_name}),
      ),
      el("td", {text: issue.description || "—"}),
      el("td", {}, (issue.record_ids || []).map(id => el("button", {
        type: "button", class: "link-button", text: id,
        onclick: () => ctx.go({
          tab: "results", result: id, page: "",
          q: "", metric: "", k: "", dataset: "", config: "", status: "", issues: "",
        }),
      }))),
    ))),
  )));
}

/* ------------------------------------------------------------ фрагменты PDF */

async function renderSources(body, ctx, summary) {
  if (summary.scanned && !summary.ocr_used) {
    fill(body, emptyState("Текстового слоя нет.", "Скан не распознан, фрагменты недоступны."));
    return;
  }
  const kinds = ctx.descriptor.kinds || KIND_LABELS;
  const kind = ctx.params.kind || "";
  const search = el("input", {
    type: "search", value: ctx.params.sq || "", "aria-label": "Поиск по фрагментам",
    placeholder: "Цитата или число",
  });
  const select = el("select", {"aria-label": "Тип фрагмента"},
    el("option", {value: "", text: "Все типы"}),
    Object.entries(kinds).map(([value, label]) => el("option", {value, text: label})));
  select.value = kind;
  const wrap = el("div", {class: "results"});
  const count = el("p", {class: "count", "aria-live": "polite"});

  fill(body,
    el("div", {class: "bar filters"},
      el("label", {class: "grow"}, "Поиск", search),
      el("label", {}, "Тип", select)),
    count, wrap);

  let request = 0;
  let timer = 0;
  ctx.signal.addEventListener("abort", () => clearTimeout(timer), {once: true});

  const load = async () => {
    if (ctx.signal.aborted) return;
    const id = ++request;
    ctx.patch({kind: select.value, sq: search.value.trim()});
    const data = await getJSON(
      `/api/documents/${encodeURIComponent(ctx.docId)}/sources`,
      {kind: select.value, q: search.value.trim(), limit: 200}, ctx.signal);
    if (id !== request || ctx.signal.aborted) return;
    count.textContent = `Фрагментов: ${number(data.total)}`;
    if (!data.items.length) {
      fill(wrap, emptyState("Фрагменты не найдены."));
      return;
    }
    fill(wrap, el("table", {class: "sources-table"},
      el("caption", {class: "sr-only", text: "Фрагменты PDF"}),
      el("thead", {}, el("tr", {},
        el("th", {scope: "col", text: "Тип"}),
        el("th", {scope: "col", text: "Цитата"}),
        el("th", {scope: "col", text: "Источник"}),
      )),
      el("tbody", {}, data.items.map(item => el("tr", {},
        el("td", {text: kinds[item.kind] || item.kind}),
        el("td", {class: "quote", text: item.fragment || item.quote || ""}),
        el("td", {}, el("button", {
          type: "button", class: "secondary", text: `Стр. ${item.page}`,
          onclick: evidenceAction(ctx, {docId: ctx.docId, source: item}),
        })),
      ))),
    ));
  };

  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(load, 250);
  });
  select.addEventListener("change", load);
  await load();
}

/* -------------------------------------------------------------------- сводка */

function renderSummary(body, ctx, summary, facets) {
  const counts = summary.counts || {};
  const statuses = facets.statuses || [];
  const metrics = [...(facets.metrics || [])].sort((a, b) => b.count - a.count).slice(0, 10);
  const run = summary.run || {};

  const statusRows = statuses.map(item => el("tr", {},
    el("th", {scope: "row"}, extractStatus(item.value)),
    el("td", {text: String(item.count)}),
  ));
  const metricRows = metrics.map(item => el("tr", {},
    el("th", {scope: "row", text: item.label}),
    el("td", {text: String(item.count)}),
  ));

  fill(body,
    el("div", {class: "cards"},
      el("div", {class: "card"},
        el("p", {class: "muted", text: "Результатов"}),
        el("p", {class: "big", text: number(counts.results ?? 0)}),
      ),
      el("div", {class: "card"},
        el("p", {class: "muted", text: "Замечаний"}),
        el("p", {class: "big", text: number(counts.issues ?? 0)}),
      ),
      el("div", {class: "card"},
        el("p", {class: "muted", text: "Обработка"}),
        el("p", {class: "big", text: seconds(run.duration_ms)}),
      ),
    ),
    el("div", {class: "summary-grid"},
      el("section", {},
        el("h2", {text: "Статусы извлечения"}),
        statuses.length
          ? el("table", {class: "compact"}, el("tbody", {}, statusRows))
          : el("p", {class: "muted", text: "Нет данных."}),
      ),
      el("section", {},
        el("h2", {text: "Основные метрики"}),
        metrics.length
          ? el("table", {class: "compact"}, el("tbody", {}, metricRows))
          : el("p", {class: "muted", text: "Нет данных."}),
      ),
    ),
    el("h2", {text: "Прогон"}),
    el("dl", {class: "detail-grid"},
      el("dt", {text: "Обработчик"}), el("dd", {text: run.handler || "—"}),
      el("dt", {text: "Модель"}), el("dd", {text: run.model || "—"}),
      el("dt", {text: "Версия источника"}), el("dd", {text: String(run.source_revision ?? "—")}),
      el("dt", {text: "Страниц"}), el("dd", {text: String(summary.pages ?? "—")}),
    ),
  );
}
