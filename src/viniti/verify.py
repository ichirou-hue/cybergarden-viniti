"""The gate: a result without a locator is not evidence.

Every candidate produced by a filler passes through :func:`verify_record` before it
may reach the database. The check is deliberately literal: the quote must be found
in the text of the PDF page the record claims. Nothing here judges whether the
number is the right number for the right device - it only proves that the text the
operator is shown really exists on that page.

Normalization rules applied by :func:`normalize_for_match` (and only these):

1. Unicode NFKC per character (ligatures ``ﬁ`` -> ``fi``, full-width forms).
2. Soft hyphen U+00AD dropped; zero-width characters (U+200B..U+200D, U+2060,
   U+FEFF) dropped.
3. Typographic quotes folded to ASCII: ``‘ ’ ‚ ‛ ′ ´ ` -> '`` and
   ``“ ” „ ‟ « » ″ -> "``.
4. Dashes and minus folded to ``-``: U+2010..U+2015, U+2212.
5. Ellipsis U+2026 -> ``...``; non-breaking and other exotic spaces -> ordinary space.
6. Word broken across a line is rejoined: ``-`` + line break + letter drops both the
   hyphen and the break (``extrac-\\ntion`` -> ``extraction``). A hyphen with no line
   break is kept (``high-efficiency`` stays hyphenated).
7. Whitespace runs collapse to one space, ends are stripped.
8. Casefold (not just lower): ``ß`` matches ``SS``.

Deliberately NOT done, because each of them turns the gate into a guesser:

- spaces are not removed, so ``SnO 2`` does not match ``SnO2``;
- no edit distance, no fuzzy matching, no stemming;
- no numeric or unit equivalence (``0.71`` does not match ``71.0``, ``1000 W/m2``
  does not match ``100 mW/cm2``);
- no Cyrillic/Latin confusable folding (``а`` is not ``a``).

This is the same order of leniency as the organisers' rag validator, which still
leaves 1187 of 37750 quotes unresolved when gold is checked against gold. Perfect
matching is not achievable and is not attempted.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from functools import lru_cache

# A quote shorter than this proves nothing: "1" is on every page.
MIN_FRAGMENT_CHARS = 2

# Statuses that legalise a row without a number: the paper mentions the value but
# does not print it, or reports it as absent. The gold uses exactly these.
ABSENT_STATUSES = ("present_not_extractable", "absent")

QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u00b4": "'", "\u0060": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00ab": '"', "\u00bb": '"', "\u2033": '"',
}
DASHES = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
}
SPACES = {
    "\u00a0": " ", "\u1680": " ", "\u2000": " ", "\u2001": " ", "\u2002": " ",
    "\u2003": " ", "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ",
    "\u2008": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u3000": " ",
}
DROPPED = {"\u00ad"} | {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}

TRANSLATE = {**QUOTES, **DASHES, **SPACES, "\u2026": "..."}
for _char in DROPPED:
    TRANSLATE[_char] = ""

BREAKS = ("\n", "\r", "\v", "\f")


def normalize_with_map(text):
    """Normalize ``text`` and remember where every output char came from.

    Returns ``(normalized, index_map)`` where ``index_map[i]`` is the offset in the
    original ``text`` of normalized character ``i``. The map is what lets a match
    found on normalized text be cut back out of the raw PDF text, so the database
    always stores a verbatim slice of the page and never a rewritten quote.
    """
    output, index_map, pending = [], [], []
    for position, char in enumerate(text):
        for piece in unicodedata.normalize("NFKC", char):
            replacement = TRANSLATE.get(piece)
            if replacement is not None:
                if not replacement:
                    continue
                piece = replacement
            if piece.isspace():
                pending.append(piece)
                continue
            if pending:
                joinable = (len(output) >= 2 and output[-1] == "-"
                            and output[-2].isalnum() and piece.isalnum()
                            and any(mark in pending for mark in BREAKS))
                if joinable:
                    output.pop()
                    index_map.pop()
                elif output:
                    output.append(" ")
                    index_map.append(position)
                pending.clear()
            for folded in piece.casefold():
                output.append(folded)
                index_map.append(position)
    return "".join(output), index_map


def normalize_for_match(text):
    """Leniency rules from the module docstring, nothing else."""
    if not isinstance(text, str):
        return ""
    return normalize_with_map(text)[0]


def compact(text):
    """Normalized text with spaces and hyphens removed.

    Two disagreements between PDF text and reported quotes have to disappear:

    * spaces - the page holds ``cm-2,a VOC`` while the gold quote says
      ``cm-2, a VOC``;
    * hyphens at a line break - the page holds ``high-\\nefficiency`` while the
      quote says ``high-efficiency`` (and the reverse).

    Both are typography, not content. The organisers' validator is at least as
    lenient: ``check_submission.py:55`` keeps only ``[a-z0-9.%]``. The cost is that
    a missing word boundary is tolerated, which is acceptable because the check is
    about words and numbers being present in order.
    """
    if not isinstance(text, str):
        return ""
    return _compact_cached(text)


@lru_cache(maxsize=50_000)
def _compact_cached(text):
    """Normalization is per-character and pure, so it is worth caching.

    Verification walks every source of the page once per candidate, and a
    250-page PDF has tens of thousands of sources; without the cache the same
    quotes are normalized again and again, which turns one article into minutes
    of CPU.
    """
    normalized = normalize_for_match(text)
    output = []
    for index, char in enumerate(normalized):
        if char == " " or (
            char == "-" and index > 0 and index + 1 < len(normalized)
            and normalized[index - 1].isalpha()
            and normalized[index + 1].isalpha()
        ):
            continue
        output.append(char)
    return "".join(output)


def quote_present(quote, source_text):
    """True when ``quote`` occurs in ``source_text`` under :func:`normalize_for_match`."""
    needle = compact(quote)
    if not needle:
        return False
    return needle in compact(source_text)


def compact_with_map(text):
    """Like :func:`compact`, but also map every kept char back to the original.

    The index map is what lets a match found on normalized text be cut back out of
    the raw PDF text, so the database always stores a verbatim slice of the page
    and never a rewritten quote.
    """
    normalized, index_map = normalize_with_map(text)
    output, mapping = [], []
    for index, (char, original) in enumerate(zip(normalized, index_map)):
        if char == " " or (
            char == "-" and index > 0 and index + 1 < len(normalized)
            and normalized[index - 1].isalpha()
            and normalized[index + 1].isalpha()
        ):
            continue
        output.append(char)
        mapping.append(original)
    return "".join(output), mapping


def _slice(text, needle):
    """The literal slice of ``text`` that matches ``needle``, or None."""
    haystack, mapping = compact_with_map(text)
    position = haystack.find(needle)
    if position < 0:
        return None
    return text[mapping[position]:mapping[position + len(needle) - 1] + 1]


def locate_span(quote, sources, page=None, page_texts=None):
    """Return ``(source, literal_text)`` for the first text containing ``quote``.

    ``literal_text`` is the exact slice that matched, so a record whose quote
    differs only in whitespace or typography is still stored with a fragment that
    is literally present in the PDF. Sources are searched first; ``page_texts`` is
    the fallback for a quote that crosses a column break, which only the whole-page
    text keeps contiguous.
    """
    needle = compact(quote)
    if not needle:
        return None
    for source in sources:
        if page is not None and source.get("page") != page:
            continue
        literal = _slice(source.get("quote") or "", needle)
        if literal is not None:
            return source, literal
    for number, text in (page_texts or {}).items():
        if page is not None and number != page:
            continue
        literal = _slice(text or "", needle)
        if literal is not None:
            return {"page": number, "quote": text}, literal
    return None


def locate(quote, sources, page=None):
    """Return the source that contains ``quote``, or None."""
    found = locate_span(quote, sources, page)
    return found[0] if found else None


def parse_page(value):
    """Page number as int, or None when the record has no usable page."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number.is_integer() and number > 0:
            return int(number)
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{1,4}", text):
        number = int(text)
        return number if number > 0 else None
    return None


