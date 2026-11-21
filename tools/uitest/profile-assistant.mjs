import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { sessionResponse } from "./account.mjs";

const root = new URL("../../static/", import.meta.url);
const dom = new JSDOM(readFileSync(new URL("manual-profile.html", root), "utf8"), {
  url: "http://localhost/manual-profile", pretendToBeVisual: true,
});
for (const key of ["window", "document", "location", "HTMLElement", "Node", "Element", "Event", "File"]) {
  Object.defineProperty(globalThis, key, {value: dom.window[key], configurable: true});
}
globalThis.fetch = async input => {
  const url = String(input);
  if (url.includes("/api/auth/me")) return sessionResponse();
  if (url.endsWith("/api/profile/source")) return {ok: true, status: 200, json: async () => ({
    pages: 1, warnings: [], sources: [{page: 1, quote: "PCE 21.5%"}],
  })};
  if (url.endsWith("/api/profile/assist")) return {ok: true, status: 200, json: async () => ({suggestions: [
    {entity: "Результат", field: "КПД", type: "number", value: "21.5%", page: 1, quote: "PCE 21.5%"},
  ]})};
  throw new Error(url);
};
await import(new URL("profile.mjs", root).href);
const $ = id => document.getElementById(id);
Object.defineProperty($("profilePdf"), "files", {value: [new File(["%PDF"], "paper.pdf")], configurable: true});
$("profilePdf").dispatchEvent(new Event("change"));
await new Promise(resolve => setTimeout(resolve, 30));
$("assistantQuestion").value = "Как указать КПД?";
$("assistantAsk").click();
await new Promise(resolve => setTimeout(resolve, 30));
assert.equal(document.querySelectorAll(".assistant-suggestion").length, 1);
assert.equal([...document.querySelectorAll('[name="fieldName"]')].some(node => node.value === "КПД"), false);
document.querySelector(".assistant-suggestion input").checked = true;
$("assistantApply").click();
const field = [...document.querySelectorAll(".profile-field")].find(node => node.querySelector('[name="fieldName"]').value === "КПД");
assert.ok(field);
assert.equal(field.querySelector('[name="type"]').value, "number");
assert.equal(field.querySelector('[name="example"]').value, "21.5%");
assert.equal(field.querySelector('[data-when="number"]').hidden, false);
assert.equal(field.querySelector('[data-when="reference"]').hidden, true);
assert.equal($("assistantApply").hidden, true);
assert.ok(document.querySelector(".assistant-messages .user-bubble"));
assert.ok(document.querySelector(".assistant-messages .assistant-bubble"));
console.log("profile assistant: proposal requires approval and fills field");
