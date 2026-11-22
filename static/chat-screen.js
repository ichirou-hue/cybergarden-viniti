import { getJSON } from "./api.js";
import { el, fill } from "./dom.js";
import { docStatus, number, plural } from "./ui.js";
import { askArticle, askLibrary, clearChat, loadChatHistory } from "./chat-api.js";
import {
  chatComposer,
  emptyChat,
  messageNode,
  scrollChat,
} from "./chat.js";

const SCOPES = [
  ["single", "Одна статья", "Ответы только по одной публикации."],
  ["multi", "Несколько статей", "Сравнение выбранных публикаций и источников."],
  ["library", "Вся библиотека", "Поиск по всем доступным публикациям."],
];

const sessions = new Map();

function keyOf(scope, ids) {
  if (scope === "library") return "library|all";
  return `${scope}|${[...ids].sort().join(",")}`;
}

function sessionFor(key) {
  if (!sessions.has(key)) sessions.set(key, {messages: [], mockSeen: false});
  return sessions.get(key);
}

function titleOf(doc) {
  return doc.title || doc.publication_id || doc.document_id;
}

function trackLabel(descriptors, track) {
  const found = descriptors.find(item => item.track === track);
  return found?.label || track || "";
}

function examplesFor(scope) {
  if (scope === "library") {
    return [
      "Что в библиотеке сказано про эффективность?",
      "Какие методы упоминаются в статьях?",
      "Сравни выводы по стабильности.",
    ];
  }
  if (scope === "multi") {
    return [
      "Какие результаты подтверждены цитатами?",
      "Есть ли расхождения между выбранными статьями?",
      "Какие метрики и условия сравнивают?",
    ];
  }
  return [
    "Какие основные результаты описаны?",
    "В каких условиях проводили измерения?",
    "Какие материалы и методы упомянуты?",
  ];
}

function historyOf(session) {
  return session.messages
    .filter(message => message.role === "user" || message.answer)
    .slice(-8)
    .map(message => ({
      role: message.role,
      content: message.role === "user" ? message.text : message.answer,
    }));
}

async function loadDocuments(ctx) {
  const tracks = ctx.descriptors.map(item => item.track);
  const results = await Promise.allSettled(tracks.map(track => getJSON(
    "/api/documents", {track, limit: 200}, ctx.signal,
  )));
  const aborted = results.find(result => result.status === "rejected"
    && result.reason && result.reason.name === "AbortError");
  if (aborted || ctx.signal.aborted) throw aborted?.reason || new DOMException("Отменено", "AbortError");
  const documents = [];
  const failed = [];
  let total = 0;
  results.forEach((result, index) => {
    const track = tracks[index];
    if (result.status === "rejected") {
      failed.push(trackLabel(ctx.descriptors, track));
      return;
    }
    const response = result.value || {};
    const items = Array.isArray(response.items) ? response.items : [];
    total += Number.isFinite(Number(response.total)) ? Number(response.total) : items.length;
    for (const item of items) {
      if (!item || !item.document_id) continue;
      documents.push({...item, track: item.track || track});
    }
  });
  if (!documents.length && failed.length === tracks.length) {
    throw new Error("Не удалось загрузить библиотеку. Проверьте соединение и повторите.");
  }
  documents.sort((a, b) => String(a.publication_id || "").localeCompare(String(b.publication_id || ""), "ru"));
  return {documents, failed, total: Math.max(total, documents.length)};
}