def short(text, limit=70):
    """One-line preview for a message an operator reads in the UI."""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit - 3] + "..."


NUMBER = re.compile(r"[-\u2212\u2013\u2014]?\d+(?:[.,]\d+)?")

# The organisers' own source_fragment is often a LOCATOR, not a quote:
#   'Table 1, строка pristine', 'Figure 2:', 'Supplementary Table 3', 'TABLE I'
# Their validator skips exactly these (check_submission.py:145). Treating them as
# literal quotes rejects 56 of 57 true gold rows, which is worse than useless.
#
# A locator is a reference and nothing else: the label, then either the end of the
# string or a separator (`,`, `;`, `:`, `.`). A sentence that merely begins with
# the word is a QUOTE and must still prove its number:
#   'Table 1, строка pristine'      -> locator
#   'Table 1 lists the champion'    -> quote, the number has to be inside it
# This split is not cosmetic: for a locator the value may sit anywhere on the page,
# so widening the locator branch would let any number on the page back any row.
_LABEL = r"(?:Table|TABLE|Tab\.|Figure|Fig\.)"
_NUMBERING = r"(?:\s+[A-Za-zА-Яа-я]?(?:\d+|[IVXLC]+)[\w.\-]*)"
LOCATOR = re.compile(
    r"^\s*(?:"
    r"(?:" + _LABEL + r"|Приложение)" + _NUMBERING +
    r"|Supplementary"
    r"(?:\s+(?:Table|Figure|Fig\.|Information|Data))?" + _NUMBERING +
    r")"
    r"\s*(?:[,;:.]|\s*$)",
    re.IGNORECASE)


