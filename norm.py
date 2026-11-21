"""Unit and condition normalization for extracted scientific results.

Pure functions only: no I/O, no database, no global state.  The module never
writes anything and never touches the stored text - normalization always
produces DERIVED values, and the caller keeps the original string next to them
(every result dict carries ``original_value`` / ``original_unit``).

Two hard rules, both driven by the real gold data:

1. Values are ``decimal.Decimal``, never ``float``.  Writing ``'0.330'`` through
   ``float`` yields ``'0.33'``, and 7451 of 37569 rag ``metric_value`` strings
   change that way; the organisers' validator compares duplicates at the
   reported precision.
2. An unknown unit is reported as unknown (``known=False`` / ``comparable=False``
   plus a reason).  No guessed conversions.

Vocabulary used below:

``value``     the number part, as Decimal (may be None)
``leftover``  everything in the input that is not the number, whitespace
              collapsed - the unit text plus any marker such as ``<`` or ``~``
``unit``      the unit text alone, markers peeled off by :func:`split_unit`
"""
from decimal import Decimal, InvalidOperation
import re

# Markers that may sit next to a number.  ``<``/``>``/``<=``/``>=``/``<=``/``>=``
# mean "a bound, not a measurement"; ``~``/``≈`` mean "approximately".
BOUND_MARKERS = ("<=", ">=", "<", ">", "\u2264", "\u2265")
APPROX_MARKERS = ("~", "\u2248", "\u223c")
_MARKER_CHARS = "<>\u2264\u2265~\u2248\u223c"

#: One numeric token: 24.1 / 24,1 / .05 / -3 / +2 / 1e-75 / 1.316e20 / 24.
_NUMBER = re.compile(r"[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?")

_NULL_TOKENS = {"", "-", "\u2013", "\u2014", "n/a", "na", "n.a.", "н/д", "нет"}


# --------------------------------------------------------------------------
# quantity parsing
# --------------------------------------------------------------------------
def parse_quantity_detail(text):
    """Split a written value into a number and the leftover text.

    Returns ``{'value', 'leftover', 'status'}`` where ``status`` is one of:

    ``ok``               a number was found
    ``empty``            blank or a null token (``-``, ``n/a``, ``н/д``)
    ``no_number``        text without a numeric token
    ``ambiguous_comma``  a comma that looks like a thousands separator

    Handled forms: plain (``24.1``), signed (``-3``, ``+2``), leading dot
    (``.05``, real in rag), scientific notation (``1e-75``, ``1.316e20``, real
    in rag), comma decimal (``1,5``, ``0,330``), an approximation marker before
    the number (``~0.0000``, ``≈20``), a bound marker (``<1``, ``>5``) and a
    unit after the number (``24.1 %``, ``100 mW/cm2``).

    Rejected on purpose: a comma with exactly three digits after it and a
    non-zero integer part (``1,234``, ``12,345``) is ambiguous - it may be a
    thousands separator - so no value is invented for it.  Digit grouping by
    spaces (``1 000``) is not handled; the leftover then keeps ``000`` and the
    caller sees a unit it does not recognise.  A range (``10-12``) yields the
    first number only, because the number token stops at the dash.
    """
    if text is None:
        return {"value": None, "leftover": "", "status": "empty"}
    raw = str(text).strip()
    # Unicode minus / dashes / non-breaking space appear in PDF-derived text.
    raw = (raw.replace("\u2212", "-").replace("\u2013", "-")
              .replace("\u2014", "-").replace("\u00a0", " "))
    if raw.lower() in _NULL_TOKENS:
        return {"value": None, "leftover": "", "status": "empty"}

    match = _NUMBER.search(raw)
    if match is None:
        return {"value": None, "leftover": raw, "status": "no_number"}

    token = match.group(0)
    value = _decimal_from_token(token)
    if value is None:
        return {"value": None, "leftover": raw, "status": "ambiguous_comma"}

    leftover = " ".join((raw[:match.start()] + " " + raw[match.end():]).split())
    return {"value": value, "leftover": leftover, "status": "ok"}


def parse_quantity(text):
    """``text -> (Decimal | None, leftover)``.

    ``(None, '')`` means "no usable number" (blank, ``n/a``, or an ambiguous
    comma).  The leftover keeps every non-numeric character of the input,
    including markers, so ``'<1'`` gives ``(Decimal('1'), '<')`` and
    ``'24.1 %'`` gives ``(Decimal('24.1'), '%')``.  Use :func:`split_unit` to
    peel markers off.  Never returns ``float``.
    """
    detail = parse_quantity_detail(text)
    if detail["status"] != "ok":
        return (None, "")
    return (detail["value"], detail["leftover"])