export async function renderChatScreen(root, ctx) {
  document.title = "Чат по библиотеке · ВИНИТИ";
  const hero = el("section", {class: "hero chat-hero"},
    el("div", {},
      el("p", {class: "eyebrow", text: "Научный чат"}),
      el("h1", {}, "Чат по", el("br"), "библиотеке"),
      el("p", {class: "lede", text: "Одна статья, несколько выбранных публикаций или вся библиотека. "
        + "Ответы по статьям показывают цитаты и страницы, а неподтверждённые утверждения помечаются отдельно."}),
    ),
  );
  fill(root, hero);

  let loaded;
  try {
    loaded = await loadDocuments(ctx);
  } catch (error) {
    if (error.name === "AbortError" || ctx.signal.aborted) throw error;
    fill(root, el("div", {class: "notice bad", role: "alert"},
      el("strong", {text: "Не удалось загрузить библиотеку"}),
      el("p", {text: error.message}),
      el("p", {}, el("button", {type: "button", class: "secondary", text: "Повторить",
        onclick: () => { if (ctx.reload) ctx.reload(); else location.reload(); }})),
    ));
    return;
  }
  if (ctx.signal.aborted) return;

  const {documents, failed, total} = loaded;
  if (!documents.length) {
    fill(root, hero, el("div", {class: "notice warn"},
      el("strong", {text: "Библиотека пуста"}),
      el("p", {text: "Загрузите PDF или откройте демо, чтобы начать чат."}),
      el("p", {}, el("a", {href: "#/", text: "Загрузить PDF"})),
    ));
    return;
  }
  const byId = new Map(documents.map(doc => [doc.document_id, doc]));
  const initialScope = ["single", "multi", "library"].includes(ctx.params.scope)
    ? ctx.params.scope : "single";
  const initialDocs = String(ctx.params.docs || "")
    .split(",").map(value => value.trim()).filter(value => byId.has(value));
  const state = {
    scope: initialScope,
    selected: new Set(initialDocs),
    query: ctx.params.q || "",
    busy: false,
  };
  if (state.scope === "single" && state.selected.size > 1) {
    state.selected = new Set([initialDocs[0]]);
  }

  let composer = null;
  let log = null;
  let status = null;
  let listNode = null;
  let countNode = null;
  let contextNode = null;
  let search = null;

  function saveState() {
    ctx.patch({
      scope: state.scope === "single" ? "" : state.scope,
      docs: state.scope === "library" ? "" : [...state.selected].join(","),
      q: state.query,
    });
  }

  function selectedDocs() {
    if (state.scope === "library") return documents;
    return documents.filter(doc => state.selected.has(doc.document_id));
  }

  function currentKey() {
    return keyOf(state.scope, state.selected);
  }

  function currentSession() {
    return sessionFor(currentKey());
  }

  const hydrated = new Set();
  async function hydrateSession() {
    const key = currentKey();
    const session = currentSession();
    if (session.messages.length || hydrated.has(key)) return;
    hydrated.add(key);
    const chosen = selectedDocs();
    const ids = chosen.map(doc => doc.document_id);
    // Mirror send(): a "multi" context with one document is a document chat.
    const mode = state.scope === "library" ? "library"
      : ids.length === 1 ? "document" : "selection";
    try {
      const history = await loadChatHistory({
        mode,
        documentId: mode === "document" ? ids[0] : undefined,
        documentIds: mode === "selection" ? ids : undefined,
        documents: chosen,
        scope: "library",
      }, ctx.signal);
      if (ctx.signal.aborted || session.messages.length || !history.length) return;
      session.messages.push(...history);
      drawLog();
      drawContext();
    } catch (error) {
      // Leaving the screen aborts the optional history request.
      if (error.name === "AbortError") return;
    }
  }

  function drawContext() {
    const session = currentSession();
    const scopeLabel = SCOPES.find(([key]) => key === state.scope)?.[1] || "Контекст";
    let body;
    if (state.scope === "library") {
      body = [
        el("p", {class: "chat-context-title", text: "Вся библиотека"}),
        el("p", {class: "muted", text: total > documents.length
          ? `Загружено ${number(documents.length)} из ${number(total)} публикаций. Серверный чат ищет по библиотеке целиком, но берёт в контекст не больше 12 статей.`
          : `${number(total)} ${plural(total, "публикация", "публикации", "публикаций")} в доступной библиотеке. Серверный чат берёт в контекст не больше 12 статей.`}),
        session.mockSeen ? el("p", {class: "muted", text: "Демо-поиск проверяет до 6 статей по совпадению слов и честно указывает их в ответе."}) : null,
      ];
    } else {
      const chosen = selectedDocs();
      if (!chosen.length) {
        body = [el("p", {class: "chat-context-title", text: "Контекст не выбран"}),
          el("p", {class: "muted", text: state.scope === "single"
            ? "Выберите статью в списке слева."
            : "Отметьте одну или несколько статей слева."})];
      } else {
        const shown = chosen.slice(0, 3);
        body = [
          el("p", {class: "chat-context-title", text: state.scope === "single" ? titleOf(chosen[0]) : `Выбрано статей: ${chosen.length}`}),
          el("div", {class: "chat-context-chips"},
            ...shown.map(doc => el("span", {class: "chat-context-chip", title: titleOf(doc), text: titleOf(doc)})),
            chosen.length > shown.length
              ? el("span", {class: "chat-context-chip", text: `+${chosen.length - shown.length}`})
              : null,
          ),
        ];
      }
    }
    fill(contextNode,
      el("div", {class: "chat-context-label"},
        el("span", {class: "badge idle", text: scopeLabel}),
        session.mockSeen ? el("span", {class: "badge warn", text: "демо", title: "Серверный чат недоступен"}) : null,
      ),
      ...body,
    );
  }

  function drawLog() {
    const session = currentSession();
    if (!session.messages.length) {
      fill(log, emptyChat({
        title: state.scope === "library" ? "Спросите библиотеку" : "Спросите статьи",
        detail: state.scope === "library"
          ? "Ответ будет ссылаться на статьи и цитаты. Если подтверждения нет, интерфейс так и скажет. "
            + "При недоступном сервере чата включается локальный демо-поиск с пометкой «демо»."
          : "Ответы по статьям показывают подтверждающие цитаты с номерами страниц. Если подтверждения нет, интерфейс так и скажет. "
            + "При недоступном сервере чата включается локальный демо-поиск с пометкой «демо».",
        examples: examplesFor(state.scope),
        onExample: text => {
          composer?.setValue(text);
          composer?.focus();
        },
      }));
      scrollChat(log);
      return;
    }
    fill(log, ...session.messages.map(message => messageNode(message, {
      signal: ctx.signal,
      groupByDocument: state.scope !== "single",
      onRetry: retry,
      onError: messageText => { status.textContent = messageText; },
    })));
    scrollChat(log);
  }

  function drawMain() {
    drawContext();
    drawLog();
    composer?.setDisabled(selectedDocs().length === 0);
    if (composer) {
      composer.input.placeholder = state.scope === "library"
        ? "Спросите по всей библиотеке"
        : "Спросите по выбранным статьям";
    }
  }

  function drawList() {
    const term = state.query.trim().toLocaleLowerCase();
    const visible = documents.filter(doc => !term
      || [doc.title, doc.publication_id, doc.document_id, doc.year, doc.authors]
        .filter(Boolean).join(" ").toLocaleLowerCase().includes(term));
    countNode.textContent = visible.length === documents.length
      ? `Публикаций: ${number(documents.length)}`
      : `Найдено: ${number(visible.length)} из ${number(documents.length)}`;
    if (!visible.length) {
      fill(listNode, el("p", {class: "muted chat-doc-empty", text: "Публикации не найдены. Измените запрос."}));
      return;
    }
    const disabled = state.scope === "library";
    fill(listNode, ...visible.map(doc => {
      const checked = state.scope !== "library" && state.selected.has(doc.document_id);
      const input = el("input", {
        type: state.scope === "multi" ? "checkbox" : "radio",
        name: "chat-doc",
        checked: checked || null,
        disabled: disabled || null,
      });
      input.addEventListener("change", () => {
        if (state.scope === "single") {
          state.selected = new Set([doc.document_id]);
        } else if (state.scope === "multi") {
          if (input.checked) state.selected.add(doc.document_id);
          else state.selected.delete(doc.document_id);
        }
        saveState();
        drawList();
        drawMain();
        hydrateSession();
      });
      return el("label", {class: "chat-doc" + (checked ? " selected" : "") + (disabled ? " disabled" : "")},
        input,
        el("span", {class: "chat-doc-text"},
          el("strong", {text: titleOf(doc)}),
          el("small", {}, doc.publication_id || doc.document_id, " · ", trackLabel(ctx.descriptors, doc.track),
            doc.year ? ` · ${doc.year}` : "", " ", docStatus(doc.status)),
        ),
      );
    }));
  }

  function setScope(scope) {
    if (scope === state.scope) return;
    state.scope = scope;
    if (scope === "single" && state.selected.size > 1) {
      state.selected = new Set([[...state.selected][0]]);
    }
    saveState();
    for (const button of scopeBar.querySelectorAll("[data-scope]")) {
      button.setAttribute("aria-pressed", String(button.dataset.scope === scope));
    }
    listNode.hidden = scope === "library";
    if (search) {
      search.disabled = scope === "library";
      const searchLabel = search.closest("label");
      if (searchLabel) searchLabel.hidden = scope === "library";
    }
    drawList();
    drawMain();
    hydrateSession();
  }

  async function send(text, {retry: isRetry = false} = {}) {
    const question = String(text || "").trim();
    if (!question || state.busy) return;
    const docs = selectedDocs();
    if (!docs.length) {
      status.textContent = "Сначала выберите статью или включите всю библиотеку.";
      return;
    }
    const session = currentSession();
    const history = historyOf(session);
    if (!isRetry) session.messages.push({role: "user", text: question});
    state.busy = true;
    composer?.setBusy(true);
    status.textContent = "";
    const pending = {
      role: "assistant", loading: true,
      text: state.scope === "library"
        ? "Ищу подтверждение в библиотеке…"
        : docs.length > 1 ? `Ищу подтверждение в ${docs.length} статьях…` : "Ищу подтверждение в статье…",
    };
    session.messages.push(pending);
    drawLog();
    try {
      const answer = docs.length === 1 && state.scope !== "library"
        ? await askArticle({
          docId: docs[0].document_id,
          title: titleOf(docs[0]),
          publicationId: docs[0].publication_id,
          question,
          history,
          signal: ctx.signal,
        })
        : await askLibrary({
          documents: docs,
          question,
          history,
          scope: state.scope === "library" ? "all" : "documents",
          signal: ctx.signal,
        });
      if (ctx.signal.aborted) {
        Object.assign(pending, {
          loading: false, error: true,
          text: "Запрос прерван. Вернитесь в чат и повторите вопрос.",
          retry: question,
        });
        return;
      }
      Object.assign(pending, answer);
      delete pending.loading;
      if (answer.mock) session.mockSeen = true;
    } catch (error) {
      if (error.name === "AbortError" || ctx.signal.aborted) {
        Object.assign(pending, {
          loading: false, error: true,
          text: "Запрос прерван. Вернитесь в чат и повторите вопрос.",
          retry: question,
        });
        return;
      }
      Object.assign(pending, {
        loading: false, error: true,
        text: error.message,
        retry: question,
      });
      status.textContent = error.message;
    } finally {
      state.busy = false;
      composer?.setBusy(false);
      drawContext();
      drawLog();
    }
  }

  function retry(text) {
    const session = currentSession();
    const index = session.messages.findIndex(message => message.error && message.retry === text);
    if (index >= 0) session.messages.splice(index, 1);
    send(text, {retry: true});
  }

  function reset(focus = true) {
    const session = currentSession();
    session.messages.length = 0;
    status.textContent = "";
    const ids = selectedDocs().map(doc => doc.document_id);
    if (state.scope === "library") {
      clearChat({mode: "library"}).catch(() => {});
    } else if (ids.length > 1) {
      clearChat({mode: "selection", documentIds: ids}).catch(() => {});
    } else if (ids.length === 1) {
      clearChat({mode: "document", documentId: ids[0]}).catch(() => {});
    }
    drawLog();
    if (focus) composer?.focus();
  }

  const scopeBar = el("div", {class: "chat-scope", role: "group", "aria-label": "Контекст чата"},
    ...SCOPES.map(([key, label, hint]) => el("button", {
      type: "button", class: "chat-scope-option", "data-scope": key,
      "aria-pressed": String(state.scope === key),
      title: hint, text: label,
      onclick: () => setScope(key),
    })),
  );
  search = el("input", {
    type: "search", value: state.query, "aria-label": "Поиск статьи",
    placeholder: "Название, автор или идентификатор",
  });
  search.addEventListener("input", () => {
    state.query = search.value;
    saveState();
    drawList();
  });
  countNode = el("p", {class: "count", "aria-live": "polite"});
  listNode = el("div", {class: "chat-doc-list", role: "group", "aria-label": "Публикации для чата"});
  const searchLabel = el("label", {class: "chat-doc-search"},
    el("span", {class: "sr-only", text: "Поиск статьи"}), search);
  if (state.scope === "library") {
    listNode.hidden = true;
    search.disabled = true;
    searchLabel.hidden = true;
  }

  const sidebar = el("aside", {class: "panel chat-sidebar", "aria-labelledby": "chat-docs-title"},
    el("header", {class: "panel-head"},
      el("div", {},
        el("p", {class: "eyebrow", text: "Контекст"}),
        el("h2", {id: "chat-docs-title", text: "Публикации"}),
      ),
    ),
    scopeBar,
    failed.length ? el("p", {class: "notice warn", text: `Не удалось загрузить область: ${failed.join(", ")}. Остальные публикации доступны.`}) : null,
    total > documents.length ? el("p", {class: "notice warn", text: `Показаны первые ${number(documents.length)} из ${number(total)} публикаций.`}) : null,
    searchLabel,
    countNode,
    listNode,
  );

  contextNode = el("div", {class: "chat-context", "aria-live": "polite"});
  log = el("div", {class: "chat-log", role: "log", "aria-live": "polite", "aria-label": "Сообщения чата"});
  status = el("p", {class: "chat-status", role: "status"});

  const headActions = el("div", {class: "chat-head-actions"},
    el("button", {type: "button", class: "secondary", text: "Очистить", title: "Удалить сообщения текущего диалога",
      onclick: () => reset(false)}),
    el("button", {type: "button", class: "secondary", text: "Новый диалог", title: "Начать диалог заново",
      onclick: () => reset(true)}),
  );

  composer = chatComposer({
    placeholder: state.scope === "library"
      ? "Спросите по всей библиотеке"
      : "Спросите по выбранным статьям",
    onSend: text => send(text),
  });

  const main = el("section", {class: "panel chat-main", "aria-labelledby": "chat-main-title"},
    el("header", {class: "panel-head chat-head"},
      el("div", {},
        el("p", {class: "eyebrow", text: "Диалог"}),
        el("h2", {id: "chat-main-title", text: "Научный чат"}),
      ),
      headActions,
    ),
    contextNode,
    log,
    status,
    composer.node,
    el("p", {class: "chat-disclaimer", text: "Ответы по статьям должны опираться на проверенные цитаты. Неподтверждённые утверждения помечаются отдельно."}),
  );

  fill(root,
    hero,
    el("div", {class: "chat-layout chat-screen"},
      sidebar,
      main,
    ),
  );

  drawList();
  drawMain();
  hydrateSession();
}
