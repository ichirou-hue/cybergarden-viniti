import { getJSON } from "./api.js";

const dialog = document.getElementById("evidence");
const title = document.getElementById("evidenceTitle");
const quote = document.getElementById("evidenceQuote");
const preview = document.getElementById("preview");
const fallback = document.getElementById("imageError");
const closeButton = document.getElementById("evidenceClose");
const pdfLink = document.getElementById("evidencePdf");

let entryHash = "";
let currentDoc = "";
let silent = false;

export function dismissEvidence() {
  if (!dialog.open) return;
  silent = true;
  dialog.close();
}

function docHash(docId, sourceId) {
  const query = (location.hash.split("?")[1] || "");
  const base = `#/doc/${encodeURIComponent(docId)}`;
  const suffix = query ? `?${query}` : "";
  return sourceId ? `${base}/source/${sourceId}${suffix}` : `${base}${suffix}`;
}

closeButton.addEventListener("click", () => dialog.close());

dialog.addEventListener("close", () => {
  preview.removeAttribute("src");
  if (silent) {
    silent = false;
    return;
  }
  if (entryHash && !entryHash.includes("/source/")) {
    history.replaceState(null, "", entryHash);
  } else if (currentDoc) {
    history.replaceState(null, "", docHash(currentDoc));
  }
});

preview.addEventListener("error", () => {
  preview.hidden = true;
  fallback.hidden = false;
});

export async function openEvidence({docId, source, sourceId, label, signal}) {
  const anchor = source || await getJSON(
    `/api/documents/${encodeURIComponent(docId)}/anchors/${sourceId}.json`, {}, signal);
  if (!anchor) throw new Error("Источник не найден.");
  const sid = anchor.source_id ?? sourceId;
  if (sid === undefined || sid === null || sid === "") {
    throw new Error("У источника нет идентификатора.");
  }
  currentDoc = docId;
  entryHash = location.hash;
  const heading = label ? `${label} · страница ${anchor.page}` : `Источник · страница ${anchor.page}`;
  title.textContent = heading;
  quote.textContent = anchor.fragment || anchor.quote || "";
  fallback.hidden = true;
  preview.hidden = false;
  preview.alt = `Страница ${anchor.page} PDF с подсветкой источника`;
  preview.src = `/api/documents/${encodeURIComponent(docId)}/anchors/${encodeURIComponent(sid)}.png`;
  pdfLink.href = `/api/documents/${encodeURIComponent(docId)}/pdf#page=${anchor.page}`;
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  history.replaceState(null, "", docHash(docId, sid));
}
