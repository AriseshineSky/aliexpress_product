

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import random
import re
import shutil
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright
from pydantic import ValidationError

from em_product.product import StandardProduct
from html_utils import clean_product_description


BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

OUTPUT_DIR = BASE_DIR / "产品详情"
USER_DATA_DIR = BASE_DIR / "browser_playwright"
PROGRESS_FILE = OUTPUT_DIR / "alixq_progress.json"
PRODUCTS_FILE = OUTPUT_DIR / "products.jsonl"
INVALID_FILE = OUTPUT_DIR / "invalid.jsonl"

ES_HOST = os.environ.get("ES_HOST", "34.16.105.219")
ES_PORT = os.environ.get("ES_PORT", "9200")
ES_USER = os.environ.get("ES_USER", "").strip()
ES_PASSWORD = os.environ.get("ES_PASSWORD", "").strip()
URLS_INDEX_NAME = "user1_aliexpress_us_product_urls"
PRODUCT_INDEX_NAME = os.environ.get("PRODUCT_INDEX_NAME", "user1_aliexpress_us_products")
URLS_BATCH_SIZE = 1000

WEBSHARE_USER = os.environ.get("WEBSHARE_USER", "").strip()
WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PASSWORD", "").strip()
WEBSHARE_COUNTRY = os.environ.get("WEBSHARE_COUNTRY", "US").strip().lower()
WEBSHARE_HOST = os.environ.get("WEBSHARE_HOST", "p.webshare.io").strip()
WEBSHARE_PORT = os.environ.get("WEBSHARE_PORT", "80").strip()
WEBSHARE_ROTATE = os.environ.get("WEBSHARE_ROTATE", "1").strip().lower() in ("1", "true", "yes", "on")

HEADLESS = False
CAPTCHA_WAIT_SECONDS = 120
CAPTCHA_MAX_ROUNDS = 30
MAX_CAPTCHA_RESTARTS_PER_URL = 3

# 0 表示不限制，抓完 ES 链接索引里的全部链接。
MAX_PRODUCTS = 0
REQUEST_DELAY_MS = (2000, 4000)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--start-maximized",
]
PROFILE_LOCK_FILES = (
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "lockfile",
)


class BrowserRestartRequired(RuntimeError):
    pass


def cleanup_profile_locks(user_data_dir: Path) -> None:
    for name in PROFILE_LOCK_FILES:
        lock_path = user_data_dir / name
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass
        default_lock = user_data_dir / "Default" / name
        if default_lock.exists():
            try:
                default_lock.unlink()
            except OSError:
                pass


def clear_browser_user_data() -> None:
    cleanup_profile_locks(USER_DATA_DIR)
    if USER_DATA_DIR.exists():
        shutil.rmtree(USER_DATA_DIR, ignore_errors=True)


def build_webshare_username() -> str:
    """Webshare 用户名格式: {base}-{country}-rotate，例如 mczdvqvq-us-rotate。"""
    raw = WEBSHARE_USER.strip()
    base = raw
    had_rotate = base.endswith("-rotate")
    if had_rotate:
        base = base[: -len("-rotate")]
    country_tag = f"-{WEBSHARE_COUNTRY}"
    if WEBSHARE_COUNTRY and base.endswith(country_tag):
        base = base[: -len(country_tag)]
    base = base.rstrip("-")

    parts = [base]
    if WEBSHARE_COUNTRY:
        parts.append(WEBSHARE_COUNTRY)
    if WEBSHARE_ROTATE or had_rotate:
        parts.append("rotate")
    return "-".join(parts)


def build_webshare_proxy() -> dict[str, str] | None:
    """构建 Webshare 代理配置，供 Playwright 浏览器使用。"""
    if not WEBSHARE_USER or not WEBSHARE_PASSWORD:
        return None

    return {
        "server": f"http://{WEBSHARE_HOST}:{WEBSHARE_PORT}",
        "username": build_webshare_username(),
        "password": WEBSHARE_PASSWORD,
    }


def webshare_proxy_label() -> str:
    proxy = build_webshare_proxy()
    if not proxy:
        return "未启用"
    username = proxy["username"]
    return f"Webshare {WEBSHARE_HOST}:{WEBSHARE_PORT} (user={username})"


