import { el } from "./dom.js";
import { openEvidence } from "./evidence.js";

function pageLabel(citation) {
  return citation && Number.isFinite(Number(citation.page)) ? `стр. ${citation.page}` : "источник";
}

function openCitation(citation, options = {}) {
  const docId = citation.document_id || options.docId;
  const label = citation.title || options.label || "";
  const fail = message => {
    if (typeof options.onError === "function") options.onError(message);
  };
  if (!docId) {
    fail("У цитаты нет документа.");
    return;
  }
  // Отдельный разбор не входит в каталог, поэтому у него нет anchors:
  // сервер уже отдал готовую ссылку на PDF с номером страницы.
  if (docId.startsWith("parse:") && typeof citation.pdf_url === "string" && citation.pdf_url) {
    window.open(citation.pdf_url, "_blank", "noopener");
    return;
  }
  if (citation.source || citation.source_id !== null && citation.source_id !== undefined) {
    openEvidence({
      docId,
      source: citation.source,
      sourceId: citation.source_id,
      label,
      signal: options.signal,
    }).catch(error => {
      if (error.name === "AbortError") return;
      fail(`Не удалось открыть цитату: ${error.message}`);
    });
    return;
  }
  if (typeof citation.pdf_url === "string" && citation.pdf_url) {
    window.open(citation.pdf_url, "_blank", "noopener");
    return;
  }
  if (Number.isFinite(Number(citation.page)) && Number(citation.page) > 0) {
    window.open(`/api/documents/${encodeURIComponent(docId)}/pdf#page=${citation.page}`,
      "_blank", "noopener");
    return;
  }
  fail("У цитаты нет страницы и идентификатора источника.");
}

export function citationButton(citation, options = {}) {
  const quote = citation.quote || "";
  return el("button", {
    type: "button",
    class: "chat-citation",
    title: quote,
    "aria-label": `Открыть источник, ${pageLabel(citation)}`
      + (citation.provenance === "recognized" ? ", распознанный текст" : ""),
    onclick: () => openCitation(citation, options),
  },
    el("span", {class: "chat-cite-page", text: pageLabel(citation)}),
    el("span", {class: "chat-cite-quote", text: quote}),
    citation.provenance === "recognized"
      ? el("span", {class: "badge warn chat-cite-recognized", text: "распознано",
        title: "Текст получен распознаванием модели, а не из буквального слоя PDF"})
      : null,
  );
}

function groupCitations(citations) {
  const groups = new Map();
  for (const citation of citations) {
    const key = citation.document_id || citation.title || "document";
    if (!groups.has(key)) {
      groups.set(key, {title: citation.title, publication_id: citation.publication_id, items: []});
    }
    const group = groups.get(key);
    if (!group.title && citation.title) group.title = citation.title;
    if (!group.publication_id && citation.publication_id) group.publication_id = citation.publication_id;
    group.items.push(citation);
  }
  return [...groups.values()];
}

export function citationList(citations, options = {}) {
  const items = Array.isArray(citations) ? citations.filter(Boolean) : [];
  if (!items.length) return null;
  const groups = options.groupByDocument ? groupCitations(items) : [{items}];
  return el("div", {class: "chat-citations", "aria-label": "Подтверждающие цитаты"},
    ...groups.map(group => el("div", {class: "chat-citation-group"},
      group.title || group.publication_id
        ? el("p", {class: "chat-citation-doc"},
          el("strong", {text: group.publication_id || ""}),
          group.publication_id && group.title ? " · " : null,
          group.title || "")
        : null,
      ...group.items.map(citation => citationButton(citation, options)),
    )),
  );
}

export function contradictionBlock(contradictions, options = {}) {
  const items = Array.isArray(contradictions) ? contradictions.filter(item => item && item.citations?.length) : [];
  if (!items.length) return null;
  return el("section", {class: "chat-contradictions", "aria-label": "Возможные расхождения"},
    el("h3", {text: "Возможные расхождения в источниках"}),
    el("p", {class: "muted", text: "Сводка не выбирает одну версию: ниже обе цитаты и страницы."}),
    ...items.map(item => el("article", {class: "chat-contradiction"},
      el("p", {class: "chat-contradiction-metric", text: item.metric || "Показатель"}),
      item.note ? el("p", {class: "muted", text: item.note}) : null,
      el("div", {class: "chat-contradiction-sides"},
        ...item.citations.map(citation => el("div", {class: "chat-contradiction-side"},
          citation.title || citation.publication_id
            ? el("p", {class: "chat-citation-doc", text: [citation.publication_id, citation.title].filter(Boolean).join(" · ")})
            : null,
          citationButton(citation, options),
        )),
      ),
    )),
  );
}

