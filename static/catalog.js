import { getJSON } from "./api.js";
import { el, fill } from "./dom.js";
import { docStatus, number, seconds } from "./ui.js";

const PAGE = 50;

const STATUS_OPTIONS = [
  ["", "Все статусы"],
  ["done", "обработано"],
  ["partial", "частично"],
  ["failed", "ошибка"],
  ["queued", "в очереди"],
  ["running", "в работе"],
];

const SORT_OPTIONS = [
  ["publication_id", "По публикации"],
  ["status", "По статусу"],
  ["issues", "По замечаниям"],
  ["results", "По результатам"],
];

export async function renderCatalog(root, ctx) {
  const {descriptor, params} = ctx;
  document.title = "Каталог публикаций · ВИНИТИ";
  const state = {
    q: params.q || "",
    status: params.status || "",
    sort: params.sort || "publication_id",
    page: Math.max(1, Number(params.page) || 1),
  };

  const search = el("input", {
    type: "search", value: state.q, "aria-label": "Поиск публикации",
    placeholder: "Название или идентификатор",
  });
  const status = el("select", {"aria-label": "Статус обработки"}, STATUS_OPTIONS.map(([value, text]) => el("option", {value, text})));
  const sort = el("select", {"aria-label": "Сортировка"}, SORT_OPTIONS.map(([value, text]) => el("option", {value, text})));
  status.value = state.status;
  sort.value = state.sort;

  const rows = el("tbody");
  const count = el("p", {class: "count", "aria-live": "polite"});
  const pager = el("nav", {class: "bar pager", "aria-label": "Страницы каталога"});
  const reset = el("button", {type: "button", class: "secondary", text: "Сбросить фильтры"});

  const table = el("table", {class: "catalog-table"},
    el("caption", {class: "sr-only", text: "Публикации выбранной области"}),
    el("thead", {}, el("tr", {},
      el("th", {scope: "col", class: "wide", text: "Публикация"}),
      el("th", {scope: "col", text: "Год"}),
      el("th", {scope: "col", text: "Страниц"}),
      el("th", {scope: "col", text: "Результатов"}),
      el("th", {scope: "col", text: "Замечаний"}),
      el("th", {scope: "col", text: "Статус"}),
      el("th", {scope: "col", text: "Время"}),
      el("th", {scope: "col", text: ""}),
    )),
    rows,
  );
  const box = el("div", {class: "results"}, table);
  const placeholder = el("div", {class: "placeholder"});

  const statTotal = el("strong", {text: "—"});
  const statDone = el("strong", {text: "—"});
  const statReview = el("strong", {text: "—"});

  fill(root,
    el("section", {class: "hero"},
      el("div", {},
        el("h1", {}, "Каталог", el("br"), "публикаций", el("span", {class: "dot", text: "."})),
        el("p", {class: "lede", text: `${descriptor.note || "Публикации предметной области"}. `
          + "Загрузите PDF или откройте демо, чтобы увидеть результаты и доказательства."}),
      ),
    ),
    el("section", {class: "stats", "aria-label": "Сводка прогона"},
      el("div", {}, el("span", {text: "В прогоне"}), statTotal),
      el("div", {}, el("span", {text: "Обработано"}), statDone),
      el("div", {}, el("span", {text: "Требуют разбора"}), statReview),
    ),
    el("section", {class: "panel catalog-panel"},
      el("header", {class: "panel-head"},
        el("div", {},
          el("p", {class: "eyebrow", text: "Документы"}),
          el("h2", {text: "Публикации"}),
        ),
      ),
      el("div", {class: "bar filters"},
        el("label", {class: "grow"}, "Поиск", search),
        el("label", {}, "Статус", status),
        el("label", {}, "Сортировка", sort),
        reset,
      ),
      count,
      placeholder,
      box,
      pager,
    ),
  );

  let timer = 0;
  let request = 0;

  ctx.signal.addEventListener("abort", () => clearTimeout(timer), {once: true});

  reset.addEventListener("click", () => {
    state.q = "";
    state.status = "";
    state.sort = "publication_id";
    state.page = 1;
    search.value = "";
    status.value = "";
    sort.value = "publication_id";
    load();
  });

  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.q = search.value.trim();
      state.page = 1;
      load();
    }, 250);
  });
  status.addEventListener("change", () => {
    state.status = status.value;
    state.page = 1;
    load();
  });
  sort.addEventListener("change", () => {
    state.sort = sort.value;
    state.page = 1;
    load();
  });

  getJSON("/api/jobs", {track: descriptor.track}, ctx.signal).then(data => {
    if (!data || ctx.signal.aborted) return;
    statTotal.textContent = number(data.total);
    statDone.textContent = number(data.done);
    statReview.textContent = number((Number(data.failed) || 0) + (Number(data.partial) || 0));
  }).catch(error => {
    // Сводка необязательна: без неё счётчики остаются с прочерками.
    if (error.name !== "AbortError" && !ctx.signal.aborted) {
      console.warn("Не удалось получить счётчики прогона:", error.message);
    }
  });

  async function load() {
    if (ctx.signal.aborted) return;
    const id = ++request;
    ctx.patch({
      q: state.q, status: state.status,
      sort: state.sort === "publication_id" ? "" : state.sort,
      page: state.page > 1 ? state.page : "",
    });
    reset.hidden = !(state.q || state.status || state.sort !== "publication_id");
    count.textContent = "Загрузка…";
    let data;
    try {
      data = await getJSON("/api/documents", {
        track: descriptor.track, q: state.q, status: state.status, sort: state.sort,
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
    drawRows(data, rows, ctx, descriptor);
    box.hidden = data.total === 0;
    fill(placeholder);
    if (!data.total) {
      fill(placeholder, emptyState(ctx));
    }
    count.textContent = data.total
      ? `Публикаций: ${number(data.total)}${data.total > PAGE ? ` · страница ${state.page} из ${pages}` : ""}`
      : "Публикаций: 0";
    pager.hidden = pages <= 1;
    fill(pager,
      el("button", {
        type: "button", class: "secondary", text: "Назад",
        disabled: state.page <= 1 ? "" : null,
        onclick: () => { state.page -= 1; load(); },
      }),
      el("span", {text: `${state.page} / ${pages}`}),
      el("button", {
        type: "button", class: "secondary", text: "Далее",
        disabled: state.page >= pages ? "" : null,
        onclick: () => { state.page += 1; load(); },
      }),
    );
  }

  await load();
}

function emptyState(ctx) {
  return el("div", {class: "empty", role: "status"},
    el("p", {text: "Ничего не найдено."}),
    el("p", {class: "muted", text: "Измените запрос, уберите фильтр или начните с демо-статьи."}),
    el("div", {class: "bar center"},
      el("button", {
        type: "button", class: "secondary", text: "Загрузить PDF",
        onclick: () => document.getElementById("startWizard")?.click(),
      }),
      el("button", {
        type: "button", class: "secondary", text: "Открыть демо",
        onclick: () => document.getElementById("demo")?.click(),
      }),
      el("button", {
        type: "button", class: "ghost", text: "Сбросить фильтры",
        onclick: () => {
          const track = ctx.descriptor.track;
          location.hash = `#/catalog?track=${encodeURIComponent(track)}`;
        },
      }),
    ),
  );
}

function drawRows(data, rows, ctx, descriptor) {
  rows.replaceChildren();
  for (const doc of data.items) {
    const link = `#/doc/${encodeURIComponent(doc.document_id)}?track=${encodeURIComponent(descriptor.track)}`;
    rows.append(el("tr", {},
      el("td", {}, el("a", {class: "pub", href: link},
        el("strong", {text: doc.publication_id}),
        el("span", {class: "pub-title", text: doc.title}),
      )),
      el("td", {class: "year", text: doc.year || "—"}),
      el("td", {text: doc.pages || "—"}),
      el("td", {text: number(doc.results_count ?? 0)}),
      el("td", {class: doc.issues_count ? "flag" : "", text: number(doc.issues_count ?? 0)}),
      el("td", {}, docStatus(doc.status), doc.error ? el("span", {class: "error-hint", text: doc.error}) : null),
      el("td", {class: "time", text: seconds(doc.duration_ms)}),
      el("td", {class: "actions"},
        doc.status === "done" || doc.status === "partial"
          ? el("a", {href: `/api/documents/${encodeURIComponent(doc.document_id)}/export.csv`, download: `${doc.publication_id}.csv`, text: "CSV"})
          : null,
        el("a", {href: link, text: "Открыть"}),
      ),
    ));
  }
}