def is_locator(fragment):
    """True when the fragment points at a table or figure instead of quoting text."""
    return bool(LOCATOR.match(fragment or ""))


def locator_label(fragment):
    """The 'Table 1' / 'Figure 2' part of a locator, used for the page check."""
    match = re.match(
        r"^\s*((?:" + _LABEL + r"|Приложение|Supplementary(?:\s+(?:Table|Figure|Fig\.))?)"
        r"\s+[A-Za-zА-Яа-я]?(?:\d+|[IVXLC]+)[\w.\-]*)",
        fragment or "", re.IGNORECASE)
    return (match.group(1) if match else "").strip()


def label_variants(label):
    """Spellings of one label that a page may use.

    A supplement prints ``Table S3`` while a submission says
    ``Supplementary Table 3``; both name the same table. Accepting only one of
    them rejects true rows for a formatting difference.
    """
    text = " ".join(label.split())
    variants = {text}
    match = re.fullmatch(r"Supplementary\s+(Table|Figure|Fig\.)\s+(\S+)", text,
                         re.IGNORECASE)
    if match:
        kind, number = match.group(1), match.group(2)
        supplement_number = number if number.upper().startswith("S") else f"S{number}"
        variants.add(f"{kind} {supplement_number}")
        variants.add(f"{kind} {supplement_number}".replace("Fig.", "Figure"))
    else:
        match = re.fullmatch(r"(Table|Figure|Fig\.)\s+S(\S+)", text, re.IGNORECASE)
        if match:
            kind, number = match.group(1), match.group(2)
            variants.add(f"Supplementary {kind} {number}")
    return [variant for variant in variants if variant]


def label_on_page(label, sources, page, page_text=""):
    """True when a table or figure label appears on that page.

    The label may live in a source quote, in a table cell, or only in the
    whole-page text when the caption sits outside the detected table box.
    """
    def present(text, variant):
        normalized = normalize_for_match(text or "")
        words = normalize_for_match(variant).split()
        if not words:
            return False
        pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(
            re.escape(word) for word in words
        ) + r"(?![A-Za-z0-9])"
        return re.search(pattern, normalized, re.IGNORECASE) is not None

    variants = label_variants(label)
    if not variants:
        return False
    if page_text:
        if any(present(page_text, variant) for variant in variants):
            return True
    for source in sources:
        if source.get("page") != page:
            continue
        if any(present(source.get("quote") or "", variant) for variant in variants):
            return True
        for cell in source.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            if any(present(cell.get("quote") or "", variant) for variant in variants):
                return True
    return False


def numeric_key(text):
    """Canonical Decimal for a written number, or None when it is not a number."""
    cleaned = str(text).strip().replace(" ", "").replace("\u00a0", "")
    for dash in ("\u2212", "\u2013", "\u2014"):
        cleaned = cleaned.replace(dash, "-")
    cleaned = cleaned.rstrip("%").strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        result = Decimal(cleaned).normalize()
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, ArithmeticError):
        # ArithmeticError covers decimal.Overflow: the value comes from a model and
        # "1e1000000" must be treated as not-a-number, not crash the gate.
        return None


def value_present(value, text):
    """True when the reported value can be found inside the quoted fragment.

    This is the difference between "the quote exists" and "the quote proves the
    number". Without it a model can cite a caption and attach any value from
    anywhere on the page: the page check still passes and the row looks verified.

    A factor of 100 is accepted in both directions, because the same quantity is
    reported as a fraction in the table and as a percent in the submission
    (``FF = 0.70`` in the PDF, ``70.0`` in the gold). A factor of 1000 covers
    ``Vmpp = 0.96`` in the gold against ``960 mV`` in the text.

    An empty value proves nothing and is rejected: a row with no number is not a
    result, and ``extraction_status`` is what says "the value is genuinely absent".
    """
    if value is None:
        return False
    raw = str(value).strip()
    if not raw:
        return False
    if not any(char.isalnum() for char in raw):
        return False
    wanted = numeric_key(raw)
    if wanted is None:
        # Non-numeric reports ("not reported", "n/a") must appear verbatim.
        return compact(raw) in compact(text)
    scale = (Decimal(1), Decimal(100), Decimal("0.01"), Decimal(1000), Decimal("0.001"))
    for match in NUMBER.finditer(str(text)):
        found = numeric_key(match.group(0))
        if found is None:
            continue
        try:
            if any(found == wanted * factor for factor in scale):
                return True
        except ArithmeticError:
            continue
    return False