def parse_status(text):
    """Status string of :func:`parse_quantity_detail`, for reports."""
    return parse_quantity_detail(text)["status"]


def split_unit(leftover):
    """``leftover -> (unit, markers)``.

    Markers are peeled off both ends: ``'~ %'`` -> ``('%', '~')``,
    ``'< V'`` -> ``('V', '<')``, ``'<'`` -> ``('', '<')``.
    """
    text = (leftover or "").strip()
    markers = ""
    while text and text[0] in _MARKER_CHARS:
        markers += text[0]
        text = text[1:].lstrip()
    while text and text[-1] in _MARKER_CHARS:
        markers += text[-1]
        text = text[:-1].rstrip()
    return (text, markers)


def is_bound(markers):
    """True when the markers say the number is a bound, not a measurement."""
    return any(marker in markers for marker in BOUND_MARKERS)


def _decimal_from_token(token):
    """Turn a regex-matched numeric token into Decimal, or None if ambiguous."""
    exponent = "e" in token.lower()
    if "," in token and not exponent:
        if token.count(",") > 1:
            return None
        whole, fraction = token.split(",")
        digits = whole.lstrip("+-")
        # '1,234' / '12,345' could be a thousands separator: refuse to guess.
        if len(fraction) == 3 and digits and not digits.startswith("0") \
                and 1 <= len(digits) <= 3:
            return None
        token = whole + "." + fraction
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


# --------------------------------------------------------------------------
# irradiance
# --------------------------------------------------------------------------
#: unit key -> multiplier to W/m2.  Only exact, SI-justified factors.
_IRRADIANCE_FACTORS = {
    "w/m2": Decimal(1),        # reference
    "w/m^2": Decimal(1),
    "wm-2": Decimal(1),
    "mw/cm2": Decimal(10),     # 1 mW/cm2 = 10 W/m2   (1e-3 W / 1e-4 m2)
    "mw/cm^2": Decimal(10),
    "mwcm-2": Decimal(10),
    "mw/m2": Decimal("0.001"),  # SI prefix m = 1e-3
    "w/cm2": Decimal(10000),    # 1 W/cm2 = 1e4 W/m2
    "kw/m2": Decimal(1000),     # SI prefix k = 1e3
    "sun": Decimal(1000),       # 1 sun = 1000 W/m2 = 100 mW/cm2
    "suns": Decimal(1000),
}

#: 1 sun in W/m2, the value the organisers' rule asks for.
ONE_SUN_W_PER_M2 = Decimal(1000)


def unit_key(unit):
    """Canonical lookup key for a unit string (case, spaces, ^ and ² dropped)."""
    text = (unit or "").strip().lower()
    text = text.replace("\u00b2", "2").replace("\u00b3", "3")
    text = text.replace("^", "").replace("\u00b7", "*").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _irradiance_unit_key(unit):
    return unit_key(unit).replace(" ", "")


def normalize_irradiance(value_text, unit_text=""):
    """Normalize an irradiance to W/m2 and suns.

    Returns ``{'w_per_m2', 'sun', 'canonical', 'known', 'exact', 'unit',
    'original_value', 'original_unit', 'reason'}``.  All numbers are Decimal.

    ``known`` is False when the number is missing, the unit is not recognised,
    the value is a bound (``<1``), or the unit inside the value disagrees with
    the unit column.  Nothing is assumed: an empty unit stays unknown, so
    ``('0', '')`` is not turned into 0 W/m2.
    """
    value, leftover = parse_quantity(value_text)
    unit_from_value, markers = split_unit(leftover)
    column_unit = (unit_text or "").strip()

    result = {
        "w_per_m2": None,
        "sun": None,
        "canonical": "unknown",
        "known": False,
        "exact": False,
        "unit": "",
        "original_value": "" if value_text is None else str(value_text),
        "original_unit": column_unit,
        "reason": "",
    }

    if value is None:
        result["reason"] = "no numeric value"
        return result

    unit = unit_from_value or column_unit
    key = _irradiance_unit_key(unit)
    if unit_from_value and column_unit \
            and _irradiance_unit_key(column_unit) != _irradiance_unit_key(unit_from_value):
        result["reason"] = ("unit in the value (%s) disagrees with the unit column (%s)"
                            % (unit_from_value, column_unit))
        return result
    if is_bound(markers):
        result["reason"] = "bound, not a measured value"
        result["exact"] = False
        return result
    if key not in _IRRADIANCE_FACTORS:
        result["reason"] = ("unit not recognised: %r" % unit) if unit else "unit not recorded"
        return result

    watts = value * _IRRADIANCE_FACTORS[key]
    result["w_per_m2"] = watts
    result["sun"] = watts / ONE_SUN_W_PER_M2
    result["unit"] = key
    result["known"] = True
    result["exact"] = not any(marker in markers for marker in APPROX_MARKERS)
    result["canonical"] = "%s W/m2" % _fmt(watts)
    if not result["exact"]:
        result["reason"] = "approximate value (marker %s)" % markers
    return result


