import { deleteJSON, getJSON, postJSON } from "./api.js";

// Чат идёт через серверный маршрут POST /api/chat с режимами document,
// selection и library. Сервер хранит историю диалога и принимает только
// буквальные цитаты из источников, поэтому клиент не собирает ответ сам.
// Если старый сервер ещё не знает маршрут (404 «Маршрут не найден», 405
// или 501), включается локальный демо-поиск по подтверждённым цитатам.
// Он не вызывает модель и не придумывает источники: каждая ссылка берётся
// из /api/documents/<id>/sources. Остальные ошибки (401, 403, 404 документа,
// 502, 503) показываются пользователю, а не подменяются ответом.
//
// Ответ сервера: {answer, citations: [{ref, quote, page, document_id,
// source_id, pdf_url, anchor_url}], not_found, unverified,
// contradictions: [{topic, refs, explanation}],
// context: {label, documents, source_count, truncated}, general_knowledge}.
const MISSING_ENDPOINT = new Set([405, 501]);
const MAX_ARTICLE_SOURCES = 4;
const MAX_LIBRARY_SOURCES = 8;
const MAX_LIBRARY_DOCUMENTS = 6;
const QUOTE_LIMIT = 900;

const STOPWORDS = new Set((
  "а и но или что как где когда какой какая какие какие-то этот эта эти то "
  + "в во на по из от до для при о об обо не ни же ли бы мы вы я ты он она оно они "
  + "есть был была были быть можно нужно ли это его её их всех весь вся все "
  + "the a an and or of in on for to from with by is are was were be been "
  + "what which how where when this that these those it its as at"
).split(/\s+/).filter(Boolean));

// Русский вопрос ищет и русские, и английские формулировки статьи.
// Это только расширение поиска: ответ по-прежнему состоит из цитат.
const SYNONYMS = {
  "эффективность": ["efficiency", "pce"],
  "кпд": ["pce", "efficiency"],
  "напряжение": ["voltage", "voc"],
  "ток": ["current", "jsc"],
  "плотность": ["density"],
  "фактор": ["factor", "ff"],
  "заполнения": ["fill", "ff"],
  "стабильность": ["stability", "stabilized", "t80"],
  "деградация": ["degradation", "degraded"],
  "материал": ["material"],
  "поглотитель": ["absorber"],
  "прибор": ["device", "cell"],
  "устройство": ["device", "cell"],
  "слой": ["layer"],
  "метод": ["method", "approach"],
  "результат": ["result"],
  "таблица": ["table"],
  "точность": ["accuracy", "precision"],
  "полнота": ["recall"],
  "задержка": ["latency", "delay"],
  "модель": ["model"],
  "набор": ["dataset", "benchmark"],
  "данные": ["data"],
  "сравнение": ["comparison", "compared"],
  "условия": ["conditions"],
  "измерение": ["measurement", "measured"],
};

export class ChatUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = "ChatUnavailable";
  }
}

function fold(text) {
  return String(text || "").normalize("NFKC").toLowerCase().replace(/ё/g, "е");
}

function tokenGroups(question) {
  const words = fold(question).split(/[^0-9a-zа-я]+/).filter(Boolean);
  const seen = new Set();
  const groups = [];
  for (const word of words) {
    if (word.length < 3 || STOPWORDS.has(word) || seen.has(word)) continue;
    seen.add(word);
    groups.push([word, ...(SYNONYMS[word] || [])]);
    if (groups.length >= 10) break;
  }
  return groups;
}

function scoreText(text, groups) {
  const haystack = fold(text);
  let score = 0;
  for (const group of groups) {
    if (group.some(term => haystack.includes(term))) score += 1;
  }
  return score;
}

function displayTerms(groups) {
  return groups.map(group => group[0]).slice(0, 6);
}

function clip(text, limit = QUOTE_LIMIT) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}

