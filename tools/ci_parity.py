"""Static portability audit: Windows dev box (Python 3.14) vs CI (3.12, ubuntu + windows).

CI runs ``python -m unittest -v`` with no ``-X utf8`` on both ubuntu-latest and
windows-latest. A test that is green on the dev machine can therefore be red in
CI, and nobody finds out until submission is late. This script reads source only:
it never imports the project, never needs ``_corpus/`` (745 MiB, gitignored) and
never needs ``data/viniti.sqlite3``, so it runs in CI as it is.

Every finding is one of:

  OK      checked, no action needed (printed only with --show-ok)
  RISK    works today, breaks on a plausible CI machine or a small change
  BREAKS  wrong on at least one CI target right now

Run:
    python -X utf8 tools/ci_parity.py
    python -X utf8 tools/ci_parity.py --fail-on RISK     # future gate, exit 1 on RISK
    python -X utf8 tools/ci_parity.py --only console,newline
    python -X utf8 tools/ci_parity.py --json

Output is deliberately ASCII-safe: offending characters are printed as U+XXXX
escapes, so the auditor itself cannot die of UnicodeEncodeError on a cp1251 or
cp1252 console.
"""
import argparse
import ast
import io
import json
import pathlib
import re
import sqlite3
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent

VERDICTS = ("OK", "RISK", "BREAKS")
RANK = {name: index for index, name in enumerate(VERDICTS)}

# Code pages a Windows console can carry. cp1251 is a Russian dev box, cp1252 is
# the en-US locale GitHub's windows-latest runner reports. Neither is UTF-8.
NARROW = ("cp1251", "cp1252")

# A value that is entirely a Windows path: one or more backslash-separated
# segments of two or more characters, no whitespace, no regex metacharacter.
WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:\\)?[^\\\s()\[\]{}*+?|^$,;]+(?:\\[^\\\s()\[\]{}*+?|^$,;]{2,})+")
REGEX_METACHARS = set("()[]{}*+?|^$\\")


def safe(text):
    """Return text that every Windows console code page can print."""
    out = []
    for char in str(text):
        try:
            char.encode("cp1251")
        except UnicodeEncodeError:
            out.append("U+%04X" % ord(char))
        else:
            out.append(char)
    return "".join(out)


def codepoints_outside(text, encoding):
    """[(index, char, codepoint)] for every char the encoding cannot represent."""
    found = []
    for index, char in enumerate(text):
        try:
            char.encode(encoding)
        except UnicodeEncodeError:
            found.append((index, char, ord(char)))
    return found


class Finding:
    __slots__ = ("rule", "path", "line", "verdict", "message")

    def __init__(self, rule, path, line, verdict, message):
        self.rule = rule
        self.path = path
        self.line = line
        self.verdict = verdict
        self.message = message

    def where(self):
        return "%s:%s" % (self.path, self.line) if self.line else str(self.path)

    def as_dict(self):
        return {"rule": self.rule, "file": str(self.path), "line": self.line,
                "verdict": self.verdict, "message": self.message}

    def sort_key(self):
        return (-RANK[self.verdict], self.rule, str(self.path), self.line or 0)


class Sources:
    """One parsed .py file plus its token stream and parent links."""

    def __init__(self, path, root):
        self.path = path
        self.rel = path.relative_to(root).as_posix()
        self.text = path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.text, filename=self.rel)
        self.strings = self._string_tokens()
        self.docstrings = self._docstrings()
        self.parents = self._parents()

    def _string_tokens(self):
        """[(line, column, token text, decoded value or None)] for string tokens."""
        tokens = []
        readline = io.StringIO(self.text).readline
        try:
            stream = tokenize.generate_tokens(readline)
            for token in stream:
                if token.type != tokenize.STRING:
                    continue
                try:
                    value = ast.literal_eval(token.string)
                except (ValueError, SyntaxError):
                    value = None
                tokens.append((token.start[0], token.start[1], token.string, value))
        except (tokenize.TokenError, IndentationError):
            pass
        return tokens

    def _docstrings(self):
        found = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(node, "body", [])
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                found.add((first.value.lineno, first.value.col_offset))
        return found

    def _parents(self):
        parent = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        return parent

    def is_docstring(self, line, column):
        return (line, column) in self.docstrings

    def ancestors(self, node):
        while node in self.parents:
            node = self.parents[node]
            yield node


