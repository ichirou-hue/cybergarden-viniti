"""Deterministic solar extraction: PDF layout -> the seven submission tables.

This module is the model-free counterpart of :mod:`solar_extract`. It reads the
PDF itself (word coordinates, page text), finds the places where the paper
states a measured number, and builds the same candidate dictionary a model would
return. :func:`solar_facts.finalize` then validates every quote and normalizes
units, so nothing here is trusted on its own.

Why word coordinates and not only ``pdf_source`` sources: most photovoltaic
tables in this corpus are borderless or have merged cells, so pdfplumber's table
detector either misses them or merges a header with body text. A column can
still be recovered from the x position of a number relative to the x position of
its header label, and that is what :func:`table_records` does.

What is extracted
-----------------
* ``results`` - from table rows (header-driven column mapping) and from prose
  sentences that name a metric and a number;
* ``devices`` - one row per distinct device label seen in tables or prose;
* ``materials`` - absorber formulas named in the device stack, with the
  material class inferred from the formula;
* ``layers`` - the layer stack written as ``A/B/C`` in the experimental section;
* ``measurements`` - the measurement conditions named near the results;
* ``publications`` - title, year, DOI and authors from the first page and
  PDF metadata;
* ``issues`` - left empty; :func:`solar_facts.check_consistency` fills them.

Everything that cannot be tied to a literal quote is dropped rather than
guessed. ``absent`` is never inferred from a failed parse.
"""

from collections import Counter
from io import BytesIO
import re
import time
import unicodedata

import pdfplumber

from . import solar_facts
from . import solar_schema
from .solar_version import PARSER_VERSION
from viniti import diagnostics

logger = diagnostics.get_logger("solar_offline")

# ---------------------------------------------------------------- vocabulary

METRIC_PATTERNS = (
    # (metric, pattern). Order matters: the first match wins.
    ("stabilized_PCE", r"stabilized\s*(?:pce|efficiency)|stabilised\s*(?:pce|efficiency)|steady[- ]state\s*(?:pce|efficiency)"),
    ("retained_PCE", r"retained\s*pce|retention\s*(?:of\s*)?pce|pce\s*retention"),
    ("Jsc_EQE", r"jsc\s*(?:from\s*)?eqe|jsc_eqe|integrated\s*(?:jsc|photocurrent|current\s*density)"),
    ("PCE", r"\bpce\b|power\s*conversion\s*efficien\w*|\befficienc\w*|\beff\.?\b|\u03b7|\beta\b"),
    ("Jsc", r"\bjsc\b|\bj\s*sc\b|j_sc|short[- ]circuit\s*current"),
    ("Voc_deficit", r"voc\s*deficit|v\s*oc\s*deficit|voltage\s*deficit"),
    ("Voc", r"\bvoc\b|\bv\s*oc\b|v_oc|open[- ]circuit\s*voltage"),
    ("FF", r"\bff\b|fill\s*factor"),
    ("Rsh", r"\brsh\b|\br\s*sh\b|shunt\s*resistance"),
    ("Rs", r"\brs\b|\br\s*s\b|series\s*resistance"),
    ("HI", r"\bhi\b|hysteresis\s*index"),
    ("T80", r"\bt80\b"),
    ("Jmpp", r"\bjmpp\b|\bj\s*mpp\b"),
    ("Vmpp", r"\bvmpp\b|\bv\s*mpp\b"),
    ("AVT", r"\bavt\b|average\s*visible\s*transmittance"),
    ("LUE", r"\blue\b|light\s*utilization"),
    ("CRI", r"\bcri\b|color\s*rendering\s*index"),
    ("bifaciality_factor", r"bifaciality\s*factor"),
    ("Eg", r"\beg\b|\be\s*g\b|band\s*gap|bandgap"),
)
METRIC_RE = tuple((name, re.compile(pattern, re.I)) for name, pattern in METRIC_PATTERNS)

# Metrics the submission template scores. Extra metrics are still extracted
# (the template allows them) but are marked so callers can filter.
SCORED_METRICS = {
    "PCE", "Voc", "Jsc", "FF", "Rs", "Rsh", "HI", "Jsc_EQE",
    "stabilized_PCE", "T80", "retained_PCE",
}

UNIT_PATTERNS = (
    ("mA/cm2", r"ma\s*/?\s*cm\s*[-−]?\s*(?:2|²)|ma\s*cm\s*[-−]?\s*(?:2|²)"),
    ("mA/cm2", r"ma\s*/\s*cm\^?2"),
    ("mV", r"\bmv\b"),
    ("V", r"\bv\b"),
    ("%", r"%|percent"),
    ("fraction", r"fraction"),
    ("Ohm*cm2", r"(?:Ω|ohm)\s*[·*]?\s*cm\s*(?:2|²)"),
    ("h", r"\bh\b|hours?"),
    ("min", r"\bmin\b"),
    ("s", r"\bs\b|seconds?"),
    ("eV", r"\bev\b"),
    ("nm", r"\bnm\b"),
    ("um", r"\b[μµ]m\b|\bum\b"),
    ("cm2", r"\bcm\s*(?:2|²)\b"),
)

NUMBER = re.compile(r"[+\-\u2212]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?")
NUMBER_FULL = re.compile(r"^[+\-\u2212]?\(?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?\)?$")
# A table value: an optional parenthesis, a number, and a percent sign either
# inside or outside the parenthesis. ``(16.5)`` is a champion value, while
# ``14.0%`` and ``(16.5%)`` are the same number with the unit glued on.
VALUE_WORD = re.compile(
    r"^(?P<open>\()?"
    r"(?P<number>[+\-\u2212]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?)"
    r"(?P<percent>%?)(?P<close>\)?)(?P<trailing>%?)$")

# A row label longer than this is prose, not a table label.
LABEL_LIMIT = 90
# Rows whose baselines differ by less than this belong to one logical row
# (a mean line, a device label line and a "(champion)" line).
BAND_GAP = 9.0
# A table ends when the vertical gap between data lines exceeds this.
BLOCK_GAP = 26.0

CAPTION = re.compile(r"^(?:table|таблица)\s+\d+", re.I)
FIGURE = re.compile(r"^(?:figure|fig\.?|рис(?:унок)?\.?)\s+\d+", re.I)

CHAMPION_HINTS = re.compile(r"champion|record|best\b|maximum\b|highest\b", re.I)
MEAN_HINTS = re.compile(r"\bmean\b|\baverage\b|\btypical\b|\bmedian\b", re.I)
PAREN_HINT = re.compile(r"parenthes|in\s+brackets|\(between", re.I)


def clean(text):
    """Same normalization solar_facts uses for quotes."""
    return " ".join(unicodedata.normalize("NFKC", text or "").replace("\u2212", "-").split())


def _dedupe_label(label):
    """Device label in the form the prose/table duplicate check compares.

    ``Control`` printed in a table and ``control`` written in a sentence name
    the same sample. An empty label stays empty: a value with no named device is
    not evidence that it belongs to a table row of some other device.
    """
    return clean(label).lower()


def metric_from_text(text):
    """Return the metric named in a header label or sentence fragment."""
    text = clean(text)
    for name, pattern in METRIC_RE:
        if pattern.search(text):
            return name
    return ""


def unit_from_text(text):
    text = clean(text)
    for unit, pattern in UNIT_PATTERNS:
        if re.search(pattern, text, re.I):
            return unit
    return ""


def number_text(token):
    """Canonical decimal text; keeps the sign.

    A comma before exactly three digits is a thousands separator in this corpus
    (``1,197 mV``), everywhere else it is the decimal comma (``0,71``).
    """
    token = clean(token).replace("\u2212", "-").strip("()")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", token):
        token = token.replace(",", "")
    else:
        token = token.replace(",", ".")
    if token.startswith("+"):
        token = token[1:]
    return token


def is_number(token):
    return bool(NUMBER_FULL.match(clean(token).replace(" ", "")))


def decimal_of(token):
    try:
        return float(number_text(token))
    except ValueError:
        return None


# ---------------------------------------------------------------- layout

def _words(page):
    """Words with coordinates, de-duplicated when the PDF prints text twice."""
    return page.dedupe_chars().extract_words(x_tolerance=1.5, y_tolerance=2)


def _lines(words, tolerance=3.0):
    """Group words into visual lines, top to bottom, left to right."""
    if not words:
        return []
    ordered = sorted(words, key=lambda word: (word["top"], word["x0"]))
    lines, current = [], [ordered[0]]
    for word in ordered[1:]:
        if abs(word["top"] - current[0]["top"]) <= tolerance:
            current.append(word)
        else:
            lines.append(current)
            current = [word]
    lines.append(current)
    result = []
    for group in lines:
        group.sort(key=lambda word: word["x0"])
        result.append({
            "top": min(word["top"] for word in group),
            "bottom": max(word["bottom"] for word in group),
            "x0": min(word["x0"] for word in group),
            "x1": max(word["x1"] for word in group),
            "words": group,
            "text": " ".join(word["text"] for word in group),
        })
    return result


def _gutter(words, width):
    """Centre of the vertical band that separates two text columns.

    Two-column papers interleave the columns line by line, so a row of a table in
    the left column would otherwise swallow a sentence of the right column and
    stop looking like a table row.

    The test is not "is this x empty" - a title or a caption bridges the gutter.
    The test is "does a gap sit here in most lines": a gap between words that
    repeats down the page is a column gutter, while a gap inside one table row
    repeats only within that table.
    """
    if not words:
        return None
    lines = _lines(words)
    if len(lines) < 6:
        return None
    low, high = int(0.25 * width), int(0.75 * width)
    gaps = [0] * (high + 2)
    for line in lines:
        previous = None
        for word in line["words"]:
            if previous is not None and word["x0"] - previous > 3.0:
                start = max(int(previous), low)
                stop = min(int(word["x0"]), high) + 1
                for x in range(start, stop):
                    if x >= 0:
                        gaps[x] += 1
            previous = word["x1"]
    need = 0.55 * len(lines)
    best, run, best_start = 0, 0, 0
    for x in range(low, high + 1):
        if gaps[x] >= need:
            if run == 0:
                start = x
            run += 1
            if run > best:
                best, best_start = run, start
        else:
            run = 0
    if best < 7:
        return None
    return best_start + best / 2


def _group_lines(words, width):
    """Split a page into column groups and turn each into visual lines.

    A word that crosses the gutter is part of a full-width element (title,
    caption, rule) and goes to its own group, which never mixes with body text.
    """
    gutter = _gutter(words, width)
    if gutter is None:
        return [_lines(words)]
    left = [word for word in words if word["x1"] <= gutter + 1]
    right = [word for word in words if word["x0"] >= gutter - 1]
    wide = [word for word in words if word["x1"] > gutter + 1 and word["x0"] < gutter - 1]
    groups = []
    for part in (left, right, wide):
        if part:
            groups.append(_lines(part))
    return groups


def layout(data):
    """Word-level page model: lines per column group, one entry per page."""
    pages = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        for page in pdf.pages:
            words = _words(page)
            pages.append({
                "number": page.page_number,
                "width": page.width,
                "height": page.height,
                "lines": _lines(words),
                "groups": _group_lines(words, page.width),
            })
    return {"pages": pages}


# ---------------------------------------------------------------- columns

def _column_centers(tokens_by_row, tolerance=9.0):
    """Cluster the x centres of numeric tokens into vertical columns.

    Data drives the geometry, not the header: header words in this corpus are
    routinely split by subscripts (``V`` / ``OC``) or shifted by merged cells,
    while a number always sits under the column it belongs to.
    """
    points = sorted(token["center"] for tokens in tokens_by_row for token in tokens)
    if not points:
        return []
    groups = [[points[0]]]
    for point in points[1:]:
        if point - groups[-1][-1] <= tolerance:
            groups[-1].append(point)
        else:
            groups.append([point])
    # Drop a group that only one value ever hits and that is far from the rest:
    # it is usually a stray number in a footnote.
    return [sum(group) / len(group) for group in groups if len(group) >= 2]