export function modelBlock(text) {
  const value = String(text || "").trim();
  if (!value) return null;
  return el("section", {class: "chat-model", "aria-label": "Знания модели"},
    el("span", {class: "badge warn", text: "Знания модели"}),
    el("p", {class: "muted", text: "Это общий ответ модели, а не сведения из библиотеки."}),
    el("p", {class: "chat-answer", text: value}),
  );
}

export function messageNode(message, options = {}) {
  if (!message) return null;
  if (message.role === "user") {
    return el("div", {class: "chat-msg user"},
      el("p", {class: "chat-answer", text: message.text || ""}),
    );
  }
  if (message.loading) {
    return el("div", {class: "chat-msg assistant loading", role: "status"},
      el("span", {class: "chat-spinner", "aria-hidden": "true"}),
      el("span", {text: message.text || "Ищу подтверждение в источниках…"}),
    );
  }
  if (message.error) {
    return el("div", {class: "chat-msg assistant error", role: "alert"},
      el("strong", {text: "Не удалось получить ответ"}),
      el("p", {text: message.text || "Повторите запрос позже."}),
      message.retry && typeof options.onRetry === "function"
        ? el("button", {type: "button", class: "secondary", text: "Повторить",
          onclick: () => options.onRetry(message.retry)})
        : null,
    );
  }
  if (message.status === "not_found") {
    return el("div", {class: "chat-msg assistant not-found"},
      el("span", {class: "badge idle", text: "Подтверждение не найдено"}),
      el("p", {class: "chat-not-found-title", text: options.notFoundTitle || "В источниках не найдено подтверждения."}),
      el("p", {class: "muted", text: message.note || "Попробуйте назвать метрику, материал или условие так, как они напечатаны в статье."}),
      message.mock ? mockBadge() : null,
    );
  }
  const modelOnly = Boolean(message.model_note && !message.answer
    && !(Array.isArray(message.citations) && message.citations.length));
  const noCitations = !modelOnly && !(Array.isArray(message.citations) && message.citations.length);
  return el("div", {class: "chat-msg assistant"},
    el("div", {class: "chat-msg-head"},
      el("span", {class: "chat-role", text: modelOnly ? "Знания модели" : "Ответ по источникам"}),
      noCitations ? el("span", {class: "badge warn", text: "без цитат"}) : null,
      message.mock ? mockBadge() : null,
    ),
    message.answer ? el("p", {class: "chat-answer", text: message.answer}) : null,
    message.note && !message.mock ? el("p", {class: "muted", text: message.note}) : null,
    noCitations ? el("p", {class: "muted", text: "В ответе нет подтверждающих цитат. Проверьте его отдельно."}) : null,
    citationList(message.citations, {...options, groupByDocument: Boolean(options.groupByDocument)}),
    contradictionBlock(message.contradictions, options),
    message.model_note ? modelBlock(message.model_note) : null,
    message.mock ? el("p", {class: "chat-mock-note", text: message.note || ""}) : null,
  );
}

function mockBadge() {
  return el("span", {
    class: "badge warn", text: "демо",
    title: "Серверный чат недоступен. Ответ собран локальным поиском по цитатам без модели.",
  });
}

export function emptyChat({title, detail, examples = [], onExample, extra}) {
  return el("div", {class: "chat-empty"},
    el("p", {class: "chat-empty-title", text: title}),
    detail ? el("p", {class: "muted", text: detail}) : null,
    extra || null,
    examples.length
      ? el("div", {class: "chat-examples", "aria-label": "Примеры вопросов"},
        ...examples.map(text => el("button", {
          type: "button", class: "chat-example", text,
          onclick: () => onExample && onExample(text),
        })))
      : null,
  );
}

export function scrollChat(node) {
  if (!node) return;
  node.scrollTop = node.scrollHeight;
}

export function chatComposer({placeholder, disabled = false, onSend, label = "Отправить"}) {
  const input = el("textarea", {
    rows: "2", maxlength: "1200", placeholder,
    "aria-label": "Вопрос по источникам",
  });
  const button = el("button", {type: "submit", class: "primary", text: label, disabled: disabled || null});
  const form = el("form", {class: "chat-composer"}, input, button);
  let busy = false;
  let locked = disabled;

  function sync() {
    button.disabled = busy || locked;
    input.disabled = locked;
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy || locked) return;
    input.value = "";
    onSend(text);
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", {cancelable: true}));
    }
  });

  return {
    node: form,
    input,
    focus: () => input.focus(),
    setBusy: value => { busy = Boolean(value); sync(); },
    setDisabled: value => { locked = Boolean(value); sync(); },
    setValue: value => { input.value = value; },
  };
}