# --------------------------------------------------------------------------
# spectrum
# --------------------------------------------------------------------------
# Canonical spectrum classes.  A class is a claim about the spectral
# distribution, so it is only assigned when the text actually makes that claim.
SPECTRUM_AM15G = "am15g"
SPECTRUM_AM15 = "am15"
SPECTRUM_LED = "led"
SPECTRUM_HALOGEN = "halogen"
SPECTRUM_SUN_UNSPECIFIED = "sun_simulator_unspecified"
SPECTRUM_UNKNOWN = "unknown"

#: Exact variants found in the gold corpus, with the reason for the mapping.
#: Keys are lowercased and whitespace-collapsed.
_SPECTRUM_EXACT = {
    # Names the G (global) reference spectrum outright.
    "am1.5g": (SPECTRUM_AM15G,
               "names AM1.5G explicitly"),
    "am1.5g (xe lamp + filter)": (SPECTRUM_AM15G,
               "names AM1.5G; a Xe lamp with filter is the usual way to "
               "approximate that reference spectrum, and the text claims it"),
    # AM1.5 without a G claim.  The parenthetical is a note about the spelling
    # in the source ('1.5AM' is AM1.5 written back to front), not about G, so
    # this is AM1.5 and NOT AM1.5G.
    "am1.5 (\u0432 \u0442\u0435\u043a\u0441\u0442\u0435 \u00ab1.5am\u00bb)": (SPECTRUM_AM15,
               "AM1.5 with a note that the source writes it as '1.5AM'; the "
               "reordering still means AM1.5, and no G is claimed"),
    # Same class, stronger epistemic status: the authors say G is not stated.
    "am1.5 (g \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e)": (SPECTRUM_AM15,
               "AM1.5 with an explicit disclaimer that G is not specified, so "
               "an AM1.5G claim would be invented"),
    # '1 sun' / '100 mW/cm2' describe irradiance, not a spectrum.  AM1.5G is
    # the PV convention for 1 sun, but the text does not say it.
    "1 sun": (SPECTRUM_SUN_UNSPECIFIED,
               "states 1 sun, i.e. irradiance only; the spectrum is unnamed"),
    "1 sun (100 mw/cm2)": (SPECTRUM_SUN_UNSPECIFIED,
               "restates 1 sun as 100 mW/cm2, still an irradiance, not a spectrum"),
    "1 sun (oai trisol class aaa)": (SPECTRUM_SUN_UNSPECIFIED,
               "1 sun from an OAI TriSol Class AAA simulator; the class bounds "
               "spectral mismatch but AM1.5G is not named"),
    # Non-solar illumination.
    "white led": (SPECTRUM_LED,
               "white LED illumination, not a solar reference spectrum"),
    "halogen lamp (non-standard)": (SPECTRUM_HALOGEN,
               "halogen lamp, and the authors themselves mark it non-standard"),
}