UNIT_WORDS = {
    "ma", "cm", "nm", "mv", "v", "ohm", "ω", "ev", "cm2", "a", "w", "mw",
    "m", "s", "h", "min", "um", "μm", "µ", "2",
}


def _bare_words(words):
    """Column label without units, parentheses and subscripts-as-numbers.

    ``J (mA cm −2) SC`` becomes ``J SC`` so the metric pattern can see the two
    letters that name the column.
    """
    kept = []
    for text in words:
        lowered = text.lower().strip("()[].,;:")
        if not lowered:
            continue
        if lowered in UNIT_WORDS or re.fullmatch(r"[−-]?\d+\)?", lowered):
            continue
        kept.append(text)
    return " ".join(kept)


def _headerish(line):
    """A header line is short, or names a metric, or is made of units.

    A body line that happens to sit above a table must not leak into the header:
    ``concentrations under simulated AM 1.5G of 100 mW/cm2`` is prose, while
    ``(mV) (mA/cm2) (%) (%) (Ωcm2)`` is a units row.
    """
    words = line["words"]
    if len(words) <= 4:
        return True
    text = line["text"]
    if metric_from_text(text):
        return True
    units = sum(1 for word in words
                if "(" in word["text"] or ")" in word["text"]
                or unit_from_text(word["text"]))
    return units / len(words) >= 0.6


def _header_for(lines, start, centers):
    """Map the header words above a data block onto the numeric columns.

    Returns ``{center: {"text", "metric", "unit"}}``. Lines within 45 pt above
    the first data row are read, subscripts included; a caption line or a prose
    line ends the header.
    """
    block_top = lines[start]["top"]
    band = []
    cursor = start - 1
    while cursor >= 0:
        line = lines[cursor]
        if block_top - line["top"] > 45:
            break
        text = line["text"].strip()
        if CAPTION.match(text) or FIGURE.match(text):
            break
        if _headerish(line):
            band.append(line)
        cursor -= 1
    band.reverse()
    columns = {}
    for line in band:
        for word in line["words"]:
            center = (word["x0"] + word["x1"]) / 2
            nearest = min(centers, key=lambda value: abs(value - center))
            if abs(nearest - center) > 26:
                continue
            column = columns.setdefault(nearest, {"words": [], "x0": word["x0"], "x1": word["x1"]})
            column["words"].append((word["x0"], word["top"], word["text"]))
            column["x0"] = min(column["x0"], word["x0"])
            column["x1"] = max(column["x1"], word["x1"])
    result = {}
    for center, column in columns.items():
        # Reading order for a column is left to right: a subscript ("OC") sits
        # below and slightly right of its letter ("V").
        column["words"].sort()
        text = " ".join(item[2] for item in column["words"])
        unit = unit_from_text(text)
        metric = metric_from_text(text)
        bare = _bare_words([item[2] for item in column["words"]])
        if not metric:
            metric = metric_from_text(bare)
        if not metric:
            # A lone symbol is common when a header is split over two lines:
            # "V" + "(mV)" is Voc, "J" + "(mA/cm2)" is Jsc. Units are stripped
            # first, so "(mV)" cannot hide the "V".
            symbols = [item[2] for item in column["words"]
                       if re.search(r"[A-Za-z]", item[2])
                       and not unit_from_text(item[2])
                       and item[2].lower().strip("()[].,;:") not in UNIT_WORDS
                       and not re.fullmatch(r"[−-]?\d+\)?", item[2].lower().strip("()[].,;:"))]
            if not symbols:
                letters = [item[2] for item in column["words"] if re.search(r"[A-Za-z]", item[2])]
                symbols = (letters or [item[2] for item in column["words"]])[:1]
            key = re.sub(r"[^a-z]", "", " ".join(symbols).lower())
            if key in ("v", "voc") and unit in ("V", "mV", ""):
                metric = "Voc"
            elif key in ("j", "jsc") and unit in ("mA/cm2", ""):
                metric = "Jsc"
            elif key in ("e", "eg") and unit == "eV":
                metric = "Eg"
            elif key in ("r", "rs") and unit == "Ohm*cm2":
                metric = "Rs"
        if not metric:
            continue
        result[center] = {
            "text": text, "metric": metric, "unit": unit,
            "center": center, "x0": column["x0"], "x1": column["x1"],
        }
    return result


def _label_boundary(lines, start, centers):
    """Left edge of the first value column.

    A ``Sample``/``Device`` header word is the most reliable marker; without one
    the leftmost numeric column minus a character's width is used.
    """
    block_top = lines[start]["top"]
    for line in reversed(lines[:start]):
        if block_top - line["top"] > 45:
            break
        if not _headerish(line):
            continue
        for word in line["words"]:
            if re.fullmatch(r"sample|samples|device|devices|cell|cells|absorber|stack|label|no\.|direction",
                            word["text"], re.I):
                return word["x1"] + 20
    return (min(centers) - 15) if centers else 0


def _parse_value(text):
    """One printed number: ``(literal, parenthesized, percent)`` or ``None``.

    Parentheses must be balanced and a percent sign may sit inside or outside
    them, but not both (``16.5%%`` is a typo, not a value).
    """
    match = VALUE_WORD.match(text)
    if not match or bool(match.group("open")) != bool(match.group("close")):
        return None
    if match.group("percent") and match.group("trailing"):
        return None
    return (match.group("number"),
            bool(match.group("open")),
            bool(match.group("percent") or match.group("trailing")))


def _value_tokens(line):
    """Numeric tokens of a data line with their x centre, joined when split.

    ``6.7 × 10 −3`` arrives as four words; it is one value. A ``±`` after a value
    is its absolute spread. The exponent may itself carry the percent sign
    (``10-3%``), which belongs to the whole value, not to the exponent.
    """
    tokens = []
    words = line["words"]
    index = 0
    while index < len(words):
        word = words[index]
        text = clean(word["text"])
        if text in ("±", "+/-"):
            if tokens and index + 1 < len(words) and is_number(words[index + 1]["text"]):
                tokens[-1]["spread"] = number_text(words[index + 1]["text"])
                tokens[-1]["x1"] = words[index + 1]["x1"]
                index += 2
                continue
            index += 1
            continue
        parsed = _parse_value(text)
        if parsed:
            number, parenthesized, percent = parsed
            value = text
            shown_percent = percent
            x0, x1 = word["x0"], word["x1"]
            cursor = index + 1
            if cursor + 2 < len(words) and clean(words[cursor]["text"]) in ("×", "x") \
                    and clean(words[cursor + 1]["text"]) == "10":
                exponent = _parse_value(clean(words[cursor + 2]["text"]))
                if exponent:
                    number = f"{number}e{exponent[0]}"
                    percent = percent or exponent[2]
                    value = number
                    shown_percent = False
                    x1 = words[cursor + 2]["x1"]
                    parenthesized = False
                    cursor += 3
            if cursor < len(words) and clean(words[cursor]["text"]) == "%":
                # ``(16.5) %`` and ``16.5 %`` print the unit as a separate word;
                # a value that already shows the sign keeps it once.
                if not shown_percent:
                    value = f"{value}%"
                    shown_percent = True
                    x1 = words[cursor]["x1"]
                percent = True
                cursor += 1
            if percent and not shown_percent:
                value = f"{value}%"
            tokens.append({"text": value, "literal": number, "percent": percent,
                           "x0": x0, "x1": x1, "center": (x0 + x1) / 2,
                           "parenthesized": parenthesized, "spread": ""})
            index = cursor
            continue
        index += 1
    return tokens


def _row_words(line):
    """Words of a data line, cut after the last numeric value.

    A two-column page welds a table row to the sentence printed beside it:
    ``0.01 M SrCl2 15.67 1,278 0.73 14.61 We then studied ...``. The values of a
    row always end at the last number, so the prose tail is dropped instead of
    making the whole line look like prose.
    """
    tokens = _value_tokens(line)
    if not tokens:
        return line["words"]
    stop = max(token["x1"] for token in tokens) + 3
    return [word for word in line["words"] if word["x1"] <= stop]


def _is_real(token):
    """A value token, as opposed to a subscript or a stray digit.

    ``-2)`` from ``mA cm-2`` and ``2`` from ``SrCl2`` are not measurements.
    """
    text = token["text"].strip().rstrip("%").strip("()").lstrip("+-")
    return "." in text or len(text) >= 2


def _data_tokens(line):
    """Tokens of the line that plausibly belong to a table body.

    Two numbers on one line is already a table row; one number is accepted only
    when the line is mostly numeric, which keeps prose out. Single-digit tokens
    without a decimal point are subscripts (``Sb2Se3``), not values, and a line
    whose only numbers are such subscripts is prose.
    """
    words = _row_words(line)
    tokens = [token for token in _value_tokens(line)
              if token["x1"] <= max((word["x1"] for word in words), default=0) + 0.01]
    if not tokens:
        return []
    real = [token for token in tokens if _is_real(token)]
    if not real:
        return []
    # A header names metrics and units; it may carry one stray number from a
    # neighbouring column ("... PCE (%) device increased to 1,245"), but a real
    # row with a metric label carries a whole line of values.
    if len(real) <= 1 and metric_from_text(line["text"]):
        return []
    count = len(words)
    if count > 16 and len(real) / count < 0.4:
        return []
    if len(real) < 2:
        if len(real) / max(count, 1) < 0.5 or count > 8:
            return []
    if line["text"].strip().endswith(".") and count > 5:
        return []
    return tokens


def _blocks(lines):
    """Group indices of data lines that sit close enough to form one table."""
    blocks, current = [], []
    for index, line in enumerate(lines):
        if not _data_tokens(line):
            continue
        if current and line["top"] - lines[current[-1]]["top"] > BLOCK_GAP:
            blocks.append(current)
            current = []
        current.append(index)
    if current:
        blocks.append(current)
    return blocks


def _label_of(line, boundary):
    """Leading text of a row, left of the first numeric column."""
    words = [word["text"] for word in line["words"] if word["x1"] <= boundary]
    return clean(" ".join(words))[:LABEL_LIMIT]


def _looks_like_prose(line):
    text = line["text"].strip()
    if not text:
        return True
    if len(text) > 160:
        return True
    words = text.split()
    return len(words) > 12 and text.endswith(".") and sum(char.isdigit() for char in text) < 3


def _caption_hints(lines, header_top):
    """Read aggregation hints from the caption above a table."""
    above = [line["text"] for line in lines if 0 < header_top - line["top"] <= 60]
    hints = {"champion": False, "mean": False, "parenthesized": False}
    for text in above:
        if CAPTION.match(text.strip()):
            hints["champion"] = bool(CHAMPION_HINTS.search(text))
            hints["mean"] = bool(MEAN_HINTS.search(text))
            hints["parenthesized"] = bool(PAREN_HINT.search(text))
    return hints


def _header_text(lines, start):
    """The header lines just above a data block, as one string.

    The units of a table live in the header, so the header is the evidence a
    value's unit is checked against. Subscript lines and stray labels sit
    between the header and the rows: they are skipped rather than ending the
    header, but a caption does end it.
    """
    block_top = lines[start]["top"]
    parts = []
    for line in reversed(lines[:start]):
        if block_top - line["top"] > 45:
            break
        text = line["text"].strip()
        if CAPTION.match(text) or FIGURE.match(text):
            break
        if not _headerish(line) and not unit_from_text(text):
            continue
        parts.append(text)
    return " ".join(reversed(parts))


