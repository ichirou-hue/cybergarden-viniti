import { getJSON } from "./api.js";
import { el, fill } from "./dom.js";
import { docStatus, number } from "./ui.js";
import { askArticle, clearChat, isConfirmedSource, loadChatHistory } from "./chat-api.js";
import {
  chatComposer,
  emptyChat,
  messageNode,
  scrollChat,
} from "./chat.js";

const sessions = new Map();

function sessionFor(docId) {
  if (!sessions.has(docId)) {
    sessions.set(docId, {
      messages: [],
      sources: null,
      sourceError: "",
      mockSeen: false,
      historyLoaded: false,
    });
  }
  return sessions.get(docId);
}

function examplesFor(track) {
  if (track === "rag") {
    return [
      "Какие метрики и наборы данных использованы?",
      "Какие модели сравнивали в статье?",
      "Какие результаты подтверждены цитатами?",
    ];
  }
  return [
    "Какие основные результаты описаны?",
    "В каких условиях проводили измерения?",
    "Какие материалы и слои упомянуты?",
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

export async function renderDocumentChat(body, ctx, summary) {
  const base = `/api/documents/${encodeURIComponent(ctx.docId)}`;
  const publication = summary.publication || {};
  const title = publication.title || summary.title || summary.publication_id || ctx.docId;
  const session = sessionFor(ctx.docId);

  fill(body, el("div", {class: "empty", role: "status"},
    el("p", {text: "Загружаем цитаты статьи…"}),
  ));

  if (!session.sources || session.sourceError) {
    try {
      const data = await getJSON(`${base}/sources`, {limit: 500}, ctx.signal);
      session.sources = Array.isArray(data?.items) ? data.items : [];
      session.sourceError = "";
    } catch (error) {
      if (error.name === "AbortError" || ctx.signal.aborted) throw error;
      session.sources = [];
      session.sourceError = "Не удалось загрузить список цитат. Чат по статье может отвечать только после восстановления доступа к источникам.";
    }
  }
  if (ctx.signal.aborted) return;

  if (!session.messages.length && !session.historyLoaded) {
    const history = await loadChatHistory({
      mode: "document",
      documentId: ctx.docId,
      documents: [{
        document_id: ctx.docId,
        title,
        publication_id: publication.publication_id || summary.publication_id || "",
      }],
      scope: "article",
    }, ctx.signal);
    if (ctx.signal.aborted) return;
    session.historyLoaded = true;
    if (history.length && !session.messages.length) session.messages.push(...history);
  }

  const sources = session.sources || [];
  const confirmedSources = sources.filter(isConfirmedSource);
  let busy = false;
  let composer = null;
  const mockNote = el("div", {class: "notice warn", hidden: session.mockSeen ? null : ""},
    el("strong", {text: "Демо-режим"}),
    el("p", {text: "Серверный чат недоступен. Ответы собраны локальным поиском по цитатам, без модели."}),
  );

  const context = el("aside", {class: "panel chat-context", "aria-labelledby": "doc-chat-context-title"},
    el("header", {class: "panel-head"},
      el("div", {},
        el("p", {class: "eyebrow", text: "Активный контекст"}),
        el("h2", {id: "doc-chat-context-title", text: "Одна статья"}),
      ),
      el("span", {class: "badge idle", text: "1 статья"}),
    ),
    el("p", {class: "chat-context-title", text: title}),
    el("dl", {class: "detail-grid chat-context-grid"},
      el("dt", {text: "Публикация"}), el("dd", {text: publication.publication_id || summary.publication_id || ctx.docId}),
      el("dt", {text: "Год"}), el("dd", {text: publication.year || summary.year || "не указан"}),
      el("dt", {text: "Страниц"}), el("dd", {text: summary.pages ? String(summary.pages) : "не указано"}),
      el("dt", {text: "Подтверждённых фрагментов"}), el("dd", {text: number(confirmedSources.length)}),
      el("dt", {text: "Статус"}), el("dd", {}, docStatus(summary.status)),
    ),
    el("p", {class: "muted", text: "Ответы строятся по цитатам этой статьи; распознанный моделью текст помечается отдельно. Номер страницы открывает существующий просмотрщик PDF."}),
    sources.length && !confirmedSources.length ? el("p", {class: "notice warn",
      text: "В статье есть только распознанные блоки без подтверждённой текстовой цитаты. Они не используются как доказательство."}) : null,
    session.sourceError ? el("p", {class: "notice warn", text: session.sourceError}) : null,
    mockNote,
    el("div", {class: "chat-context-actions"},
      el("a", {class: "button secondary", href: `${base}/pdf#page=1`, target: "_blank", rel: "noopener", text: "Открыть PDF"}),
    ),
  );

  const log = el("div", {class: "chat-log", role: "log", "aria-live": "polite", "aria-label": "Чат по статье"});
  const status = el("p", {class: "chat-status", role: "status"});

  function drawLog() {
    if (!session.messages.length) {
      fill(log, emptyChat({
        title: "Спросите статью",
        detail: "Вопрос ищет подтверждение только в этой статье. Если цитаты нет, интерфейс так и скажет. "
          + "Если сервер чата недоступен, включается локальный демо-поиск с пометкой «демо».",
        examples: examplesFor(ctx.descriptor.track),
        onExample: text => {
          composer?.setValue(text);
          composer?.focus();
        },
      }));
      scrollChat(log);
      return;
    }
    fill(log, ...session.messages.map(message => messageNode(message, {
      docId: ctx.docId,
      signal: ctx.signal,
      notFoundTitle: "В статье не найдено подтверждения.",
      onRetry: retry,
      onError: messageText => ctx.say(messageText, true),
    })));
    scrollChat(log);
  }

  async function send(text, {retry: isRetry = false} = {}) {
    const question = String(text || "").trim();
    if (!question || busy) return;
    const history = historyOf(session);
    if (!isRetry) session.messages.push({role: "user", text: question});
    busy = true;
    composer?.setBusy(true);
    status.textContent = "";
    const pending = {
      role: "assistant", loading: true,
      text: "Ищу подтверждение в статье…",
    };
    session.messages.push(pending);
    drawLog();
    try {
      const answer = await askArticle({
        docId: ctx.docId,
        title,
        publicationId: publication.publication_id || summary.publication_id || "",
        question,
        history,
        sources,
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
      if (answer.mock) {
        session.mockSeen = true;
        mockNote.hidden = false;
      }
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
      busy = false;
      composer?.setBusy(false);
      drawLog();
    }
  }

  function retry(text) {
    const index = session.messages.findIndex(message => message.error && message.retry === text);
    if (index >= 0) session.messages.splice(index, 1);
    send(text, {retry: true});
  }

  function reset(focus = true) {
    session.messages.length = 0;
    status.textContent = "";
    clearChat({mode: "document", documentId: ctx.docId}).catch(() => {});
    drawLog();
    if (focus) composer?.focus();
  }

  composer = chatComposer({
    placeholder: "Например: какая эффективность подтверждена цитатой?",
    onSend: text => send(text),
  });

  const actions = el("div", {class: "chat-head-actions"},
    el("button", {type: "button", class: "secondary", text: "Очистить", title: "Удалить сообщения текущего диалога",
      onclick: () => reset(false)}),
    el("button", {type: "button", class: "secondary", text: "Новый диалог", title: "Начать диалог заново",
      onclick: () => reset(true)}),
  );

  fill(body,
    el("div", {class: "chat-layout doc-chat-layout"},
      el("section", {class: "panel chat-main", "aria-labelledby": "doc-chat-title"},
        el("header", {class: "panel-head chat-head"},
          el("div", {},
            el("p", {class: "eyebrow", text: "Чат по статье"}),
            el("h2", {id: "doc-chat-title", text: "Вопросы к источнику"}),
          ),
          actions,
        ),
        el("div", {class: "chat-context-bar"},
          el("span", {class: "badge idle", text: "Контекст" }),
          el("span", {class: "chat-context-chip", text: title}),
        ),
        log,
        status,
        composer.node,
        el("p", {class: "chat-disclaimer", text: "Интерфейс не подтверждает научную корректность связей и полноту извлечения. Проверяйте цитаты по PDF."}),
      ),
      context,
    ),
  );
  drawLog();
}
