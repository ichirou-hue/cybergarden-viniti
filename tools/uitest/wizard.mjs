import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { seedWindow, sessionResponse } from "./account.mjs";

const root = new URL("../../static/", import.meta.url);
const dom = new JSDOM(readFileSync(new URL("index.html", root), "utf8"), {
  url: "http://localhost/#/", pretendToBeVisual: true,
});
for (const key of ["window", "document", "location", "history", "HTMLElement", "Node", "Element", "Event", "File"]) {
  Object.defineProperty(globalThis, key, {value: dom.window[key], configurable: true});
}
globalThis.fetch = async input => {
  if (String(input).includes("/api/auth/me")) return sessionResponse();
  throw new Error("На первом этапе запрос к серверу не нужен");
};
// Мастер живёт за экраном первого входа: подкладываем готовый аккаунт.
globalThis.localStorage = dom.window.localStorage;
seedWindow(dom.window);
await import(new URL("app.js", root).href);

const $ = id => document.getElementById(id);
assert.equal($("wizard").hidden, false);
assert.equal($("profileStep").hidden, true);
assert.equal($("wizardTitle").textContent, "Начните с PDF");
assert.equal(document.querySelectorAll("#wizard h1").length, 1);
assert.equal(document.body.classList.contains("wizard-active"), true);
assert.equal(document.querySelectorAll(".track-switch .track").length, 1);
assert.equal(document.querySelector(".track-switch .track").textContent.trim(), "Паспорт");
assert.equal(document.querySelectorAll("#tracks").length, 0);
assert.equal($("wizardLLM").checked, false);
assert.equal($("wizardLLMState").textContent, "Без модели");

$("file").dispatchEvent(new dom.window.Event("change"));
const invalid = new File(["text"], "notes.txt", {type: "text/plain"});
Object.defineProperty($("file"), "files", {value: [invalid], configurable: true});
$("file").dispatchEvent(new dom.window.Event("change"));
assert.equal($("wizardError").hidden, false);
assert.equal($("profileStep").hidden, true);

Object.defineProperty($("file"), "files", {value: [new File(["%PDF-1.4"], "paper.pdf", {type: "application/pdf"})], configurable: true});
$("file").dispatchEvent(new dom.window.Event("change"));
assert.equal($("profileStep").hidden, false);
assert.equal($("wizardTitle").textContent, "Как разобрать статью?");
assert.equal(document.querySelectorAll("#wizard h1").length, 1);
assert.equal($("wizard").dataset.step, "2");
assert.equal($("dropZone").hidden, true);
assert.equal($("selectedFile").textContent, "paper.pdf");
assert.equal(document.querySelectorAll('[name="wizardProfile"]').length, 2);
assert.equal(document.querySelector('[name="wizardProfile"]:checked')?.value, "rag");
$("wizardBack").click();
assert.equal($("profileStep").hidden, true);
assert.equal($("dropZone").hidden, false);
console.log("wizard: этапы, проверка PDF и возврат пройдены");