def _caption_label(lines, header_top):
    """The ``Table N`` label above a block, if any."""
    for line in reversed(lines):
        if 0 < header_top - line["top"] <= 70 and CAPTION.match(line["text"].strip()):
            match = re.match(r"((?:table|таблица)\s+\d+)", line["text"].strip(), re.I)
            return match.group(1) if match else ""
    return ""


def _unit_belongs(metric, unit):
    """The unit has to be one the metric is measured in.

    ``UNITS`` is the same conversion table the normaliser uses, so a unit that
    fails here would have left ``normalized_value`` empty anyway. It stops a
    mis-paired sentence ("PCE ... 0.73 V") from becoming a result row.
    """
    allowed = solar_schema.UNITS.get(metric)
    if allowed is None:
        return True
    factors = {
        solar_facts.unit_name(item) for item in allowed[1]
    }
    return solar_facts.unit_name(unit or "") in factors


def plausible(metric, value, unit):
    """Reject a value that cannot belong to the metric in that unit.

    This is a guard against mis-assigned columns, not a physics claim.
    """
    if value is None or not _unit_belongs(metric, unit):
        return False
    if metric == "Voc":
        if unit == "mV":
            return 40 <= value <= 3000
        return 0.04 <= value <= 3.0
    if metric in ("Jsc", "Jsc_EQE"):
        return 0.2 <= value <= 120
    if metric in ("FF", "HI", "retained_PCE", "stabilized_PCE"):
        if unit == "fraction" or (unit == "" and value <= 1.0):
            return 0.05 <= value <= 1.0
        return 2 <= value <= 100
    if metric == "PCE":
        return 0.005 <= value <= 100
    if metric in ("Rs", "Rsh"):
        return 0.0001 <= value <= 1e7
    if metric == "Eg":
        return 0.2 <= value <= 5
    return True


def _canonical_unit(metric, unit, value):
    """Choose the raw unit that solar_facts.UNITS can convert.

    A value below 1 with a ``%`` header is a fraction: FF, HI, PCE retention and
    stabilized PCE are reported both ways in the corpus, and the template wants
    percent. For PCE the percent reading is kept, because sub-1 % efficiencies
    are real (D021 reports 0.36 %).
    """
    if metric in ("FF", "HI", "retained_PCE", "stabilized_PCE") \
            and unit in ("%", "") and value is not None and value <= 1.0:
        return "fraction"
    if metric == "PCE" and unit == "fraction":
        return "fraction"
    if metric == "Voc" and unit == "":
        return "V" if value is not None and value < 10 else "mV"
    if metric in ("Jsc", "Jsc_EQE") and unit == "":
        return "mA/cm2"
    if metric in ("Rs", "Rsh") and unit == "":
        return "Ohm*cm2"
    return unit


def table_records(page):
    """Read results out of every header+rows block on one page.

    Each text column is scanned separately so that a table row never merges with
    a sentence printed beside it.
    """
    records = []
    for lines in page.get("groups") or [page["lines"]]:
        records.extend(_group_records(page, lines))
    return records


def _group_records(page, lines):
    """Read results out of every header+rows block of one column group."""
    records = []
    for block in _blocks(lines):
        tokens_by_row = [_data_tokens(lines[index]) for index in block]
        if len(block) < 2:
            continue
        centers = _column_centers(tokens_by_row)
        if len(centers) < 2:
            continue
        header = _header_for(lines, block[0], centers)
        if len(header) < 2:
            continue
        mapped = sorted(header.values(), key=lambda column: column["center"])
        hints = _caption_hints(lines, lines[block[0]]["top"])
        header_text = _header_text(lines, block[0])
        table_label = _caption_label(lines, lines[block[0]]["top"]) or hints.get("label", "")
        boundary = _label_boundary(lines, block[0], centers)

        rows = [(lines[index], tokens) for index, tokens in zip(block, tokens_by_row)]
        bands = []
        band_labels = []
        for row in rows:
            line = row[0]
            own_label = _label_of(line, boundary)
            annotation = bool(own_label) and own_label.startswith("(") and own_label.endswith(")")
            own_label = "" if annotation else own_label
            new_band = (not bands) or line["top"] - bands[-1][-1][0]["top"] > BAND_GAP
            # A device may be labelled on its second line, after its averages:
            # only a second label closes the band that already has one.
            if not new_band and own_label and band_labels[-1]:
                new_band = True
            if new_band:
                bands.append([row])
                band_labels.append(own_label)
            else:
                bands[-1].append(row)
                band_labels[-1] = band_labels[-1] or own_label

        for band in bands:
            label = ""
            for line, _ in band:
                candidate = _label_of(line, boundary)
                if candidate and not is_number(candidate.split()[0]):
                    label = candidate
                    break
            if not label:
                for line, _ in band:
                    candidate = _label_of(line, boundary)
                    if candidate:
                        label = candidate
                        break
            if not label or len(label) > LABEL_LIMIT:
                continue
            if label.lower().startswith(("sample", "device", "cell", "table")):
                continue
            tokens = [token for _, line_tokens in band for token in line_tokens]
            quote = " ".join(" ".join(word["text"] for word in _row_words(line))
                             for line, _ in band)
            assigned = {}
            for token in tokens:
                nearest = min(mapped, key=lambda column: abs(column["center"] - token["center"]))
                if abs(nearest["center"] - token["center"]) > 26:
                    continue
                assigned.setdefault(nearest["center"], []).append(token)
            for column in mapped:
                for token in assigned.get(column["center"], []):
                    raw = token["text"]
                    parenthesized = token["parenthesized"]
                    literal = token["literal"]
                    value = decimal_of(literal)
                    # A percent sign printed in the cell is a unit even when the
                    # header forgot it; otherwise the header decides.
                    unit = "%" if token["percent"] else column["unit"]
                    # Canonicalise first: a ``0.71`` under an ``FF`` header is a
                    # fraction, and the unit guard has to see that, not the bare
                    # header text.
                    raw_unit = _canonical_unit(column["metric"], unit, value)
                    if not plausible(column["metric"], value, raw_unit):
                        continue
                    aggregation = ""
                    if parenthesized:
                        aggregation = "champion"
                    elif hints["mean"]:
                        aggregation = "mean"
                    elif hints["champion"]:
                        aggregation = "champion"
                    records.append({
                        "metric_name": column["metric"],
                        "raw_value": printed_value(literal),
                        "raw_unit": raw_unit,
                        "spread_value": token["spread"],
                        "aggregation": aggregation,
                        "device_label": label,
                        "page": page["number"],
                        "quote": quote,
                        "header_text": header_text,
                        "table_label": table_label,
                        "section": "",
                    })
    return records


# ---------------------------------------------------------------- prose

# One definition for the whole module: a second one further down used to shadow
# this name, so the wrapped-delta check silently read a different rule than the
# one written next to it. A line that ends a sentence stops the delta verb of
# the previous line from governing the next one; a comma keeps the list open, so
# a wrapped delta list still reads as a delta. A colon or semicolon ends the
# clause as before. Closing quotes and brackets belong to the ending
# (``... 16.5 %."``).
SENTENCE_END = re.compile(r"[.!?:;][\"')\]]?\s*$")
ABBREVIATIONS = ("e.g.", "i.e.", "et al.", "Fig.", "Figs.", "vs.", "ca.", "approx.", "ref.", "Eq.")


def _sentences(text):
    """Split page text into lines, keeping offsets into the page text.

    A printed line is the unit a metric and its value are read from: joining
    wrapped lines recovers a few values but mixes neighbouring statements and
    costs more in wrong rows than it gains. The ligatures are folded here
    because ``efﬁciency`` is one glyph pair in most PDFs and would otherwise
    hide the word from the metric patterns; verification normalises the same
    way, so the quote stays literal.
    """
    text = text or ""
    parts = []
    for match in re.finditer(r"[^\n]+", text):
        stripped = unicodedata.normalize("NFKC", match.group(0)).strip()
        if stripped:
            offset = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
            parts.append((offset, stripped))
    return parts


def _unit_after(text, end):
    """Unit printed right after a number, if any."""
    window = text[end:end + 14]
    unit = unit_from_text(window)
    if unit == "%" and not window.lstrip().startswith("%"):
        unit = ""
    if unit == "V":
        # "V" only counts when it is the unit token, not the start of a word.
        match = re.match(r"\s*v\b(?!\w)", window, re.I)
        if not match:
            unit = ""
    return unit


def _number_mentions(text):
    """Numbers in reading order with their offsets, unit and literal text."""
    found = []
    for match in NUMBER.finditer(text):
        token = match.group(0)
        if not token or token in ("-", "+"):
            continue
        before = text[:match.start()]
        after = text[match.end():]
        if before and re.search(r"[\w]$", before):
            continue
        if after and re.match(r"^[\w]", after) and not re.match(r"^[×x]\s*10", after):
            continue
        # A number glued to a unit letter ("1.2V") is still a value, but a
        # reference marker ("[12]") and a year are not.
        if re.search(r"[\[(]\s*$", before[-2:]) and after.startswith("]"):
            continue
        value = printed_value(token)
        if value is None or value == "":
            continue
        found.append({
            "start": match.start(), "end": match.end(), "value": value,
            "unit": _unit_after(text, match.end()), "literal": token,
        })
    return found


def metric_mentions(text):
    """Metric names in reading order, with the text that named them."""
    found = []
    taken = []
    for name, pattern in METRIC_RE:
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if any(start < other_end and other_start < end for other_start, other_end in taken):
                continue
            literal = text[max(0, start - 12):end + 12].lower()
            if name == "PCE" and re.search(r"quantum\s*efficienc|external\s*efficienc", literal):
                continue
            if name == "HI" and re.search(r"\bhi\b", text[start:end], re.I) is None:
                continue
            taken.append((start, end))
            found.append({"start": start, "end": end, "metric": name,
                          "raw": text[start:end]})
    found.sort(key=lambda item: item["start"])
    return found


DEVICE_SKIP = {"and", "the", "with", "from", "for", "reference", "control", "target"}


def _label_tokens(label):
    tokens = []
    for token in re.split(r"[^0-9A-Za-z]+", clean(label).lower()):
        if len(token) >= 2 and token not in DEVICE_SKIP:
            tokens.append(token)
    return tokens


def prose_records(page_text, devices):
    """Results stated in sentences: a metric name and a number next to it.

    Detection only; attribution to a device is a separate, explicit guess and a
    value with no device stays a ``cited`` row rather than being assigned to
    whichever device was mentioned last. Prose is the noisiest channel, so a
    pair is accepted only when the number reads like a measurement rather than
    a subscript, a year, a reference or a relative change.
    """
    records = []
    previous = ""
    delta_mode = False
    for offset, sentence in _sentences(page_text):
        metrics = [item for item in metric_mentions(sentence) if item["metric"] in MY_METRICS]
        if not metrics:
            previous = sentence
            delta_mode = False
            continue
        numbers = _number_mentions(sentence)
        if not numbers:
            previous = sentence
            delta_mode = False
            continue
        # "improves Voc of 0.127 V, Jsc of 3.1 ..." governs every item of the
        # list, including one that wraps to the next line.
        delta_mode = _delta_governed(sentence, metrics) \
            or (delta_mode and not SENTENCE_END.search(previous))
        context = "" if SENTENCE_END.search(previous) else previous
        pairs = []
        # A list such as "JSC, VOC, FF and η were 31.3, 0.41, 59 and 7.8" pairs
        # the metrics and the values in order; nearest-name pairing would give
        # every value the last name of the list.
        if len(metrics) >= 2 and len(numbers) >= 2 \
                and max(item["end"] for item in metrics) < min(item["start"] for item in numbers) \
                and len(metrics) == len(numbers):
            pairs = list(zip(metrics, numbers))
        else:
            for number in numbers:
                before = [m for m in metrics if m["end"] <= number["start"]]
                if before:
                    pairs.append((before[-1], number))
                    continue
                after = [m for m in metrics if m["start"] >= number["end"]]
                if after:
                    pairs.append((after[0], number))
        labels = _label_sequence(sentence, numbers, devices)
        for metric, number in pairs:
            # A second sentence on the same printed line is not governed by the
            # delta verb of the first one.
            governed = delta_mode and SENTENCE_BREAK.search(sentence[:metric["start"]]) is None
            left = (context + " " + sentence[:number["start"]]).strip()
            if not _prose_pair_ok(metric, number, sentence, governed, left):
                continue
            name = _prose_metric(metric["metric"], number, sentence)
            records.append({
                "metric_name": name,
                "raw_value": number["value"],
                "raw_unit": _canonical_unit(name, number["unit"] or "",
                                            decimal_of(number["value"])),
                "spread_value": "",
                "aggregation": "",
                "device_label": labels.get(number["start"]) or
                _nearby_label(sentence, number["end"], devices),
                "quote": sentence.strip(),
                "sentence": sentence,
                "page": None,
            })
        previous = sentence
    return records