async def launch_browser_context(playwright, retries: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        clear_browser_user_data()
        try:
            proxy = build_webshare_proxy()
            launch_kwargs: dict[str, Any] = {
                "headless": HEADLESS,
                "args": CHROMIUM_ARGS,
                "ignore_default_args": ["--enable-automation"],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {
                "user_agent": USER_AGENT,
                "locale": "en-US",
                "viewport": None,
                "no_viewport": True,
            }
            if proxy and WEBSHARE_COUNTRY == "us":
                context_kwargs["timezone_id"] = "America/New_York"
            context = await browser.new_context(**context_kwargs)
            await context.add_init_script(STEALTH_SCRIPT)
            page = await context.new_page()
            return browser, context, page
        except Exception as exc:
            last_error = exc
            message = str(exc)
            print(f"浏览器启动失败 ({attempt}/{retries}): {message}")
            if attempt < retries:
                print("3 秒后重试。若仍失败，请关闭所有 Chrome/Edge 窗口后再运行。")
                await asyncio.sleep(3)
    if last_error is not None:
        raise last_error
    raise RuntimeError("浏览器启动失败")


def get_total_link_count() -> int:
    """获取 Elasticsearch 产品链接索引总数。"""
    base_url = f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}"
    count_resp = requests.get(f"{base_url}/_count", auth=(ES_USER, ES_PASSWORD), timeout=30)
    count_resp.raise_for_status()
    return int(count_resp.json().get("count", 0))


def load_link_batch(search_after: list[Any] | None = None) -> tuple[list[str], list[Any] | None]:
    """从 Elasticsearch 产品链接索引读取一批 URL。"""
    base_url = f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}"
    auth = (ES_USER, ES_PASSWORD)

    search_body = {
        "_source": ["url", "product_id"],
        "size": URLS_BATCH_SIZE,
        "query": {"exists": {"field": "url"}},
        "sort": [
            {"product_id": {"order": "asc", "missing": "_last"}},
            {"url": {"order": "asc"}},
        ],
    }
    if search_after is not None:
        search_body["search_after"] = search_after

    resp = requests.post(f"{base_url}/_search", auth=auth, json=search_body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", {}).get("hits", [])
    links: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        source = hit.get("_source") or {}
        link = str(source.get("url") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        links.append(link)

    next_search_after = hits[-1].get("sort") if hits else None
    return links, next_search_after


def upload_product(product: dict[str, Any]) -> bool:
    """上传产品详情到 Elasticsearch 产品索引。"""
    product_id = str(product.get("product_id") or product_id_from_url(str(product.get("url") or ""))).strip()
    if not product_id:
        print("  [上传失败] 缺少 product_id")
        return False

    doc_id = str(
        product.get("_id")
        or product_doc_id(
            str(product.get("source") or source_from_url(str(product.get("url") or ""))),
            product_id,
        )
    )
    body = {k: v for k, v in product.items() if k != "_id"}
    url = f"http://{ES_HOST}:{ES_PORT}/{PRODUCT_INDEX_NAME}/_doc/{doc_id}"
    try:
        resp = requests.put(url, auth=(ES_USER, ES_PASSWORD), json=body, timeout=30)
        if resp.status_code in (200, 201):
            print(f"  [上传成功] 索引={PRODUCT_INDEX_NAME} id={doc_id}")
            return True
        print(f"  [上传失败] id={doc_id} status={resp.status_code} body={resp.text[:300]}")
    except Exception as exc:
        print(f"  [上传失败] id={doc_id} error={exc}")
    return False


def load_progress() -> set[str]:
    if not PROGRESS_FILE.exists():
        return set()
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return set(data.get("processed_urls") or [])


def save_progress(processed_urls: set[str]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        "processed_count": len(processed_urls),
        "processed_urls": sorted(processed_urls),
    }
    PROGRESS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def product_id_from_url(url: str) -> str:
    match = re.search(r"/item/(\d+)\.html", url)
    return match.group(1) if match else ""


def source_from_url(url: str) -> str:
    """按 URL 域名区分 AliExpress 站点：aliexpress.com / aliexpress.us"""
    host = urlparse(str(url or "")).netloc.lower()
    if host.endswith("aliexpress.com"):
        return "aliexpress.com"
    return "aliexpress.us"


def product_doc_id(source: str, product_id: str) -> str:
    return f"{source}_{product_id}"


def normalize_image_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return "https://" + url.lstrip("/")


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return default
    return float(match.group(0))


def to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    text = str(value).replace(",", "")
    match = re.search(r"\d+", text)
    if not match:
        return default
    return int(match.group(0))


PLACEHOLDER_IMAGE = "https://ae01.alicdn.com/kf/S4dae688db3454ee4b1bc54ed018dcbd3l.jpg"
MISSING_PRODUCT_DESCRIPTION = "<p>Product does not exist or is unavailable.</p>"
MISSING_PRODUCT_TITLE_PREFIX = "Unavailable Product"


def clean_html(html: str) -> str:
    return clean_product_description(html)


def normalize_https_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    if url.startswith("https://"):
        return url
    return "https://" + url.lstrip("/")


def normalize_record_for_validation(record: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in record.items() if k != "_id"}
    optional_defaults = {
        "title_en": None,
        "summary": None,
        "upc": None,
        "brand": None,
        "specifications": None,
        "categories": None,
        "videos": None,
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
        "available_qty": None,
        "currency": "USD",
        "has_only_default_variant": True,
    }
    for key, default in optional_defaults.items():
        payload.setdefault(key, default)
    payload["url"] = normalize_https_url(str(payload.get("url") or ""))
    payload["source"] = str(payload.get("source") or source_from_url(payload["url"]))
    payload["product_id"] = str(payload.get("product_id") or "")
    payload["sku"] = str(payload.get("sku") or payload["product_id"])
    payload["title"] = str(payload.get("title") or "").strip() or f"{MISSING_PRODUCT_TITLE_PREFIX} {payload['product_id']}"
    payload["shipping_fee"] = float(payload.get("shipping_fee") or 0)
    payload["price"] = float(payload.get("price") or 0)
    payload["existence"] = bool(payload.get("existence"))

    description = clean_product_description(str(payload.get("description") or ""))
    if not description:
        if payload["existence"]:
            title = payload["title"]
            description = f"<p>{title}</p>" if title else "<p>See product images for details.</p>"
        else:
            description = MISSING_PRODUCT_DESCRIPTION
    payload["description"] = description

    images = str(payload.get("images") or PLACEHOLDER_IMAGE)
    if not any(part.startswith("http") for part in images.split(";") if part.strip()):
        images = PLACEHOLDER_IMAGE
    payload["images"] = images
    return payload


def validate_product_record(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    payload = normalize_record_for_validation(record)
    try:
        validated = StandardProduct(**payload).model_dump()
        for key in ("title_en", "description_en", "created_at", "updated_at"):
            validated.pop(key, None)
        validated["_id"] = product_doc_id(str(validated["source"]), str(validated["product_id"]))
        return validated, None
    except ValidationError as exc:
        return None, str(exc)


def make_empty_record(url: str) -> dict[str, Any]:
    """不存在商品的兜底记录，通过 StandardProduct 校验后写入 ES。"""
    pid = product_id_from_url(url)
    source = source_from_url(url)
    return {
        "date": datetime.now().replace(microsecond=0).isoformat(),
        "url": normalize_https_url(url),
        "source": source,
        "product_id": pid,
        "existence": False,
        "title": f"{MISSING_PRODUCT_TITLE_PREFIX} {pid}",
        "title_en": None,
        "description": MISSING_PRODUCT_DESCRIPTION,
        "summary": None,
        "sku": pid,
        "upc": None,
        "brand": None,
        "specifications": None,
        "categories": None,
        "images": PLACEHOLDER_IMAGE,
        "videos": None,
        "price": 0.0,
        "currency": "USD",
        "available_qty": None,
        "options": None,
        "variants": None,
        "returnable": None,
        "reviews": None,
        "rating": None,
        "sold_count": None,
        "shipping_fee": 0,
        "shipping_days_min": None,
        "shipping_days_max": None,
        "weight": None,
        "width": None,
        "height": None,
        "length": None,
        "has_only_default_variant": True,
        "_id": product_doc_id(source, pid),
    }
    validated, _ = validate_product_record(record)
    return validated if validated else record


def _get_api_result(api_data: dict[str, Any] | None) -> dict[str, Any]:
    """从 API 数据中提取 result 字典"""
    if not api_data:
        return {}
    data = api_data.get("data", {})
    if isinstance(data, dict):
        return data.get("result", {}) or data
    return {}


def pick_price_from_api(api_data: dict[str, Any]) -> float:
    """从 MTOP API 返回数据中提取价格（新结构: data.result.PRICE）"""
    result = _get_api_result(api_data)
    price_data = result.get("PRICE") or result.get("priceModule") or {}
    target = price_data.get("targetSkuPriceInfo") or {}
    if target:
        if "salePriceLocal" in target:
            # format: "2,980円|2980|"
            match = re.search(r"\|(\d+)\|", str(target["salePriceLocal"]))
            if match:
                p = to_float(match.group(1))
                if p > 0:
                    return p
        sale = str(target.get("salePriceString") or "")
        p = to_float(sale)
        if p > 0:
            return p
    # fallback: try old structure
    price_module = result.get("priceModule") or {}
    price_component = result.get("priceComponent") or {}
    candidates = [
        price_component.get("maxActivityAmount", {}).get("value"),
        price_component.get("minActivityAmount", {}).get("value"),
        price_component.get("maxAmount", {}).get("value"),
        price_component.get("minAmount", {}).get("value"),
        (price_module.get("discountPrice") or {}).get("maxActivityAmount", {}).get("value"),
        (price_module.get("discountPrice") or {}).get("minActivityAmount", {}).get("value"),
        (price_module.get("origPrice") or {}).get("maxAmount", {}).get("value"),
        (price_module.get("origPrice") or {}).get("minAmount", {}).get("value"),
        price_module.get("formatedActivityPrice"),
        price_module.get("formatedPrice"),
    ]
    for candidate in candidates:
        p = to_float(candidate)
        if p > 0:
            return p
    return 0.0


def parse_specs_from_api(api_data: dict[str, Any]) -> list[dict[str, str]]:
    """从 API 数据提取规格参数（新结构: data.result.PRODUCT_PROP_PC）"""
    result = _get_api_result(api_data)
    specs = []
    # 新结构
    prop_pc = result.get("PRODUCT_PROP_PC") or {}
    props = prop_pc.get("showedProps") or prop_pc.get("props") or []
    # 旧结构兼容
    if not props:
        props = (result.get("specsModule") or result.get("productPropComponent") or {}).get("props", [])
    for prop in props:
        name = str(prop.get("attrName") or "").strip()
        value = str(prop.get("attrValue") or "").strip()
        if name and value:
            specs.append({"name": name, "value": value})
    return specs


def parse_breadcrumbs_from_api(api_data: dict[str, Any]) -> str:
    """从 API 数据提取面包屑类目（新结构: data.result.GLOBAL_DATA.breadcrumb）"""
    result = _get_api_result(api_data)
    global_data = result.get("GLOBAL_DATA") or {}
    breadcrumb = global_data.get("breadcrumb") or {}
    path_list = (
        breadcrumb.get("pathList")
        or result.get("breadcrumbComponent", {}).get("pathList")
        or result.get("crossLinkModule", {}).get("breadCrumbPathList")
        or []
    )
    names = []
    for item in path_list:
        name = str(item.get("name") or "").strip()
        if name and name.lower() not in {"home", "all categories", "ホーム"}:
            names.append(name)
    return " > ".join(names) if names else ""


def parse_images_from_api(api_data: dict[str, Any]) -> str:
    """从 API 数据提取图片列表（新结构: data.result.HEADER_IMAGE_PC）"""
    result = _get_api_result(api_data)
    header_img = result.get("HEADER_IMAGE_PC") or result.get("imageComponent") or result.get("imageModule") or {}
    image_list = header_img.get("imagePathList", [])
    images = [normalize_image_url(img) for img in image_list if img]
    return ";".join(images) if images else PLACEHOLDER_IMAGE


def get_currency_from_api(api_data: dict[str, Any]) -> str:
    """从 API 数据推断货币（新结构: data.result.GLOBAL_DATA.currencyCode / PRICE）"""
    result = _get_api_result(api_data)
    # 新结构: GLOBAL_DATA.currencyCode
    global_data = result.get("GLOBAL_DATA") or {}
    currency = global_data.get("currencyCode") or ""
    if currency:
        return str(currency).upper()
    # 新结构: PRICE.targetSkuPriceInfo.originalPrice.currency
    price_data = result.get("PRICE") or {}
    target = price_data.get("targetSkuPriceInfo") or {}
    orig = target.get("originalPrice") or {}
    currency = orig.get("currency") or ""
    if currency:
        return str(currency).upper()
    # 旧结构兼容
    price_module = result.get("priceModule") or {}
    currency = price_module.get("currency") or price_module.get("currencyCode") or ""
    if not currency:
        price_component = result.get("priceComponent") or {}
        currency = (
            price_component.get("maxActivityAmount", {}).get("currency")
            or price_component.get("minAmount", {}).get("currency")
            or ""
        )
    return str(currency).upper() if currency else "USD"


def build_standard_record(api_data: dict[str, Any] | None, ld_json_list: list[dict[str, Any]],
                          dom_data: dict[str, Any], url: str) -> dict[str, Any]:
    """合并数据映射为 30 字段 Schema。优先级: LD+JSON > DOM > API"""
    pid = product_id_from_url(url)
    source = source_from_url(url)
    api_result = _get_api_result(api_data) if api_data else {}

    # ---- 从 LD+JSON 提取结构化数据 ----
    ld_product: dict[str, Any] = {}
    ld_breadcrumbs: list[dict[str, Any]] = []
    ld_videos: list[str] = []
    for ld in ld_json_list:
        t = ld.get("@type", "")
        if t == "Product":
            ld_product = ld
        elif t == "BreadcrumbList":
            ld_breadcrumbs = ld.get("itemListElement", [])

    # ---- title: LD+JSON > DOM > API ----
    title = ld_product.get("name", "") or dom_data.get("title") or ""
    if not title and api_result:
        product_title = api_result.get("PRODUCT_TITLE") or {}
        title = product_title.get("text") or product_title.get("title") or ""
        if not title:
            title = dom_data.get("title") or ""
    if not title:
        title = "ERROR"

    # ---- price: LD+JSON > API > DOM ----
    price = 0.0
    if ld_product.get("offers"):
        price = to_float(ld_product["offers"].get("price", 0))
    if price <= 0 and api_result:
        price = pick_price_from_api(api_data) if api_data else 0.0
    if price <= 0:
        price = to_float(dom_data.get("priceText", ""))

    # ---- currency: LD+JSON > API ----
    currency = "USD"
    if ld_product.get("offers"):
        currency = (ld_product["offers"].get("priceCurrency") or "USD").upper()
    if currency == "USD" and api_result:
        currency = get_currency_from_api(api_data) if api_data else "USD"

    # ---- images: LD+JSON > DOM > API ----
    images = PLACEHOLDER_IMAGE
    if ld_product.get("image"):
        ld_imgs = ld_product["image"] if isinstance(ld_product["image"], list) else [ld_product["image"]]
        imgs = [normalize_image_url(img) for img in ld_imgs if img]
        if imgs:
            images = ";".join(imgs)
    if images == PLACEHOLDER_IMAGE and dom_data.get("images"):
        dom_imgs = [normalize_image_url(img) for img in dom_data["images"]]
        if dom_imgs:
            images = ";".join(dom_imgs)
    if images == PLACEHOLDER_IMAGE and api_result:
        api_imgs = parse_images_from_api(api_data) if api_data else PLACEHOLDER_IMAGE
        if api_imgs != PLACEHOLDER_IMAGE:
            images = api_imgs

    # ---- rating: LD+JSON > API > DOM ----
    rating = None
    if ld_product.get("aggregateRating"):
        rating = to_float(ld_product["aggregateRating"].get("ratingValue"))
    if rating is None and api_result:
        pc_rating = api_result.get("PC_RATING") or {}
        rating_val = pc_rating.get("rating") or pc_rating.get("evarageStar")
        if rating_val:
            rating = to_float(rating_val)
        if rating is None:
            title_module_data = api_result.get("titleModule", {}).get("feedbackRating") or {}
            rating = to_float(title_module_data.get("averageStar"))
    if rating is None:
        rating = to_float(dom_data.get("rating"))

    # ---- reviews: LD+JSON > API ----
    reviews = None
    if ld_product.get("aggregateRating"):
        reviews = to_int(ld_product["aggregateRating"].get("reviewCount"))
    if reviews is None and api_result:
        pc_rating = api_result.get("PC_RATING") or {}
        reviews = to_int(pc_rating.get("totalValidNum"))
    if reviews is None:
        reviews = to_int(dom_data.get("reviews"))

    # ---- sold_count: API > DOM ----
    sold_count = None
    if api_result:
        pc_rating = api_result.get("PC_RATING") or {}
        other_text = pc_rating.get("otherText") or ""
        if other_text:
            sold_count = to_int(other_text)
        if sold_count is None:
            trade = api_result.get("tradeComponent") or {}
            sold_count = to_int(trade.get("formatTradeCount"))
        if sold_count is None:
            sold_count = to_int(api_result.get("titleModule", {}).get("formatTradeCount"))
    if sold_count is None:
        sold_count = to_int(dom_data.get("soldCount"))

    # ---- specifications: API > DOM ----
    specifications_list = parse_specs_from_api(api_data) if api_data else []
    if not specifications_list:
        specifications_list = dom_data.get("specifications") or []

    # ---- categories: LD+JSON > DOM > API ----
    categories = ""
    if ld_breadcrumbs:
        names = [item.get("name", "") for item in ld_breadcrumbs
                 if item.get("name") and item.get("name").lower() not in {"home", "ホーム"}]
        categories = " > ".join(names)
    if not categories:
        dom_cats = dom_data.get("categories") or []
        if dom_cats:
            categories = " > ".join(dom_cats)
    if not categories and api_result:
        api_cats = parse_breadcrumbs_from_api(api_data) if api_data else ""
        if api_cats:
            categories = api_cats

    # ---- videos: LD+JSON ----
    videos = None
    for ld in ld_json_list:
        if ld.get("@type") == "VideoObject" and ld.get("contentUrl"):
            videos = ld["contentUrl"]
            break

    # ---- description: DOM > API ----
    description = clean_product_description(str(dom_data.get("description") or ""))

    return {
        "date": datetime.now().replace(microsecond=0).isoformat(),
        "url": normalize_https_url(url),
        "source": source,
        "product_id": pid,
        "existence": bool(title and title != "ERROR"),
        "title": title,
        "title_en": None,
        "description": description,
        "summary": None,
        "sku": pid,
        "upc": None,
        "brand": None,
        "specifications": specifications_list if specifications_list else None,
        "categories": categories if categories else None,
        "images": images,
        "videos": videos,
        "price": price,
        "currency": currency,
        "available_qty": None,
        "options": None,
        "variants": None,
        "returnable": None,
        "reviews": reviews,
        "rating": rating,
        "sold_count": sold_count,
        "shipping_fee": 0,
        "shipping_days_min": None,
        "shipping_days_max": None,
        "weight": None,
        "width": None,
        "height": None,
        "length": None,
        "has_only_default_variant": True,
        "_id": product_doc_id(source, pid),
    }


def is_blocked_url(url: str) -> bool:
    lowered = url.lower()
    return "punish" in lowered or "tmd" in lowered


async def navigate_product_page(page: Page, full_url: str, captcha_state: dict[str, bool]) -> None:
    """打开商品页并在风控页时重试导航，直到进入正常商品页或达到重试上限。"""
    for attempt in range(1, 4):
        await page.goto(full_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        await handle_captcha(page, captcha_state)
        await sleep(short=True)
        if not is_blocked_url(page.url):
            return
        print(f"  [导航] 仍在风控页，重试 {attempt}/3: {page.url[:120]}")
    if is_blocked_url(page.url):
        raise BrowserRestartRequired("验证码通过后仍停留在风控页")


def parse_api_response_body(body: str) -> dict[str, Any] | None:
    match = re.search(r"/\*\*/\w+\((.*)\)\s*$", body, re.S)
    if not match:
        return None
    parsed = json.loads(match.group(1))
    return parsed if isinstance(parsed, dict) else None


def is_captcha_text(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "captcha",
        "verify you are human",
        "security check",
        "slide to verify",
        "unusual traffic",
    ]
    return any(marker in lowered for marker in markers)

async def sleep(short: bool = False) -> None:
    low, high = REQUEST_DELAY_MS
    if short:
        low, high = max(300, low // 2), max(600, high // 2)
    await asyncio.sleep(random.randint(low, high) / 1000)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "grok-4.3")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.x.ai/v1/chat/completions")
CAPTCHA_IMAGE_DIR = BASE_DIR / "img"

OPENAI_PROMPT = """这是一张包含多个小图的验证码图片（通常是3x3的九宫格或4x4的十六宫格）。
请从左到右、从上到下为每个小图编号（从1开始）。
请仔细观察，找出所有包含“{keyword}”的图片（只要包含该物体的一部分，就算符合）。

【严格输出格式】
1. 简要说明哪些序号包含该物体。
2. 必须在回答的最后一行，单独输出一个纯数字数组，例如：[2, 4, 9]。
3. 如果全都没有，请输出：[]。
请务必确保最后一行只有中括号和数字！"""

def draw_grid_on_image(image_bytes: bytes, grid_size: int) -> bytes:
    image = Image.open(io.BytesIO(image_bytes))
    draw = ImageDraw.Draw(image)
    width, height = image.size
    step_x = width / grid_size
    step_y = height / grid_size
    
    for i in range(1, grid_size):
        draw.line([(i * step_x, 0), (i * step_x, height)], fill="red", width=3)
        draw.line([(0, i * step_y), (width, i * step_y)], fill="red", width=3)
        
    try:
        font = ImageFont.truetype("arial.ttf", int(step_y / 4))
    except Exception:
        font = ImageFont.load_default()
        
    for row in range(grid_size):
        for col in range(grid_size):
            num = row * grid_size + col + 1
            x = col * step_x + 10
            y = row * step_y + 10
            draw.text((x-2, y-2), str(num), font=font, fill="black")
            draw.text((x+2, y-2), str(num), font=font, fill="black")
            draw.text((x-2, y+2), str(num), font=font, fill="black")
            draw.text((x+2, y+2), str(num), font=font, fill="black")
            draw.text((x, y), str(num), font=font, fill="yellow")
            
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


async def screenshot_current_captcha_grid(images_frame) -> tuple[bytes, int]:
    """截取当前验证码网格，包含每轮点击后动态刷新的新图片。"""
    target = images_frame.locator(".rc-imageselect-target").first
    await target.wait_for(state="visible", timeout=5000)
    await images_frame.wait_for_function(
        """
        () => {
          const target = document.querySelector('.rc-imageselect-target');
          if (!target) return false;
          const imgs = Array.from(target.querySelectorAll('img'));
          return imgs.length > 0 && imgs.every((img) => img.complete && img.naturalWidth > 0);
        }
        """,
        timeout=5000,
    )
    await asyncio.sleep(0.8)
    grid_size = await images_frame.evaluate(
        """
        () => {
          const table = document.querySelector('.rc-imageselect-target table');
          const className = table ? table.className : '';
          if (String(className).includes('44')) return 4;
          if (String(className).includes('33')) return 3;
          const rows = document.querySelectorAll('.rc-imageselect-target tr').length;
          return rows === 4 ? 4 : 3;
        }
        """
    )
    image_bytes = await target.screenshot(type="jpeg", quality=90)
    return draw_grid_on_image(image_bytes, int(grid_size or 3)), int(grid_size or 3)


async def click_captcha_images_from_model(images_frame, model_text: str):
    print(f"[验证码识别] 解析模型返回...")
    list_matches = re.findall(r'\[([0-9\s,]+)\]', model_text)
    if list_matches:
        matches = re.findall(r'\d+', list_matches[-1])
    else:
        lines = [line.strip() for line in model_text.strip().split('\n') if line.strip()]
        last_sentences = " ".join(lines[-3:])
        matches = re.findall(r'第\s*(\d+)\s*[张个幅图]', last_sentences)
        if not matches:
            if "无" in last_sentences or "没有" in last_sentences or "[]" in last_sentences:
                matches = []
            else:
                cleaned_text = re.sub(r'\d+\s*[到-]\s*\d+', '', last_sentences)
                cleaned_text = re.sub(r'9宫格|九宫格|16宫格|十六宫格', '', cleaned_text)
                matches = re.findall(r'\d+', cleaned_text)

    valid_numbers = sorted(list(set(int(n) for n in matches if 1 <= int(n) <= 16)))
    if not valid_numbers:
        print("[验证码识别] 未找到匹配图片序号，直接点击验证...")
    else:
        print(f"[验证码识别] 点击序号: {valid_numbers}")
        for num in valid_numbers:
            index = num - 1
            selector = f"#\\\\3{str(index)[0]} {str(index)[1:]}" if len(str(index)) > 1 else f"#\\\\3{index} "
            try:
                clicked = await images_frame.evaluate(f"""() => {{
                    const tile = document.querySelector('{selector}');
                    if (!tile) return false;
                    tile.click();
                    return true;
                }}""")
                if not clicked:
                    tile_locator = images_frame.locator(selector).first
                    if await tile_locator.count() > 0:
                        await tile_locator.click()
            except Exception as e:
                print(f"[验证码识别] 点击序号 {num} 失败: {e}")
            await asyncio.sleep(random.uniform(0.3, 0.7))
            
    verify_selector = "#recaptcha-verify-button"
    try:
        clicked_verify = await images_frame.evaluate(f"""() => {{
            const btn = document.querySelector('{verify_selector}');
            if (!btn) return false;
            btn.click();
            return true;
        }}""")
        if not clicked_verify:
            verify_locator = images_frame.locator(verify_selector).first
            if await verify_locator.count() > 0:
                await verify_locator.click()
    except Exception as e:
        print(f"[验证码识别] 点击验证按钮失败: {e}")

async def wait_for_frame_with_locator(page: Page, selector: str, timeout_sec: int = 10):
    for _ in range(timeout_sec * 2):
        for f in page.frames:
            try:
                if await f.locator(selector).count() > 0:
                    return f
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return None

async def solve_captcha(page: Page) -> bool:
    # 尝试寻找验证码 iframe
    checkbox_frame = None
    for _ in range(10):
        for f in page.frames:
            try:
                if await f.locator("#recaptcha-anchor > div.recaptcha-checkbox-border").count() > 0:
                    checkbox_frame = f
                    break
            except Exception:
                pass
        if checkbox_frame:
            break
        await asyncio.sleep(0.5)

    if checkbox_frame:
        try:
            print("[验证码识别] 点击我不是机器人...")
            await checkbox_frame.locator("#recaptcha-anchor > div.recaptcha-checkbox-border").first.click(timeout=5000)
        except Exception as e:
            print(f"[验证码识别] 点击 checkbox 失败: {e}")

        retry_count = 0
        while retry_count < CAPTCHA_MAX_ROUNDS:
            retry_count += 1
            print(f"[验证码识别] 等待图片弹窗加载... ({retry_count}/{CAPTCHA_MAX_ROUNDS})")
            await asyncio.sleep(2)
            images_frame = await wait_for_frame_with_locator(page, "#recaptcha-verify-button", timeout_sec=10)

            if images_frame:
                question_keyword = ""
                try:
                    strong_locator = images_frame.locator(".rc-imageselect-desc-wrapper strong").first
                    if await strong_locator.count() > 0:
                        question_keyword = (await strong_locator.inner_text()).strip()
                except Exception:
                    pass

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                CAPTCHA_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

                try:
                    # 每轮直接截当前验证码网格，避免点选后局部刷新仍沿用旧图片。
                    image_bytes, grid_size = await screenshot_current_captcha_grid(images_frame)
                    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
                    data_url = f"data:image/jpeg;base64,{image_base64}"
                    image_path = CAPTCHA_IMAGE_DIR / f"captcha_src_{timestamp}_round{retry_count}.jpg"
                    with open(image_path, "wb") as f:
                        f.write(image_bytes)

                    headers = {
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    }
                    final_prompt = OPENAI_PROMPT.replace("{keyword}", question_keyword) if question_keyword else OPENAI_PROMPT.replace("（{keyword}）", "")
                    payload = {
                        "model": OPENAI_MODEL,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": final_prompt},
                                    {"type": "image_url", "image_url": {"url": data_url}}
                                ]
                            }
                        ],
                        "max_tokens": 1000
                    }

                    def call_model():
                        return requests.post(OPENAI_API_BASE, headers=headers, json=payload, timeout=60)

                    print(f"[验证码识别] 请求模型解答: {question_keyword} ...")
                    resp = await asyncio.to_thread(call_model)
                    if resp.ok:
                        model_reply = resp.json()['choices'][0]['message']['content']
                        reply_last_line = model_reply.strip().split('\n')[-1]
                        print(f"[验证码识别] 模型回复: {reply_last_line}")
                        await click_captcha_images_from_model(images_frame, model_reply)
                        await asyncio.sleep(3)

                        needs_retry = False
                        for f in page.frames:
                            if await f.locator("#recaptcha-verify-button").count() > 0:
                                needs_retry = True
                                break

                        if not needs_retry:
                            print("[验证码识别] 验证码弹窗消失，验证可能已通过！")
                            return True
                    else:
                        print(f"[验证码识别] 模型调用失败: {resp.text}")
                        return False

                except Exception as e:
                    print(f"[验证码识别] 截图/交互失败: {e}")
                    return False
            else:
                print("[验证码识别] 没有看到验证码图片弹窗，可能验证已通过或遇到不同形式验证码。")
                return True

    return True


async def handle_captcha(page: Page, captcha_state: dict[str, bool]) -> bool:
    title = await page.title()
    html = await page.content()
    
    if not is_captcha_text(title + "\\n" + html):
        # 兜底判断是否有 recaptcha checkbox
        has_checkbox = False
        for f in page.frames:
            if await f.locator("#recaptcha-anchor > div.recaptcha-checkbox-border").count() > 0:
                has_checkbox = True
                break
        if not has_checkbox:
            return False

    print(f"\\n检测到验证/风控页面：{page.url}")
    if captcha_state.get("solved_once"):
        raise BrowserRestartRequired("爬取过程中再次检测到验证码，清空浏览器状态并重启")

    print("[验证码识别] 本轮浏览器第一次遇到验证码，先尝试自动解决。")
    if await solve_captcha(page):
        captcha_state["solved_once"] = True
        return True
    raise BrowserRestartRequired("验证码自动解决失败，清空浏览器状态并重启")

async def extract_ld_json(page: Page) -> list[dict[str, Any]]:
    """从页面提取所有 LD+JSON 结构化数据，自动展平嵌套数组"""
    raw = await page.evaluate(
        """() => {
          const results = [];
          document.querySelectorAll('script[type="application/ld+json"]').forEach(el => {
            try {
              const parsed = JSON.parse(el.textContent);
              if (Array.isArray(parsed)) {
                results.push(...parsed);
              } else if (parsed && typeof parsed === 'object') {
                results.push(parsed);
              }
            } catch(e) {}
          });
          return results;
        }"""
    )
    return raw if isinstance(raw, list) else []


async def extract_dom_data(page: Page) -> dict[str, Any]:
    """从渲染后的 HTML 页面提取数据（拆成多个短 evaluate，避免长脚本失败）。"""
    basic = await page.evaluate(
        """() => {
          const bodyText = document.body.innerText || '';
          const title =
            (document.querySelector('h1') || {}).innerText?.trim() ||
            (document.querySelector('[data-pl="product-title"]') || {}).innerText?.trim() ||
            document.title ||
            '';
          const priceEl = document.querySelector('[class*="current--"], [class*="current--"] span');
          const priceText = priceEl ? priceEl.innerText.trim() : '';
          const breadItems = [];
          document.querySelectorAll(
            '[class*="breadcrumb"] a, [class*="breadCrumb"] a, nav[aria-label="breadcrumb"] a'
          ).forEach(a => {
            const t = a.innerText.trim();
            if (t && !/^(ホーム|Home|All Categories)$/i.test(t)) breadItems.push(t);
          });
          let rating = null;
          let reviews = null;
          let soldCount = null;
          const ratingMatch = bodyText.match(/(\\d\\.\\d)\\s*(?:\\n|\\s)*(?:reviews?|レビュー)/i);
          if (ratingMatch) rating = ratingMatch[1];
          const reviewsMatch = bodyText.match(/([\\d,]+)\\s*(?:reviews?|レビュー)/i);
          if (reviewsMatch) reviews = reviewsMatch[1].replace(/,/g, '');
          const soldMatch = bodyText.match(/([\\d,]+\\+?)\\s*\\S*\\s*(?:販売|sold|vendidos)/i);
          if (soldMatch) soldCount = soldMatch[1].replace(/,/g, '');
          return { title, priceText, categories: breadItems, rating, reviews, soldCount };
        }"""
    )
    images = await page.evaluate(
        """() => {
          const imageSrcs = [];
          const seenImg = new Set();
          document.querySelectorAll(
            '.images-view-item img, [class*="image-view"] img, [class*="gallery"] img, [class*="main-image"] img'
          ).forEach(img => {
            if (img.naturalWidth < 200) return;
            let src = img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
            if (!src) return;
            src = src.replace(/_(?:80x80|120x120|220x220|960x960)q75\\.jpg.*$/i, '');
            const clean = src.replace(/_\\.avif$/i, '').replace(/\\?.*$/, '');
            if (clean && !clean.includes('icon') && !clean.includes('logo') && !clean.includes('banner')) {
              if (!seenImg.has(clean)) {
                seenImg.add(clean);
                imageSrcs.push(clean);
              }
            }
          });
          if (imageSrcs.length === 0) {
            document.querySelectorAll('img[src*="aliexpress"], img[src*="alicdn"]').forEach(img => {
              let src = img.src || '';
              if (!src) return;
              src = src.replace(/_(?:80x80|120x120|220x220|960x960)q75\\.jpg.*$/i, '').replace(/_\\.avif$/i, '');
              if (src && !src.includes('icon') && !src.includes('logo') && !src.includes('banner')) {
                if (!seenImg.has(src)) {
                  seenImg.add(src);
                  imageSrcs.push(src);
                }
              }
            });
          }
          return imageSrcs;
        }"""
    )
    specifications = await page.evaluate(
        """() => {
          const specifications = [];
          document.querySelectorAll('[class*="specification--prop"]').forEach((row) => {
            const titleEl = row.querySelector('[class*="specification--title"]');
            const descEl = row.querySelector('[class*="specification--desc"]');
            if (titleEl && descEl) {
              const name = (titleEl.innerText || '').trim();
              const value = (descEl.innerText || '').trim();
              if (name && value) specifications.push({ name, value });
            }
          });
          if (specifications.length === 0) {
            document.querySelectorAll('[class*="product-property"] li, [data-pl="product-specs"] li').forEach((item) => {
              const itemText = (item.innerText || '').trim();
              if (!itemText) return;
              const parts = itemText.split(/\\n|:/).map(p => p.trim()).filter(Boolean);
              if (parts.length >= 2) specifications.push({ name: parts[0], value: parts.slice(1).join(' ') });
            });
          }
          return specifications;
        }"""
    )
    return {
        **basic,
        "images": images if isinstance(images, list) else [],
        "specifications": specifications if isinstance(specifications, list) else [],
        "description": "",
    }


async def extract_product_description(page: Page) -> str:
    """提取 #product-description shadow-root 内的 HTML，并清理 script/style/空白。"""
    try:
        await page.evaluate("window.scrollBy(0, 900)")
        await page.wait_for_timeout(800)
        await page.wait_for_selector(
            "#nav-specification, #nav-description, #product-description, [data-pl='product-description']",
            timeout=10000,
        )
    except Exception:
        pass

    try:
        raw = await page.evaluate(
            """() => {
              const descriptionSection = document.querySelector('#nav-description');
              if (descriptionSection) {
                descriptionSection.scrollIntoView({ block: 'center' });
              }

              const descriptionHosts = [
                document.querySelector('#nav-description #product-description'),
                document.querySelector('#nav-description [class*="product-description"]'),
                document.querySelector('#product-description'),
                document.querySelector('[data-pl="product-description"]'),
              ].filter(Boolean);

              const findDescriptionHtml = (root) => {
                if (!root) return '';
                if (root.shadowRoot) {
                  const inner = root.shadowRoot.querySelector('.product-description, #product-description');
                  if (inner?.innerHTML?.trim()) return inner.innerHTML;
                }
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                let node;
                while ((node = walker.nextNode())) {
                  if (!node.shadowRoot) continue;
                  const inner = node.shadowRoot.querySelector('.product-description, #product-description');
                  if (inner?.innerHTML?.trim()) return inner.innerHTML;
                }
                const direct = root.querySelector('.product-description, [class*="product-description"]');
                if (direct?.innerHTML?.trim()) return direct.innerHTML;
                if (root.innerHTML?.trim() && root.innerHTML.length > 40) return root.innerHTML;
                return '';
              };

              for (const host of descriptionHosts) {
                const html = findDescriptionHtml(host);
                if (html) return html;
              }
              return '';
            }"""
        )
        return clean_product_description(str(raw or ""))
    except Exception:
        return ""


async def fetch_product(page: Page, url: str, captcha_state: dict[str, bool]) -> dict[str, Any] | None:
    """打开产品页，拦截 MTOP API 获取数据，映射为标准 30 字段 Schema"""
    full_url = url if "gatewayAdapt" in url else f"{url}?gatewayAdapt=glo2usa"
    api_data: dict[str, Any] | None = None

    try:
        async with page.expect_response(
            lambda response: "mtop.aliexpress.pdp.pc.query" in response.url,
            timeout=90000,
        ) as response_info:
            await page.goto(full_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            await handle_captcha(page, captcha_state)
            await sleep(short=True)
            try:
                response = await response_info.value
                api_data = parse_api_response_body(await response.text())
                if api_data:
                    print("  [API] 拦截到 mtop.aliexpress.pdp.pc.query 响应")
            except PlaywrightTimeoutError:
                print("  [API] 未在 90s 内拦截到 pdp 接口，降级使用 LD+JSON")
    except PlaywrightTimeoutError:
        await page.goto(full_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        await handle_captcha(page, captcha_state)
        await sleep(short=True)
        print("  [API] 未在 90s 内拦截到 pdp 接口，降级使用 LD+JSON")

    # ---- 第四步：提取 LD+JSON + DOM 数据 ----
    ld_json_list = await extract_ld_json(page)
    dom_data = await extract_dom_data(page)
    description = await extract_product_description(page)
    if description:
        dom_data["description"] = description

    # ---- 第五步：如果有 API 数据，尝试获取远程描述 ----
    if api_data:
        api_result = _get_api_result(api_data)
        desc_component = api_result.get("DESC") or api_result.get("descriptionComponent") or {}
        desc_url = str(desc_component.get("pcDescUrl") or "")
        if desc_url and not dom_data.get("description"):
            try:
                resp = await page.request.get(desc_url)
                if resp.ok:
                    dom_data["description"] = clean_product_description(await resp.text())
            except Exception:
                pass

    # ---- 第六步：构建标准记录 ----
    record = build_standard_record(api_data, ld_json_list, dom_data, url)

    # 检查是否 404 页面
    if api_data:
        api_result = _get_api_result(api_data)
        i18n = api_result.get("GLOBAL_DATA", {}).get("i18n", {}) or {}
        if i18n.get("ItemDetailResp", {}).get("PAGE_NOT_FOUND_NOTICE"):
            print(f"  页面不存在：{url}")
            return make_empty_record(url)

    if not record.get("title") or record["title"] == "ERROR":
        print(f"  未提取到有效标题：{url}")
        return make_empty_record(url)

    validated, error = validate_product_record(record)
    if not validated:
        print(f"  格式校验失败：{error}")
        return make_empty_record(url)

    return validated


async def main_async() -> None:
    print("=" * 60)
    print("AliExpress 商品详情抓取")
    print("=" * 60)
    print(f"链接来源: http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}")
    print(f"上传索引: {PRODUCT_INDEX_NAME}")
    print(f"详情输出目录: {OUTPUT_DIR}")
    print("浏览器模式: 无痕模式，不保存用户目录")
    print(f"浏览器代理: {webshare_proxy_label()}")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clear_browser_user_data()
    processed_urls = load_progress()
    products_path = PRODUCTS_FILE
    invalid_path = INVALID_FILE
    total_links = get_total_link_count()
    print(f"[ES链接] 索引 {URLS_INDEX_NAME} 总数: {total_links}")

    success = 0
    failed = 0
    skipped = 0
    batch_no = 0
    fetched_count = 0
    search_after: list[Any] | None = None
    captcha_restart_counts: dict[str, int] = {}

    while True:
        if MAX_PRODUCTS > 0 and success >= MAX_PRODUCTS:
            break
        links, search_after = load_link_batch(search_after)
        if not links:
            if batch_no == 0:
                print("ES 链接索引中没有找到商品链接。")
            break
        batch_no += 1
        batch_start = fetched_count
        fetched_count += len(links)
        current_index = 0
        print(f"[ES链接] 第 {batch_no} 批读取 {len(links)} 条，累计读取 {fetched_count}/{total_links}")

        batch_finished = False
        async with async_playwright() as playwright:
            while current_index < len(links):
                if MAX_PRODUCTS > 0 and success >= MAX_PRODUCTS:
                    batch_finished = True
                    break

                browser = None
                context = None
                try:
                    browser, context, page = await launch_browser_context(playwright)
                    print("浏览器已打开（无痕模式）。")
                    captcha_state = {"solved_once": False}

                    with products_path.open("a", encoding="utf-8") as products_fh, invalid_path.open(
                        "a", encoding="utf-8"
                    ) as invalid_fh:
                        while current_index < len(links):
                            if MAX_PRODUCTS > 0 and success >= MAX_PRODUCTS:
                                batch_finished = True
                                break

                            url = links[current_index]
                            index = batch_start + current_index + 1
                            if url in processed_urls:
                                skipped += 1
                                current_index += 1
                                continue

                            print(f"[{index}/{total_links}] 第 {batch_no} 批 {current_index + 1}/{len(links)} 抓取详情: {url}")
                            try:
                                product = await fetch_product(page, url, captcha_state)
                                if not product:
                                    product = make_empty_record(url)

                                validated, validation_error = validate_product_record(product)
                                if not validated:
                                    invalid_fh.write(
                                        json.dumps(
                                            {
                                                "url": url,
                                                "product_id": product_id_from_url(url),
                                                "error": validation_error or "StandardProduct 校验失败",
                                                "date": datetime.now().replace(microsecond=0).isoformat(),
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "\n"
                                    )
                                    invalid_fh.flush()
                                    failed += 1
                                    processed_urls.add(url)
                                    save_progress(processed_urls)
                                    current_index += 1
                                    print(f"  失败: {validation_error}")
                                    await sleep()
                                    continue

                                products_fh.write(json.dumps(validated, ensure_ascii=False) + "\n")
                                products_fh.flush()
                                upload_product(validated)
                                processed_urls.add(url)
                                save_progress(processed_urls)
                                current_index += 1
                                if validated.get("existence"):
                                    success += 1
                                    desc_len = len(str(validated.get("description") or ""))
                                    print(
                                        f"  成功: [{validated.get('source')}] {validated.get('product_id')} | "
                                        f"{str(validated.get('title'))[:80]} | description={desc_len} chars"
                                    )
                                else:
                                    print(
                                        f"  已保存不存在商品: [{validated.get('source')}] "
                                        f"{validated.get('product_id')} | existence=False"
                                    )
                            except BrowserRestartRequired:
                                raise
                            except Exception as exc:
                                failed += 1
                                invalid_fh.write(
                                    json.dumps(
                                        {
                                            "url": url,
                                            "product_id": product_id_from_url(url),
                                            "error": str(exc),
                                            "date": datetime.now().replace(microsecond=0).isoformat(),
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n"
                                )
                                invalid_fh.flush()
                                processed_urls.add(url)
                                save_progress(processed_urls)
                                current_index += 1
                                print(f"  失败: {exc}")

                            await sleep()
                        if current_index >= len(links):
                            batch_finished = True
                except BrowserRestartRequired as exc:
                    current_url = links[current_index] if current_index < len(links) else ""
                    captcha_restart_counts[current_url] = captcha_restart_counts.get(current_url, 0) + 1
                    retry_count = captcha_restart_counts[current_url]
                    print(
                        f"[主流程] {exc}，当前商品验证码重启 {retry_count}/"
                        f"{MAX_CAPTCHA_RESTARTS_PER_URL}。"
                    )
                    if retry_count >= MAX_CAPTCHA_RESTARTS_PER_URL:
                        with invalid_path.open("a", encoding="utf-8") as invalid_fh:
                            invalid_fh.write(
                                json.dumps(
                                    {
                                        "url": current_url,
                                        "product_id": product_id_from_url(current_url),
                                        "error": f"连续 {retry_count} 次遇到验证码，跳过当前商品",
                                        "date": datetime.now().replace(microsecond=0).isoformat(),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        failed += 1
                        processed_urls.add(current_url)
                        save_progress(processed_urls)
                        current_index += 1
                        print("[主流程] 当前商品连续遇到验证码，已记录失败并跳到下一条。")
                finally:
                    if context:
                        try:
                            await asyncio.wait_for(context.close(), timeout=8)
                        except Exception:
                            pass
                    if browser:
                        try:
                            await asyncio.wait_for(browser.close(), timeout=8)
                        except Exception:
                            pass
                    clear_browser_user_data()

                if not batch_finished and current_index < len(links):
                    print("[主流程] 等待 5 秒后重新启动浏览器。")
                    await asyncio.sleep(5)

        if MAX_PRODUCTS > 0 and success >= MAX_PRODUCTS:
            break
        print(f"[ES链接] 第 {batch_no} 批处理完成，准备读取下一批。")

    print("\n完成。")
    print(f"成功: {success}")
    print(f"失败: {failed}")
    print(f"跳过已处理: {skipped}")
    print(f"详情文件: {products_path}")
    print(f"失败文件: {invalid_path}")
    print(f"进度文件: {PROGRESS_FILE}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