export function citationFrom(source, doc = {}) {
  if (!source || typeof source !== "object") return null;
  const quote = clip(source.quote || source.fragment || "");
  if (!quote) return null;
  const page = Number(source.page);
  const sourceId = source.source_id ?? source.id ?? null;
  return {
    document_id: doc.document_id || source.document_id || "",
    publication_id: doc.publication_id || source.publication_id || "",
    title: doc.title || source.title || "",
    source_id: sourceId === null || sourceId === undefined ? null : sourceId,
    page: Number.isFinite(page) && page > 0 ? page : null,
    quote,
    provenance: source.provenance || "pdf_text",
    source: sourceId !== null && sourceId !== undefined ? source : undefined,
  };
}

function sourceText(source) {
  return [source.quote, source.fragment, source.display_text, source.latex]
    .filter(Boolean).join(" ");
}

export function isConfirmedSource(source) {
  if (!source || typeof source !== "object") return false;
  if (source.verification === "visual_unverified" || source.requires_review === true) return false;
  return Boolean(sourceText(source).trim());
}

function citationsOf(sources, doc) {
  return sources.map(source => citationFrom(source, doc)).filter(Boolean);
}

const sourceCache = new Map();

export async function loadDocumentSources(docId, signal) {
  if (!docId) return [];
  if (sourceCache.has(docId)) return sourceCache.get(docId);
  const data = await getJSON(
    `/api/documents/${encodeURIComponent(docId)}/sources`,
    {limit: 500},
    signal,
  );
  const items = Array.isArray(data?.items) ? data.items : [];
  sourceCache.set(docId, items);
  return items;
}

function mockResponse({question, scope, doc, documents, citations, searched, emptyNote = ""}) {
  const groups = tokenGroups(question);
  const terms = displayTerms(groups);
  if (!terms.length) {
    return {
      mock: true, status: "not_found", scope, answer: "", citations: [],
      contradictions: [], searched_documents: searched,
      note: emptyNote || "Сформулируйте вопрос словами из статьи: метрика, материал, условие или вывод.",
    };
  }
  if (!citations.length) {
    return {
      mock: true, status: "not_found", scope, answer: "", citations: [],
      contradictions: [], searched_documents: searched,
      note: emptyNote || "Локальный поиск не нашёл цитат по словам вопроса. Это не значит, что данных в статье нет.",
    };
  }
  const where = scope === "article"
    ? `в статье «${doc?.title || doc?.publication_id || "без названия"}»`
    : `в ${documents.length} ${documents.length === 1 ? "статье" : "статьях"}`;
  const named = [...new Set(citations.map(item => item.publication_id || item.title).filter(Boolean))].slice(0, 4);
  return {
    mock: true,
    status: "answered",
    scope,
    answer: `Локальный демонстрационный поиск без модели: ${where} найдены фрагменты со словами ${terms.map(term => `«${term}»`).join(", ")}.`
      + (scope === "article" || !named.length ? "" : ` Подтверждения относятся к: ${named.join(", ")}.`)
      + " Ниже только сами цитаты с номерами страниц. Это подбор источников, а не научный вывод и не проверка полноты.",
    citations,
    contradictions: [],
    searched_documents: searched,
    note: scope === "article"
      ? "Ответ собран локально из подтверждённых цитат. Серверный чат недоступен."
      : `Ответ собран локальным поиском по ${searched.length} ${searched.length === 1 ? "статье" : "статьям"}. Серверный чат недоступен.`,
  };
}

function documentIndex(documents) {
  return new Map((Array.isArray(documents) ? documents : [])
    .filter(doc => doc && doc.document_id)
    .map(doc => [doc.document_id, doc]));
}

function backendCitation(item, documents) {
  if (!item || typeof item !== "object") return null;
  const doc = documents.get(item.document_id) || item;
  const citation = citationFrom(item, doc);
  if (!citation) return null;
  // У отдельного разбора (parse:<id>) нет каталога anchors; ссылку на PDF
  // отдаёт сам сервер в pdf_url, поэтому не изобретаем путь к anchors.
  if (citation.document_id.startsWith("parse:")) citation.source_id = null;
  if (typeof item.pdf_url === "string" && item.pdf_url) citation.pdf_url = item.pdf_url;
  return citation;
}

