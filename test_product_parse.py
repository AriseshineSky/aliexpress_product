from __future__ import annotations

import unittest

from html_utils import clean_product_description


SAMPLE_DESCRIPTION = """
<div id="product-description" data-spm="1000023" class="description--product-description--Mjtql28" data-pl="product-description">
<div><template shadowrootmode="open"><style>
        .product-description { position: relative; max-width: 944px; overflow: hidden; }
      </style><div id="product-description" class="product-description"><div class="detailmodule_html"><div class="detail-desc-decorate-richtext"><p><img src="https://ae01.alicdn.com/kf/S32b3bde107854176a6580fbf167791ec0.jpg" slate-data-type="image"><img src="https://ae01.alicdn.com/kf/S7d24c2e638474fe5ac46103581a323c5Q.jpg" slate-data-type="image"></p></div></div>
<script>window.adminAccountId=2678280160;</script>
</div></template></div></div>
"""


class ProductParseTestCase(unittest.TestCase):
    def test_clean_product_description_removes_script_style_and_whitespace(self):
        cleaned = clean_product_description(SAMPLE_DESCRIPTION)
        self.assertIn("S32b3bde107854176a6580fbf167791ec0.jpg", cleaned)
        self.assertNotIn("<script", cleaned.lower())
        self.assertNotIn("<style", cleaned.lower())
        self.assertNotIn("window.adminAccountId", cleaned)
        self.assertNotIn("<a ", cleaned.lower())

    def test_make_empty_record_passes_validation(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        record = mod.make_empty_record("https://www.aliexpress.com/item/1005001281271984.html")
        validated, error = mod.validate_product_record(record)
        self.assertIsNone(error, msg=error)
        self.assertIsNotNone(validated)
        self.assertFalse(validated["existence"])
        self.assertIn("unavailable", validated["description"].lower())

    def test_source_from_url(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.source_from_url("https://www.aliexpress.com/item/1.html"), "aliexpress.com")
        self.assertEqual(mod.source_from_url("https://www.aliexpress.us/item/1.html"), "aliexpress.us")
        self.assertEqual(
            mod.product_doc_id("aliexpress.us", "123"),
            "aliexpress.us_123",
        )


if __name__ == "__main__":
    unittest.main()