#: Keyword fallbacks for variants not seen in the gold corpus yet.  Ordered:
#: the AM1.5G tests must run before the AM1.5 test, and 'xe'/'halogen' before
#: the generic simulator test.
_SPECTRUM_RULES = (
    ("am1.5g", SPECTRUM_AM15G, "contains AM1.5G"),
    ("am1.5 g", SPECTRUM_AM15G, "contains AM1.5 G"),
    ("am1.5global", SPECTRUM_AM15G, "contains AM1.5 global"),
    ("am 1.5g", SPECTRUM_AM15G, "contains AM 1.5G"),
    ("am1.5", SPECTRUM_AM15, "contains AM1.5 without a G"),
    ("am 1.5", SPECTRUM_AM15, "contains AM 1.5 without a G"),
    ("led", SPECTRUM_LED, "mentions LED"),
    ("halogen", SPECTRUM_HALOGEN, "mentions a halogen lamp"),
    ("tungsten", SPECTRUM_HALOGEN, "mentions a tungsten lamp"),
    ("incandescent", SPECTRUM_HALOGEN, "mentions an incandescent lamp"),
    ("sun", SPECTRUM_SUN_UNSPECIFIED, "mentions sun/irradiance only"),
    ("simulator", SPECTRUM_SUN_UNSPECIFIED, "mentions a simulator without a spectrum"),
    ("aaa", SPECTRUM_SUN_UNSPECIFIED, "mentions an IEC class without a spectrum"),
)


def _spectrum_key(text):
    return " ".join((text or "").split()).lower()


def classify_spectrum(text):
    """``text -> {'class', 'original', 'reason', 'am15g_claimed'}``.

    The original string is always carried through untouched: a class is a
    derived label, never a replacement for what the paper wrote.
    """
    original = "" if text is None else str(text)
    key = _spectrum_key(original)
    if not key:
        return {
            "class": SPECTRUM_UNKNOWN,
            "original": original,
            "reason": "spectrum not recorded; an empty field is not an AM1.5G claim",
            "am15g_claimed": False,
        }
    exact = _SPECTRUM_EXACT.get(key)
    if exact is not None:
        return {
            "class": exact[0],
            "original": original,
            "reason": exact[1],
            "am15g_claimed": exact[0] == SPECTRUM_AM15G,
        }
    for needle, klass, reason in _SPECTRUM_RULES:
        if needle in key:
            return {
                "class": klass,
                "original": original,
                "reason": reason,
                "am15g_claimed": klass == SPECTRUM_AM15G,
            }
    return {
        "class": SPECTRUM_UNKNOWN,
        "original": original,
        "reason": "unrecognised spectrum text, no class is invented",
        "am15g_claimed": False,
    }


def spectrum_class(text):
    """Canonical spectrum class of ``text`` (see :func:`classify_spectrum`).

    ``am15`` is deliberately a separate class from ``am15g``: merging them
    would turn "the paper did not say G" into "the paper said G".
    """
    return classify_spectrum(text)["class"]


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
#: canonical unit, unit family, comparable.  Solar units present in gold:
#: '%' 342, 'V' 123, 'mA/cm2' 119, 'Ohm*cm2' 65, '' 10.
_SOLAR_UNITS = {
    "%": ("percent", "ratio", True),
    "percent": ("percent", "ratio", True),
    "pct": ("percent", "ratio", True),
    "fraction": ("percent", "ratio", True),
    "v": ("volt", "voltage", True),
    "mv": ("volt", "voltage", True),
    "volt": ("volt", "voltage", True),
    "volts": ("volt", "voltage", True),
    "ma/cm2": ("mA/cm2", "current_density", True),
    "ma/cm^2": ("mA/cm2", "current_density", True),
    "ma cm-2": ("mA/cm2", "current_density", True),
    "ohm*cm2": ("ohm*cm2", "area_specific_resistance", True),
    "ohm cm2": ("ohm*cm2", "area_specific_resistance", True),
    "ohm*cm^2": ("ohm*cm2", "area_specific_resistance", True),
}

#: metric name -> unit family the name implies, for a solar consistency check.
_SOLAR_EXPECTED_FAMILY = {
    "pce": "ratio",
    "ff": "ratio",
    "retained_pce": "ratio",
    "stabilized_pce": "ratio",
    "avt": "ratio",
    "lue": "ratio",
    "hi": "ratio",
    "voc": "voltage",
    "voc_deficit": "voltage",
    "vmpp": "voltage",
    "jsc": "current_density",
    "jsc_eqe": "current_density",
    "jmpp": "current_density",
    "rs": "area_specific_resistance",
    "rsh": "area_specific_resistance",
}

#: rag units that are a plain ratio and therefore interconvertible.
_RATIO_UNITS = {"fraction", "percent", "%", "pct", "reported_%"}

