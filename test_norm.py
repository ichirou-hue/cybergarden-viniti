"""Tests for norm.py - unit and condition normalization.

Run:  python -X utf8 -m unittest test_norm -v
"""
from decimal import Decimal
import unittest

import norm


GOLD_SPECTRUM_VARIANTS = [
    # (text as written in the gold corpus, expected class)
    ("AM1.5G", "am15g"),
    ("", "unknown"),
    ("AM1.5 (\u0432 \u0442\u0435\u043a\u0441\u0442\u0435 \u00ab1.5AM\u00bb)", "am15"),
    ("1 sun (OAI TriSol Class AAA)", "sun_simulator_unspecified"),
    ("white LED", "led"),
    ("halogen lamp (non-standard)", "halogen"),
    ("AM1.5G (Xe lamp + filter)", "am15g"),
    ("AM1.5 (G \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e)", "am15"),
    ("1 sun (100 mW/cm2)", "sun_simulator_unspecified"),
    ("1 sun", "sun_simulator_unspecified"),
]

STANDARD_CONDITION = {
    "spectrum": "AM1.5G",
    "light_intensity": "100",
    "intensity_unit": "mW/cm2",
    "scan_direction": "reverse",
    "mask_used": "true",
}

EMPTY_CONDITION = {
    "spectrum": "",
    "light_intensity": "",
    "intensity_unit": "",
    "scan_direction": "",
    "mask_used": "",
}


class ParseQuantityTest(unittest.TestCase):
    def test_reported_precision_is_preserved(self):
        # '0.330' must not become Decimal('0.33'); 7451 rag values depend on it.
        value, leftover = norm.parse_quantity("0.330")
        self.assertEqual(value, Decimal("0.330"))
        self.assertEqual(str(value), "0.330")
        self.assertEqual(leftover, "")

    def test_never_returns_float(self):
        for text in ("0.330", "24.1", "1,5", "1e-75", ".05", "<1"):
            value, _ = norm.parse_quantity(text)
            if value is not None:
                self.assertIsInstance(value, Decimal, text)

    def test_plain_and_signed(self):
        self.assertEqual(norm.parse_quantity("24.1"), (Decimal("24.1"), ""))
        self.assertEqual(norm.parse_quantity("-3"), (Decimal("-3"), ""))
        self.assertEqual(norm.parse_quantity("+2.5"), (Decimal("2.5"), ""))

    def test_comma_decimal(self):
        self.assertEqual(norm.parse_quantity("1,5"), (Decimal("1.5"), ""))
        self.assertEqual(norm.parse_quantity("0,330"), (Decimal("0.330"), ""))

    def test_thousands_comma_is_ambiguous_and_rejected(self):
        # '291,888' appears in rag raw_value and may be 291888, not 291.888.
        self.assertEqual(norm.parse_quantity("291,888"), (None, ""))
        self.assertEqual(norm.parse_status("291,888"), "ambiguous_comma")

    def test_leading_dot_and_exponent(self):
        self.assertEqual(norm.parse_quantity(".05"), (Decimal("0.05"), ""))
        self.assertEqual(norm.parse_quantity("1e-75"), (Decimal("1E-75"), ""))
        self.assertEqual(norm.parse_quantity("1.316e20"), (Decimal("1.316E+20"), ""))

    def test_markers(self):
        self.assertEqual(norm.parse_quantity("\u224820"), (Decimal("20"), "\u2248"))
        self.assertEqual(norm.parse_quantity("<1"), (Decimal("1"), "<"))
        self.assertEqual(norm.parse_quantity("~0.0000"), (Decimal("0.0000"), "~"))
        self.assertEqual(norm.split_unit("<"), ("", "<"))
        self.assertEqual(norm.split_unit("~ %"), ("%", "~"))
        self.assertTrue(norm.is_bound("<"))
        self.assertFalse(norm.is_bound("~"))

    def test_unit_leftover(self):
        self.assertEqual(norm.parse_quantity("24.1 %"), (Decimal("24.1"), "%"))
        self.assertEqual(norm.parse_quantity("100 mW/cm2"),
                         (Decimal("100"), "mW/cm2"))

    def test_no_number(self):
        for text in ("", "n/a", "-", "zero", "encoded only by bubble area", None):
            self.assertEqual(norm.parse_quantity(text), (None, ""), repr(text))


class UnitKeyTest(unittest.TestCase):
    def test_solar_caret_units_share_canonical_keys(self):
        self.assertEqual(norm.unit_key("mA/cm^2"), norm.unit_key("mA/cm2"))
        self.assertEqual(norm.unit_key("Ohm*cm^2"), norm.unit_key("ohm*cm2"))
        self.assertTrue(norm.normalize_metric("solar", "Jsc", "1", "mA/cm^2")["comparable"])
        self.assertTrue(norm.normalize_metric("solar", "Rs", "1", "Ohm*cm^2")["comparable"])