def load_sources(root):
    sources = []
    for path in sorted(root.glob("*.py")) + sorted((root / "tools").glob("*.py")):
        try:
            sources.append(Sources(path, root))
        except SyntaxError as error:
            broken = Finding("syntax", path.relative_to(root).as_posix(), error.lineno,
                             "BREAKS", "unparsable: %s" % error.msg)
            sources.append(broken)
    return sources


def live(sources):
    for source in sources:
        if not isinstance(source, Finding):
            yield source


def call_name(node):
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


# --------------------------------------------------------------- 1. backslashes
def check_backslash_paths(sources):
    """A backslash path literal is one nonexistent file name on Linux."""
    findings = []
    for source in live(sources):
        for line, column, raw, value in source.strings:
            if source.is_docstring(line, column):
                continue
            if not isinstance(value, str) or "\\" not in value:
                continue
            if value.count("\\") != value.count("\\") or "\\\\" in value:
                continue
            match = WINDOWS_PATH.fullmatch(value)
            if not match:
                continue
            findings.append(Finding(
                "backslash-path", source.rel, line, "BREAKS",
                "path literal %s uses a backslash separator: on Linux that is one "
                "file name, not a directory. Build it with pathlib: Path(a) / b"
                % safe(repr(value))))
    return findings


# ------------------------------------------------------- 2. non-ASCII in paths
def check_cyrillic_paths(sources):
    """Cyrillic paths are fine through pathlib, risky through a shell or os.path."""
    findings = []
    for source in live(sources):
        joined = set()
        for node in ast.walk(source.tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                for side in (node.left, node.right):
                    for sub in ast.walk(side):
                        if isinstance(sub, ast.Constant):
                            joined.add(id(sub))
            if isinstance(node, ast.Call) and call_name(node) in (
                    "Path", "PurePath", "PosixPath", "WindowsPath"):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant):
                            joined.add(id(sub))
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) not in joined:
                continue
            if not any(ord(char) > 127 for char in node.value):
                continue
            findings.append(Finding(
                "cyrillic-path", source.rel, node.lineno, "OK",
                "non-ASCII path %s is joined with pathlib, so it is a sequence of "
                "path components and not a byte string"
                % safe(repr(node.value))))
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call):
                continue
            if call_name(node) in ("run", "call", "check_output", "Popen", "system"):
                for keyword in node.keywords:
                    if keyword.arg == "shell" and getattr(keyword.value, "value", False):
                        findings.append(Finding(
                            "cyrillic-path", source.rel, node.lineno, "RISK",
                            "subprocess with shell=True: a Cyrillic path becomes a "
                            "shell-quoting problem on one platform only"))
            if call_name(node) in ("fsencode", "fsdecode"):
                findings.append(Finding(
                    "cyrillic-path", source.rel, node.lineno, "RISK",
                    "os.fsencode/fsdecode on a path: bytes length and decoding "
                    "depend on the filesystem encoding, which differs per target"))
    return findings


# ------------------------------------------------------- 3. console encoding
PRINT_FUNCS = {"print"}
STDOUT = "stdout"
STDERR = "stderr"


def _target_of(call):
    for keyword in call.keywords:
        if keyword.arg == "file":
            text = ast.unparse(keyword.value)
            return STDERR if text.endswith("stderr") else STDOUT
    return STDOUT


def _iter_strings(node):
    """Every string constant inside ``node``.

    ``ast.walk`` already descends into ``JoinedStr.values``, so an f-string part
    is a plain ``Constant`` here; matching ``JoinedStr`` as well would yield the
    same node twice and inflate the per-file counts.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub


def _printed_strings(tree):
    """[(node, stream)] for every string that reaches a console."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)
        if name in PRINT_FUNCS:
            target = _target_of(node)
            for arg in node.args:
                for item in _iter_strings(arg):
                    found.append((item, target))
        elif name == "write" and isinstance(node.func, ast.Attribute):
            # Only a console write. ``self.write`` or ``buffer.write`` is a normal
            # method call and must not be mistaken for stdout.
            text = ast.unparse(node.func.value)
            if text.endswith("stderr"):
                target = STDERR
            elif text.endswith(("stdout", "__stdout__")):
                target = STDOUT
            else:
                continue
            for arg in node.args:
                for item in _iter_strings(arg):
                    found.append((item, target))
        elif name in ("SystemExit", "warn"):
            for arg in node.args:
                for item in _iter_strings(arg):
                    found.append((item, STDERR))
        elif name == "ArgumentParser":
            for keyword in node.keywords:
                if keyword.arg in ("description", "epilog", "prog"):
                    for item in _iter_strings(keyword.value):
                        found.append((item, STDOUT))
        elif name == "add_argument":
            for keyword in node.keywords:
                if keyword.arg in ("help", "metavar"):
                    for item in _iter_strings(keyword.value):
                        found.append((item, STDOUT))
    return found