#: rag units with an explicit, non-ratio canonical meaning.
_RAG_UNITS = {
    # percentage points: an absolute difference between two percentages.  It is
    # NOT a plain %: 1 pp is one hundredth of the full scale of the quantity
    # being differenced, and turning it into a fraction would need the base
    # value, which the submission does not record.
    "percentage_points": ("percentage_point", "percentage_point", True),
    "percentage points": ("percentage_point", "percentage_point", True),
    "percentagepoint": ("percentage_point", "percentage_point", True),
    # relative / delta units: the baseline is not recorded, so no comparison.
    "relative_percent": ("relative_percent", "relative", False),
    "relative percent": ("relative_percent", "relative", False),
    "relative_to_single_step": ("relative_to_single_step", "relative", False),
    "fraction_difference": ("fraction_difference", "delta_ratio", False),
    "percentage_points_per_log_million_tokens":
        ("percentage_point_per_log_million_tokens", "relative", False),
    # bounded but distinct from a fraction.
    "probability": ("probability", "probability", True),
    # dimensionless ratio that is not a fraction of 1 (can exceed 1).
    "ratio": ("ratio", "ratio_unbounded", True),
    # countable / physical units: comparable with the same unit only.
    "sentences": ("sentences", "count", True),
    "queries": ("queries", "count", True),
    "responses": ("responses", "count", True),
    "documents": ("documents", "count", True),
    "questions": ("questions", "count", True),
    "examples": ("examples", "count", True),
    "characters": ("characters", "count", True),
    "files": ("files", "count", True),
    "steps": ("steps", "count", True),
    "steps_per_query": ("steps_per_query", "rate", True),
    "sources_per_query": ("sources_per_query", "rate", True),
    "searches_per_query": ("searches_per_query", "rate", True),
    "tokens": ("tokens", "count", True),
    "tokens/file": ("tokens/file", "rate", True),
    "samples": ("samples", "count", True),
    "chunks": ("chunks", "count", True),
    "trajectories": ("trajectories", "count", True),
    "settings": ("settings", "count", True),
    "reported_steps": ("reported_steps", "count", True),
    "cases": ("cases", "count", True),
    "benchmarks": ("benchmarks", "count", True),
    "articles": ("articles", "count", True),
    "nodes": ("nodes", "count", True),
    "instances": ("instances", "count", True),
    "references": ("references", "count", True),
    "pairs": ("pairs", "count", True),
    "document_slots": ("document_slots", "count", True),
    "contacts": ("contacts", "count", True),
    "triplets": ("triplets", "count", True),
    "reports": ("reports", "count", True),
    "passes": ("passes", "count", True),
    "organizations": ("organizations", "count", True),
    "edges": ("edges", "count", True),
    "datasets": ("datasets", "count", True),
    "clusters": ("clusters", "count", True),
    "categories": ("categories", "count", True),
    "qa pairs": ("QA pairs", "count", True),
    "distinct_markers": ("distinct_markers", "count", True),
    "count": ("count", "count", True),
    "ms": ("ms", "duration", True),
    "hours": ("hours", "duration", True),
    "mib": ("MiB", "bytes", True),
    "mb": ("MB", "bytes", True),
    "gib": ("GiB", "bytes", True),
    "gb": ("GB", "bytes", True),
    "usd": ("USD", "currency", True),
    "usd/token": ("USD/token", "currency_rate", True),
    "usd_per_language": ("USD_per_language", "currency", True),
    "flops": ("FLOPs", "compute", True),
    "gpu_seconds": ("GPU_seconds", "compute", True),
    "h20_gpu_hours": ("H20_GPU_hours", "compute", True),
}

#: rag units whose scale is not defined by the submission schema, so two
#: values carrying the same label cannot be placed on one axis.
_RAG_SCALE_LESS = {
    "score": "a bare score with no scale is not comparable across papers",
    "score [-10,10]": "the scale is stated per paper, not by the schema",
    "score [0,10]": "the scale is stated per paper, not by the schema",
    "score_1_to_5": "the scale is stated per paper, not by the schema",
    "index": "a bare index has no defined unit",
    "rating_points": "rating scales differ between papers",
    "points": "a bare point count has no defined scale",
    "robust anomaly score": "model-specific score, no shared scale",
    "impact_score": "paper-specific score, no shared scale",
    "sentiment_score": "paper-specific score, no shared scale",
    "perplexity": "model-dependent, not comparable without the model",
    "l2 distance": "distance depends on the embedding model",
    "correlation": "a correlation needs the paired quantity to be comparable",
    "coefficient": "the coefficient kind is not recorded",
    "kappa": "agreement statistic on its own scale, not a percentage",
}