# ``retained 93 % of the initial efficiency`` is a stability result, not an
# efficiency of the device as built. The number is named by the words around it
# rather than by a metric word, so the metric is corrected after pairing.
RETENTION_AFTER = re.compile(r"^\s*%\s*(?:of|relative\s+to)\s+(?:the|its)\s+(?:initial|original|starting)\b", re.I)
RETENTION_BEFORE = re.compile(
    r"(?:retain|remain|maintain|keep|kept|preserv)\w*\s+(?:at\s+|about\s+|approximately\s+|over\s+|after\s+[\d.,]+\s*\w+\s+)?$",
    re.I)
STABILIZED_BEFORE = re.compile(r"(?:stabiliz|stabilis|steady[- ]state)\w*\s+(?:pce|efficiency|power\s+conversion\s+efficiency)\s*(?:of|at|=)?\s*$", re.I)


def _prose_metric(metric, number, sentence):
    """Correct ``PCE`` to a stability metric when the sentence says so."""
    if metric != "PCE":
        return metric
    before = sentence[max(0, number["start"] - 60):number["start"]]
    after = sentence[number["end"]:number["end"] + 40]
    if STABILIZED_BEFORE.search(before):
        return "stabilized_PCE"
    if RETENTION_BEFORE.search(before) and RETENTION_AFTER.search(after):
        return "retained_PCE"
    return metric


# A change relative to another device is not a measurement of this device.
RELATIVE = re.compile(
    r"^\s*%\s*(?:more|less|higher|lower|larger|smaller)|^\s*(?:more|less|higher|lower)\b"
    r"|increment|increase|decrease|improvement|enhanc|reduction|boost|compared|\bfold\b|\bthan\b",
    re.I,
)

# "improves Voc of 0.127 V" names the gain over another device, not the Voc of
# this one. The verb governs the metric, so it must sit directly before the
# metric name; an adjective ("the improved device showed a PCE of 16.5 %") does
# not make the measurement a delta. A change goes both ways, so the negative
# verbs are in the set: "the PCE decreased by 1.2 %" is as much a delta as an
# increase.
DELTA_STEMS = (
    r"improv|increas|enhanc|boost|gain|reduc|decreas|drop|declin|diminish|shrink|"
    r"lower|rais|grow|grew|ris(?:e|es|en|ing)|fall|fell|climb")
# Bare "lower" is a comparative adjective ("the lower PCE of 12 %"), so the
# list-governing check takes only inflected verb forms of it.
DELTA_GOVERNING_STEMS = (
    r"improv|increas|enhanc|boost|gain|reduc|decreas|drop|declin|diminish|shrink|"
    r"lower(?:s|ed|ing)|rais|grow|grew|ris(?:e|es|en|ing)|fall|fell|climb")
DELTA_VERB = re.compile(r"\b(?:" + DELTA_STEMS + r")\w*", re.I)
SENTENCE_BREAK = re.compile(r"[.!?][\"')\]]?\s")
DELTA_BEFORE = re.compile(
    r"\b(?:" + DELTA_GOVERNING_STEMS + r")\w*\s+"
    r"(?:(?:the|its|their|a|an)\s+)?(?:\w+\s+){0,1}$",
    re.I,
)
# The delta is linked to its number by "of" or "by", not by "to": "improved to
# 16.5 %" is the final value of a measurement, not a change. An adverb between
# the link and the number ("increased by roughly 1.2 %") does not make it a
# value either.
DELTA_ADVERB = (r"(?:about|approximately|approx\.|ca\.|around|roughly|nearly|"
                r"almost|some|~)?")
DELTA_LINK = re.compile(r"\b(?:of|by)\s*" + DELTA_ADVERB + r"\s*$", re.I)
# "an increase of 25 % in PCE" writes the change before the metric name.
DELTA_NUMBER_BEFORE = re.compile(
    r"\b(?:" + DELTA_STEMS + r")\w*\s+(?:of|by)\s*" + DELTA_ADVERB + r"\s*$", re.I)

# "efficiency exceeding 9 %" is a lower bound taken from the literature, not a
# measured PCE. The threshold word has to sit right before the number, so
# "PCE over the 1000 h test was 15 %" is still read.
THRESHOLD_END = re.compile(
    r"\b(?:exceed\w*|over|above|more\s+than|greater\s+than|higher\s+than)\s*$", re.I)


def _delta_phrase(metric, number, sentence, governed=False, left=""):
    """True when the number is a printed change, not the metric's value.

    ``governed`` says a delta verb governs the sentence's metric list, which
    keeps ``improves Voc of 0.127 V, Jsc of 3.1 ...`` from turning each item of
    the list into a measurement. ``left`` is the text before the number,
    including the wrapped line above when the list continues there.
    """
    if metric["end"] <= number["start"]:
        between = sentence[metric["end"]:number["start"]]
        if DELTA_LINK.search(between) is None:
            return False
        return bool(governed or DELTA_VERB.search(between))
    return bool(DELTA_NUMBER_BEFORE.search(left)
                or (governed and DELTA_LINK.search(left) is not None))


def _delta_governed(sentence, metrics):
    """True when a delta verb directly precedes a metric in this sentence."""
    return any(DELTA_BEFORE.search(sentence[:metric["start"]]) for metric in metrics)


def _threshold_phrase(metric, number, sentence):
    """True when the number is a lower bound printed next to the metric."""
    start, end = (metric["end"], number["start"]) if metric["end"] <= number["start"] \
        else (number["end"], metric["start"])
    return THRESHOLD_END.search(sentence[start:end]) is not None


def _prose_pair_ok(metric, number, sentence, governed=False, left=""):
    """Reject numbers that only look like measurements."""
    text = number_text(number["value"])
    unit = number["unit"] or ""
    distance = number["start"] - metric["end"] if number["start"] >= metric["end"] \
        else metric["start"] - number["end"]
    if distance > 80:
        return False
    if not unit and "." not in text:
        # Without a printed unit only a long integer reads as a value (a voltage
        # in mV). One or two digits are subscripts (``SrCl2``), four are years.
        if len(text.lstrip("-")) < 3:
            return False
        if re.fullmatch(r"(?:19|20)\d{2}", text):
            return False
    if re.match(r"^\s*(?:±|\+/-)", sentence[number["end"]:number["end"] + 6]):
        return False
    if _delta_phrase(metric, number, sentence, governed, left) \
            or _threshold_phrase(metric, number, sentence):
        return False
    return RELATIVE.search(sentence[number["end"]:number["end"] + 14]) is None


def _label_sequence(sentence, numbers, devices):
    """Labels of a printed list, in the order the values are written.

    ``The efficiency of NiO/GO and NiO are 15.3 % and 7.8 %`` names the samples
    and then the values; pairing by position keeps the second value with the
    second sample, while nearest-name pairing would give both to ``NiO/GO``.
    An empty mapping means the sentence is not such a list and the caller has
    to fall back to the nearest label.
    """
    found = {}
    for device in devices:
        form = clean(device["label"]).lower()
        if not form:
            continue
        pattern = r"(?<![\w])" + re.escape(form) + r"(?![\w])"
        for match in re.finditer(pattern, sentence.lower()):
            # ``NiO`` also matches at the start of ``NiO/GO``; the longer label
            # is the sample actually named at that position.
            if len(device["label"]) > len(found.get(match.start(), "")):
                found[match.start()] = device["label"]
    if len(found) < 2 or len(found) != len(numbers):
        return {}
    ordered = sorted(found.items())
    # Every name has to be written before the first value, otherwise the order
    # of the values does not follow the order of the names.
    if ordered[-1][0] >= numbers[0]["start"]:
        return {}
    return {number["start"]: label for (_, label), number in zip(ordered, numbers)}


def _nearby_label(sentence, position, devices):
    """Device named in the sentence before a value, leftmost match of the set.

    The whole label is matched first, so ``NiO/GO`` is preferred over its own
    prefix ``NiO`` when both start at the same word: taking the shorter would
    move the value to a different device without any evidence for it. A label
    whose printed form does not repeat in the sentence falls back to its tokens.
    """
    best = _label_at(sentence, position, devices, whole=True)
    if best:
        return best
    return _label_at(sentence, position, devices, whole=False)


def _label_at(sentence, position, devices, whole):
    """Leftmost label occurrence before ``position``; longer label on a tie."""
    best = ""
    best_position = -1
    lowered = sentence.lower()
    for device in devices:
        if whole:
            forms = [clean(device["label"]).lower()]
        else:
            forms = _label_tokens(device["label"])
        for form in forms:
            if not form:
                continue
            pattern = r"(?<![\w])" + re.escape(form) + (r"(?![\w])" if whole else "")
            for match in re.finditer(pattern, lowered):
                if match.start() > position:
                    continue
                if best_position < 0 or match.start() < best_position \
                        or (match.start() == best_position and len(device["label"]) > len(best)):
                    best, best_position = device["label"], match.start()
    return best


# ---------------------------------------------------------------- device ids

def _slug(label):
    """A compact identifier taken from the paper's own sample name.

    ``A1`` keeps its code, ``0Ge``/``DIO-5`` keep the distinguishing number, and
    a prose label such as ``pristine`` has no code at all.
    """
    text = clean(label)
    low = text.lower()
    if re.search(r"referenc", low):
        return "ref"
    if re.search(r"bifacial", low):
        return "bif"
    match = re.search(r"annealed\s+(\d{1,3})\s*min", low)
    if match:
        return match.group(1)
    if len(text) > 16:
        return ""
    match = re.fullmatch(r"([A-Za-z]{1,3})[-\s]?(\d{1,4})(?:[-\s]([A-Za-z]{1,3}))?", text)
    if match:
        return match.group(1) + match.group(2) + (("-" + match.group(3)) if match.group(3) else "")
    match = re.fullmatch(r"(\d{1,3})[A-Za-z]{0,3}", text)
    if match:
        return match.group(1)
    match = re.fullmatch(r"[A-Za-z]{2,6}[-\s](\d{1,3})(?:\s.*)?", text)
    if match:
        return match.group(1)
    return ""


def _device_ok(label):
    """Reject labels that name a scan direction or a table artefact, not a device."""
    low = clean(label).lower()
    # A numbered heading ("4. Conclusions") is a section, not a sample; the
    # stripper sometimes welds one to the table label above it.
    if re.match(r"^\d{1,2}\.\s+[a-z]", low):
        return False
    if re.fullmatch(r"(?:fb|sc)\s*[-\u2013]\s*(?:fb|sc)", low):
        return False
    if re.fullmatch(r"(?:reverse|forward|both)\s*(?:scan)?", low):
        return False
    if re.fullmatch(r"\(?champion\)?|\(?mean\)?|\(?average\)?", low):
        return False
    if len(low) > LABEL_LIMIT:
        return False
    return True


