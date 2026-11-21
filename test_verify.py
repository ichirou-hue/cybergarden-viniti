import unittest

import verify


class VerificationBoundaryTest(unittest.TestCase):
    def test_sign_and_range_are_not_erased(self):
        self.assertFalse(verify.value_present("-", "anything"))
        self.assertFalse(verify.quote_present("-5.2", "the value is 5.2"))
        self.assertFalse(verify.quote_present("12", "range 1-2"))

    def test_table_labels_require_boundaries(self):
        self.assertFalse(verify.label_on_page(
            "Table 1", [], 7, "Table 10. Photovoltaic parameters"))
        self.assertTrue(verify.label_on_page(
            "Table 1", [], 7, "Table 1 0.71 Photovoltaic parameters"))

    def test_supplementary_label_variants_do_not_cross_tables(self):
        self.assertNotIn("Table 3", verify.label_variants("Table S3"))
        self.assertNotIn("Table SS2", verify.label_variants("Supplementary Table S2"))
        self.assertIn("Table S2", verify.label_variants("Supplementary Table S2"))

    def test_locator_needs_a_number(self):
        self.assertFalse(verify.is_locator("Table:"))
        self.assertEqual(verify.locator_label("Приложение 1,"), "Приложение 1")

    def test_non_finite_numbers_are_not_numeric(self):
        self.assertIsNone(verify.numeric_key("nan"))
        self.assertIsNone(verify.numeric_key("inf"))


if __name__ == "__main__":
    unittest.main()