def ratio_unit_kind(unit):
    """``'fraction'``, ``'percent'`` or ``None`` for a ratio-family unit."""
    key = unit_key(unit)
    if key not in _RATIO_UNITS:
        return None
    return "fraction" if key == "fraction" else "percent"


def convert_fraction_percent(value, from_unit, to_unit):
    """Convert between ``fraction`` and ``%``; ``None`` if not applicable.

    Exactly 100 is the factor, so the conversion is lossless in Decimal.
    ``percentage_points`` is deliberately not accepted here.
    """
    if value is None:
        return None
    source = unit_key(from_unit)
    target = unit_key(to_unit)
    if source not in _RATIO_UNITS or target not in _RATIO_UNITS:
        return None
    as_fraction = value if source == "fraction" else value / Decimal(100)
    if target == "fraction":
        return as_fraction
    return as_fraction * Decimal(100)


def normalize_metric(area, metric_name, value_text, unit_text=""):
    """Normalize one measurement value to a canonical unit.

    Returns ``{'value', 'unit_canonical', 'comparable', 'reason', 'family',
    'as_fraction', 'as_percent', 'original_value', 'original_unit', 'unit'}``.

    ``comparable`` means "this value can be placed on the axis named by
    ``unit_canonical`` next to other values of the same family".  For solar
    that is the axis of the metric; whether two *measurements* share physical
    conditions is a separate question answered by :func:`same_condition`.
    """
    area_key = (area or "").strip().lower()
    original_value = "" if value_text is None else str(value_text)
    original_unit = (unit_text or "").strip()
    value, leftover = parse_quantity(value_text)
    unit_from_value, markers = split_unit(leftover)
    unit_text_in_value = unit_from_value

    result = {
        "value": value,
        "unit_canonical": "unknown",
        "comparable": False,
        "reason": "",
        "family": "unknown",
        "as_fraction": None,
        "as_percent": None,
        "original_value": original_value,
        "original_unit": original_unit,
        "unit": "",
    }

    if value is None:
        result["reason"] = "no numeric value (%s)" % parse_status(value_text)
        return result
    if unit_text_in_value and original_unit \
            and unit_key(unit_text_in_value) != unit_key(original_unit):
        result["reason"] = ("unit in the value (%s) disagrees with the unit column (%s)"
                            % (unit_text_in_value, original_unit))
        return result
    if is_bound(markers):
        result["reason"] = "bound, not a measured value"
        return result

    unit = unit_text_in_value or original_unit
    key = unit_key(unit)
    result["unit"] = key

    if not key:
        result["reason"] = "unit not recorded; a value without a unit is not comparable"
        return result

    if area_key == "solar":
        spec = _SOLAR_UNITS.get(key)
        if spec is None:
            result["reason"] = "unit not recognised for area solar: %r" % unit
            return result
        canonical, family, comparable = spec
        expected = _SOLAR_EXPECTED_FAMILY.get(unit_key(metric_name))
        if expected is not None and expected != family:
            result["unit_canonical"] = canonical
            result["family"] = family
            result["reason"] = ("unit family %s does not match metric %r, which "
                                "expects %s" % (family, metric_name, expected))
            return result
        result.update({
            "unit_canonical": canonical,
            "family": family,
            "comparable": comparable,
            "reason": "" if comparable else "unit not comparable",
        })
        if family == "ratio":
            if key == "fraction":
                result["as_fraction"] = value
                result["as_percent"] = value * Decimal(100)
                result["value"] = result["as_percent"]
            else:
                result["as_fraction"] = value / Decimal(100)
                result["as_percent"] = value
        elif family == "voltage" and key == "mv":
            result["value"] = value / Decimal(1000)
        return result

    if area_key == "rag":
        if key in _RATIO_UNITS:
            result.update({
                "unit_canonical": "fraction" if key == "fraction" else "percent",
                "family": "ratio",
                "comparable": True,
                "reason": "convertible between fraction and % (factor 100)",
            })
            result["as_fraction"] = (value if key == "fraction"
                                     else value / Decimal(100))
            result["as_percent"] = (value * Decimal(100) if key == "fraction"
                                    else value)
            return result
        spec = _RAG_UNITS.get(key)
        if spec is not None:
            canonical, family, comparable = spec
            result.update({
                "unit_canonical": canonical,
                "family": family,
                "comparable": comparable,
                "reason": "" if comparable else "unit carries no comparable scale",
            })
            if key == "percentage_points" or key == "percentage points" \
                    or key == "percentagepoint":
                result["reason"] = ("percentage point is an absolute difference "
                                    "between percentages; the base value needed "
                                    "to express it as a fraction is not recorded, "
                                    "so it is not a plain %")
            return result
        reason = _RAG_SCALE_LESS.get(key)
        if reason is not None:
            result["unit_canonical"] = key
            result["family"] = "score"
            result["reason"] = reason
            return result
        result["reason"] = "unit not in the known list for area rag: %r" % unit
        return result

    result["reason"] = "unknown area %r" % area
    return result


