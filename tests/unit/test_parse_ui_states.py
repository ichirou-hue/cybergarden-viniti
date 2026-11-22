"""Regression guard for the parse screen's result states.

The repository has no browser or DOM harness, so this test cannot click through
``static/parse.js``. It guards what can regress without one:

- the module still parses as an ES module;
- the copy that separates a technical ``status=ok`` from a confirmed scientific
  result is still present;
- the scope rule stays honest: a publication row is in scope only when it says
  so explicitly, never because ``in_scope`` is missing, unknown, a boolean or a
  number;
- a scope label names both facts when part of the rows are outside the area and
  the rest have no flag at all;
- the audit summary falls back to the job summary when ``manifest.counts`` is
  an empty object;
- the ``llm/manifest.json`` link is not invented when the manifest did not load
  or the artifact is explicitly absent.

The scope, audit, artifact and cell cases run the real functions from
``static/parse.js`` under Node, which is what keeps D016 (``in_scope="false"``)
excluded and keeps a blank field from defaulting to "in scope". Node is
preinstalled on both CI targets; where it is absent the case tests skip instead
of failing.

The rendered copy itself is checked by the manual scenario in
``prototypes/parse-result-state.md``.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
PARSE_JS = STATIC / "parse.js"

# Markers that carry the honest states. Removing one of them means the screen
# can again present a finished job as a verified, complete result.
REQUIRED_MARKERS = (
    "JOB_STATUS",
    'ok: ["Разбор завершён"',
    "in_scope",
    "exclusion_reason",
    "Вне области",
    "Определена не полностью",
    "Научные записи отсутствуют",
    "Подтверждённых строк для этой таблицы",
    "candidate",
    "context_status",
    "не входят в официальную выгрузку",
    "coverage",
    "completeness",
    # Semantic layer: always labelled as model-only, neutral status, per-field
    # evidence and an honest error count that is not a quarantine tally.
    "semantic_only",
    "Не для официальных CSV",
    'ok: ["Собран", "idle"]',
    "Замечания проверки",
    "Не определено · требует проверки",
    "Нет подтверждающей цитаты",
    "field_sources",
    "scope_reason",
    "Показаны первые",
    "semantic.json",
)

# Copy that promised more than the server actually guarantees.
FORBIDDEN_MARKERS = (
    "Проверенные данные",
    "Результат и проверенная интерпретация готовы",
    "Таблицы, значения и цитаты готовы",
    "Скачать результат",
    # The semantic layer must not turn its error list into a quarantine count,
    # and must not use a green "verified" status.
    "карантин",
    "Отклонённые записи",
    'ok: ["Проверено"',
)

# The export line is appended to a temporary copy: the production module keeps
# its single public export, and the probe only reads pure functions.
EXPORT_LINE = (
    "\nexport { scopeState, scopeLabel, scientificRowCount, auditCounts, artifacts,"
    " cellText, semanticBadge, semanticClaimText, semanticCountState, semanticState,"
    " semanticFieldCell, semanticRecordTable, draftState, draftNotice,"
    " resultStatePanel, attributionState, attributionNotice, interpretationResult };\n"
)

SCOPE_PROBE = """
const {scopeState, scopeLabel} = await import("./parse.js");
const cases = JSON.parse(process.argv[2]);
const out = cases.map(rows => {
  const state = scopeState({publications: rows});
  return {
    label: scopeLabel(state),
    known: state.known,
    inside: state.inside.length,
    outside: state.outside.length,
    unknown: state.unknown.length,
    reasons: state.reasons,
  };
});
process.stdout.write(JSON.stringify(out));
"""

# A minimal element stub: ``el`` only sets className, textContent, attributes and
# children, which is exactly what this stub records.
UI_PROBE = """
globalThis.Node = class Node {};
globalThis.document = {
  createElement: tag => Object.assign(new Node(), {
    tagName: String(tag).toUpperCase(), className: "", textContent: "",
    dataset: {}, attributes: {}, children: [],
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] ?? null; },
    append(child) { this.children.push(child); },
    replaceChildren() {}, addEventListener() {}, removeAttribute() {},
  }),
  createTextNode: text => Object.assign(new Node(), {text: String(text)}),
};
const {auditCounts, artifacts, cellText} = await import("./parse.js");
const input = JSON.parse(process.argv[2]);
const rowText = row => (row.children || []).map(child => child.textContent || "").join("|");
const audit = input.audits.map(([manifest, job]) => (auditCounts(manifest, job).children || []).map(rowText));
const links = node => {
  const found = [];
  const walk = item => {
    if (!item) return;
    if (item.tagName === "A" && item.attributes?.href) found.push(item.attributes.href);
    for (const child of item.children || []) walk(child);
  };
  walk(node);
  return found;
};
const files = input.files.map(([job, manifest]) => links(artifacts(job, manifest)));
const cells = input.cells.map(value => cellText("in_scope", value));
process.stdout.write(JSON.stringify({audit, files, cells}));
"""

# The semantic helpers return DOM nodes, so the probe needs the same minimal
# element stub as UI_PROBE. Every text child is collected so a cell can be
# checked for its own quote and against a foreign quote.
SEMANTIC_PROBE = """
globalThis.Node = class Node {};
globalThis.document = {
  createElement: tag => Object.assign(new Node(), {
    tagName: String(tag).toUpperCase(), className: "", textContent: "",
    dataset: {}, attributes: {}, children: [],
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] ?? null; },
    append(child) { this.children.push(child); },
    replaceChildren() {}, addEventListener() {}, removeAttribute() {},
  }),
  createTextNode: text => Object.assign(new Node(), {text: String(text)}),
};
const {semanticBadge, semanticClaimText, semanticCountState, semanticState,
       semanticFieldCell, semanticRecordTable, draftState, draftNotice,
       resultStatePanel, attributionState, attributionNotice,
       interpretationResult} = await import("./parse.js");