class IrradianceTest(unittest.TestCase):
    def test_three_forms_of_one_sun_are_equal(self):
        mw = norm.normalize_irradiance("100", "mW/cm2")
        w = norm.normalize_irradiance("1000", "W/m2")
        sun = norm.normalize_irradiance("1", "sun")
        for result in (mw, w, sun):
            self.assertTrue(result["known"])
            self.assertEqual(result["w_per_m2"], Decimal("1000"))
            self.assertEqual(result["sun"], Decimal("1"))
            self.assertEqual(result["canonical"], "1000 W/m2")

    def test_mw_cm2_times_ten(self):
        result = norm.normalize_irradiance("50", "mW/cm2")
        self.assertEqual(result["w_per_m2"], Decimal("500"))
        self.assertEqual(result["sun"], Decimal("0.5"))

    def test_unit_inside_value(self):
        result = norm.normalize_irradiance("100 mW/cm2", "")
        self.assertTrue(result["known"])
        self.assertEqual(result["w_per_m2"], Decimal("1000"))

    def test_unknown_unit_is_not_guessed(self):
        result = norm.normalize_irradiance("1", "furlongs")
        self.assertFalse(result["known"])
        self.assertIsNone(result["w_per_m2"])
        self.assertIn("not recognised", result["reason"])

    def test_missing_unit_is_not_guessed(self):
        result = norm.normalize_irradiance("100", "")
        self.assertFalse(result["known"])
        self.assertEqual(result["reason"], "unit not recorded")

    def test_bound_is_not_a_measurement(self):
        result = norm.normalize_irradiance("<100", "mW/cm2")
        self.assertFalse(result["known"])
        self.assertIn("bound", result["reason"])

    def test_conflicting_units(self):
        result = norm.normalize_irradiance("100 W/m2", "mW/cm2")
        self.assertFalse(result["known"])
        self.assertIn("disagrees", result["reason"])

    def test_original_text_survives(self):
        result = norm.normalize_irradiance("100", "mW/cm2")
        self.assertEqual(result["original_value"], "100")
        self.assertEqual(result["original_unit"], "mW/cm2")


class SpectrumTest(unittest.TestCase):
    def test_every_gold_variant_is_mapped(self):
        self.assertEqual(len(GOLD_SPECTRUM_VARIANTS), 10)
        for text, expected in GOLD_SPECTRUM_VARIANTS:
            self.assertEqual(norm.spectrum_class(text), expected, repr(text))

    def test_am15_is_not_merged_into_am15g(self):
        self.assertNotEqual(norm.spectrum_class("AM1.5 (G \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e)"),
                            norm.spectrum_class("AM1.5G"))
        self.assertEqual(norm.spectrum_class("AM1.5 (G \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e)"),
                         "am15")
        info = norm.classify_spectrum("AM1.5 (G \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e)")
        self.assertFalse(info["am15g_claimed"])

    def test_reordered_spelling_is_am15(self):
        info = norm.classify_spectrum("AM1.5 (\u0432 \u0442\u0435\u043a\u0441\u0442\u0435 \u00ab1.5AM\u00bb)")
        self.assertEqual(info["class"], "am15")
        self.assertFalse(info["am15g_claimed"])

    def test_original_text_is_preserved(self):
        for text, expected in GOLD_SPECTRUM_VARIANTS:
            info = norm.classify_spectrum(text)
            self.assertEqual(info["original"], text)
            self.assertEqual(info["class"], expected)

    def test_empty_is_unknown_not_am15g(self):
        info = norm.classify_spectrum("")
        self.assertEqual(info["class"], "unknown")
        self.assertFalse(info["am15g_claimed"])

    def test_unrecognised_text_is_unknown(self):
        self.assertEqual(norm.spectrum_class("laser diode 660 nm"), "unknown")


