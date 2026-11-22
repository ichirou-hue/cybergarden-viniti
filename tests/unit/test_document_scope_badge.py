"""Regression guard for the document card's scope badge.

The repository has no browser or DOM harness. The badge is a pure decision plus
one DOM node, so the real ``scopeBadge`` from ``static/document.js`` runs under
Node with a minimal element stub: the stub records ``className``,
``textContent`` and attributes, which is all the badge sets.

The rule under test: only the explicit strings ``"true"``/``"false"`` give a
positive/negative scope. Unknown, malformed, boolean or empty values are
unknownness and must render the neutral "Область не определена" badge, never
"в области". Before this guard the badge treated anything except
``"false"``/``"0"``/``""`` as in scope, so ``"maybe"`` and a bare ``true``
silently counted as inside the area.

Where Node is absent the execution cases skip instead of failing, like
``test_parse_ui_states.py``.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
DOCUMENT_JS = STATIC / "document.js"

# The production module keeps a single public export; the probe appends the
# badge helpers to a temporary copy and only reads them.
EXPORT_LINE = "\nexport { scopeBadge, scopeFlag, noticeBlock };\n"

PROBE = """
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
  getElementById: () => ({
    addEventListener() {}, removeAttribute() {}, close() {}, showModal() {},
    open: false, scrollTop: 0, hidden: false, src: "", href: "",
  }),
};
globalThis.location = {hash: ""};
globalThis.history = {replaceState() {}};

const {scopeBadge, scopeFlag, noticeBlock} = await import("./document.js");
const publications = JSON.parse(process.argv[2]);
// A partial, empty result is the summary where noticeBlock consults the scope:
// both "Обработано частично" and "Подтверждённых результатов не найдено" are
// suppressed only when the publication is explicitly out of scope.
const noticeTitles = publication => noticeBlock({
  status: "partial", results_count: 0, publication,
}).map(block => block.children.map(child => child.textContent || child.text || "").join(" "));
const out = publications.map(publication => {
  const badge = scopeBadge(publication);
  return {
    flag: scopeFlag(publication.in_scope),
    class: badge.className,
    text: badge.textContent,
    title: badge.attributes.title ?? "",
    notices: noticeTitles(publication),
  };
});
const bare = scopeBadge(undefined);
out.push({flag: scopeFlag(undefined), class: bare.className, text: bare.textContent});
process.stdout.write(JSON.stringify(out));
"""

NEUTRAL = "Область не определена"

# (name, publication, expected flag, expected class, expected text)
SCOPE_CASES = (
    ("explicit true", {"in_scope": "true"}, "true", "badge idle", "в области"),
    ("true with padding and case", {"in_scope": " TRUE "}, "true", "badge idle", "в области"),
    ("explicit false", {"in_scope": "false"}, "false", "badge warn", "вне области"),
    ("false with padding and case", {"in_scope": " FALSE "}, "false", "badge warn", "вне области"),
    ("false with reason", {
        "in_scope": "false", "exclusion_reason": "Моделирование.",
    }, "false", "badge warn", "вне области: Моделирование."),
    ("field missing", {}, "", "badge idle", NEUTRAL),
    ("field null", {"in_scope": None}, "", "badge idle", NEUTRAL),
    ("field empty", {"in_scope": ""}, "", "badge idle", NEUTRAL),
    ("unknown word", {"in_scope": "maybe"}, "", "badge idle", NEUTRAL),
    ("boolean true", {"in_scope": True}, "", "badge idle", NEUTRAL),
    ("boolean false", {"in_scope": False}, "", "badge idle", NEUTRAL),
    ("numeric zero", {"in_scope": 0}, "", "badge idle", NEUTRAL),
    ("string zero", {"in_scope": "0"}, "", "badge idle", NEUTRAL),
    ("number", {"in_scope": 1}, "", "badge idle", NEUTRAL),
    ("false with blank reason", {
        "in_scope": "false", "exclusion_reason": "  ",
    }, "false", "badge warn", "вне области"),
)


class DocumentScopeBadgeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_module_still_parses(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = pathlib.Path(temporary) / "document.mjs"
            shutil.copyfile(DOCUMENT_JS, module)
            completed = subprocess.run(
                [shutil.which("node"), "--check", str(module)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _probe(self, publications):
        payload = json.dumps(publications, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            for name in ("api.js", "dom.js", "ui.js", "evidence.js"):
                shutil.copyfile(STATIC / name, directory / name)
            (directory / "document.js").write_text(
                DOCUMENT_JS.read_text(encoding="utf-8") + EXPORT_LINE, encoding="utf-8")
            probe = directory / "probe.mjs"
            probe.write_text(PROBE, encoding="utf-8")
            completed = subprocess.run(
                [shutil.which("node"), str(probe), payload],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_scope_badge_only_trusts_explicit_flag(self):
        results = self._probe([publication for _, publication, _, _, _ in SCOPE_CASES])
        self.assertEqual(len(results), len(SCOPE_CASES) + 1)
        for (name, _, flag, css, text), result in zip(SCOPE_CASES, results):
            with self.subTest(case=name):
                self.assertEqual(result["flag"], flag)
                self.assertEqual(result["class"], css)
                self.assertEqual(result["text"], text)
                # The rule under test: no unknown value may read as "в области".
                if not flag:
                    self.assertNotEqual(result["text"], "в области")
        bare = results[-1]
        with self.subTest(case="no publication"):
            self.assertEqual(bare["flag"], "")
            self.assertEqual(bare["class"], "badge idle")
            self.assertEqual(bare["text"], NEUTRAL)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_notice_block_uses_the_same_scope_rule_as_the_badge(self):
        """Regression: noticeBlock read the raw string, so " FALSE " stayed
        in scope and showed "Обработано частично" plus "Подтверждённых
        результатов не найдено" next to an "вне области" badge."""
        results = self._probe([publication for _, publication, _, _, _ in SCOPE_CASES])
        for (name, _, flag, _, _), result in zip(SCOPE_CASES, results):
            with self.subTest(case=name):
                notices = result["notices"]
                if flag == "false":
                    self.assertEqual(notices, [])
                else:
                    self.assertTrue(any("Обработано частично" in n for n in notices))
                    self.assertTrue(any("Подтверждённых результатов не найдено" in n for n in notices))


if __name__ == "__main__":
    unittest.main()
