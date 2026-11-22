"""Keep typographic indices with their parent glyph using PDF geometry only."""

from copy import deepcopy


def _size(char):
    return float(char.get("size") or char["bottom"] - char["top"])


def _baseline(char):
    matrix = char.get("matrix")
    if matrix and len(matrix) == 6:
        return -float(matrix[5])
    return float(char["bottom"]) - .2 * _size(char)


def _parent(script, candidates):
    matches = []
    for base in candidates:
        size = _size(base)
        delta = _baseline(script) - _baseline(base)
        gap = script["x0"] - base["x1"]
        if (base["text"].strip() and _size(script) <= .95 * size
                and -.15 * size <= gap <= .85 * size
                and .12 * size <= abs(delta) <= .85 * size):
            matches.append((abs(gap) + abs(delta) / 3, base, "subscript" if delta > 0 else "superscript"))
    if not matches:
        return None
    _, base, position = min(matches, key=lambda entry: entry[0])
    return base, position


def _render(chars, attachments, *, latex):
    attached = {id(c) for values in attachments.values() for _, group in values for c in group}
    def glyph(char, visited):
        if id(char) in visited:
            return char["text"]
        visited = visited | {id(char)}
        text = char["text"]
        for position, group in attachments.get(id(char), []):
            index = "".join(glyph(c, visited) for c in sorted(group, key=lambda c: c["x0"]))
            text += (("_{" if position == "subscript" else "^{") + index + "}") if latex else index
        return text

    def right_edge(char):
        return max([char["x1"], *[right_edge(c) for _, group in attachments.get(id(char), []) for c in group]])

    chunks, previous = [], None
    for char in sorted(chars, key=lambda c: c["x0"]):
        if id(char) in attached:
            continue
        if previous is not None and char["x0"] - right_edge(previous) > .15 * _size(char):
            chunks.append(" ")
        chunks.append(glyph(char, set()))
        previous = char
    return "".join(chunks).strip()


def _parents(chars, candidates):
    """Let all glyphs of an index label follow the first attached glyph."""
    matches, previous, previous_parent = {}, None, None
    for char in sorted(chars, key=lambda c: c["x0"]):
        if (previous is not None and previous_parent is not None
                and abs(_size(previous) - _size(char)) <= .04 * _size(char)
                and abs(_baseline(previous) - _baseline(char)) <= .12 * _size(char)
                and -.03 * _size(char) <= char["x0"] - previous["x1"] <= .5 * _size(char)):
            parent = previous_parent
        else:
            parent = _parent(char, candidates)
        matches[id(char)] = parent
        previous, previous_parent = char, parent
    return matches


def attach_script_lines(lines):
    """Return copied pdfplumber lines with attached small-font indices.

    ``text`` stays plain text; ``original_lines`` retains untouched parser text.
    ``display_text`` uses LaTeX index notation, not semantic equation inference.
    ``math_spans`` records the base glyph, index text and coordinate provenance.
    Only smaller shifted glyphs are moved; neighboring full-size text stays put.
    """
    lines = list(lines)
    result = deepcopy(lines)
    for line in result:
        line.pop("display_text", None)
        line.pop("math_spans", None)
        line["original_lines"] = [line["text"]]
    # A PDF text row may contain both a sidebar and an index belonging to the
    # neighboring main column. Move individual glyphs, not the entire row.
    originals = [list(line["chars"]) for line in result]
    moves = {}
    for index, chars in enumerate(originals):
        options = {}
        visible = [c for c in chars if c["text"].strip()]
        if not visible:
            continue
        for target_index, candidates in enumerate(originals):
            candidates = [c for c in candidates if c["text"].strip()]
            if not candidates:
                continue
            largest = max(map(_size, candidates))
            if min(map(_size, visible)) > .95 * largest:
                continue
            if (min(map(_baseline, visible)) - max(map(_baseline, candidates)) > .85 * largest
                    or min(map(_baseline, candidates)) - max(map(_baseline, visible)) > .85 * largest):
                continue
            matched = _parents(visible, candidates)
            for char in visible:
                parent = matched[id(char)]
                if parent is None:
                    continue
                base = parent[0]
                score = abs(_baseline(char) - _baseline(base)) + abs(char["x0"] - base["x1"]) / 3
                # Existing same-line attachment is preferred to moving glyphs.
                candidate = (target_index != index, score, target_index, base)
                if id(char) not in options or candidate[:2] < options[id(char)][:2]:
                    options[id(char)] = candidate
        for char in visible:
            option = options.get(id(char))
            if option and option[2] != index:
                moves[id(char)] = (option[2], option[3])

    def destination(char, fallback):
        seen = set()
        while id(char) in moves and id(char) not in seen:
            seen.add(id(char))
            fallback, char = moves[id(char)]
        return fallback

    grouped_lines = [[] for _ in result]
    origins = [set() for _ in result]
    for index, chars in enumerate(originals):
        for char in chars:
            target = destination(char, index)
            grouped_lines[target].append(char)
            origins[target].add(index)
    removed = set()
    for index, line in enumerate(result):
        line["chars"] = grouped_lines[index]
        if not any(c["text"].strip() for c in line["chars"]):
            removed.add(index)
            continue
        line["original_lines"] = [lines[i]["text"] for i in sorted(origins[index])]
        if len(line["chars"]) != len(originals[index]):
            line["text"] = _render([c for c in line["chars"] if c["text"].strip()], {}, latex=False)
    output = []
    for index, line in enumerate(result):
        if index in removed:
            continue
        chars = [c for c in line["chars"] if c["text"].strip()]
        attachments = {}
        grouped = {}
        matched = _parents(chars, chars)
        for char in chars:
            parent = matched[id(char)]
            if parent:
                base, position = parent
                key = (id(base), position)
                grouped.setdefault(key, (base, []))[1].append(char)
        spans = []
        for (_, position), (base, group) in grouped.items():
            attachments.setdefault(id(base), []).append((position, group))
            spans.append({"base": base["text"], "text": "".join(c["text"] for c in sorted(group, key=lambda c: c["x0"])),
                          "position": position, "bbox": [min(c["x0"] for c in group), min(c["top"] for c in group),
                                                           max(c["x1"] for c in group), max(c["bottom"] for c in group)],
                          "base_bbox": [base["x0"], base["top"], base["x1"], base["bottom"]]})
        if spans:
            line["text"] = _render(chars, attachments, latex=False)
            line["display_text"] = _render(chars, attachments, latex=True)
            line["math_spans"] = spans
            line.update(x0=min(c["x0"] for c in chars), x1=max(c["x1"] for c in chars),
                        top=min(c["top"] for c in chars), bottom=max(c["bottom"] for c in chars))
        output.append(line)
    return output