function normalizeBackend(data, scope, documents = []) {
  if (!data || typeof data !== "object") {
    throw new ChatUnavailable("Сервер вернул пустой ответ чата.");
  }
  const index = documentIndex(documents);
  const rawCitations = Array.isArray(data.citations) ? data.citations : [];
  const citations = [];
  const byRef = new Map();
  for (const item of rawCitations) {
    const citation = backendCitation(item, index);
    if (!citation) continue;
    citations.push(citation);
    if (item && typeof item.ref === "string") byRef.set(item.ref, citation);
  }
  const contradictions = (Array.isArray(data.contradictions) ? data.contradictions : [])
    .map(item => ({
      metric: String(item?.topic || ""),
      note: String(item?.explanation || "Расхождение требует ручной проверки."),
      citations: (Array.isArray(item?.refs) ? item.refs : [])
        .map(ref => byRef.get(ref)).filter(Boolean),
    }))
    .filter(item => item.citations.length);
  const notFound = data.not_found === true;
  const unverified = data.unverified === true;
  const answer = typeof data.answer === "string" ? data.answer.trim() : "";
  let note = "";
  if (notFound) {
    note = answer || "В доступных источниках нет ответа на этот вопрос.";
  } else if (unverified) {
    note = "Ответ подтверждён не полностью: сверьте утверждения с цитатами и PDF.";
  }
  if (data.context?.truncated) {
    note = [note, "Часть источников не поместилась в контекст модели."].filter(Boolean).join(" ");
  }
  return {
    mock: false,
    status: notFound ? "not_found" : "answered",
    scope,
    answer: notFound ? "" : answer,
    citations: notFound ? [] : citations,
    contradictions: notFound ? [] : contradictions,
    searched_documents: Array.isArray(data.context?.documents) ? data.context.documents : [],
    source_count: Number(data.context?.source_count) || 0,
    truncated: Boolean(data.context?.truncated),
    note,
    general_knowledge: data.general_knowledge === true,
  };
}

function contextParams({mode = "document", documentId, documentIds}) {
  const params = new URLSearchParams({mode});
  if (mode === "document") {
    if (!documentId) return null;
    params.set("document_id", documentId);
  }
  if (mode === "selection") {
    // The server stores one conversation per selection and caps the context at
    // 12 documents, so the key must be built from the same first 12 ids.
    const ids = (Array.isArray(documentIds) ? documentIds : [])
      .map(String).filter(Boolean).slice(0, 12);
    if (!ids.length) return null;
    params.set("document_ids", ids.join(","));
  }
  return params;
}

export function clearChat({mode = "document", documentId, documentIds} = {}, signal) {
  const params = contextParams({mode, documentId, documentIds});
  if (!params) return Promise.resolve(null);
  return deleteJSON(`/api/chat?${params.toString()}`, signal);
}

export async function loadChatHistory({mode = "document", documentId, documentIds,
  documents = [], scope = "article"} = {}, signal) {
  const params = contextParams({mode, documentId, documentIds});
  if (!params) return [];
  let data;
  try {
    data = await getJSON("/api/chat", Object.fromEntries(params), signal);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    return [];
  }
  const items = Array.isArray(data?.history) ? data.history : [];
  return items.map(item => {
    if (!item || typeof item !== "object") return null;
    if (item.role === "user") {
      return {role: "user", text: String(item.content || "")};
    }
    if (item.role !== "assistant") return null;
    const payload = item.payload && typeof item.payload === "object" ? item.payload : {};
    return normalizeBackend({
      answer: item.content,
      citations: payload.citations,
      not_found: payload.not_found,
      unverified: payload.unverified,
      contradictions: payload.contradictions,
      context: payload.context,
    }, scope, documents);
  }).filter(Boolean);
}

function isMissingEndpoint(error) {
  if (!error) return false;
  if (MISSING_ENDPOINT.has(error.status)) return true;
  // Старый сервер отвечает на неизвестный маршрут кодом 404 с явным текстом.
  // 404 «документ не найден» уже не должен включать демо-подмену.
  return error.status === 404 && /маршрут не найден/i.test(String(error.message || ""));
}