def check_console_encoding(sources):
    """Any printed character the console code page cannot encode is a crash.

    sys.stdout uses errors='strict', so one bad character aborts the process.
    sys.stderr uses backslashreplace, so the same character only looks odd.
    Cyrillic survives cp1251 (a Russian box) but not cp1252 (en-US windows-latest),
    so the two code pages are reported separately: a cp1251 hit is BREAKS today, a
    cp1252-only hit is RISK that only bites when a test calls that CLI entry point.
    """
    findings = []
    for source in live(sources):
        buckets = {}
        for node, target in _printed_strings(source.tree):
            for encoding in NARROW:
                bad = codepoints_outside(node.value, encoding)
                if not bad:
                    continue
                key = (target, encoding)
                buckets.setdefault(key, []).append((node.lineno, node.value, bad))
                break
        for (target, encoding), hits in sorted(buckets.items()):
            hits.sort(key=lambda hit: hit[0])
            first_line, first_value, first_bad = hits[0]
            detail = ", ".join("U+%04X at %d" % (code, index)
                               for index, _, code in first_bad[:6])
            if len(first_bad) > 6:
                detail += ", ..."
            if target == STDERR:
                findings.append(Finding(
                    "console-encoding", source.rel, first_line, "OK",
                    "%d stderr string(s) are not %s-encodable (first at line %d: %s); "
                    "stderr uses backslashreplace, so it escapes instead of crashing"
                    % (len(hits), encoding, first_line, detail)))
                continue
            findings.append(Finding(
                "console-encoding", source.rel, first_line,
                "BREAKS" if encoding == "cp1251" else "RISK",
                "%d stdout string(s) are not %s-encodable (first at line %d: %s in %s); "
                "sys.stdout is errors='strict', so a cp1251 console raises "
                "UnicodeEncodeError and kills the run%s"
                % (len(hits), encoding, first_line, detail,
                   safe(repr(first_value[:40])),
                   "" if encoding == "cp1251" else
                   " - cp1252 is what an en-US windows-latest reports, and no test "
                   "calls these entry points yet")))
    return findings


# ------------------------------------------------------------ 4. file ordering
DISCOVERY = {"glob", "rglob", "iterdir", "listdir", "scandir"}
SORTING = {"sorted", "set", "frozenset", "Counter", "max", "min"}


def check_ordering(sources):
    """NTFS and ext4 return directory entries in different order."""
    findings = []
    for source in live(sources):
        sorted_names = set()
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Call) and call_name(node) in SORTING:
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name):
                            sorted_names.add(sub.id)
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call) or call_name(node) not in DISCOVERY:
                continue
            name = call_name(node)
            if any(isinstance(outer, ast.Call) and call_name(outer) in SORTING
                   for outer in source.ancestors(node)):
                continue
            holder = None
            for outer in source.ancestors(node):
                if isinstance(outer, (ast.Assign, ast.AnnAssign)):
                    holder = outer
                    break
            if holder is not None:
                targets = getattr(holder, "targets", None) or [holder.target]
                names = {sub.id for target in targets for sub in ast.walk(target)
                         if isinstance(sub, ast.Name)}
                if names & sorted_names:
                    continue
            findings.append(Finding(
                "ordering", source.rel, node.lineno, "RISK",
                "%s() result reaches output without sorted(): NTFS and ext4 return "
                "entries in different order, so the output differs between CI targets"
                % name))
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call) or call_name(node) != "sorted":
                continue
            for keyword in node.keywords:
                if keyword.arg == "key":
                    text = ast.unparse(keyword.value)
                    if ".lower()" in text or ".casefold()" in text:
                        findings.append(Finding(
                            "ordering", source.rel, node.lineno, "RISK",
                            "sort key %s folds case: names that differ only in case "
                            "tie, and the tie is broken by filesystem order"
                            % safe(text)))
    return findings