const input = JSON.parse(process.argv[2]);
const textOf = node => {
  const parts = [];
  const walk = item => {
    if (!item) return;
    if (item.textContent) parts.push(item.textContent);
    for (const child of item.children || []) walk(child);
  };
  walk(node);
  return parts.join(" ");
};
const findTag = (node, tag) => {
  if (!node) return null;
  if (node.tagName === tag) return node;
  for (const child of node.children || []) {
    const found = findTag(child, tag);
    if (found) return found;
  }
  return null;
};
const claims = input.claims.map(([field, value]) => semanticClaimText(field, value));
const counts = input.counts.map(([semantic, solar]) => semanticCountState(semantic, solar));
const states = input.states.map(([job, manifest, semantic, error]) => {
  const state = semanticState(job, manifest, semantic, error);
  return {status: state.status, legacy: state.legacy};
});
const cells = input.cells.map(([record, field]) => textOf(semanticFieldCell(record, field)));
const badges = (input.badges || []).map(status => {
  const node = semanticBadge(status);
  return {text: node.textContent, className: node.className};
});
const tables = input.tables.map(([kind, rows]) => {
  const node = semanticRecordTable(kind, rows);
  const head = (node.children || []).find(child => child.className === "interpretation-table-head");
  const headText = head ? (head.children || []).map(child => child.textContent || "").join(" ") : "";
  const body = findTag(node, "TBODY");
  const thead = findTag(node, "THEAD");
  const limit = (node.children || []).find(child => String(child.className || "").includes("interpretation-limit"));
  return {
    headText,
    rows: body ? (body.children || []).length : 0,
    columns: thead && thead.children[0] ? (thead.children[0].children || []).length : 0,
    limit: limit ? limit.textContent || "" : "",
  };
});
const collect = node => {
  const texts = [], links = [];
  const walk = item => {
    if (!item) return;
    if (item.textContent) texts.push(item.textContent);
    if (item.tagName === "A" && item.attributes?.href) links.push(item.attributes.href);
    for (const child of item.children || []) walk(child);
  };
  walk(node);
  return {text: texts.join(" "), links};
};
const drafts = (input.drafts || []).map(([job, manifest]) => {
  const state = draftState(job, manifest);
  const rejected = draftNotice(job, state, true);
  const normal = draftNotice(job, state, false);
  return {
    state,
    rejected: rejected ? collect(rejected) : null,
    normal: normal ? collect(normal) : null,
  };
});
const summaries = (input.summaries || []).map(([job, manifest, interpretation]) =>
  collect(resultStatePanel(job, manifest, null, interpretation, "", "",
    {count: 0, unresolved: 0, known: true}))
);
const attributions = (input.attributions || []).map(([job, manifest, interpretation]) => {
  const state = attributionState(job, manifest, interpretation);
  const notice = attributionNotice(state);
  return {
    state: {
      known: state.known, source: state.source, state: state.state,
      requiresReview: state.requiresReview, totalRows: state.totalRows,
      totalFields: state.totalFields, hasSensitive: state.hasSensitive,
      fields: state.fields,
    },
    notice: notice ? collect(notice) : null,
  };
});
const results = (input.results || []).map(([job, manifest, interpretation]) => {
  const node = interpretationResult(job, manifest, interpretation, "");
  return collect(node);
});
process.stdout.write(JSON.stringify({
  claims, counts, states, cells, tables, badges, drafts, summaries, attributions, results,
}));
"""

# (name, publication rows, expected label, expected counts)
SCOPE_CASES = (
    ("no rows", [], "Не определена", (0, 0, 0)),
    ("explicit false (D016)", [
        {"publication_id": "D016", "in_scope": "false", "exclusion_reason": "Моделирование."},
    ], "Вне области", (0, 1, 0)),
    ("explicit true", [{"publication_id": "D001", "in_scope": "true"}], "В области", (1, 0, 0)),
    ("true with padding and case", [{"publication_id": "D001", "in_scope": " TRUE "}],
     "В области", (1, 0, 0)),
    ("field missing", [{"publication_id": "D001"}], "Не определена", (0, 0, 1)),
    ("field empty", [{"publication_id": "D001", "in_scope": ""}], "Не определена", (0, 0, 1)),
    ("field null", [{"publication_id": "D001", "in_scope": None}], "Не определена", (0, 0, 1)),
    ("unknown word", [{"publication_id": "D001", "in_scope": "maybe"}], "Не определена", (0, 0, 1)),
    # A boolean or a number is not the documented string form, so it is
    # unknownness, not a decision.
    ("boolean true", [{"publication_id": "D001", "in_scope": True}], "Не определена", (0, 0, 1)),
    ("boolean false", [{"publication_id": "D001", "in_scope": False}], "Не определена", (0, 0, 1)),
    ("number one", [{"publication_id": "D001", "in_scope": 1}], "Не определена", (0, 0, 1)),
    ("number zero", [{"publication_id": "D001", "in_scope": 0}], "Не определена", (0, 0, 1)),
    ("string zero", [{"publication_id": "D001", "in_scope": "0"}], "Не определена", (0, 0, 1)),
    # false plus an unmarked row: the label must name both facts.
    ("blank with false", [
        {"publication_id": "D001", "in_scope": "false", "exclusion_reason": "r"},
        {"publication_id": "D002"},
    ], "Часть вне области, определена не полностью", (0, 1, 1)),
    ("true, false and blank", [
        {"publication_id": "D001", "in_scope": "true"},
        {"publication_id": "D002", "in_scope": "false", "exclusion_reason": "r"},
        {"publication_id": "D003"},
    ], "Часть вне области, определена не полностью", (1, 1, 1)),
    ("true with blank", [
        {"publication_id": "D001", "in_scope": "true"},
        {"publication_id": "D002"},
    ], "Определена не полностью", (1, 0, 1)),
    ("true and false", [
        {"publication_id": "D001", "in_scope": "true"},
        {"publication_id": "D002", "in_scope": "false", "exclusion_reason": "r"},
    ], "Часть вне области", (1, 1, 0)),
)

COUNT_LABELS = ("Записей интерпретировано", "Замечаний структуры", "Находок проверки")


def count_rows(*values):
    return [f"{value}|{label}" for value, label in zip(values, COUNT_LABELS)]


# (name, manifest, job, expected rows from auditCounts)
AUDIT_CASES = (
    ("manifest counts win",
     {"counts": {"interpreted_records": 4, "validation_issues": 2, "audit_findings": 1}},
     {"summary": {"llm": {"counts": {"interpreted_records": 9}}}},
     count_rows(4, 2, 1)),
    # The regression: an empty manifest.counts object used to win over the job
    # summary and render "-" instead of the counts the server already had.
    ("empty manifest counts fall back to job",
     {"counts": {}},
     {"summary": {"llm": {"counts": {
         "interpreted_records": 7, "validation_issues": 0, "audit_findings": 2}}}},
     count_rows(7, 0, 2)),
    ("no counts anywhere", {}, {"summary": {"llm": {"counts": {}}}}, []),
    ("manifest missing", None,
     {"summary": {"llm": {"counts": {"interpreted_records": 1}}}},
     count_rows(1, "-", "-")),
)

MAIN_LINKS = (
    "/api/parse/abc/file/result.json",
    "/api/parse/abc/file/candidates.csv",
)

# (name, job, manifest, expected links)
FILES_CASES = (
    # The regression: the fallback llm/manifest.json link used to appear even
    # though the manifest never loaded, so it pointed at a missing file.
    ("manifest not loaded", {"id": "abc", "status": "ok", "llm": "mock"}, None,
     list(MAIN_LINKS)),
    ("manifest loaded lists artifacts", {"id": "abc", "status": "ok", "llm": "mock"},
     {"artifacts": {"sources.json": {}, "audit.json": {}}},
     list(MAIN_LINKS) + [
         "/api/parse/abc/file/llm/sources.json",
         "/api/parse/abc/file/llm/audit.json",
         "/api/parse/abc/file/llm/manifest.json",
     ]),
    # An explicit false/null entry means "this file is not there".
    ("manifest artifact explicitly absent", {"id": "abc", "status": "ok", "llm": "mock"},
     {"artifacts": {"manifest.json": None, "sources.json": {}}},
     list(MAIN_LINKS) + ["/api/parse/abc/file/llm/sources.json"]),
    ("llm off", {"id": "abc", "status": "ok", "llm": "off"}, None, list(MAIN_LINKS)),
    ("job not ok", {"id": "abc", "status": "running", "llm": "mock"}, None, []),
)

# (name, raw in_scope value, expected cell text)
CELL_CASES = (
    ("string true", "true", "В области"),
    ("padded false", " FALSE ", "Вне области Solar"),
    ("boolean true", True, "Не указана"),
    ("number one", 1, "Не указана"),
    ("unknown word", "maybe", "Не указана"),
)

# (name, field, raw value, expected text). `unknown`, an invalid token and a
# missing value must never collapse into a false "not relevant" verdict.
SEMANTIC_CLAIM_CASES = (
    ("experimental", "study_type", "experimental", "Эксперимент"),
    ("unknown is not false", "study_type", "unknown", "Не определено · требует проверки"),
    ("invalid token is unknown", "study_type", "guess", "Не определено · требует проверки"),
    ("null is no data", "study_type", None, "Нет данных"),
    ("empty is no data", "data_provenance", "", "Нет данных"),
    ("explicit not relevant", "thematic_relevance", "not_relevant", "Не по теме"),
    ("literature provenance", "data_provenance", "literature", "Из литературы"),
)

# (name, semantic, solar, expected (layers, conditions, metrics, errors)).
# Accepted counts come from records; `errors` is the error-list length, never a
# quarantine count and never the sum of accepted records.
SEMANTIC_COUNTS_CASES = (
    ("records win over manifest", {
        "records": {"layers": [{}, {}], "conditions": [{}], "metrics": [{}, {}, {}]},
        "errors": [{"error": "a"}, {"error": "b"}],
     }, {"semantic_counts": {"layers": 9, "conditions": 9, "metrics": 9, "errors": 9}},
     (2, 1, 3, 2)),
    ("manifest fallback when file missing", None,
     {"semantic_counts": {"layers": 5, "conditions": 0, "metrics": 4, "errors": 3}},
     (5, 0, 4, 3)),
    ("no data", None, None, (None, None, None, None)),
    ("errors are not accepted records", {
        "records": {"layers": [{}], "conditions": [], "metrics": []},
        "errors": [{"error": "x"}, {"error": "y"}, {"error": "z"}],
     }, None, (1, 0, 0, 3)),
)

# (name, job, manifest, semantic, fetch error, expected (status, legacy)).
SEMANTIC_STATE_CASES = (
    ("file status wins",
     {"summary": {"llm": {"solar": {"semantic_status": "partial"}}}},
     {"solar": {"semantic_status": "partial"}},
     {"status": "ok"}, "", ("ok", False)),
    ("manifest fallback when file missing",
     {"summary": {}}, {"solar": {"semantic_status": "missing"}},
     None, "Файл недоступен.", ("missing", False)),
    ("summary fallback without manifest",
     {"summary": {"llm": {"solar": {"semantic_status": "partial"}}}},
     None, None, "Файл недоступен.", ("partial", False)),
    ("legacy parse without solar block",
     {"summary": {}}, None, None, "Файл недоступен.", (None, True)),
)

# Per-field evidence: every nonempty field shows its own quote, never a single
# row quote that would look like proof of all fields.
SEMANTIC_FIELD_RECORD = {
    "name": "PCE", "value": "22.4", "unit": "%", "page": 2,
    "field_sources": {
        "name": {"source_id": 1, "page": 2, "quote": "PCE of 22.4 %"},
        "value": {"source_id": 1, "page": 2, "quote": "PCE of 22.4 %"},
        "unit": {"source_id": 2, "page": 2, "quote": "power conversion efficiency in %"},
    },
}
SEMANTIC_FIELD_CASES = (
    ("name uses its own quote", SEMANTIC_FIELD_RECORD, "name",
     "PCE of 22.4 %", "power conversion efficiency in %"),
    ("unit uses its own quote, not the row quote", SEMANTIC_FIELD_RECORD, "unit",
     "power conversion efficiency in %", "PCE of 22.4 %"),
    ("value without anchor is not proved by name", {
        "name": "PCE", "value": "30", "field_sources": {
            "name": {"source_id": 1, "page": 2, "quote": "PCE of 30 %"},
        },
     }, "value", "Нет подтверждающей цитаты", "PCE of 30 %"),
    ("empty field is a dash", SEMANTIC_FIELD_RECORD, "role", "-", "PCE of 22.4 %"),
)

# (name, kind, rows, expected rows shown, expected head text, expected limit).
SEMANTIC_TABLE_CASES = (
    ("limit keeps an honest total", "metrics",
     [{"name": "m", "value": str(i), "unit": "%"} for i in range(150)],
     100, "150 записей", "Показаны первые 100 из 150"),
    ("small table is not truncated", "layers",
     [{"name": "a", "material": "b"}, {"name": "c", "material": "d"}],
     2, "2 записи", ""),
)

# (name, status, expected label, expected class). `ok` must stay neutral: it
# means "no validation notes", not a green scientific confirmation.
SEMANTIC_BADGE_CASES = (
    ("ok is neutral", "ok", "Собран", "badge idle"),
    ("partial warns", "partial", "Собран частично", "badge warn"),
    ("missing is neutral", "missing", "Не сформирован", "badge idle"),
    ("rejected is bad", "rejected", "Отклонён", "badge bad"),
    ("unknown status", "other", "Неизвестно", "badge idle"),
)

# Row-level draft fallback. The manifest carries only status and counts; the
# rows stay in draft.json. A rejected strict interpretation must not read as
# "no data" while the draft kept accepted rows.
DRAFT_JOB = {"id": "abc", "status": "ok", "llm": "api", "summary": {}}
DRAFT_MANIFEST = {
    "solar": {"draft_status": "partial",
              "draft_counts": {"rows": 5, "accepted": 3, "quarantined": 2}},
}

# (name, job, manifest, expected (known, status, accepted, quarantined), notice).
DRAFT_CASES = (
    ("partial draft with accepted rows", DRAFT_JOB, DRAFT_MANIFEST,
     (True, "partial", 3, 2), True),
    ("summary fallback without manifest",
     {"id": "abc", "status": "ok", "llm": "api", "summary": {"llm": {"solar": {
         "draft_status": "partial",
         "draft_counts": {"accepted": 1, "quarantined": 4}}}}},
     None, (True, "partial", 1, 4), True),
    ("missing draft", DRAFT_JOB,
     {"solar": {"draft_status": "missing",
                "draft_counts": {"rows": 0, "accepted": 0, "quarantined": 0}}},
     (True, "missing", 0, 0), False),
    ("blocked draft with no accepted rows", DRAFT_JOB,
     {"solar": {"draft_status": "blocked",
                "draft_counts": {"rows": 2, "accepted": 0, "quarantined": 2}}},
     (True, "blocked", 0, 2), False),
    ("legacy manifest without solar", DRAFT_JOB, None, (False, "", 0, 0), False),
)


# (name, job, manifest, interpretation, expected (known, source, state,
# requiresReview, totalRows, totalFields, field names, hasSensitive), notice).
# `requires_review` is ALWAYS true in the contract, even for an empty inventory,
# so the notice is gated on sensitive fields, not on that flag.
ATTRIBUTION_JOB = {"id": "abc", "status": "ok", "llm": "api", "profile": "solar",
                   "summary": {}}
ATTRIBUTION_FILE = {
    "state": "unverified", "requires_review": True, "truncated": False,
    "counts": {"rows": 3, "fields": 4,
               "by_field": {"layers.annealing_time": 2, "results.n_devices": 1,
                            "layers.atmosphere": 1}},
    "rows": [{"table": "layers", "key": "L1", "fields": ["annealing_time", "atmosphere"]},
             {"table": "results", "key": "R1", "fields": ["n_devices"]}],
}
ATTRIBUTION_CASES = (
    ("inventory from file", ATTRIBUTION_JOB, {"solar": {}},
     {"status": "ok", "tables": {}, "attribution": ATTRIBUTION_FILE},
     (True, "file", "unverified", True, 3, 4,
      ["layers.annealing_time", "layers.atmosphere", "results.n_devices"], True), True),
    # The contract keeps requires_review=true for an empty inventory. That must
    # not become a warning about sensitive fields that do not exist, and must
    # not read as "science verified" or "review not needed".
    ("empty inventory keeps requires_review true", ATTRIBUTION_JOB, {"solar": {}},
     {"status": "ok", "tables": {},
      "attribution": {"state": "none", "requires_review": True,
                      "counts": {"rows": 0, "fields": 0, "by_field": {}},
                      "rows": [], "truncated": False}},
     (True, "file", "none", True, 0, 0, [], False), True),
    ("unverified with zero counts is not a field warning",
     ATTRIBUTION_JOB, {"solar": {}},
     {"status": "ok", "tables": {},
      "attribution": {"state": "unverified", "requires_review": True,
                      "counts": {"rows": 0, "fields": 0, "by_field": {}},
                      "rows": [], "truncated": False}},
     (True, "file", "unverified", True, 0, 0, [], False), True),
    ("manifest fallback without file",
     {"id": "abc", "status": "ok", "llm": "api", "profile": "solar",
      "summary": {"llm": {"solar": {"attribution_state": "unverified",
                                    "attribution_counts": {"rows": 5, "fields": 6},
                                    "requires_review": True}}}},
     None, {"status": "ok", "tables": {}},
     (True, "manifest", "unverified", True, 5, 6, [], True), True),
    ("legacy job without metadata", ATTRIBUTION_JOB, None,
     {"status": "ok", "tables": {}},
     (False, "legacy", "unknown", False, 0, 0, [], False), True),
)

STRICT_OK_TEXT = "Научные связи и полнота автоматически не подтверждены."
FORBIDDEN_SCIENCE_CLAIMS = (
    "Проверка пройдена",
    "Полнота научных данных не подтверждена",
    "Научные связи подтверждены",
)


def escaped(text):
    """ASCII-only text for assertion messages on a narrow console."""
    return str(text).encode("unicode_escape").decode("ascii")


class ParseResultStateSourceTest(unittest.TestCase):
    def setUp(self):
        self.source = PARSE_JS.read_text(encoding="utf-8")

    def test_honest_state_markers_are_present(self):
        for marker in REQUIRED_MARKERS:
            with self.subTest(marker=escaped(marker)):
                self.assertTrue(marker in self.source, "missing state marker: " + escaped(marker))

    def test_overpromising_copy_is_gone(self):
        for marker in FORBIDDEN_MARKERS:
            with self.subTest(marker=escaped(marker)):
                self.assertTrue(marker not in self.source,
                                "overpromising copy is back: " + escaped(marker))

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_module_still_parses(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = pathlib.Path(temporary) / "parse.mjs"
            shutil.copyfile(PARSE_JS, module)
            completed = subprocess.run(
                [shutil.which("node"), "--check", str(module)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _probe(self, probe_source, payload):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            for name in ("dom.js", "api.js"):
                shutil.copyfile(STATIC / name, directory / name)
            with (directory / "parse.js").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(PARSE_JS.read_text(encoding="utf-8") + EXPORT_LINE)
            probe = directory / "probe.mjs"
            with probe.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(probe_source)
            completed = subprocess.run(
                [shutil.which("node"), str(probe), payload],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_scope_is_known_only_by_explicit_flag(self):
        payload = json.dumps([rows for _, rows, _, _ in SCOPE_CASES], ensure_ascii=False)
        results = self._probe(SCOPE_PROBE, payload)
        self.assertEqual(len(results), len(SCOPE_CASES))
        for (name, _, label, counts), result in zip(SCOPE_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(result["label"], label)
                self.assertEqual(
                    (result["inside"], result["outside"], result["unknown"]), counts)
                self.assertEqual(result["known"], counts[0] + counts[1] > 0)
                # The rule under test: a blank or unknown flag can never be read
                # as "in scope", and "in scope" needs every row explicit true.
                if result["unknown"]:
                    self.assertNotEqual(result["label"], "В области")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_scope_label_names_outside_and_unknown_together(self):
        results = self._probe(
            SCOPE_PROBE,
            json.dumps([rows for _, rows, _, _ in SCOPE_CASES], ensure_ascii=False))
        for (name, _, _, counts), result in zip(SCOPE_CASES, results):
            with self.subTest(case=name):
                known = counts[0] + counts[1] > 0
                if counts[1] and counts[2]:
                    # Both facts must survive in the label: neither the outside
                    # rows nor the unmarked rows may be dropped.
                    self.assertIn("вне области", result["label"])
                    self.assertIn("не полностью", result["label"])
                elif counts[2] and known:
                    self.assertEqual(result["label"], "Определена не полностью")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_audit_counts_fall_back_to_job_summary(self):
        payload = json.dumps({
            "audits": [[manifest, job] for _, manifest, job, _ in AUDIT_CASES],
            "files": [],
            "cells": [],
        }, ensure_ascii=False)
        results = self._probe(UI_PROBE, payload)["audit"]
        self.assertEqual(len(results), len(AUDIT_CASES))
        for (name, _, _, expected), rows in zip(AUDIT_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(rows, expected)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_manifest_link_is_not_invented(self):
        payload = json.dumps({
            "audits": [],
            "files": [[job, manifest] for _, job, manifest, _ in FILES_CASES],
            "cells": [],
        }, ensure_ascii=False)
        results = self._probe(UI_PROBE, payload)["files"]
        self.assertEqual(len(results), len(FILES_CASES))
        for (name, _, _, expected), links in zip(FILES_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(links, expected)
                # The link may only appear when the manifest actually loaded and
                # did not mark the artifact absent.
                if not any(href.endswith("/llm/manifest.json") for href in expected):
                    self.assertNotIn("/api/parse/abc/file/llm/manifest.json", links)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_scope_cell_uses_the_same_strict_rule(self):
        payload = json.dumps({
            "audits": [],
            "files": [],
            "cells": [value for _, value, _ in CELL_CASES],
        }, ensure_ascii=False)
        results = self._probe(UI_PROBE, payload)["cells"]
        self.assertEqual(len(results), len(CELL_CASES))
        for (name, _, expected), text in zip(CELL_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(text, expected)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_semantic_status_badges_are_neutral_and_explicit(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "badges": [status for _, status, _, _ in SEMANTIC_BADGE_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["badges"]
        self.assertEqual(len(results), len(SEMANTIC_BADGE_CASES))
        for (name, _, label, classes), result in zip(SEMANTIC_BADGE_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(result["text"], label)
                self.assertEqual(result["className"], classes)
                # `ok` is never the green confirmation badge.
                if name == "ok is neutral":
                    self.assertNotIn("ok", result["className"].split())

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_semantic_unknown_is_not_a_false_verdict(self):
        payload = json.dumps({
            "claims": [[field, value] for _, field, value, _ in SEMANTIC_CLAIM_CASES],
            "counts": [], "states": [], "cells": [], "tables": [],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["claims"]
        self.assertEqual(len(results), len(SEMANTIC_CLAIM_CASES))
        for (name, _, _, expected), text in zip(SEMANTIC_CLAIM_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(text, expected)
                self.assertNotIn(text, ("false", "Не подходит"))

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_semantic_errors_are_not_a_quarantine_count(self):
        payload = json.dumps({
            "claims": [], "states": [], "cells": [], "tables": [],
            "counts": [[semantic, solar] for _, semantic, solar, _ in SEMANTIC_COUNTS_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["counts"]
        self.assertEqual(len(results), len(SEMANTIC_COUNTS_CASES))
        for (name, _, _, expected), result in zip(SEMANTIC_COUNTS_CASES, results):
            with self.subTest(case=name):
                got = (result["layers"], result["conditions"], result["metrics"], result["errors"])
                self.assertEqual(got, expected)
        # One accepted record, three validation notes: the error count is not the
        # number of accepted (or rejected) records.
        last = results[-1]
        accepted = last["layers"] + last["conditions"] + last["metrics"]
        self.assertNotEqual(last["errors"], accepted)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_semantic_state_is_graceful_for_legacy_manifests(self):
        payload = json.dumps({
            "claims": [], "counts": [], "cells": [], "tables": [],
            "states": [[job, manifest, semantic, error]
                       for _, job, manifest, semantic, error, _ in SEMANTIC_STATE_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["states"]
        self.assertEqual(len(results), len(SEMANTIC_STATE_CASES))
        for (name, _, _, _, _, expected), result in zip(SEMANTIC_STATE_CASES, results):
            with self.subTest(case=name):
                self.assertEqual((result["status"], result["legacy"]), expected)
        # Legacy is exactly the case with no file, no manifest solar block and no
        # summary fallback: it is a note, not a broken parse.
        self.assertTrue(results[-1]["legacy"])
        self.assertFalse(any(result["legacy"] for result in results[:-1]))

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_semantic_evidence_is_per_field(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "tables": [],
            "cells": [[record, field] for _, record, field, _, _ in SEMANTIC_FIELD_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["cells"]
        self.assertEqual(len(results), len(SEMANTIC_FIELD_CASES))
        for (name, _, _, must, must_not), text in zip(SEMANTIC_FIELD_CASES, results):
            with self.subTest(case=name):
                self.assertIn(must, text)
                self.assertNotIn(must_not, text)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_semantic_table_limit_keeps_an_honest_total(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [],
            "tables": [[kind, rows] for _, kind, rows, _, _, _ in SEMANTIC_TABLE_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["tables"]
        self.assertEqual(len(results), len(SEMANTIC_TABLE_CASES))
        columns = {"layers": 6, "conditions": 4, "metrics": 4}
        for (name, kind, _, expected_rows, head, limit), result in zip(
                SEMANTIC_TABLE_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(result["rows"], expected_rows)
                self.assertIn(head, result["headText"])
                self.assertEqual(result["columns"], columns[kind])
                if limit:
                    self.assertIn(limit, result["limit"])
                else:
                    self.assertEqual(result["limit"], "")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_draft_fallback_keeps_independent_rows_visible(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "drafts": [[job, manifest] for _, job, manifest, _, _ in DRAFT_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["drafts"]
        self.assertEqual(len(results), len(DRAFT_CASES))
        for (name, _, _, expected, notice), result in zip(DRAFT_CASES, results):
            with self.subTest(case=name):
                state = result["state"]
                self.assertEqual(
                    (state["known"], state["status"], state["accepted"],
                     state["quarantined"]), expected)
                if not notice:
                    self.assertIsNone(result["rejected"])
                    self.assertIsNone(result["normal"])
                    continue
                rejected = result["rejected"]
                self.assertIsNotNone(rejected)
                self.assertIn("Строгая интерпретация отклонена", rejected["text"])
                self.assertIn(
                    f"{expected[2]} принятых и {expected[3]} отклонённых строк",
                    rejected["text"])
                self.assertIn("Не официальные CSV", rejected["text"])
                self.assertIn("не подтверждённые научные записи", rejected["text"])
                # The regression: a rejected strict pass must never read as
                # "there is no scientific data" while the draft kept rows.
                self.assertNotIn("научных данных нет", rejected["text"])
                self.assertTrue(any(href.endswith("/llm/draft.json")
                                    for href in rejected["links"]))
                self.assertIn("Построчный черновик", result["normal"]["text"])
                self.assertNotIn("отклонена", result["normal"]["text"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_rejected_summary_names_the_strict_label_and_draft(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "summaries": [[DRAFT_JOB, DRAFT_MANIFEST, {"status": "rejected"}]],
        }, ensure_ascii=False)
        text = self._probe(SEMANTIC_PROBE, payload)["summaries"][0]["text"]
        # A rejected interpretation has no strict tables, so the card must say
        # the strict rows were not assembled, not a bare "no scientific data".
        self.assertIn("Научные записи (строгие)", text)
        self.assertIn("построчный черновик сохранил 3 принятых и 2 отклонённых строк", text)
        self.assertIn("draft.json", text)
        self.assertNotIn("научных данных нет", text)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_draft_is_not_claimed_when_there_are_no_accepted_rows(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "drafts": [[job, manifest] for _, job, manifest, _, _ in DRAFT_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["drafts"]
        for (name, _, _, expected, notice), result in zip(DRAFT_CASES, results):
            with self.subTest(case=name):
                if expected[2] == 0:
                    self.assertIsNone(result["rejected"])
                    self.assertIsNone(result["normal"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_attribution_states_and_legacy_are_explicit(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "attributions": [[job, manifest, interpretation]
                             for _, job, manifest, interpretation, _, _ in ATTRIBUTION_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["attributions"]
        self.assertEqual(len(results), len(ATTRIBUTION_CASES))
        for (name, _, _, _, expected, notice), result in zip(ATTRIBUTION_CASES, results):
            with self.subTest(case=name):
                state = result["state"]
                got = (state["known"], state["source"], state["state"],
                       state["requiresReview"], state["totalRows"],
                       state["totalFields"], [item["name"] for item in state["fields"]],
                       state["hasSensitive"])
                self.assertEqual(got, expected)
                if notice:
                    self.assertIsNotNone(result["notice"])
                else:
                    self.assertIsNone(result["notice"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_empty_or_legacy_attribution_never_claims_verified_science(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "attributions": [[job, manifest, interpretation]
                             for _, job, manifest, interpretation, _, _ in ATTRIBUTION_CASES],
        }, ensure_ascii=False)
        results = self._probe(SEMANTIC_PROBE, payload)["attributions"]
        by_name = {name: result for (name, *_), result in zip(ATTRIBUTION_CASES, results)}
        # requires_review=true with an empty inventory must not warn about
        # sensitive fields that do not exist, and must not say "review not needed".
        for name in ("empty inventory keeps requires_review true",
                     "unverified with zero counts is not a field warning"):
            text = by_name[name]["notice"]["text"]
            self.assertIn("Чувствительных полей привязки не найдено", text)
            self.assertIn("не подтверждены", text)
            self.assertNotIn("Требуют проверки привязки", text)
            self.assertNotIn("не нужна", text)
            self.assertNotIn("не требуется", text)
        legacy = by_name["legacy job without metadata"]["notice"]["text"]
        self.assertIn("Сведения о привязке отсутствуют", legacy)
        self.assertIn("не подтверждены", legacy)
        # The inventory is a warning, never a quarantine: no row is called rejected.
        for result in results:
            if not result["notice"]:
                continue
            text = result["notice"]["text"]
            self.assertNotIn("карантин", text)
            self.assertNotIn("отклонён", text)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_strict_ok_uses_neutral_badge_and_never_promises_science(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "results": [[ATTRIBUTION_JOB, {"solar": {}},
                         {"status": "ok", "tables": {}, "attribution": ATTRIBUTION_FILE}]],
        }, ensure_ascii=False)
        text = self._probe(SEMANTIC_PROBE, payload)["results"][0]["text"]
        self.assertIn("Формальные проверки пройдены", text)
        self.assertIn(STRICT_OK_TEXT, text)
        # The field labels are shown for the reviewer, not the raw names.
        self.assertIn("Время отжига", text)
        self.assertIn("Число приборов", text)
        for marker in FORBIDDEN_SCIENCE_CLAIMS:
            with self.subTest(marker=escaped(marker)):
                self.assertNotIn(marker, text)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_strict_ok_without_attribution_metadata_is_unknown(self):
        payload = json.dumps({
            "claims": [], "counts": [], "states": [], "cells": [], "tables": [],
            "results": [[ATTRIBUTION_JOB, None, {"status": "ok", "tables": {}}]],
        }, ensure_ascii=False)
        text = self._probe(SEMANTIC_PROBE, payload)["results"][0]["text"]
        self.assertIn("Сведения о привязке отсутствуют", text)
        self.assertIn(STRICT_OK_TEXT, text)
        self.assertNotIn("Проверка пройдена", text)


if __name__ == "__main__":
    unittest.main()
