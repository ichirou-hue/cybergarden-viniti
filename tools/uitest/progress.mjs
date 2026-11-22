import assert from "node:assert/strict";
import { uploadPDFWithProgress } from "../../static/api.js";

const seen = [];
globalThis.fetch = async (path, options) => {
  assert.equal(path, "/api/documents/stream");
  const headers = options.headers instanceof Headers ? options.headers : new Headers(options.headers);
  assert.equal(headers.get("X-Track"), "solar");
  const events = [
    {completed: 0, total: 4, label: "PDF загружен"},
    {completed: 1, total: 4, label: "Страницы PDF прочитаны"},
    {completed: 4, total: 4, label: "Готово", result: {document_id: "solar:test"}},
  ];
  const bytes = new TextEncoder().encode(events.map(item => JSON.stringify(item)).join("\n") + "\n");
  return new Response(new ReadableStream({start(controller) {
    controller.enqueue(bytes.slice(0, 17));
    controller.enqueue(bytes.slice(17));
    controller.close();
  }}), {headers: {"Content-Type": "application/x-ndjson"}});
};
const result = await uploadPDFWithProgress({name: "test.pdf"}, "solar", event => seen.push(event.completed));
assert.deepEqual(seen, [0, 1, 4]);
assert.equal(result.document_id, "solar:test");
console.log("progress: потоковые события и результат пройдены");