# --------------------------------------------------------- 5. file name case
# Only artifacts whose names come from outside the repo: a *.PY source file is not
# a realistic name, a *.PDF in the corpus or a *.CSV from the organisers is.
ARTIFACT_SUFFIXES = {"pdf", "csv", "json", "jsonl", "txt", "xml", "sqlite3", "db"}


def check_case(sources):
    """Linux is case-sensitive, Windows is not."""
    findings = []
    for source in live(sources):
        patterns = set()
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Call) and call_name(node) in ("glob", "rglob") \
                    and node.args:
                value = getattr(node.args[0], "value", None)
                if isinstance(value, str):
                    patterns.add(value)
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call) or call_name(node) not in ("glob", "rglob"):
                continue
            if not node.args:
                continue
            pattern = getattr(node.args[0], "value", None)
            if not isinstance(pattern, str):
                continue
            suffix = pattern.rsplit(".", 1)[-1]
            if suffix.lower() not in ARTIFACT_SUFFIXES:
                continue
            if suffix and suffix.islower() and suffix.upper() not in suffix:
                upper = pattern[:-len(suffix)] + suffix.upper()
                if upper in patterns:
                    continue
                findings.append(Finding(
                    "case", source.rel, node.lineno, "RISK",
                    "glob(%s) misses %s on Linux (case-sensitive) but matches it on "
                    "Windows: the two CI targets see different file sets"
                    % (safe(repr(pattern)), safe(repr(upper)))))
            elif suffix != suffix.lower():
                findings.append(Finding(
                    "case", source.rel, node.lineno, "RISK",
                    "glob(%s) relies on case-insensitive matching" % safe(repr(pattern))))
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Compare):
                text = ast.unparse(node)
                if ("casefold()" in text or ".lower() ==" in text or ".lower() !=" in text) \
                        and (".name" in text or "name" in text):
                    findings.append(Finding(
                        "case", source.rel, node.lineno, "RISK",
                        "file name compared case-insensitively (%s): two distinct "
                        "files on Linux are one file here" % safe(text[:70])))
    return findings


# --------------------------------------------------------------- 6. SQLite
SQLITE_FEATURES = (
    (3, 24, r"ON\s+CONFLICT[\s\S]{0,80}DO\s+UPDATE", "UPSERT"),
    (3, 25, r"\bOVER\s*\(", "window function"),
    (3, 35, r"\bRETURNING\b", "RETURNING clause"),
    (3, 37, r"\bSTRICT\b", "STRICT table"),
    (3, 38, r"\bjson_(extract|array_length|each|group_array)\b", "JSON1 function"),
    (3, 38, r"->>?", "JSON arrow operator"),
    (3, 39, r"\b(FULL|RIGHT)\s+JOIN\b", "FULL/RIGHT JOIN"),
)


def version_tuple(text):
    parts = []
    for piece in text.split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def check_sqlite(sources):
    """Python 3.12 and 3.14 bundle different SQLite versions."""
    findings = []
    current = version_tuple(sqlite3.sqlite_version)
    findings.append(Finding(
        "sqlite", "<runtime>", 0, "OK",
        "this interpreter bundles SQLite %s; Python 3.12 on the CI runners bundles "
        "an older build, so only features above that build's minimum matter"
        % sqlite3.sqlite_version))
    statement = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|PRAGMA|WITH)\b",
                           re.IGNORECASE)
    for source in live(sources):
        for line, column, raw, value in source.strings:
            if not isinstance(value, str) or source.is_docstring(line, column):
                continue
            if not statement.search(value):
                continue                    # prose, not SQL: "text -> value" is not JSON
            for major, minor, pattern, label in SQLITE_FEATURES:
                if not re.search(pattern, value, re.IGNORECASE):
                    continue
                findings.append(Finding(
                    "sqlite", source.rel, line,
                    "OK" if current >= (major, minor) else "RISK",
                    "%s needs SQLite %d.%d or newer" % (label, major, minor)))
    return findings


