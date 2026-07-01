from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


def load_alixq3():
    spec = importlib.util.spec_from_file_location("alixq3", "alixq3.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SkuParseTestCase(unittest.TestCase):
    def test_parse_mtopjsonp_response_body(self):
        mod = load_alixq3()
        body = ' mtopjsonp2({"api":"mtop.aliexpress.pdp.pc.query","data":{"result":{"SKU":{"skuProperties":[]}}},"ret":["SUCCESS::调用成功"],"v":"1.0"})'
        parsed = mod.parse_api_response_body(body)
        self.assertIsNotNone(parsed)
        self.assertIn("data", parsed)

    def test_extract_sale_price_not_original(self):
        mod = load_alixq3()
        info = {
            "originalPrice": {"currency": "USD", "value": 52.65},
            "salePriceString": "$27.48",
            "salePriceLocal": "$27.48|27|48",
        }
        self.assertEqual(mod._extract_sale_price(info), 27.48)
        _, sale = mod._price_amount_from_sku_info(info)
        self.assertEqual(sale["value"], 27.48)

    def test_pick_price_from_api_uses_sale_price(self):
        mod = load_alixq3()
        api_data = {
            "data": {
                "result": {
                    "PRICE": {
                        "targetSkuPriceInfo": {
                            "originalPrice": {"currency": "USD", "value": 9.99},
                            "salePriceString": "$4.99",
                        }
                    }
                }
            }
        }
        self.assertEqual(mod.pick_price_from_api(api_data), 4.99)

    def test_get_sku_price_prefers_activity_price(self):
        mod = load_alixq3()
        sku_val = {
            "availQuantity": 10,
            "skuCalPrice": 9.99,
            "actSkuCalPrice": 4.99,
            "skuAmount": {"value": 9.99},
        }
        self.assertEqual(mod._get_sku_price_from_val(sku_val), 4.99)

    def test_parse_options_and_variants_from_modular_api(self):
        mod = load_alixq3()
        sample_path = Path("/home/sky/src/aliexpress-spider/data/analysis/pdp_result_sku_blocks.json")
        if not sample_path.exists():
            self.skipTest("sample sku block file not available")
        blocks = json.loads(sample_path.read_text(encoding="utf-8"))
        api_result = {
            "SKU": blocks["SKU"],
            "PRICE": blocks["PRICE"],
        }
        options, variants, has_only, min_price, qty = mod.parse_options_and_variants(
            api_result,
            "3256808061849428",
            "USD",
            {},
        )
        self.assertFalse(has_only)
        self.assertIsNotNone(options)
        self.assertGreater(len(options or []), 0)
        self.assertIsNotNone(variants)
        self.assertGreater(len(variants or []), 1)
        self.assertGreater(min_price, 0)
        self.assertGreater(qty or 0, 0)
        validated, error = mod.validate_product_record(
            {
                "date": "2026-07-01T10:00:00",
                "url": "https://www.aliexpress.us/item/3256808061849428.html",
                "source": "aliexpress.us",
                "product_id": "3256808061849428",
                "existence": True,
                "title": "Sample Product",
                "description": "<p>Sample</p>",
                "sku": "3256808061849428",
                "images": mod.PLACEHOLDER_IMAGE,
                "price": min_price,
                "currency": "USD",
                "shipping_fee": 0,
                "options": options,
                "variants": variants,
                "has_only_default_variant": has_only,
                "upc": None,
                "brand": None,
                "specifications": None,
                "categories": None,
                "available_qty": qty,
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
            }
        )
        self.assertIsNone(error, msg=error)
        self.assertFalse(validated["has_only_default_variant"])

    def test_parse_multi_option_sku_attr(self):
        mod = load_alixq3()
        api_result = {
            "SKU": {
                "skuProperties": [
                    {
                        "skuPropertyId": 200000226,
                        "skuPropertyName": "Metal Mass",
                        "skuPropertyValues": [
                            {
                                "propertyValueId": 193,
                                "propertyValueName": "1.6mm thick",
                                "propertyValueDisplayName": "1.6mm thick",
                            },
                            {
                                "propertyValueId": 175,
                                "propertyValueName": "2.6mm thick",
                                "propertyValueDisplayName": "2.6mm thick",
                            },
                        ],
                    },
                    {
                        "skuPropertyId": 200000639,
                        "skuPropertyName": "Length",
                        "skuPropertyValues": [
                            {
                                "propertyValueIdLong": 200661028,
                                "propertyValueName": "45cm",
                                "propertyValueDisplayName": "45cm",
                            },
                            {
                                "propertyValueIdLong": 884,
                                "propertyValueName": "60cm",
                                "propertyValueDisplayName": "60cm",
                            },
                        ],
                    },
                ],
                "skuPaths": [
                    {
                        "skuAttr": "200000226:193;200000639:200661028#45cm",
                        "skuIdStr": "111",
                        "skuStock": 5,
                        "salable": True,
                    },
                    {
                        "skuAttr": "200000226:175;200000639:884#60cm",
                        "skuIdStr": "222",
                        "skuStock": 3,
                        "salable": True,
                    },
                ],
            },
            "PRICE": {
                "skuIdStrPriceInfoMap": {
                    "111": {
                        "originalPrice": {"currency": "USD", "value": 20.0},
                        "salePriceString": "$10.50",
                    },
                    "222": {
                        "originalPrice": {"currency": "USD", "value": 24.0},
                        "salePriceString": "$12.00",
                    },
                }
            },
        }
        options, variants, has_only, min_price, qty = mod.parse_options_and_variants(
            api_result,
            "2251832046791923",
            "USD",
            {},
        )
        self.assertFalse(has_only)
        self.assertEqual(len(options or []), 2)
        self.assertEqual(len(variants or []), 2)
        self.assertEqual(min_price, 10.5)
        self.assertEqual(qty, 8)
        self.assertEqual(len(variants[0]["option_values"]), 2)


if __name__ == "__main__":
    unittest.main()