def _register(devices, order, label):
    """Add a device label once, assigning the next free identifier."""
    used = {device["id"] for device in order}
    slug = _slug(label)
    device_id = "DV-" + slug if slug and ("DV-" + slug) not in used else ""
    if not device_id:
        number = 1
        while f"DV{number}" in used:
            number += 1
        device_id = f"DV{number}"
    device = {"id": device_id, "label": label}
    devices[label] = device
    order.append(device)
    return device


SERIES_LABEL = re.compile(r"^([A-Za-z]{2,6})\s*[-\s]\s*(\d{1,4})(?:\s|$)")


def _simplify_series(devices):
    """Drop a series prefix when every sample of the paper shares it.

    ``DIO-5``, ``DIO-10``, ``DIO-15`` are one dilution series and the number is
    what tells them apart, so the id becomes ``DV5``. ``G1`` and ``I1`` differ
    in the letter, which carries the meaning, so those keep it.
    """
    matches = [SERIES_LABEL.match(clean(device["label"])) for device in devices]
    if not devices or any(match is None for match in matches):
        return
    prefix = matches[0].group(1).lower()
    if any(match.group(1).lower() != prefix for match in matches):
        return
    numbers = [match.group(2) for match in matches]
    if len(set(numbers)) != len(numbers):
        return
    for device, number in zip(devices, numbers):
        device["id"] = "DV" + number


def _device_registry(records):
    """Devices in order of first appearance, with stable identifiers."""
    devices, order = {}, []
    for record in records:
        label = record["device_label"]
        if not label or label in devices or not _device_ok(label):
            continue
        _register(devices, order, label)
    return devices, order


# ---------------------------------------------------------------- entities

YEAR = re.compile(r"\b(20[0-2]\d)\b")
DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")

FORMULAS = (
    (r"CsPbI\s*\d?Br\s*\d?|CsPbI\s*\d?Cl\s*\d?|CsPbBr\s*\d?", "perovskite"),
    (r"MA\s*PbI\s*\d|FA\s*PbI\s*\d|\(MA|\(FA", "perovskite"),
    (r"Cu\(In,Ga\)Se\s*\d|CIGS|CuInSe\s*\d", "CIGS"),
    (r"CdTe", "CdTe"),
    (r"Cu\s*\d?\s*ZnSn\(S,?Se\)\s*\d?|CZTS|CZTSe|Cu\s*\dZnSn", "kesterite"),
    (r"Sb\s*\d?\s*Se\s*\d|Sb2Se3", "Sb2Se3"),
    (r"Sb\s*\d?\s*S\s*\d", "other"),
    (r"a-Si|a-Si:H|amorphous\s*Si", "thin-film Si"),
)

STACK_RUN = re.compile(r"[A-Za-z][A-Za-z0-9().,+\-% ]{0,29}(?:/[A-Za-z][A-Za-z0-9().,+\-% ]{0,29}){2,}")
STACK_STOP = re.compile(
    r"\b(?:figure|fig\.?|and|the|with|were|was|is|are|of|in|on|for|see|from|shows?|using|"
    r"device|devices|structure|configuration|respectively|which|that)\b", re.I)
STACK_MATERIAL = re.compile(
    r"tio2|sno2|zno|cds|cdo|zns|perovsk|spiro|p3ht|pedot|ito|fto|azo|czts|cdte|cigs|moo|nio|"
    r"sb2|a-si|au$|ag$|al$|cu$|mo$|glass",
    re.I)


def _compact(text):
    return re.sub(r"\s+", "", clean(text)).lower()


def _formula_token(text):
    """One formula, subscripts glued back: ``CsPbI 2Br`` -> ``CsPbI2Br``."""
    return re.sub(r"\s+(?=\d)", "", clean(text)).strip()


def _absorber(page_text):
    """The first absorber formula as printed, its normalized form and class."""
    text = clean(page_text)
    for pattern, material_class in FORMULAS:
        match = re.search(pattern, text)
        if match:
            printed = clean(match.group(0))
            normalized = _formula_token(printed)
            if normalized:
                return printed, normalized, material_class
    return "", "", ""


BANDGAP = re.compile(
    r"(?:band[\s\-\u2010\u2011]*gap|\bEg\b)[^.]{0,80}?(\d+(?:[.,]\d+)?)\s*(?:eV|эВ)",
    re.I)
# A range prints the unit on the last bound only ("between 1.62 and 1.51 eV")
# as often as on both ("between 1.62 eV and 1.51 eV"). The template wants the
# first bound of the stated range, and BANDGAP cannot reach it across the dot of
# "1.62", so the range is matched before the single value.
BANDGAP_RANGE = re.compile(
    r"(?:band[\s\-\u2010\u2011]*gap|\bEg\b)[^.]{0,60}?"
    r"\b(?:between|from)\s+(\d+(?:[.,]\d+)?)\s*(?:eV|эВ)?\s*"
    r"(?:and|to|[-\u2013])\s*\d+(?:[.,]\d+)?\s*(?:eV|эВ)",
    re.I)
# "wide bandgap, higher than 3 eV" describes a transport layer, not the
# absorber; taking the first match would put that number on every material.
# Only the words glued to this bandgap count: "more than 104 cm-1" elsewhere in
# the sentence is an absorption coefficient, not a threshold on the gap.
BANDGAP_NOISE = re.compile(r"(?:higher|larger|greater|more)\s+than|\babove\b", re.I)
BANDGAP_AFTER = re.compile(r"^\s*(?:or\s+)?(?:more|higher|larger|greater|above)\b", re.I)
WIDE_GAP = re.compile(r"wide[\s\-\u2010\u2011]*band[\s\-\u2010\u2011]*gap", re.I)


def _bandgap(page_texts, material=""):
    """A band gap stated in the text, with its page and sentence.

    A value inside a comparison ("higher than", "above") is skipped: the
    template wants the absorber value, not a threshold. "Wide bandgap" is
    skipped only when the context is not the absorber - a wide-gap absorber is
    still an absorber, while a wide-gap transport layer is not. A stated range
    contributes its first bound, in the order the paper writes it.
    """
    for page in sorted(page_texts, key=int):
        text = clean(page_texts[page])
        matches = [(match.start(), match.group(1), match.group(0))
                   for match in BANDGAP_RANGE.finditer(text)]
        matches.extend((match.start(), match.group(1), match.group(0))
                       for match in BANDGAP.finditer(text))
        # Both patterns can start at the same word ("band gap between 1.62 eV
        # and ..." is also a single value for BANDGAP). Sorting is stable, so
        # the range comes first and its quote, which contains both bounds,
        # survives the dedupe below.
        matches.sort(key=lambda item: item[0])
        seen = set()
        for start, raw, quote in matches:
            if start in seen:
                continue
            seen.add(start)
            value = number_text(raw)
            if not value or not 0.2 <= float(value) <= 5:
                continue
            # Only the words glued to this gap count: "more than 104 cm-1 with a
            # band gap of 1.62 eV" is an absorption coefficient plus a real gap,
            # while "just above the bulk bandgap of CdS" is a comparison. A
            # sentence-wide window would let either phrase veto the other.
            lead = text[max(0, start - 24):start]
            found = lead + quote
            if BANDGAP_NOISE.search(found):
                continue
            if WIDE_GAP.search(found):
                context = text[max(0, start - 60):start + len(quote) + 60]
                absorber = "absorber" in context.lower() \
                    or (material and _compact(material) in _compact(context))
                if not absorber:
                    continue
            after = text[start + len(quote):start + len(quote) + 20]
            if BANDGAP_AFTER.match(after):
                continue
            return value, page, quote
    return "", None, ""


def _material_rows(devices, printed, normalized, material_class, publication_id, bandgap):
    """One material per device: the absorber plus the device's own doping."""
    rows = []
    for index, device in enumerate(devices, 1):
        name = printed
        if printed:
            if re.search(r"pristine|reference|control|untreated|w/o", device["label"], re.I):
                name = f"{printed} ({device['label']})"
            else:
                name = f"{device['label']} {printed}"
        rows.append({
            "publication_id": publication_id,
            "material_id": f"M{index}",
            "material_name_raw": name,
            "formula_normalized": normalized,
            "material_class": material_class,
            "bandgap_value": bandgap[0] if bandgap else "",
            "bandgap_unit": "eV" if bandgap else "",
            "bandgap_method": "cited" if bandgap else "",
            "note": "",
        })
    return rows


def _stack(page_texts):
    """The layer stack written as ``A/B/C`` in the experimental section.

    The longest chain that names a known material wins: a DOI path or a figure
    reference never contains one, so it cannot be mistaken for a stack. The
    chain usually starts mid-sentence (``... configuration of FTO/...``), so the
    first segment keeps only its trailing material name, and every segment is
    cut at an opening parenthesis or at a word that belongs to prose.
    """
    best = (0, None, [], "")
    for page in sorted(page_texts, key=int):
        text = clean(page_texts[page])
        for match in STACK_RUN.finditer(text):
            raw = match.group(0).split("/")
            head = raw[0].split()[-2:]
            while head and STACK_STOP.fullmatch(head[0]):
                head.pop(0)
            parts = []
            for position, part in enumerate([" ".join(head)] + raw[1:]):
                part = re.split(r"\s*\(", part)[0].strip(" .,;:")
                if not part or len(part) > 30 or STACK_STOP.search(part):
                    break
                parts.append(part)
            if len(parts) < 3 or not any(STACK_MATERIAL.search(part) for part in parts):
                continue
            if len(parts) > best[0]:
                best = (len(parts), page, parts, match.group(0))
    return best[2], best[1], best[3]


def _layer_rows(parts, devices, materials, publication_id):
    """One layer per stack part per device; the absorber follows the device."""
    by_device = {device["device_id"]: materials[position]
                 for position, device in enumerate(devices) if position < len(materials)}
    rows = []
    for device in devices:
        material = by_device.get(device["device_id"])
        for order, part in enumerate(parts, 1):
            name = part
            function = _layer_function(part)
            if function == "absorber" and material and material["material_name_raw"]:
                name = material["material_name_raw"]
            rows.append({
                "publication_id": publication_id,
                "layer_id": f"{device['device_id']}-L{order}",
                "device_id": device["device_id"],
                "layer_order": str(order),
                "layer_function": function,
                "layer_material": name,
                "note": "",
            })
    return rows


def _layer_function(name):
    low = name.lower()
    if re.search(r"\bag\b|\bau\b|\bal\b|metal|grid|electrode", low) and "sno" not in low:
        return "back_contact"
    if re.search(r"ito|fto|azo|zno:al|tco|contact", low):
        return "front_contact"
    if re.search(r"tio|sno|cdo|zns|cds|etl|electron", low):
        return "ETL"
    if re.search(r"spiro|p3ht|nio|moo|htl|hole", low):
        return "HTL"
    if re.search(r"perovsk|sb2se|sb2s|cdte|cigs|czts|kesterite|absorber|a-si", low):
        return "absorber"
    if re.search(r"ito/|interlayer|passiv", low):
        return "interlayer"
    return ""


JV_METRICS = ("PCE", "Voc", "Jsc", "FF", "Rs", "Rsh", "HI", "Jmpp", "Vmpp", "Voc_deficit")
MEASUREMENT_ORDER = (
    ("J-V", JV_METRICS),
    ("EQE", ("Jsc_EQE",)),
    ("MPP tracking", ("stabilized_PCE",)),
    ("stability", ("retained_PCE", "T80")),
)