class MetricTest(unittest.TestCase):
    def test_solar_percent(self):
        result = norm.normalize_metric("solar", "PCE", "12.89", "%")
        self.assertEqual(result["value"], Decimal("12.89"))
        self.assertEqual(result["unit_canonical"], "percent")
        self.assertTrue(result["comparable"])

    def test_solar_units(self):
        cases = [
            ("Voc", "1.197", "V", "volt"),
            ("Jsc", "15.28", "mA/cm2", "mA/cm2"),
            ("Rs", "2.5", "Ohm*cm2", "ohm*cm2"),
        ]
        for metric, value, unit, expected in cases:
            result = norm.normalize_metric("solar", metric, value, unit)
            self.assertEqual(result["unit_canonical"], expected, unit)
            self.assertTrue(result["comparable"], unit)

    def test_solar_millivolts_are_canonical_volts(self):
        result = norm.normalize_metric("solar", "Voc", "960", "mV")
        self.assertEqual(result["value"], Decimal("0.960"))
        self.assertEqual(result["unit_canonical"], "volt")
        self.assertTrue(result["comparable"])

    def test_solar_fraction_requires_explicit_unit(self):
        fraction = norm.normalize_metric("solar", "PCE", "0.70", "fraction")
        self.assertEqual(fraction["value"], Decimal("70.00"))
        self.assertEqual(fraction["unit_canonical"], "percent")
        self.assertFalse(norm.normalize_metric("solar", "PCE", "0.70", "")["comparable"])

    def test_solar_unknown_unit_and_printed_precision(self):
        unknown = norm.normalize_metric("solar", "Voc", "1.200", "furlongs")
        self.assertFalse(unknown["comparable"])
        exact = norm.normalize_metric("solar", "Voc", "960", "mV")
        self.assertEqual(exact["original_value"], "960")
        self.assertEqual(exact["original_unit"], "mV")

    def test_solar_empty_unit_is_not_comparable(self):
        result = norm.normalize_metric("solar", "FF", "71.0", "")
        self.assertFalse(result["comparable"])
        self.assertEqual(result["unit_canonical"], "unknown")
        self.assertIn("unit not recorded", result["reason"])

    def test_solar_unit_metric_mismatch(self):
        # Jsc is a current density; a bare volt cannot be its unit.
        result = norm.normalize_metric("solar", "Jsc", "15.28", "V")
        self.assertFalse(result["comparable"])
        self.assertIn("does not match", result["reason"])

    def test_rag_fraction_and_percent_convert(self):
        fraction = norm.normalize_metric("rag", "Answer F1", "0.330", "fraction")
        percent = norm.normalize_metric("rag", "Answer F1", "33.0", "%")
        self.assertTrue(fraction["comparable"])
        self.assertTrue(percent["comparable"])
        self.assertEqual(fraction["as_fraction"], Decimal("0.330"))
        self.assertEqual(fraction["as_percent"], Decimal("33.000"))
        self.assertEqual(percent["as_fraction"], Decimal("0.330"))
        self.assertEqual(norm.convert_fraction_percent(Decimal("0.330"),
                                                       "fraction", "%"),
                         Decimal("33.000"))
        self.assertEqual(norm.convert_fraction_percent(Decimal("33.0"),
                                                       "%", "fraction"),
                         Decimal("0.330"))

    def test_rag_percentage_points_is_not_percent(self):
        result = norm.normalize_metric("rag", "Factuality", "1.2",
                                       "percentage_points")
        self.assertEqual(result["unit_canonical"], "percentage_point")
        self.assertNotEqual(result["unit_canonical"], "percent")
        self.assertIsNone(result["as_fraction"])
        self.assertIn("absolute difference", result["reason"])
        # And the conversion helper refuses it outright.
        self.assertIsNone(norm.convert_fraction_percent(Decimal("1.2"),
                                                        "percentage_points", "%"))
        spaced = norm.normalize_metric("rag", "Factuality", "1.2",
                                       "percentage points")
        self.assertEqual(spaced["unit_canonical"], "percentage_point")

    def test_rag_unknown_unit(self):
        result = norm.normalize_metric("rag", "X", "5", "furlongs")
        self.assertFalse(result["comparable"])
        self.assertIn("not in the known list", result["reason"])

    def test_unknown_area(self):
        result = norm.normalize_metric("chemistry", "X", "5", "%")
        self.assertFalse(result["comparable"])
        self.assertIn("unknown area", result["reason"])

    def test_value_and_unit_text_are_preserved(self):
        result = norm.normalize_metric("rag", "Answer F1", "0.330", "fraction")
        self.assertEqual(result["original_value"], "0.330")
        self.assertEqual(result["original_unit"], "fraction")


class SameConditionTest(unittest.TestCase):
    def test_standard_condition_is_comparable(self):
        self.assertTrue(norm.same_condition(STANDARD_CONDITION,
                                            dict(STANDARD_CONDITION)))

    def test_equivalent_irradiance_units_agree(self):
        w = dict(STANDARD_CONDITION, light_intensity="1000", intensity_unit="W/m2")
        sun = dict(STANDARD_CONDITION, light_intensity="1", intensity_unit="sun")
        self.assertTrue(norm.same_condition(STANDARD_CONDITION, w))
        self.assertTrue(norm.same_condition(w, sun))

    def test_empty_condition_is_not_comparable(self):
        self.assertFalse(norm.same_condition(STANDARD_CONDITION, EMPTY_CONDITION))
        self.assertFalse(norm.same_condition(EMPTY_CONDITION, EMPTY_CONDITION))

    def test_each_missing_field_breaks_comparability(self):
        for field in STANDARD_CONDITION:
            broken = dict(STANDARD_CONDITION)
            broken[field] = ""
            self.assertFalse(norm.same_condition(STANDARD_CONDITION, broken), field)

    def test_non_standard_conditions(self):
        variants = [
            dict(STANDARD_CONDITION, spectrum="AM1.5"),
            dict(STANDARD_CONDITION, spectrum="1 sun"),
            dict(STANDARD_CONDITION, light_intensity="50"),
            dict(STANDARD_CONDITION, scan_direction="forward"),
            dict(STANDARD_CONDITION, mask_used="false"),
        ]
        for variant in variants:
            self.assertFalse(norm.same_condition(STANDARD_CONDITION, variant),
                             variant)

    def test_condition_report_reasons(self):
        report = norm.condition_report(EMPTY_CONDITION)
        self.assertFalse(report["ok"])
        self.assertEqual(report["spectrum_class"], "unknown")
        self.assertEqual(len(report["reasons"]), 4)


if __name__ == "__main__":
    unittest.main()