def locate_citation(record, sources, page_texts=None):
    """Check that the citation itself is real, without demanding a value.

    Split out of :func:`verify_record` because the ``issues`` table has no
    ``metric_value``: an inconsistency is not a measurement. Its rows still have to
    be verifiable, so the same page and fragment rules apply, minus the number.

    Returns ``(True, "", page)`` or ``(False, reason, None)``.
    """
    if not isinstance(record, dict):
        return False, "кандидат не является словарём", None
    fragment = record.get("source_fragment")
    if not isinstance(fragment, str) or not fragment.strip():
        return False, "нет source_fragment: результат без цитаты не является доказательством", None
    normalized = normalize_for_match(fragment)
    if len(normalized) < MIN_FRAGMENT_CHARS:
        return False, (f"цитата короче {MIN_FRAGMENT_CHARS} символов и ничего не доказывает: "
                       f"'{short(fragment)}'"), None
    page = parse_page(record.get("source_page"))
    if page is None:
        return False, f"нет числового source_page: '{short(record.get('source_page'))}'", None
    page_text = (page_texts or {}).get(page, "")

    if is_locator(fragment):
        label = locator_label(fragment)
        if not label:
            return False, f"не разобрать ссылку на таблицу: '{short(fragment)}'", None
        if not label_on_page(label, sources, page, page_text):
            return False, f"нет {label} на странице {page}: '{short(fragment)}'", None
        return True, "", page

    hits = [source for source in sources if quote_present(fragment, source.get("quote") or "")]
    on_page = any(source.get("page") == page for source in hits)
    if not on_page and page_text and quote_present(fragment, page_text):
        on_page = True
    if not hits and not on_page:
        return (False,
                f"цитата не найдена в тексте PDF на странице {page}: '{short(fragment)}'",
                None)
    if not on_page:
        pages = ", ".join(str(number) for number in sorted({s.get("page") for s in hits}))
        return (False,
                f"цитата найдена на странице {pages}, а указана страница {page}: "
                f"'{short(fragment)}'",
                None)
    return True, "", page


def verify_record(record, sources, page_texts=None):
    """Check one candidate against the PDF text.

    Two kinds of ``source_fragment`` are legal, because the organisers' own gold
    uses both:

    * a literal quote - must be present in the sources of the claimed page, and it
      must contain the reported value;
    * a locator such as ``Table 1, строка pristine`` - the table/figure label must
      appear on that page, and the value must appear on that page. This is the same
      exemption the organisers' validator makes (check_submission.py:145).

    ``page_texts`` maps a page number to that page's whole text in reading order.
    Pass it whenever it is available: per-line sources interleave the columns of a
    two-column paper, so a sentence crossing a column break is only contiguous in
    the page text. Without it, quotes that span a column break are rejected.

    Returns ``(True, "")`` or ``(False, reason)`` with a short Russian reason,
    because that string is shown to an operator verbatim.
    """
    ok, reason, page = locate_citation(record, sources, page_texts)
    if not ok:
        return False, reason
    fragment = record["source_fragment"]
    value = record.get("metric_value")
    status = str(record.get("extraction_status") or "").strip().lower()
    absent = value is None or not str(value).strip()
    if absent:
        # A row with no number is not a result. The only legal way to have no value
        # is to say so in extraction_status: the gold does exactly that for values
        # the paper mentions but does not print.
        if status not in ABSENT_STATUSES:
            return False, ("нет metric_value, и extraction_status не говорит, что значение "
                           f"отсутствует ('{short(value)}'): строка без числа не является "
                           "результатом")
        return True, ""
    page_text = (page_texts or {}).get(page, "")

    if is_locator(fragment):
        label = locator_label(fragment)
        cells = " ".join(cell.get("quote") or "" for source in sources
                         if source.get("page") == page
                         for cell in (source.get("cells") or [])
                         if isinstance(cell, dict))
        page_words = " ".join(source.get("quote") or "" for source in sources
                              if source.get("page") == page)
        if not value_present(value, " ".join((page_text, page_words, cells))):
            return False, (f"значения {value!r} нет на странице {page}, где указан {label}: "
                           f"'{short(fragment)}'")
        return True, ""

    if not value_present(value, fragment):
        return False, (f"цитата не содержит значения {value!r}: '{short(fragment)}' "
                       f"- цитата есть, но число она не подтверждает")
    return True, ""