def _measurement_rows(results, devices, publication_id, page_texts):
    """One measurement per method used; two J-V rows when both scans appear.

    The ids are positional, so the order is fixed: J-V (reverse, then forward),
    EQE, MPP tracking, stability. Naming the scan keeps the reverse and forward
    sweeps apart instead of collapsing them into one measurement.
    """
    present = {result["metric_name"] for result in results}
    # A direction named on a table row (``FB-SC``) is as much evidence as the
    # paper saying "reverse scan", so every direction actually used gets a row;
    # otherwise the result would be matched to a measurement of another sweep.
    scans = {record.get("scan") for record in results if record.get("scan")}
    kinds = []
    if present.intersection(JV_METRICS):
        text = clean(" ".join(page_texts.values()))
        reverse = "reverse" in scans or bool(
            re.search(r"reverse\s+scan|FB\s*[-\u2013]\s*SC", text, re.I))
        forward = "forward" in scans or bool(
            re.search(r"forward\s+scan|SC\s*[-\u2013]\s*FB|SC\s+to\s+FB", text, re.I))
        # A reverse sweep is the common convention, but saying so without the
        # paper saying it is a guess: an unevidenced direction stays empty.
        if reverse:
            kinds.append(("J-V", "reverse"))
        if forward:
            kinds.append(("J-V", "forward"))
        if not kinds:
            kinds.append(("J-V", ""))
    for kind, metrics in MEASUREMENT_ORDER:
        if kind == "J-V":
            continue
        if present.intersection(metrics):
            kinds.append((kind, ""))
    rows = []
    for kind, direction in kinds:
        rows.append({
            "publication_id": publication_id,
            "measurement_id": f"MS{len(rows) + 1}",
            "device_id": ";".join(device["id"] for device in devices),
            "measurement_type": kind,
            "scan_direction": direction,
            "note": "",
        })
    return rows


def _publication_row(publication_id, doc, page_texts):
    first = clean(page_texts.get("1", ""))
    metadata_title = clean(doc.get("title", ""))
    lines = [clean(line) for line in (page_texts.get("1") or "").splitlines() if clean(line)]
    title = ""
    for index, line in enumerate(lines[:8]):
        if len(line) >= 25 and not re.match(r"^(research article|original research|article|cite this)", line, re.I):
            title = line
            if index + 1 < len(lines) and len(lines[index + 1]) >= 25 \
                    and not re.search(r"\d{4}|doi|@", lines[index + 1]):
                title += " " + lines[index + 1]
            break
    if not title or re.search(r"\d+\s*\.\.\s*\d+", title):
        title = metadata_title
    year_match = re.search(r"(?:©|\(c\)|copyright)\s*(20[0-2]\d)", first, re.I) \
        or re.search(r"\b(20[0-2]\d)\b", lines[4] if len(lines) > 4 else first)
    doi_match = DOI.search(" ".join(page_texts.get(str(page), "") for page in range(1, min(3, len(page_texts) + 1))))
    authors = clean(doc.get("author", ""))
    return {
        "publication_id": publication_id,
        "title": title[:400],
        "year": year_match.group(1) if year_match else "",
        "DOI": doi_match.group(0) if doi_match else "",
        "authors": authors[:400],
        "in_scope": "true",
        "exclusion_reason": "",
    }


EXCLUDED = re.compile(
    r"numerical\s+modell|simulat|first-principles|density\s+functional|\bDFT\b|"
    r"theoretical\s+study|band\s*gap\s*narrowing", re.I)


def _in_scope(title, page_texts):
    text = clean(" ".join((page_texts.get(page, "") or "")[:4000] for page in sorted(page_texts)[:1]))
    # A modelling paper still says "experimental" when it validates against
    # somebody else's data; only an actual fabrication claim puts it in scope.
    if EXCLUDED.search(title) and not re.search(r"fabricat|measured\s+devices", text, re.I):
        return False, "Моделирование или теория без изготовленных приборов."
    return True, ""


# ---------------------------------------------------------------- evidence

THOUSANDS = re.compile(r"(?<![\d.,])([1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?)(?![\d.,])")
DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")


def _plain(text):
    """Thousands separators dropped, decimal commas turned into points."""
    text = THOUSANDS.sub(lambda match: match.group(1).replace(",", ""), clean(text))
    return DECIMAL_COMMA.sub(".", text)


def _value_pattern(value):
    """Literal pattern for a printed number: 1322 may be printed as 1,322."""
    value = clean(value)
    thousands = bool(re.fullmatch(r"\d{4,}", value))
    pattern = re.escape(value).replace(r"\.", "[.,]")
    if thousands:
        pattern = re.sub(r"(?<=\d)(?=(\d{3})+$)", ",?", pattern)
    return pattern


def _has_value(quote, value):
    """Same boundary test :func:`solar_facts.has_value` applies to a scalar."""
    if not value:
        return False
    quote, value = _plain(quote), _plain(value)
    for match in re.finditer(re.escape(value), quote):
        before, after = quote[:match.start()], quote[match.end():]
        if before and re.search(r"[\w.,]$", before):
            continue
        if re.search(r"[+\-±\u2013\u2014<>≤≥]\s*$", before):
            continue
        if after and re.match(r"^[\w.,]", after):
            continue
        if re.match(r"^\s*(?:±|\+/-|[\u2013\u2014\u2212-]|\bto\b|[<>≤≥])", after):
            continue
        if re.search(r"(?:±|\+/-|[\u2013\u2014\u2212]|\bto)\s*$", before):
            continue
        return True
    return False


def _overlap(left, right):
    left = set(re.findall(r"[a-zа-я]{3,}", clean(left).lower()))
    right = set(re.findall(r"[a-zа-я]{3,}", clean(right).lower()))
    return len(left & right)


def _window(quote, value, width=80):
    quote = clean(quote)
    match = re.search(_value_pattern(value), quote)
    if match is None:
        return ""
    index, end = match.start(), match.end()
    start = max(0, index - width)
    stop = min(len(quote), end + width)
    while start > 0 and not quote[start - 1].isspace():
        start -= 1
    while stop < len(quote) and not quote[stop].isspace():
        stop += 1
    return quote[start:stop].strip()


def _numbers(text):
    return {number_text(match.group(0)) for match in NUMBER.finditer(clean(text))}


def _best_source(sources, value, quote):
    """The source that best matches a quote: the value first, then the numbers.

    Several table rows on one page can share a label, and word overlap alone
    then picks the wrong row. The numbers of the row are what distinguishes it,
    so a source containing more of them wins.
    """
    hits = [source for source in sources if _has_value(source["quote"], value)]
    if not hits:
        return None
    wanted = _numbers(quote)

    def score(source):
        return (len(wanted & _numbers(source["quote"])), _overlap(source["quote"], quote))

    inside = [source for source in hits if clean(quote) and clean(quote) in clean(source["quote"])]
    return max(inside or hits, key=score)


def _page_source(index, page):
    """The longest text source on a page, used when a literal cannot be found.

    Subscripts are typeset as separate lines by pdfplumber (``CsPbI Br`` and a
    lone ``2``), so a formula or a device label may be present on the page as a
    whole and in no single source line. Entity rows still need a real source id;
    the page itself is the evidence.
    """
    sources = [source for source in index.get(page, []) if source.get("quote")]
    if not sources:
        return []
    return [max(sources, key=lambda source: len(source["quote"]))["id"]]


def _evidence(index, page, value):
    """Source ids for an entity name: literal, then subscript-tolerant, then page."""
    if not page:
        return []
    ids, _ = _attach(index, page, "", value)
    if ids:
        return ids
    return _page_source(index, page)


def _attach(index, page, quote, value, header_text=""):
    """Source ids and a literal fragment that contains the value."""
    sources = index.get(page, [])
    source = _best_source(sources, value, quote)
    if source is None:
        return [], ""
    fragment = clean(quote) if clean(quote) and clean(quote) in clean(source["quote"]) \
        else _window(source["quote"], value)
    if not _has_value(fragment, value):
        fragment = _window(source["quote"], value)
    ids = [source["id"]]
    if header_text:
        # The header carries the units; it is often a different source from the
        # row, so match it by the unit rather than by word overlap. A prose line
        # that happens to contain "V" is not the header, so a detected table row
        # wins and, among equals, the shortest line does.
        unit = unit_from_text(header_text)
        if unit:
            from .solar_facts import has_unit
            candidates = [candidate for candidate in sources
                          if candidate["id"] not in ids and has_unit(unit, [candidate])]
            if candidates:
                header = min(candidates, key=lambda item: (item.get("kind") != "table",
                                                           len(item["quote"])))
                ids.append(header["id"])
    return list(dict.fromkeys(ids)), fragment


# ---------------------------------------------------------------- build

MY_METRICS = {
    "PCE": ("PCE", "J-V"), "Voc": ("Voc", "J-V"), "Jsc": ("Jsc", "J-V"),
    "FF": ("FF", "J-V"), "Rs": ("Rs", "J-V"), "Rsh": ("Rsh", "J-V"),
    "HI": ("HI", "J-V"), "Jmpp": ("Jmpp", "J-V"), "Vmpp": ("Vmpp", "J-V"),
    "stabilized_PCE": ("stabilized_PCE", "MPP tracking"),
    "retained_PCE": ("retained_PCE", "stability"), "T80": ("T80", "stability"),
    "Jsc_EQE": ("Jsc_EQE", "EQE"),
    # A voltage deficit is read off the same J-V curve as Voc.
    "Voc_deficit": ("Voc_deficit", "J-V"),
}

# Metrics the template can carry. Band gap is a material property, not a result
# of a device measurement, so it is not exported here.
RESULT_METRICS = set(MY_METRICS) | {"Voc_deficit", "AVT", "CRI", "LUE", "bifaciality_factor"}


def printed_value(text):
    """The number as printed, with a thousands separator removed.

    A decimal comma stays: quotes are checked against the page text, and the
    page says ``0,71``. ``1,197`` is a thousands separator and becomes ``1197``.
    """
    text = clean(text).replace("\u2212", "-").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    text = text.rstrip("%")
    text = text.rstrip(")").rstrip()
    if re.fullmatch(r"[+\-]?\d{1,3}(?:,\d{3})+", text):
        return text.replace(",", "")
    return text


def _table_candidates(model):
    records = []
    for page in model["pages"]:
        for record in table_records(page):
            record["origin"] = "table"
            records.append(record)
    return records


def _prose_candidates(doc, devices):
    """Prose results, with sample names discovered in the prose itself.

    A paper whose data lives in figures never has a table label to reuse, so the
    samples have to come from the sentences: ``the 0.001 M SrCl2 device``,
    ``5% Y-Sn3O4 cells``. Only a short noun phrase directly before
    ``device/cell/film/sample`` is taken, and only when it looks like a sample
    name rather than a generic word, so ``the reference device`` works but
    ``this device`` does not.
    """
    labels = []
    corpus = " ".join(doc["page_texts"][number] for number in sorted(doc["page_texts"]))
    for number in sorted(doc["page_texts"]):
        for label in _prose_labels(doc["page_texts"][number]):
            if label in labels:
                continue
            # A sample name is used repeatedly; a one-off code is a typo, a
            # formula fragment or a page artefact.
            if label.lower() not in GENERIC_SAMPLES \
                    and len(re.findall(r"(?<![\w])" + re.escape(label) + r"(?![\w])", corpus)) < 2:
                continue
            labels.append(label)
    known = {device["label"] for device in devices}
    extra = [{"id": "", "label": label} for label in labels if label not in known]
    records = []
    for number in sorted(doc["page_texts"]):
        text = doc["page_texts"][number]
        for record in prose_records(text, devices + extra):
            record["page"] = int(number)
            record["origin"] = "prose"
            records.append(record)
    return records


PROSE_SAMPLE = re.compile(
    r"(?:the\s+|a\s+)?([A-Za-z0-9][A-Za-z0-9().,%+\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9().,%+\-]*){0,2})\s+"
    r"(?:solar\s+)?(?:devices?|cells?|films?|samples?)\b", re.I)
GENERIC_SAMPLES = {"target", "control", "reference", "pristine", "undoped", "champion",
                   "optimized", "best"}