# ------------------------------------------------------------- 7. atomic write
def check_atomic(sources):
    """os.rename fails on Windows when the target exists; os.replace does not."""
    findings = []
    for source in live(sources):
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            receiver = ast.unparse(node.func.value)
            if name == "replace" and receiver in ("os", "nt", "posix"):
                findings.append(Finding(
                    "atomic-write", source.rel, node.lineno, "OK",
                    "os.replace() overwrites an existing target on both platforms"))
            elif name == "rename" and (receiver in ("os", "nt", "posix") or
                                       ".rename" in ast.unparse(node.func)):
                findings.append(Finding(
                    "atomic-write", source.rel, node.lineno, "RISK",
                    "rename() fails on Windows when the target exists, and fails "
                    "across filesystems: use os.replace() on a temp file created in "
                    "the same directory"))
            elif name == "move" and receiver == "shutil":
                findings.append(Finding(
                    "atomic-write", source.rel, node.lineno, "RISK",
                    "shutil.move() is not atomic and differs per platform: use "
                    "os.replace() on a temp file in the same directory"))
            elif name in ("NamedTemporaryFile", "mkstemp"):
                if not any(keyword.arg == "dir" for keyword in node.keywords):
                    findings.append(Finding(
                        "atomic-write", source.rel, node.lineno, "RISK",
                        "%s() without dir=: the temp file may land on another "
                        "filesystem, so the following rename is not atomic" % name))
    return findings


# ------------------------------------------------- 8. Python 3.12 compatibility
PEP695_NODES = {"TypeAlias", "TypeVar", "ParamSpec", "TypeVarTuple", "MatchType"}


def check_syntax(sources):
    findings = [Finding(
        "syntax", "<runtime>", 0, "OK",
        "parsing against the 3.12 grammar, the version CI pins (this interpreter is "
        "%d.%d)" % (sys.version_info[0], sys.version_info[1]))]
    for source in sources:
        if isinstance(source, Finding):
            findings.append(source)
            continue
        try:
            ast.parse(source.text, filename=source.rel, feature_version=(3, 12))
        except SyntaxError as error:
            findings.append(Finding(
                "syntax", source.rel, error.lineno, "BREAKS",
                "does not parse as Python 3.12: %s" % error.msg))
        for node in ast.walk(source.tree):
            if type(node).__name__ in PEP695_NODES:
                findings.append(Finding(
                    "syntax", source.rel, node.lineno, "BREAKS",
                    "%s is PEP 695 syntax, Python 3.12 only at the earliest, and the "
                    "grammar check above already accepts it only if the interpreter "
                    "does" % type(node).__name__))
    return findings


# ------------------------------------------------------- 9. newline / encoding
def _open_mode(node):
    """(is_text_mode, has_newline, has_encoding) for a file-opening call.

    ``open(path, mode)`` is the builtin (mode at position 1); ``path.open(mode)``
    is a method (mode at position 0). When the mode argument is not a literal the
    call is skipped: ``pdfplumber.open(BytesIO(data))`` is not a file open at all.
    """
    keywords = {keyword.arg for keyword in node.keywords}
    position = 1 if isinstance(node.func, ast.Name) else 0
    mode = None
    if len(node.args) > position:
        candidate = node.args[position]
        if not isinstance(candidate, ast.Constant) or not isinstance(candidate.value, str):
            return None
        mode = candidate.value
    if mode is None:
        text_mode = "mode" not in keywords
    else:
        text_mode = "b" not in mode
    return text_mode, "newline" in keywords, "encoding" in keywords


def check_newline(sources):
    """Text mode without newline='' is the classic Windows \\r\\r\\n bug."""
    findings = []
    for source in live(sources):
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name == "open":
                shape = _open_mode(node)
                if shape is None:
                    continue
                text_mode, has_newline, has_encoding = shape
                if text_mode and not has_newline:
                    findings.append(Finding(
                        "newline", source.rel, node.lineno, "RISK",
                        "text-mode open() without newline='': Windows turns every \\n "
                        "into \\r\\n, and a value that already holds \\r\\n becomes "
                        "\\r\\r\\n, so the bytes differ between CI targets"))
                if text_mode and not has_encoding:
                    findings.append(Finding(
                        "newline", source.rel, node.lineno, "RISK",
                        "text-mode open() without encoding=: Windows picks cp1251 and "
                        "Linux picks UTF-8, so the same bytes decode differently"))
            elif name == "write_text":
                findings.append(Finding(
                    "newline", source.rel, node.lineno, "RISK",
                    "write_text() cannot pass newline='': it writes CRLF on Windows "
                    "and LF on Linux, so the file bytes differ between CI targets "
                    "whenever those bytes are compared or committed"))
            elif name == "read_text":
                if not any(keyword.arg == "encoding" for keyword in node.keywords):
                    findings.append(Finding(
                        "newline", source.rel, node.lineno, "RISK",
                        "read_text() without encoding=: the platform code page decides"))
            elif name == "writer":
                if not any(keyword.arg == "lineterminator" for keyword in node.keywords):
                    findings.append(Finding(
                        "newline", source.rel, node.lineno, "RISK",
                        "csv.writer without lineterminator: the terminator comes from "
                        "the stream dialect, and the submission format is fixed"))
    return findings