# --------------------------------------------------------------------------
# measurement conditions
# --------------------------------------------------------------------------
#: the organisers' comparability rule for solar.
STANDARD_IRRADIANCE_W_PER_M2 = Decimal(1000)   # 100 mW/cm2 == 1000 W/m2 == 1 sun
STANDARD_SCAN = "reverse"
STANDARD_MASK = "true"

_CONDITION_ALIASES = {
    "spectrum": ("spectrum",),
    "intensity": ("light_intensity", "irradiance", "intensity", "value"),
    "unit": ("intensity_unit", "irradiance_unit", "unit"),
    "scan": ("scan_direction", "scan"),
    "mask": ("mask_used", "mask"),
}


def _field(record, name, default=""):
    for key in _CONDITION_ALIASES[name]:
        if isinstance(record, dict):
            if key in record and record[key] is not None:
                return record[key]
        else:
            try:
                return record[key]
            except (KeyError, IndexError, TypeError):
                continue
    return default


def condition_report(record):
    """Per-condition verdict for one measurement.

    ``{'ok', 'spectrum_class', 'irradiance', 'scan', 'mask', 'reasons'}``.
    Every condition must be recorded AND match the standard; an empty field
    fails, because absence of a condition is not agreement.
    """
    spectrum = _field(record, "spectrum")
    intensity = _field(record, "intensity")
    unit = _field(record, "unit")
    scan = _field(record, "scan")
    mask = _field(record, "mask")

    reasons = []
    klass = spectrum_class(spectrum)
    if klass != SPECTRUM_AM15G:
        reasons.append("spectrum is %s, not am15g (%r)" % (klass, spectrum))

    irradiance = normalize_irradiance(intensity, unit)
    if not irradiance["known"]:
        reasons.append("irradiance unknown: %s" % irradiance["reason"])
    elif irradiance["w_per_m2"] != STANDARD_IRRADIANCE_W_PER_M2:
        reasons.append("irradiance is %s W/m2, not 1000"
                       % _fmt(irradiance["w_per_m2"]))

    scan_key = " ".join(str(scan or "").split()).lower()
    if scan_key != STANDARD_SCAN:
        reasons.append("scan_direction is %r, not %r" % (scan, STANDARD_SCAN))

    mask_key = " ".join(str(mask or "").split()).lower()
    if mask_key != STANDARD_MASK:
        reasons.append("mask_used is %r, not %r" % (mask, STANDARD_MASK))

    return {
        "ok": not reasons,
        "spectrum_class": klass,
        "irradiance": irradiance,
        "scan": scan_key,
        "mask": mask_key,
        "reasons": reasons,
    }


def same_condition(a, b):
    """True only if ``a`` and ``b`` are both measured under the standard.

    The organisers' rule is absolute, not pairwise: a measurement is comparable
    only when spectrum is AM1.5G, irradiance is 100 mW/cm2 (1000 W/m2, 1 sun),
    ``scan_direction`` is ``reverse`` and ``mask_used`` is ``true``.  Two
    measurements are therefore comparable exactly when both satisfy it, which
    is what this function checks.  A missing value fails: an unrecorded
    condition is not agreement.
    """
    return condition_report(a)["ok"] and condition_report(b)["ok"]


def condition_signature(record):
    """Hashable signature of the standard condition, for grouping."""
    report = condition_report(record)
    if not report["ok"]:
        return None
    return (report["spectrum_class"],
            _fmt(report["irradiance"]["w_per_m2"]),
            report["scan"], report["mask"])


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def _fmt(value):
    """Decimal -> plain string, no exponent, trailing zeros trimmed."""
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