# A sample code such as ``Cs5``, ``Cs10``, ``A1`` or ``B4``. A formula such as
# ``MoS2`` or ``TiO2`` has more than one letter before the number and is not a
# sample name; neither is a bare count like ``20``.
SAMPLE_CODE = re.compile(r"^[A-Z][a-z]?\d{1,3}$")


def _prose_labels(text):
    """Sample names written in running text, in order of appearance.

    Only an explicit sample code or a generic role word counts. A paper that
    writes "the champion cell" or "the Cs5 devices" names its samples; one that
    writes "the perovskite solar cell" does not, and inventing a device there
    would attribute a measurement to a sample that was never distinguished.
    """
    labels = []
    for match in PROSE_SAMPLE.finditer(clean(text)):
        words = [word.strip(" .,;:") for word in match.group(1).split()]
        if not words:
            continue
        if " ".join(words).lower() in GENERIC_SAMPLES:
            label = " ".join(words)
        elif SAMPLE_CODE.match(words[-1]):
            label = words[-1]
        else:
            continue
        if label not in labels:
            labels.append(label)
    return labels


def _measurement_for(metric, measurements, record=None):
    kind = MY_METRICS.get(metric, ("", ""))[1]
    if not kind:
        return ""
    direction = (record or {}).get("scan", "")
    for row in measurements:
        if row["measurement_type"] != kind:
            continue
        if kind == "J-V" and direction and row["scan_direction"] != direction:
            continue
        return row["measurement_id"]
    return ""


SCAN_LABEL = re.compile(r"^(?:FB|SC)\s*[-\u2013]\s*(?:FB|SC)$", re.I)


def _scan_direction(label):
    """``FB-SC`` is reverse scan and ``SC-FB`` is forward; else empty."""
    label = clean(label)
    if not SCAN_LABEL.match(label):
        return ""
    return "forward" if label.lower().startswith("sc") else "reverse"


def _assign_scans(records):
    """A table labelled by scan direction belongs to the paper's best device.

    ``FB-SC`` / ``SC-FB`` name the sweep, not a sample. The row still describes
    a real device, and the only device a table would report a second sweep for
    is the champion, so the row takes the label of the highest-PCE device and
    keeps its own direction.
    """
    best, best_value = None, None
    for record in records:
        if record["metric_name"] != "PCE" or _scan_direction(record["device_label"]):
            continue
        value = decimal_of(record["raw_value"])
        if value is not None and (best_value is None or value > best_value):
            best, best_value = record["device_label"], value
    if not best:
        return
    for record in records:
        direction = _scan_direction(record["device_label"])
        if direction:
            record["device_label"] = best
            record["scan"] = direction


def _empty_result(publication_id, doc, page_texts):
    """Seven tables for a document with no measured value.

    Scope is a property of the paper, not of the parse: a review or a paper
    whose numbers live only in figures is still in scope and simply contributes
    no rows. Only a modelling or theory paper is excluded, with its reason.
    """
    publication = _publication_row(publication_id, doc, page_texts)
    in_scope, reason = _in_scope(publication["title"], page_texts)
    publication["in_scope"] = "true" if in_scope else "false"
    publication["exclusion_reason"] = "" if in_scope else reason
    return {"publications": [publication], "materials": [], "devices": [],
            "layers": [], "measurements": [], "results": [], "issues": []}


def build(data, publication_id):
    """Seven candidate tables for one PDF, ready for ``solar_facts.finalize``."""
    from . import pdf_source
    from .solar_facts import has_unit, unit_name

    started = time.perf_counter()
    doc = pdf_source.extract(data)
    diagnostics.event(logger, "solar.build.start", publication_id=publication_id,
                      pages=doc.get("pages", 0), sources=len(doc.get("sources") or []),
                      warnings=len(doc.get("warnings") or []))
    if doc.get("fallback_pages"):
        # PyMuPDF words replaced an empty pdfplumber word layer: results from
        # these pages have no table detection, only line sources.
        diagnostics.warning(logger, "solar.parse.fallback", parser="pymupdf",
                            pages=doc["fallback_pages"])
    if doc.get("warnings"):
        diagnostics.debug(logger, "solar.parse.warnings",
                          warnings=doc["warnings"][:5], total=len(doc["warnings"]))
    model = layout(data)
    # pdf_source keys page text by int; the rest of this module uses the string
    # form everywhere, so convert once instead of at every lookup.
    page_texts = {str(page): text for page, text in doc["page_texts"].items()}
    index = {}
    for source in doc["sources"]:
        index.setdefault(source["page"], []).append(source)

    table_rows = _table_candidates(model)
    _assign_scans(table_rows)
    registry, devices = _device_registry(table_rows)

    prose_rows = _prose_candidates(doc, devices)
    # A sample named only in prose is still a device of the paper: register it
    # after the table devices, so a table column always keeps the lower id.
    for record in prose_rows:
        label = record["device_label"]
        if label and label not in registry and _device_ok(label):
            _register(registry, devices, label)
    _simplify_series(devices)

    candidates = table_rows + prose_rows
    # A sentence that repeats a number already read from a table is the same
    # result said twice; keeping both only adds a row that no gold bucket wants.
    # The key carries the device and the sweep, because the same metric/value on
    # another device or another measurement is a different result and dropping
    # it would silently lose data.
    table_values = {(_dedupe_label(record["device_label"]),
                     record["metric_name"],
                     number_text(record["raw_value"]),
                     record.get("scan", ""))
                    for record in table_rows}
    kept = []
    for record in candidates:
        if record["metric_name"] not in RESULT_METRICS:
            continue
        if record["origin"] == "prose" \
                and (_dedupe_label(record["device_label"]),
                     record["metric_name"],
                     number_text(record["raw_value"]),
                     record.get("scan", "")) in table_values:
            continue
        value = decimal_of(record["raw_value"])
        unit = record["raw_unit"]
        if not plausible(record["metric_name"], value, unit):
            continue
        record["unit_ok"] = True
        kept.append(record)

    kept.sort(key=lambda record: (record["page"], 0 if record["origin"] == "table" else 1))
    measurements = _measurement_rows(kept, devices, publication_id, page_texts) if devices else []

    results, seen = [], {}
    for record in kept:
        device = registry.get(record["device_label"], {"id": ""})
        measurement = _measurement_for(record["metric_name"], measurements, record)
        metric = record["metric_name"]
        raw = printed_value(record["raw_value"])
        key = (device["id"], measurement, metric, decimal_of(raw), unit_name(record["raw_unit"]))
        ids, fragment = _attach(index, record["page"], record["quote"], raw,
                               record.get("header_text", ""))
        if not ids:
            # No literal anchor on that page: the value cannot be exported.
            continue
        duplicate = seen.get(key)
        if duplicate:
            # The same value restated elsewhere: keep the new spelling, because
            # ``23`` and ``23.0`` are printed differently and the fragment has
            # to contain what this row exports.
            row = dict(duplicate)
            row["result_id"] = f"R{len(results) + 1:03d}"
            row["duplicate_of"] = duplicate["result_id"]
            row["raw_value"] = raw
            row["raw_unit"] = record["raw_unit"]
            row["source_page"] = str(record["page"])
            row["source_fragment"] = fragment
            row["source_table"] = record.get("table_label", "")
            row["_source_ids"] = ids
            results.append(row)
            continue
        row = {
            "publication_id": publication_id,
            "result_id": f"R{len(results) + 1:03d}",
            "device_id": device["id"],
            "measurement_id": measurement,
            "material_id": "",
            "metric_name": metric,
            "metric_value": "", "metric_unit": "",
            "aggregation": record.get("aggregation", ""),
            "spread_value": "",
            "n_devices": "",
            "time_point": "", "time_unit": "",
            "raw_value": raw,
            "raw_unit": record["raw_unit"],
            "normalized_value": "", "normalized_unit": "",
            "result_origin": "primary" if device["id"] else "cited",
            "cited_publication_id": "" if device["id"] else "cited",
            "source_page": str(record["page"]),
            "source_section": "",
            "source_table": record.get("table_label", ""),
            "source_figure": "",
            "source_fragment": fragment,
            "extraction_status": "extracted",
            "duplicate_of": "",
            "note": "",
            "_source_ids": ids,
        }
        if not device["id"]:
            row["measurement_id"] = ""
        seen[key] = row
        results.append(row)

    if not results:
        diagnostics.event(logger, "solar.build.empty", publication_id=publication_id,
                          reason="no_results",
                          duration_ms=int((time.perf_counter() - started) * 1000),
                          table_rows=len(table_rows), prose_rows=len(prose_rows),
                          kept=len(kept))
        return _empty_result(publication_id, doc, page_texts)

    publication = _publication_row(publication_id, doc, page_texts)
    in_scope, reason = _in_scope(publication["title"], page_texts)
    if not in_scope:
        # The template allows an excluded paper to carry nothing but its own row;
        # keeping the rows found in it would make the whole bundle invalid.
        publication["in_scope"] = "false"
        publication["exclusion_reason"] = reason
        diagnostics.event(logger, "solar.build.empty", publication_id=publication_id,
                          reason="out_of_scope",
                          duration_ms=int((time.perf_counter() - started) * 1000))
        return {"publications": [publication], "materials": [], "devices": [],
                "layers": [], "measurements": [], "results": [], "issues": []}
    publication["in_scope"] = "true"
    publication["exclusion_reason"] = ""

    # ---- materials: one per device, so the absorber follows the doping
    scope_text = " ".join(page_texts.get(str(page), "") for page in range(1, min(4, len(page_texts) + 1)))
    printed, normalized, material_class = _absorber(scope_text)
    bandgap = _bandgap(page_texts, printed)
    materials = _material_rows(devices, printed, normalized, material_class, publication_id,
                               (bandgap[0],) if bandgap[0] else None) if printed else []
    if materials:
        page = next((int(page) for page in sorted(page_texts, key=int)
                     if _compact(printed) in _compact(page_texts[page])), None)
        ids = _evidence(index, page, printed)
        if not ids:
            materials = []
        else:
            for row in materials:
                row["_source_ids"] = ids
    if materials and bandgap[0]:
        value, page, quote = bandgap
        page = int(page)
        ids, _ = _attach(index, page, quote, value)
        anchors = [source for source in index.get(page, []) if source["id"] in ids]
        if not ids or not has_unit("eV", anchors):
            for row in materials:
                row["bandgap_value"] = ""
                row["bandgap_unit"] = ""
                row["bandgap_method"] = ""
        else:
            # The formula and the band gap are usually printed on different
            # pages; the material row needs both anchors.
            for row in materials:
                row["_source_ids"] = list(dict.fromkeys(row["_source_ids"] + ids))

    by_device = {device["id"]: materials[position]
                 for position, device in enumerate(devices) if position < len(materials)}
    device_rows = []
    for device in devices:
        material = by_device.get(device["id"])
        row = {
            "publication_id": publication_id,
            "device_id": device["id"],
            "absorber_material_id": material["material_id"] if material else "",
            "device_label": device["label"],
            "device_role": "control" if re.search(r"pristine|reference|control|untreated|w/o", device["label"], re.I) else "target",
            "device_type": "module" if re.search(r"module", device["label"], re.I) else "cell",
            "architecture": "", "substrate_type": "", "area_definition": "",
            "active_area": "", "area_unit": "", "encapsulation": "", "note": "",
        }
        page = next((int(item) for item in sorted(page_texts, key=int)
                     if _compact(device["label"]) in _compact(page_texts[item])), None)
        ids = _evidence(index, page, device["label"])
        if not ids:
            ids = results[0]["_source_ids"] if results else []
        if not ids:
            continue
        row["_source_ids"] = ids
        device_rows.append(row)
    devices_out = device_rows

    # ---- layers: the stack repeated for every device, absorber replaced
    stack, stack_page, stack_text = _stack(page_texts)
    layers = []
    if stack and devices_out:
        stack_page = int(stack_page)
        target = next((part for part in stack if _has_value(page_texts[str(stack_page)], part)), stack[0])
        ids, fragment = _attach(index, stack_page, page_texts[str(stack_page)], target)
        if ids:
            layers = _layer_rows(stack, devices_out, materials, publication_id)
            for row in layers:
                row["_source_ids"] = ids

    # ---- measurements: attach to the first result measured that way
    first_by_measurement = {}
    for row in results:
        if row.get("measurement_id") and row["measurement_id"] not in first_by_measurement:
            first_by_measurement[row["measurement_id"]] = row["_source_ids"]
    measurement_rows = []
    if devices_out:
        for row in measurements:
            ids = first_by_measurement.get(row["measurement_id"]) or (results[0]["_source_ids"] if results else [])
            if not ids:
                continue
            row["_source_ids"] = ids
            measurement_rows.append(row)
    measurements = measurement_rows

    for row in results:
        row["_source_ids"] = row.get("_source_ids", [])
    results = [row for row in results if row["_source_ids"]]
    if not results:
        diagnostics.event(logger, "solar.build.empty", publication_id=publication_id,
                          reason="no_anchored_results",
                          duration_ms=int((time.perf_counter() - started) * 1000),
                          kept=len(kept))
        return _empty_result(publication_id, doc, page_texts)

    tables = {
        "publications": [publication],
        "materials": materials,
        "devices": devices_out,
        "layers": layers,
        "measurements": measurements,
        "results": results,
        "issues": [],
    }
    diagnostics.event(logger, "solar.build.done", publication_id=publication_id,
                      duration_ms=int((time.perf_counter() - started) * 1000),
                      pages=doc.get("pages", 0), sources=len(doc.get("sources") or []),
                      table_rows=len(table_rows), prose_rows=len(prose_rows),
                      kept=len(kept), devices=len(devices_out), materials=len(materials),
                      layers=len(layers), measurements=len(measurements),
                      results=len(results))
    return tables


