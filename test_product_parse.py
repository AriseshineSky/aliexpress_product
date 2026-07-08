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

    def test_is_blocked_url_detects_punish_pages(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(mod.is_blocked_url("https://www.aliexpress.com/_____tmd_____/punish?x=1"))
        self.assertTrue(mod.is_blocked_url("https://example.com/punish/redirect"))
        self.assertFalse(mod.is_blocked_url("https://www.aliexpress.com/item/1005001281271984.html"))

    def test_captcha_text_detects_security_pages(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(mod.is_captcha_text("Please complete the security check"))
        self.assertTrue(mod.is_captcha_text("Unusual traffic from your network"))
        self.assertFalse(mod.is_captcha_text("Pet Grooming Brush for dogs"))

    def test_browser_network_error_page_detection(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(
            mod.is_browser_network_error_page(
                title="This site can't be reached",
                page_text="Check any cables and reboot any routers, modems, or other network devices.",
                page_url="https://www.aliexpress.us/item/123.html",
            )
        )
        self.assertTrue(
            mod.is_browser_network_error_page(
                title="",
                page_text="",
                page_url="chrome-error://chromewebdata/",
            )
        )
        self.assertFalse(
            mod.is_browser_network_error_page(
                title="Pet Grooming Brush for dogs",
                page_text="Add to cart",
                page_url="https://www.aliexpress.us/item/123.html",
            )
        )

    def test_unavailable_product_detects_generic_title(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = "https://www.aliexpress.com/item/1005002295468695.html"
        redirected = "https://www.aliexpress.com/item/1005009999999999.html"
        record = {
            "title": "Aliexpress",
            "price": 0.0,
            "images": mod.PLACEHOLDER_IMAGE,
            "specifications": None,
            "categories": None,
        }
        self.assertTrue(
            mod.is_unavailable_product_page(
                api_data=None,
                record=record,
                dom_data={},
                page_text="",
            )
        )
        self.assertTrue(mod.is_generic_page_title("Aliexpress"))

    def test_redirect_alone_is_not_unavailable(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = "https://www.aliexpress.us/item/1005004506189269.html"
        redirected = "https://www.aliexpress.us/item/3256804319874517.html"
        record = {
            "title": "Pet Grooming Brush",
            "price": 12.99,
            "images": "https://ae01.alicdn.com/kf/example.jpg",
            "specifications": [{"name": "Material", "value": "Plastic"}],
            "categories": "Pet Supplies",
        }
        self.assertFalse(
            mod.is_unavailable_product_page(
                api_data=None,
                record=record,
                dom_data={},
                page_text="",
            )
        )

    def test_build_redirect_info(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = "https://www.aliexpress.us/item/1005004506189269.html"
        final = "https://www.aliexpress.us/item/3256804319874517.html?gatewayAdapt=4itemAdapt"
        info = mod.build_redirect_info(original, final)
        self.assertIsNotNone(info)
        self.assertEqual(info["redirect_product_id"], "3256804319874517")
        self.assertEqual(info["original_source"], "aliexpress.us")
        self.assertEqual(info["final_source"], "aliexpress.us")
        self.assertIn("requested_source=", mod.format_redirect_summary(info))
        record = {
            "date": "2026-07-01T10:00:00",
            "url": final.split("?")[0],
            "source": "aliexpress.us",
            "product_id": "3256804319874517",
            "existence": True,
            "title": "Sample Product",
            "description": "<p>Sample</p>",
            "sku": "3256804319874517",
            "images": mod.PLACEHOLDER_IMAGE,
            "price": 9.99,
            "currency": "USD",
            "shipping_fee": 0,
            "upc": None,
            "brand": None,
            "specifications": None,
            "categories": None,
            "options": None,
            "variants": None,
            "returnable": None,
            "reviews": None,
            "rating": None,
            "sold_count": None,
            "shipping_days_min": None,
            "shipping_days_max": None,
            "weight": None,
            "width": None,
            "height": None,
            "length": None,
            "has_only_default_variant": True,
        }
        record = mod.apply_redirect_metadata(record, info)
        validated, error = mod.validate_product_record(record)
        self.assertIsNone(error, msg=error)
        self.assertTrue(validated["existence"])
        self.assertEqual(validated["product_id"], "3256804319874517")
        self.assertIn("requested_url=", validated["summary"])
        self.assertIn("1005004506189269", validated["summary"])

    def test_make_superseded_record_marks_original_url_unavailable(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = "https://www.aliexpress.us/item/1005004506189269.html"
        final = "https://www.aliexpress.us/item/3256804319874517.html?gatewayAdapt=4itemAdapt"
        info = mod.build_redirect_info(original, final)
        superseded = mod.make_superseded_record(info)
        validated, error = mod.validate_product_record(superseded)
        self.assertIsNone(error, msg=error)
        self.assertFalse(validated["existence"])
        self.assertEqual(validated["product_id"], "1005004506189269")
        self.assertEqual(validated["url"], "https://www.aliexpress.us/item/1005004506189269.html")
        self.assertIn("original_url_no_longer_exists", validated["summary"])
        self.assertIn("3256804319874517", validated["summary"])

        records = mod.finalize_fetch_records({"product_id": "3256804319874517"}, info)
        self.assertEqual(len(records), 2)

    def test_build_redirect_info_for_com_to_us_same_product_id(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = "https://www.aliexpress.com/item/1005004506189269.html"
        final = "https://www.aliexpress.us/item/1005004506189269.html?gatewayAdapt=glo2usa"
        info = mod.build_redirect_info(original, final)
        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "source_redirect")
        self.assertEqual(info["original_source"], "aliexpress.com")
        self.assertEqual(info["final_source"], "aliexpress.us")
        self.assertEqual(info["original_product_id"], "1005004506189269")
        self.assertEqual(info["redirect_product_id"], "1005004506189269")

        superseded = mod.make_superseded_record(info)
        validated, error = mod.validate_product_record(superseded)
        self.assertIsNone(error, msg=error)
        self.assertEqual(validated["_id"], "aliexpress.com_1005004506189269")
        self.assertIn("aliexpress.com", validated["summary"])
        self.assertIn("aliexpress.us", validated["summary"])


if __name__ == "__main__":
    unittest.main()