CHECKS = {
    "backslash": check_backslash_paths,
    "cyrillic": check_cyrillic_paths,
    "console": check_console_encoding,
    "ordering": check_ordering,
    "case": check_case,
    "sqlite": check_sqlite,
    "atomic": check_atomic,
    "newline": check_newline,
}


def audit(root):
    sources = load_sources(root)
    findings = []
    for check in CHECKS.values():
        findings.extend(check(sources))
    findings.extend(check_syntax(sources))
    findings.append(Finding(
        "filesystem", "<runtime>", 0,
        "OK" if sys.getfilesystemencoding().lower().replace("-", "") == "utf8"
        else "RISK",
        "filesystem encoding is %s; a non-UTF-8 one cannot create the Cyrillic "
        "corpus directory" % sys.getfilesystemencoding()))
    findings.sort(key=Finding.sort_key)
    return findings, sources


def counts_of(findings):
    counts = {name: 0 for name in VERDICTS}
    for finding in findings:
        counts[finding.verdict] += 1
    return counts


def report(findings, sources, show_ok=False):
    counts = counts_of(findings)
    current_rule = None
    for finding in findings:
        if finding.verdict == "OK" and not show_ok:
            continue
        if finding.rule != current_rule:
            print("\n== %s" % finding.rule)
            current_rule = finding.rule
        print("  %-6s %s: %s" % (finding.verdict, finding.where(),
                                 safe(finding.message)))
    print("\n%d files scanned: %d BREAKS, %d RISK, %d OK"
          % (len(sources), counts["BREAKS"], counts["RISK"], counts["OK"]))
    return counts


def harden_streams():
    """Make the audit itself unkillable by a narrow console.

    ``safe()`` escapes what cp1251 cannot print, but a cp1252 console (en-US
    windows-latest) also rejects Cyrillic, and that would abort this tool with
    UnicodeEncodeError - the very bug it reports. ``errors='backslashreplace'``
    degrades such a character to ``\\u041f`` instead.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass  # not a TextIOWrapper (a test captured it), nothing to do


def main(argv):
    harden_streams()
    parser = argparse.ArgumentParser(
        description="Static portability audit of the Windows/3.14 dev box against "
                    "the 3.12 ubuntu+windows CI matrix")
    parser.add_argument("--root", type=pathlib.Path, default=ROOT,
                        help="repository root (default: the parent of tools/)")
    parser.add_argument("--fail-on", choices=VERDICTS + ("NONE",), default="BREAKS",
                        help="exit 1 if any finding reaches this verdict "
                             "(default: BREAKS)")
    parser.add_argument("--only", default="",
                        help="comma-separated rules: " + ", ".join(sorted(CHECKS)))
    parser.add_argument("--show-ok", action="store_true",
                        help="also print OK findings")
    parser.add_argument("--json", action="store_true",
                        help="print only the JSON report instead of the text one")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    if not root.is_dir():
        print("no such root: %s" % root, file=sys.stderr)
        return 2
    findings, sources = audit(root)
    if args.only:
        wanted = [name.strip() for name in args.only.split(",") if name.strip()]
        findings = [finding for finding in findings
                    if any(finding.rule.startswith(name) for name in wanted)]

    counts = counts_of(findings)
    if args.json:
        print(json.dumps({"files": len(sources),
                          "counts": counts,
                          "findings": [finding.as_dict() for finding in findings]},
                         ensure_ascii=True, indent=1))
    else:
        report(findings, sources, show_ok=args.show_ok)
    if args.fail_on == "NONE":
        return 0
    worst = max([RANK[finding.verdict] for finding in findings] or [0])
    return 1 if worst >= RANK[args.fail_on] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