# ---------------------------------------------------------------- export

def project(tables):
    """Keep exactly the template columns plus ``source_ids``."""
    from .solar_schema import TABLES as SCHEMA

    cart = {}
    for table, columns in SCHEMA.items():
        rows = []
        for original in tables.get(table, []):
            row = {column: (original.get(column) or "") for column in columns}
            source_ids = original.get("source_ids") or original.get("_source_ids") or []
            row["source_ids"] = [int(item) for item in source_ids]
            rows.append(row)
        cart[table] = rows
    return cart


def extract_pdf(path, publication_id=None, *, model="", progress=None):
    """Document plus the seven tables, validated by :mod:`solar_facts`."""
    from pathlib import Path

    from . import solar_facts

    from pdfplumber.utils.exceptions import PdfminerException

    data = Path(path).read_bytes()
    started = time.perf_counter()
    document = solar_facts.prepare_pdf(publication_id or Path(path).name, data)
    if progress: progress(1, "Страницы PDF прочитаны")
    try:
        tables = project(build(data, document["publication_id"]))
    except PdfminerException:
        # prepare_pdf parsed the same bytes, so this is a table-layer failure,
        # not a different file; report it the same way.
        diagnostics.exception(logger, "solar.extract.failed", kind="PdfminerException",
                              publication_id=document.get("publication_id", ""))
        raise ValueError("PDF повреждён, зашифрован или не может быть разобран.") from None
    if progress: progress(2, "Результаты извлечены")
    result = solar_facts.finalize(tables, document, model=model)
    diagnostics.event(logger, "solar.extract.done",
                      publication_id=document.get("publication_id", ""),
                      duration_ms=int((time.perf_counter() - started) * 1000),
                      pages=document.get("pages", 0),
                      tables={name: len(rows) for name, rows in tables.items()})
    return result


def _write_envelope(connection, area, publication_id, result, source_path):
    """Record the publication and its page texts, replacing a previous run.

    ``documents`` carries the real year and scope from the extracted row, so the
    site can filter by them; ``blocks`` keeps one row per page of pypdf text, so
    a ``source_fragment`` stays checkable without reopening the PDF.
    """
    page_texts = result.get("page_texts") or {}
    publication = (result["tables"]["publications"] or [{}])[0]
    connection.execute(
        "INSERT OR REPLACE INTO documents "
        "(area, publication_id, sha256, title, year, in_scope, exclusion_reason, "
        " parser_status, parser_version, page_count, text_chars, source_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (area, publication_id, result.get("sha256") or "",
         publication.get("title") or "", publication.get("year") or "",
         publication.get("in_scope") or "true", publication.get("exclusion_reason") or "",
         "ok", PARSER_VERSION, int(result.get("pages") or 0),
         sum(len(text or "") for text in page_texts.values()), str(source_path or "")))
    connection.execute("DELETE FROM blocks WHERE area = ? AND publication_id = ?",
                       (area, publication_id))
    payload = [(area, publication_id, int(page), int(page), "", "page", text or "", "[]")
               for page, text in sorted(page_texts.items(), key=lambda item: int(item[0]))]
    connection.executemany(
        "INSERT OR REPLACE INTO blocks "
        "(area, publication_id, ordinal, page, section, kind, text, bbox_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", payload)
    return len(payload)


def process(area, publication_id, pdf_path):
    """Processor for ``batch.run``: one solar paper into all seven tables.

    This is the default extractor for the solar area. It writes the same rows
    the CSV path exports, plus the ``documents``/``blocks`` envelope, and it
    replaces every row of the publication on a rerun, so a changed extractor
    leaves no stale rows behind. The target database is ``VINITI_DATA_DB``
    (``batch`` sets it from ``--db``), falling back to the shared default.
    """
    import os
    from pathlib import Path

    from . import db as storage
    from . import extract

    if area != "solar":
        raise ValueError("solar_offline обслуживает только область solar.")
    path = Path(pdf_path)
    started = time.perf_counter()
    data = path.read_bytes()
    diagnostics.event(logger, "solar.process.start", area=area,
                      publication_id=publication_id, bytes=len(data))

    db_path = Path(os.environ.get("VINITI_DATA_DB") or storage.DEFAULT_DB)
    if not db_path.exists():
        storage.init(db_path)
    area_tables = storage.load_passport()["areas"]["solar"]["tables"]
    connection = storage.connect(db_path)
    written = {}
    try:
        # Refuse an imported publication before paying for the parse: a batch
        # run against a database with the gold set must fail fast, not spend
        # minutes extracting files it will not write.
        previous = connection.execute(
            "SELECT parser_status FROM documents "
            "WHERE area = ? AND publication_id = ?",
            (area, publication_id)).fetchone()
        if previous and previous["parser_status"] == "imported":
            # Gold rows share the same tables and the same R ids; replacing
            # them silently would destroy the reference the scorer needs.
            diagnostics.error(logger, "solar.process.refused", area=area,
                              publication_id=publication_id,
                              reason="documents.parser_status=imported")
            raise ValueError(
                f"{publication_id}: в базе эталон (parser_status=imported); "
                "извлечение заменит его - выберите отдельную базу")
        if previous is None:
            # A hand-made or old database may hold rows without a documents row;
            # those rows are not ours to replace either. Check every table, not
            # only results: a partial import may hold materials or devices.
            for table in area_tables:
                name = storage.table_name(area, table)
                existing = connection.execute(
                    f"SELECT 1 FROM {storage.quote(name)} "
                    f"WHERE area = ? AND publication_id = ? LIMIT 1",
                    (area, publication_id)).fetchone()
                if existing:
                    diagnostics.error(logger, "solar.process.refused", area=area,
                                      publication_id=publication_id,
                                      reason="rows_without_documents", table=table)
                    raise ValueError(
                        f"{publication_id}: строки без записи documents (вероятно, "
                        "эталон); извлечение заменит их - выберите отдельную базу")
        result = extract_pdf(path, publication_id)
        with connection:
            blocks = _write_envelope(connection, area, publication_id, result, pdf_path)
            for table, meta in area_tables.items():
                name = storage.table_name(area, table)
                connection.execute(
                    f"DELETE FROM {storage.quote(name)} "
                    f"WHERE area = ? AND publication_id = ?",
                    (area, publication_id))
                rows = result["tables"][table]
                written[table] = extract._insert(connection, name, area, table, meta, rows) \
                    if rows else 0
    except Exception as exc:
        diagnostics.exception(logger, "solar.process.failed", area=area,
                              publication_id=publication_id,
                              kind=type(exc).__name__)
        raise
    finally:
        connection.close()
    summary = {"area": area, "publication_id": publication_id, "tables": written,
               "blocks": blocks, "pages": result.get("pages", 0),
               "sources": len(result.get("sources") or []), "sha256": result.get("sha256", "")}
    diagnostics.event(logger, "solar.process.done", area=area,
                      publication_id=publication_id,
                      duration_ms=int((time.perf_counter() - started) * 1000),
                      pages=summary["pages"], sources=summary["sources"],
                      blocks=blocks, rows=sum(written.values()),
                      tables={table: count for table, count in written.items()})
    return summary


def main(argv=None):
    import argparse
    import csv
    import json
    from pathlib import Path

    from .solar_schema import TABLES as SCHEMA

    diagnostics.configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="*", type=Path, help="PDF files to parse")
    parser.add_argument("--pdf-dir", type=Path, help="directory of PDFs")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--json", action="store_true",
                        help="also write <name>.json with the full result per PDF")
    parser.add_argument("--skip-errors", action="store_true",
                        help="keep going when one PDF cannot be validated")
    arguments = parser.parse_args(argv)

    paths = list(arguments.pdf)
    if arguments.pdf_dir:
        if not arguments.pdf_dir.is_dir():
            parser.error(f"нет каталога {arguments.pdf_dir}")
        paths.extend(sorted(path for path in arguments.pdf_dir.iterdir()
                            if path.suffix.lower() == ".pdf"))
    if not paths:
        parser.error("нет входных PDF")

    arguments.out.mkdir(parents=True, exist_ok=True)
    bundle = {table: [] for table in SCHEMA}
    failures = []
    seen = set()
    for path in paths:
        # The publication id is the file stem, and it is the primary key of
        # every table: two files with one name would merge into one invalid
        # bundle. Refuse the duplicate instead of writing it.
        if path.stem in seen:
            message = "повторяющийся publication_id: имя файла уже обработано."
            failures.append((path.name, message))
            if not arguments.skip_errors:
                print(f"{path.name}: ошибка: {message}")
                return 1
            continue
        try:
            result = extract_pdf(path)
        except (ValueError, OSError) as error:
            failures.append((path.name, str(error)))
            diagnostics.error(logger, "solar.cli.file_failed",
                              publication_id=path.stem,
                              kind=type(error).__name__)
            if not arguments.skip_errors:
                print(f"{path.name}: ошибка: {error}")
                return 1
            continue
        # Only a parsed file claims its publication id: a broken first file must
        # not block a valid second file with the same name.
        seen.add(path.stem)
        for table, rows in result["tables"].items():
            bundle[table].extend(rows)
        if arguments.json:
            (arguments.out / f"{path.stem}.json").write_text(
                json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                encoding="utf-8")
        print(f"{path.stem}: " + ", ".join(f"{table}={len(rows)}" for table, rows in result["tables"].items()))

    for table, columns in SCHEMA.items():
        # utf-8-sig matches export_csv: the template and Excel expect the BOM.
        with (arguments.out / f"{table}.csv").open("w", encoding="utf-8-sig",
                                                   newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(bundle[table])

    for name, error in failures:
        print(f"{name}: пропущен: {error}")
    print(f"итого: {sum(len(rows) for rows in bundle.values())} строк, {len(failures)} PDF пропущено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