export async function askArticle({docId, title, publicationId, question, history = [], sources, signal}) {
  try {
    const data = await postJSON("/api/chat", {
      mode: "document",
      document_id: docId,
      question,
    }, signal);
    return normalizeBackend(data, "article",
      [{document_id: docId, title, publication_id: publicationId}]);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    if (!isMissingEndpoint(error)) throw error;
  }
  const doc = {document_id: docId, title, publication_id: publicationId};
  const rawItems = Array.isArray(sources) ? sources : await loadDocumentSources(docId, signal);
  const items = rawItems.filter(isConfirmedSource);
  const emptyNote = rawItems.length && !items.length
    ? "В статье есть только распознанные блоки без подтверждённой текстовой цитаты. Они не используются как доказательство."
    : "";
  const groups = tokenGroups(question);
  const ranked = items
    .map(source => ({source, score: scoreText(sourceText(source), groups)}))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score || (a.source.page || 0) - (b.source.page || 0))
    .slice(0, MAX_ARTICLE_SOURCES);
  return mockResponse({
    question, scope: "article", doc, documents: [doc],
    citations: citationsOf(ranked.map(item => item.source), doc),
    searched: [doc], emptyNote,
  });
}

async function documentsForLibrary({documents, question}) {
  const groups = tokenGroups(question);
  const ranked = documents
    .map(doc => ({doc, score: scoreText([doc.title, doc.publication_id, doc.year].join(" "), groups)}))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, MAX_LIBRARY_DOCUMENTS);
  if (ranked.length) return ranked.map(item => item.doc);
  return documents
    .filter(doc => doc.status === "done" || doc.status === "partial" || !doc.status)
    .slice(0, Math.min(4, MAX_LIBRARY_DOCUMENTS));
}

export async function askLibrary({documents, question, history = [], signal, scope: libraryScope = "documents"}) {
  const scope = "library";
  const ids = (Array.isArray(documents) ? documents : [])
    .map(doc => doc && doc.document_id).filter(Boolean);
  const selectedIds = ids.slice(0, 12);
  try {
    const payload = libraryScope === "all" || !selectedIds.length
      ? {mode: "library", question}
      : {mode: "selection", document_ids: selectedIds, question};
    const data = await postJSON("/api/chat", payload, signal);
    const result = normalizeBackend(data, "library", documents);
    if (libraryScope !== "all" && ids.length > selectedIds.length) {
      result.note = [result.note,
        `В контекст вошли первые ${selectedIds.length} статей из ${ids.length}.`]
        .filter(Boolean).join(" ");
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw error;
    if (!isMissingEndpoint(error)) throw error;
  }
  if (!documents.length) {
    return {mock: true, status: "not_found", scope: "library", answer: "", citations: [],
      contradictions: [], searched_documents: [], note: "Сначала выберите хотя бы одну статью."};
  }
  const groups = tokenGroups(question);
  if (!groups.length) {
    return {mock: true, status: "not_found", scope: "library", answer: "", citations: [],
      contradictions: [], searched_documents: [], note: "Сформулируйте вопрос словами из статей."};
  }
  const selected = await documentsForLibrary({documents, question});
  const batches = await Promise.all(selected.map(async doc => {
    try {
      const sources = await loadDocumentSources(doc.document_id, signal);
      return {doc, sources: sources.filter(isConfirmedSource)};
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return {doc, sources: []};
    }
  }));
  const ranked = batches.flatMap(({doc, sources}) => sources
    .map(source => ({doc, source, score: scoreText(sourceText(source), groups)}))
    .filter(item => item.score > 0))
    .sort((a, b) => b.score - a.score || (a.source.page || 0) - (b.source.page || 0))
    .slice(0, MAX_LIBRARY_SOURCES);
  const citations = ranked.map(item => citationFrom(item.source, item.doc)).filter(Boolean);
  return mockResponse({
    question, scope: "library", documents: selected,
    citations, searched: selected,
  });
}
