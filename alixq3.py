

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
from urllib.parse import unquote, urlparse

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

def resolve_es_config() -> tuple[str, str, str, str]:
    """Read ES settings from .env (supports ES_* and ELASTICSEARCH_URL)."""
    host = os.environ.get("ES_HOST", "").strip()
    port = os.environ.get("ES_PORT", "").strip()
    user = os.environ.get("ES_USER", "").strip()
    password = os.environ.get("ES_PASSWORD", "").strip()

    es_url = os.environ.get("ELASTICSEARCH_URL", "").strip()
    if es_url:
        parsed = urlparse(es_url)
        if not host and parsed.hostname:
            host = parsed.hostname
        if not port:
            port = str(parsed.port or 9200)
        if not user and parsed.username:
            user = unquote(parsed.username)
        if not password and parsed.password:
            password = unquote(parsed.password)

    if not user:
        user = os.environ.get("ELASTICSEARCH_USERNAME", "").strip()
    if not password:
        password = os.environ.get("ELASTICSEARCH_PASSWORD", "").strip()

    servers = os.environ.get("ELASTICSEARCH_SERVERS", "").strip()
    if servers and not host:
        if ":" in servers:
            host, port_part = servers.rsplit(":", 1)
            if not port:
                port = port_part
        else:
            host = servers

    if not host:
        host = "34.16.105.219"
    if not port:
        port = "9200"

    return host, port, user, password


ES_HOST, ES_PORT, ES_USER, ES_PASSWORD = resolve_es_config()
URLS_INDEX_NAME = "user1_aliexpress_us_product_urls"
PRODUCT_INDEX_NAME = os.environ.get("PRODUCT_INDEX_NAME", "user1_aliexpress_us_products")
URLS_BATCH_SIZE = 1000

WEBSHARE_USER = os.environ.get("WEBSHARE_USER", "").strip()
WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PASSWORD", "").strip()
WEBSHARE_COUNTRY = os.environ.get("WEBSHARE_COUNTRY", "US").strip().lower()
WEBSHARE_HOST = os.environ.get("WEBSHARE_HOST", "p.webshare.io").strip()
WEBSHARE_PORT = os.environ.get("WEBSHARE_PORT", "80").strip()
WEBSHARE_ROTATE = os.environ.get("WEBSHARE_ROTATE", "1").strip().lower() in ("1", "true", "yes", "on")

HEADLESS = os.environ.get("HEADLESS", "0").strip().lower() in ("1", "true", "yes", "on")
CAPTCHA_WAIT_SECONDS = 120
CAPTCHA_MAX_ROUNDS = 30
CAPTCHA_RECOVERY_ROUNDS = int(os.environ.get("CAPTCHA_RECOVERY_ROUNDS", "5") or "5")
CAPTCHA_MANUAL_PAUSE_SECONDS = int(os.environ.get("CAPTCHA_MANUAL_PAUSE_SECONDS", "8") or "8")
MAX_CAPTCHA_RESTARTS_PER_URL = int(os.environ.get("MAX_CAPTCHA_RESTARTS_PER_URL", "3") or "3")
BROWSER_RESTART_DELAY_SECONDS = int(os.environ.get("BROWSER_RESTART_DELAY_SECONDS", "5") or "5")
WORKER_COUNT = max(1, int(os.environ.get("WORKER_COUNT", "1") or "1"))

# 0 表示不限制；本地试跑可设 MAX_PRODUCTS=1
MAX_PRODUCTS = int(os.environ.get("MAX_PRODUCTS", "0") or "0")
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


def is_browser_closed_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "connection closed" in message
    )


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


def worker_user_data_dir(worker_id: int = 0) -> Path:
    if WORKER_COUNT <= 1:
        return USER_DATA_DIR
    return USER_DATA_DIR / f"worker_{worker_id}"


def clear_browser_user_data(worker_id: int | None = None) -> None:
    if worker_id is not None:
        user_data_dir = worker_user_data_dir(worker_id)
        cleanup_profile_locks(user_data_dir)
        if user_data_dir.exists():
            shutil.rmtree(user_data_dir, ignore_errors=True)
        return

    if WORKER_COUNT <= 1:
        clear_browser_user_data(0)
        return

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
    rotate_note = "，每次重启浏览器会建立新连接并轮换 IP" if username.endswith("-rotate") else ""
    return f"Webshare {WEBSHARE_HOST}:{WEBSHARE_PORT} (user={username}){rotate_note}"


def captcha_restart_label() -> str:
    return (
        f"单轮最多 {CAPTCHA_RECOVERY_ROUNDS} 次验证码尝试，"
        f"单商品最多重启浏览器 {MAX_CAPTCHA_RESTARTS_PER_URL} 次"
    )


def build_chromium_args(worker_id: int = 0) -> list[str]:
    args = list(CHROMIUM_ARGS)
    if WORKER_COUNT > 1:
        args = [arg for arg in args if arg != "--start-maximized"]
    if not HEADLESS and WORKER_COUNT > 1:
        cols = min(WORKER_COUNT, 3)
        row = worker_id // cols
        col = worker_id % cols
        args.extend(
            [
                f"--window-position={col * 680},{row * 420}",
                "--window-size=640,400",
            ]
        )
    return args


async def launch_browser_context(playwright, worker_id: int = 0, retries: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        clear_browser_user_data(worker_id)
        try:
            proxy = build_webshare_proxy()
            launch_kwargs: dict[str, Any] = {
                "headless": HEADLESS,
                "args": build_chromium_args(worker_id),
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


def is_generic_page_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(title or "").strip().lower())
    if not normalized or normalized == "error":
        return True
    if normalized in GENERIC_PAGE_TITLES:
        return True
    if len(normalized) <= 20 and normalized.replace(".", "") in {"aliexpress", "aliexpresscom", "aliexpressus"}:
        return True
    return False


def build_redirect_info(original_url: str, final_url: str) -> dict[str, str] | None:
    """Return redirect metadata when product_id or site (.com/.us) changes."""
    original_pid = product_id_from_url(original_url)
    final_pid = product_id_from_url(final_url)
    original_source = source_from_url(original_url)
    final_source = source_from_url(final_url)
    if not original_pid or not final_pid:
        return None
    if original_pid == final_pid and original_source == final_source:
        return None

    if original_pid != final_pid and original_source != final_source:
        reason = "product_id_and_source_redirect"
    elif original_source != final_source:
        reason = "source_redirect"
    else:
        reason = "product_id_redirect"

    return {
        "reason": reason,
        "original_product_id": original_pid,
        "redirect_product_id": final_pid,
        "original_source": original_source,
        "final_source": final_source,
        "original_url": normalize_https_url(original_url),
        "final_url": normalize_https_url(final_url),
    }


def format_redirect_summary(redirect_info: dict[str, str]) -> str:
    return (
        f"reason={redirect_info['reason']}; "
        f"requested_source={redirect_info['original_source']}; "
        f"requested_url={redirect_info['original_url']}; "
        f"requested_product_id={redirect_info['original_product_id']}; "
        f"final_source={redirect_info['final_source']}; "
        f"final_url={redirect_info['final_url']}; "
        f"final_product_id={redirect_info['redirect_product_id']}"
    )


def apply_redirect_metadata(record: dict[str, Any], redirect_info: dict[str, str] | None) -> dict[str, Any]:
    if not redirect_info:
        return record
    note = format_redirect_summary(redirect_info)
    existing = str(record.get("summary") or "").strip()
    record["summary"] = f"{existing}; {note}" if existing else note
    return record


def is_unavailable_product_page(
    *,
    api_data: dict[str, Any] | None,
    record: dict[str, Any],
    dom_data: dict[str, Any],
    page_text: str,
) -> bool:
    """Detect delisted / 404 pages on the final rendered product page."""
    if api_data:
        api_result = _get_api_result(api_data)
        i18n = api_result.get("GLOBAL_DATA", {}).get("i18n", {}) or {}
        if i18n.get("ItemDetailResp", {}).get("PAGE_NOT_FOUND_NOTICE"):
            return True

    lowered_page_text = str(page_text or "").lower()
    if any(marker in lowered_page_text for marker in NOT_FOUND_PAGE_MARKERS):
        return True

    title = str(record.get("title") or "").strip()
    if is_generic_page_title(title):
        return True

    return False


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
GENERIC_PAGE_TITLES = frozenset(
    {
        "aliexpress",
        "aliexpress.com",
        "aliexpress.us",
        "aliexpress - online shopping for popular electronics, fashion, home & garden, toys & sports, automobiles and more.",
    }
)
NOT_FOUND_PAGE_MARKERS = (
    "page not found",
    "page you requested can not be found",
    "page you requested cannot be found",
    "sorry, this item",
    "item is no longer available",
    "item you requested doesn't exist",
    "product you are trying to view is not available",
    "this product is no longer available",
    "page_not_found_notice",
)


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


def make_empty_record(
    url: str,
    *,
    redirect_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    """不存在商品的兜底记录，通过 StandardProduct 校验后写入 ES。"""
    pid = product_id_from_url(url)
    source = source_from_url(url)
    description = MISSING_PRODUCT_DESCRIPTION
    summary = None
    if redirect_info:
        summary = format_redirect_summary(redirect_info)
        description = (
            "<p>Product unavailable on redirected target page.</p>"
            f"<p>Requested URL: {redirect_info['original_url']}</p>"
            f"<p>Final URL: {redirect_info['final_url']}</p>"
        )
    record = {
        "date": datetime.now().replace(microsecond=0).isoformat(),
        "url": normalize_https_url(url),
        "source": source,
        "product_id": pid,
        "existence": False,
        "title": f"{MISSING_PRODUCT_TITLE_PREFIX} {pid}",
        "title_en": None,
        "description": description,
        "summary": summary,
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


def make_superseded_record(redirect_info: dict[str, str]) -> dict[str, Any]:
    """Mark the originally requested URL as no longer existing after redirect."""
    original_url = redirect_info["original_url"]
    pid = redirect_info["original_product_id"]
    source = redirect_info["original_source"]
    target_pid = redirect_info["redirect_product_id"]
    target_url = redirect_info["final_url"]
    target_source = redirect_info["final_source"]
    description = (
        "<p>Original URL no longer exists.</p>"
        f"<p>Requested site: {source}</p>"
        f"<p>Redirect site: {target_source}</p>"
        f"<p>Redirect product ID: {target_pid}</p>"
        f"<p>Redirect URL: {target_url}</p>"
    )
    summary = (
        f"reason=original_url_no_longer_exists; status=superseded_by_redirect; "
        f"requested_source={source}; final_source={target_source}; "
        f"redirect_product_id={target_pid}; redirect_url={target_url}"
    )
    record = {
        "date": datetime.now().replace(microsecond=0).isoformat(),
        "url": normalize_https_url(original_url),
        "source": source,
        "product_id": pid,
        "existence": False,
        "title": f"{MISSING_PRODUCT_TITLE_PREFIX} {pid}",
        "title_en": None,
        "description": description,
        "summary": summary,
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


def finalize_fetch_records(record: dict[str, Any], redirect_info: dict[str, str] | None) -> list[dict[str, Any]]:
    records = [record]
    if redirect_info:
        records.append(make_superseded_record(redirect_info))
    return records


def _get_api_result(api_data: dict[str, Any] | None) -> dict[str, Any]:
    """从 API 数据中提取 result 字典"""
    if not api_data:
        return {}
    data = api_data.get("data", {})
    if isinstance(data, dict):
        return data.get("result", {}) or data
    return {}


def _extract_sale_price(info: dict[str, Any]) -> float:
    """Extract current sale/activity price from AliExpress price info."""
    if not isinstance(info, dict):
        return 0.0
    sale_obj = info.get("salePrice") or info.get("saleAmount") or {}
    if isinstance(sale_obj, dict):
        value = to_float(sale_obj.get("value"))
        if value > 0:
            return value
    sale_string = str(info.get("salePriceString") or "").strip()
    if sale_string:
        value = to_float(sale_string.replace("$", "").replace(",", ""))
        if value > 0:
            return value
    sale_local = str(info.get("salePriceLocal") or "").strip()
    if sale_local:
        head = sale_local.split("|", 1)[0]
        value = to_float(head.replace("$", "").replace(",", ""))
        if value > 0:
            return value
    for key in ("maxActivityAmount", "minActivityAmount"):
        nested = info.get(key) or {}
        if isinstance(nested, dict):
            value = to_float(nested.get("value"))
            if value > 0:
                return value
    return 0.0


def pick_price_from_api(api_data: dict[str, Any]) -> float:
    """从 MTOP API 返回数据中提取销售价（新结构: data.result.PRICE）"""
    result = _get_api_result(api_data)
    price_data = result.get("PRICE") or result.get("priceModule") or {}
    if isinstance(price_data, dict):
        target = price_data.get("targetSkuPriceInfo") or {}
        price = _extract_sale_price(target)
        if price > 0:
            return price
        sku_map = price_data.get("skuIdStrPriceInfoMap") or {}
        selected_sku = str(price_data.get("selectedSkuId") or "")
        if selected_sku and selected_sku in sku_map:
            price = _extract_sale_price(sku_map[selected_sku])
            if price > 0:
                return price
        sale_prices = [_extract_sale_price(info) for info in sku_map.values()]
        sale_prices = [p for p in sale_prices if p > 0]
        if sale_prices:
            return min(sale_prices)
    # fallback: try old structure (activity/discount price only)
    price_module = result.get("priceModule") or {}
    price_component = result.get("priceComponent") or {}
    candidates = [
        price_component.get("maxActivityAmount", {}).get("value"),
        price_component.get("minActivityAmount", {}).get("value"),
        (price_module.get("discountPrice") or {}).get("maxActivityAmount", {}).get("value"),
        (price_module.get("discountPrice") or {}).get("minActivityAmount", {}).get("value"),
        price_module.get("formatedActivityPrice"),
        price_component.get("maxAmount", {}).get("value"),
        price_component.get("minAmount", {}).get("value"),
        (price_module.get("origPrice") or {}).get("maxAmount", {}).get("value"),
        (price_module.get("origPrice") or {}).get("minAmount", {}).get("value"),
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


def _sku_property_value_id(property_value: dict[str, Any]) -> str:
    value_id = property_value.get("propertyValueId")
    if value_id in (None, "", "0", 0):
        value_id = property_value.get("propertyValueIdLong")
    return str(value_id or "")


def _adapt_modular_sku_properties(sku_block: dict[str, Any]) -> list[dict[str, Any]]:
    properties: list[dict[str, Any]] = []
    for sku_property in sku_block.get("skuProperties") or []:
        if not isinstance(sku_property, dict):
            continue
        values: list[dict[str, Any]] = []
        for property_value in sku_property.get("skuPropertyValues") or []:
            if not isinstance(property_value, dict):
                continue
            values.append(
                {
                    "propertyValueId": property_value.get("propertyValueId"),
                    "propertyValueIdLong": property_value.get("propertyValueIdLong"),
                    "propertyValueName": property_value.get("propertyValueName", ""),
                    "propertyValueDisplayName": property_value.get("propertyValueDisplayName")
                    or property_value.get("propertyValueDefinitionName", ""),
                    "skuPropertyImagePath": property_value.get("skuPropertyImagePath", ""),
                }
            )
        properties.append(
            {
                "skuPropertyId": sku_property.get("skuPropertyId"),
                "skuPropertyName": sku_property.get("skuPropertyName", ""),
                "skuPropertyValues": values,
            }
        )
    return properties


def _price_amount_from_sku_info(info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    original = info.get("originalPrice") or {}
    currency = original.get("currency") or "USD"
    original_value = to_float(original.get("value"))
    sale_value = _extract_sale_price(info)
    if sale_value <= 0:
        sale_value = original_value
    return (
        {"currency": currency, "value": original_value},
        {"currency": currency, "value": sale_value},
    )


def _adapt_modular_sku_price_list(
    sku_block: dict[str, Any], sku_map: dict[str, Any]
) -> list[dict[str, Any]]:
    sku_price_list: list[dict[str, Any]] = []
    for path in sku_block.get("skuPaths") or []:
        if not isinstance(path, dict):
            continue
        sku_id = str(path.get("skuIdStr") or path.get("skuId") or "")
        price_info = sku_map.get(sku_id) or {}
        _, sale_amount = _price_amount_from_sku_info(price_info)
        stock = path.get("skuStock")
        if stock is None:
            stock = 100 if path.get("salable", True) else 0
        sku_price_list.append(
            {
                "skuAttr": path.get("skuAttr") or path.get("path") or sku_id,
                "skuVal": {
                    "availQuantity": to_int(stock),
                    "skuAmount": sale_amount,
                },
            }
        )
    if sku_price_list:
        return sku_price_list

    for sku_id, info in sku_map.items():
        _, sale_amount = _price_amount_from_sku_info(info)
        sku_price_list.append(
            {
                "skuAttr": str(sku_id),
                "skuVal": {
                    "availQuantity": 100,
                    "skuAmount": sale_amount,
                },
            }
        )
    return sku_price_list


def _extract_sku_module(api_result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sku_block = api_result.get("SKU") or {}
    if isinstance(sku_block, dict) and sku_block:
        price_block = api_result.get("PRICE") or {}
        sku_map = price_block.get("skuIdStrPriceInfoMap") or {} if isinstance(price_block, dict) else {}
        return (
            _adapt_modular_sku_properties(sku_block),
            _adapt_modular_sku_price_list(sku_block, sku_map),
        )

    legacy_module = api_result.get("skuModule") or api_result.get("skuComponent") or {}
    if not isinstance(legacy_module, dict):
        return [], []
    return (
        legacy_module.get("productSKUPropertyList") or [],
        legacy_module.get("skuPriceList") or [],
    )


def _get_sku_price_from_val(sku_val: dict[str, Any]) -> float:
    for key in (
        "actSkuCalPrice",
        "actSkuMultiCurrencyCalPrice",
        "actSkuMultiCurrencyDisplayPrice",
    ):
        value = to_float(sku_val.get(key))
        if value > 0:
            return value
    sku_amount = to_float((sku_val.get("skuAmount") or {}).get("value"))
    if sku_amount > 0:
        return sku_amount
    for key in (
        "skuCalPrice",
        "skuMultiCurrencyCalPrice",
        "skuMultiCurrencyDisplayPrice",
    ):
        value = to_float(sku_val.get(key))
        if value > 0:
            return value
    return 0.0


def _build_options_mapping(product_sku_property_list: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    options_mapping: dict[str, dict[str, Any]] = {}
    for sku_property in product_sku_property_list:
        option_id = str(sku_property.get("skuPropertyId") or "")
        if not option_id:
            continue
        skus: dict[str, dict[str, Any]] = {}
        for property_value in sku_property.get("skuPropertyValues") or []:
            if not isinstance(property_value, dict):
                continue
            sku_id = _sku_property_value_id(property_value)
            if not sku_id:
                continue
            sku_meta = {
                "id": sku_id,
                "name": property_value.get("propertyValueName", ""),
                "presentation": property_value.get("propertyValueDisplayName")
                or property_value.get("propertyValueName", ""),
                "img_url": normalize_image_url(property_value.get("skuPropertyImagePath", "")),
            }
            skus[sku_id] = sku_meta
            long_id = property_value.get("propertyValueIdLong")
            if long_id not in (None, "", "0", 0):
                skus[str(long_id)] = sku_meta
            short_id = property_value.get("propertyValueId")
            if short_id not in (None, "", "0", 0):
                skus[str(short_id)] = sku_meta
        options_mapping[option_id] = {
            "option_name": sku_property.get("skuPropertyName", ""),
            "skus": skus,
        }
    return options_mapping


def _build_option_values(
    sku_attr: str, options_mapping: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    option_values: list[dict[str, str]] = []
    if not sku_attr:
        return option_values
    for part in sku_attr.split(";"):
        attr = part.split("#")[0]
        attr_parts = attr.split(":")
        if len(attr_parts) < 2:
            continue
        option_id, option_val = attr_parts[0], attr_parts[1]
        option = options_mapping.get(option_id)
        if not option:
            continue
        sku = option["skus"].get(option_val)
        if not sku:
            continue
        option_values.append(
            {
                "option_id": option_id,
                "option_value_id": option_val,
                "option_name": option["option_name"],
                "option_value": sku.get("presentation") or sku.get("name") or option_val,
            }
        )
    return option_values


def _options_from_dom(dom_data: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw_options = dom_data.get("skuOptions") or []
    if not raw_options:
        return None
    options: list[dict[str, Any]] = []
    for item in raw_options:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        option_id = str(item.get("id") or "").strip()
        if not name or not option_id:
            continue
        options.append({"name": name, "id": option_id})
    return options or None


def parse_options_and_variants(
    api_result: dict[str, Any],
    product_id: str,
    currency: str,
    dom_data: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None, bool, float, int | None]:
    product_sku_property_list, sku_price_list = _extract_sku_module(api_result)
    options_mapping = _build_options_mapping(product_sku_property_list)
    standard_options = [
        {"name": meta["option_name"], "id": option_id}
        for option_id, meta in options_mapping.items()
        if meta.get("option_name")
    ] or None

    if len(sku_price_list) <= 1:
        dom_options = _options_from_dom(dom_data or {})
        if dom_options and not standard_options:
            standard_options = dom_options
        available_qty = None
        if sku_price_list:
            available_qty = to_int((sku_price_list[0].get("skuVal") or {}).get("availQuantity"))
        return standard_options, None, True, 0.0, available_qty

    variants: list[dict[str, Any]] = []
    prices: list[float] = []
    total_qty = 0
    for sku_product in sku_price_list:
        sku_attr = str(sku_product.get("skuAttr") or "")
        sku_val = sku_product.get("skuVal") or {}
        quantity = to_int(sku_val.get("availQuantity"))
        if quantity <= 0:
            continue
        formatted_attr = ";".join(part.split("#")[0] for part in sku_attr.split(";") if part)
        option_values = _build_option_values(sku_attr, options_mapping)
        variant_images = None
        for ov in option_values:
            option = options_mapping.get(ov.get("option_id") or "")
            if not option:
                continue
            sku_meta = option["skus"].get(ov.get("option_value_id") or "")
            if sku_meta and sku_meta.get("img_url"):
                variant_images = sku_meta["img_url"]
                break
        variant_price = _get_sku_price_from_val(sku_val)
        if variant_price > 0:
            prices.append(variant_price)
        total_qty += quantity
        variants.append(
            {
                "sku": f"ALI_{product_id}_{formatted_attr or 'default'}",
                "barcode": None,
                "variant_id": formatted_attr or str(product_id),
                "price": variant_price,
                "currency": currency,
                "available_qty": quantity,
                "option_values": option_values,
                "images": variant_images,
            }
        )

    has_only_default_variant = len(variants) == 0
    if has_only_default_variant:
        dom_options = _options_from_dom(dom_data or {})
        if dom_options and not standard_options:
            standard_options = dom_options
        return standard_options, None, True, 0.0, None

    variant_price = min(prices) if prices else 0.0
    return standard_options, variants, False, variant_price, total_qty or None


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

    # ---- price: API sale > LD+JSON > DOM; multi-variant uses min sale ----
    price = 0.0
    if api_result and api_data:
        price = pick_price_from_api(api_data)
    if price <= 0:
        offers = ld_product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            price = to_float(offers.get("lowPrice") or offers.get("price") or 0)
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

    options, variants, has_only_default_variant, variant_price, available_qty = parse_options_and_variants(
        api_result,
        pid,
        currency,
        dom_data,
    )
    if variant_price > 0:
        price = variant_price
    if variants:
        print(f"  [SKU] options={len(options or [])} variants={len(variants)}")

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
        "available_qty": available_qty,
        "options": options,
        "variants": variants,
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
        "has_only_default_variant": has_only_default_variant,
        "_id": product_doc_id(source, pid),
    }


def is_blocked_url(url: str) -> bool:
    lowered = url.lower()
    return "punish" in lowered or "tmd" in lowered


async def is_risk_control_page(page: Page) -> bool:
    """仍在风控/验证码流程中的页面，不应保存商品数据。"""
    if is_blocked_url(page.url):
        return True
    try:
        if await is_captcha_page_visible(page):
            return True
        title = await page.title()
        page_text = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 4000) : ''"
        )
        if is_captcha_text(f"{title}\n{page_text}"):
            return True
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserRestartRequired("浏览器已关闭，重新启动") from exc
    return False


async def raise_if_risk_control_page(page: Page) -> None:
    if await is_risk_control_page(page):
        raise BrowserRestartRequired("页面仍在风控/验证状态，不保存商品数据")


async def navigate_product_page(page: Page, full_url: str, captcha_state: dict[str, bool]) -> None:
    """打开商品页并在风控页时重试导航，直到进入正常商品页或达到重试上限。"""
    for attempt in range(1, 4):
        await page.goto(full_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        await handle_captcha(page, captcha_state, product_url=full_url)
        await sleep(short=True)
        if not is_blocked_url(page.url):
            return
        print(f"  [导航] 仍在风控页，重试 {attempt}/3: {page.url[:120]}")
    if is_blocked_url(page.url):
        raise BrowserRestartRequired("验证码通过后仍停留在风控页")


def parse_api_response_body(body: str) -> dict[str, Any] | None:
    text = (body or "").strip()
    if not text:
        return None
    for pattern in (
        r"mtopjsonp\d+\((.*)\)\s*$",
        r"/\*\*/\w+\((.*)\)\s*$",
        r"^\s*(\{.*\})\s*$",
    ):
        match = re.search(pattern, text, re.S)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _score_pdp_api_payload(parsed: dict[str, Any], body_len: int) -> int:
    score = body_len
    result = _get_api_result(parsed)
    if not result:
        return score
    if result.get("SKU"):
        score += 10_000_000
    if result.get("PRICE"):
        score += 1_000_000
    if result.get("PRODUCT_TITLE") or result.get("PC_RATING"):
        score += 100_000
    ret = parsed.get("ret") or []
    if any("FAIL" in str(item).upper() for item in ret):
        score -= 50_000_000
    return score


async def capture_pdp_api_data(page: Page, navigate: Any) -> dict[str, Any] | None:
    """Collect the richest mtop.aliexpress.pdp.pc.query response during navigation."""
    best: tuple[int, dict[str, Any]] | None = None

    async def on_response(response: Any) -> None:
        nonlocal best
        url = response.url
        if "mtop.aliexpress.pdp.pc.query" not in url:
            return
        if "punish" in url or "_____tmd_____" in url:
            return
        try:
            body = await response.text()
        except Exception:
            return
        parsed = parse_api_response_body(body)
        if not parsed:
            return
        score = _score_pdp_api_payload(parsed, len(body))
        if best is None or score > best[0]:
            best = (score, parsed)

    page.on("response", on_response)
    try:
        await navigate()
        await page.wait_for_timeout(3500)
    finally:
        page.remove_listener("response", on_response)

    return best[1] if best else None


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

def resolve_xai_api_key() -> str:
    """Read xAI API key from .env (XAI_API_KEY or OPENAI_API_KEY)."""
    for name in ("XAI_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip().strip('"').strip("'")
        if not value:
            continue
        lowered = value.lower()
        if lowered.startswith("your_") or lowered in {"changeme", "none", "null"}:
            continue
        return value
    return ""


def resolve_xai_config() -> tuple[str, str, str, bool]:
    api_key = resolve_xai_api_key()
    model = os.environ.get("OPENAI_MODEL", os.environ.get("XAI_MODEL", "grok-4.3")).strip() or "grok-4.3"
    api_base = os.environ.get(
        "OPENAI_API_BASE",
        os.environ.get("XAI_API_BASE", "https://api.x.ai/v1/chat/completions"),
    ).strip()
    auto_solve = os.environ.get("CAPTCHA_AUTO_SOLVE", "1").strip().lower() in ("1", "true", "yes", "on")
    return api_key, model, api_base, auto_solve


XAI_API_KEY, OPENAI_MODEL, OPENAI_API_BASE, CAPTCHA_AUTO_SOLVE = resolve_xai_config()
OPENAI_API_KEY = XAI_API_KEY
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

async def is_captcha_page_visible(page: Page) -> bool:
    title = await page.title()
    html = await page.content()
    if is_captcha_text(title + "\n" + html):
        return True
    for frame in page.frames:
        try:
            if await frame.locator("#recaptcha-anchor > div.recaptcha-checkbox-border").count() > 0:
                return True
            if await frame.locator("#recaptcha-verify-button").count() > 0:
                return True
        except Exception:
            continue
    return False


def is_invalid_xai_key_error(response_text: str) -> bool:
    lowered = str(response_text or "").lower()
    return "incorrect api key" in lowered or "invalid api key" in lowered or "invalid-argument" in lowered and "api key" in lowered


async def wait_for_manual_captcha(page: Page, timeout_sec: int = CAPTCHA_WAIT_SECONDS) -> bool:
    print(
        f"[验证码识别] 请在浏览器窗口中手动完成验证码（最多等待 {timeout_sec} 秒）..."
    )
    for elapsed in range(1, timeout_sec + 1):
        try:
            if not await is_captcha_page_visible(page):
                print("[验证码识别] 验证码已通过（手动）。")
                return True
        except Exception as exc:
            if is_browser_closed_error(exc):
                raise BrowserRestartRequired("浏览器已关闭，重新启动") from exc
            raise
        if elapsed in {1, 15, 30, 60, 90} or elapsed == timeout_sec:
            print(f"[验证码识别] 仍在等待手动验证... {elapsed}/{timeout_sec}s")
        await asyncio.sleep(1)
    print("[验证码识别] 手动验证超时。")
    return False


def xai_config_label() -> str:
    if not XAI_API_KEY:
        return "未配置 XAI_API_KEY，遇到验证码时需手动完成"
    masked = f"{XAI_API_KEY[:8]}...{XAI_API_KEY[-4:]}" if len(XAI_API_KEY) > 12 else "(已配置)"
    mode = "自动识别" if CAPTCHA_AUTO_SOLVE else "仅手动"
    return f"{mode} | model={OPENAI_MODEL} | key={masked}"


async def is_product_page_ready(page: Page) -> bool:
    if is_blocked_url(page.url):
        return False
    try:
        title = await page.evaluate(
            """() => {
              const el = document.querySelector('h1, [data-pl="product-title"]');
              return el ? (el.innerText || '').trim() : '';
            }"""
        )
        return bool(title) and not is_generic_page_title(str(title))
    except Exception:
        return False


async def close_captcha_popup(page: Page) -> None:
    print("[验证码识别] 尝试关闭验证码弹窗...")
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)
        await page.keyboard.press("Escape")
    except Exception:
        pass

    for selector in (
        '[aria-label="Close"]',
        'button[aria-label="close"]',
        '.next-dialog-close',
        '.comet-modal-close',
        '.btn-close',
        '.close-btn',
    ):
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                await locator.click(timeout=2000)
                await asyncio.sleep(0.4)
        except Exception:
            continue

    try:
        await page.evaluate(
            """() => {
              const hide = (el) => {
                if (!el) return;
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
              };
              document.querySelectorAll(
                'iframe[title*="recaptcha challenge"], iframe[src*="recaptcha/api2/bframe"]'
              ).forEach((frame) => hide(frame.closest('div') || frame));
              document.querySelectorAll('.g-recaptcha-bubble-arrow, #rc-imageselect').forEach(hide);
              document.querySelectorAll('div').forEach((el) => {
                const iframe = el.querySelector('iframe[src*="recaptcha"]');
                if (!iframe) return;
                const z = parseInt(window.getComputedStyle(el).zIndex || '0', 10);
                if (z >= 200) hide(el);
              });
            }"""
        )
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserRestartRequired("浏览器已关闭，重新启动") from exc


async def refresh_after_captcha(page: Page, product_url: str) -> None:
    print(f"[验证码识别] 刷新页面: {product_url[:120]}")
    await page.goto(product_url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(2500)


async def captcha_cleared_after_refresh(page: Page) -> bool:
    if await is_product_page_ready(page):
        return True
    return not await is_captcha_page_visible(page)


async def solve_captcha(page: Page, max_rounds: int = CAPTCHA_MAX_ROUNDS) -> bool:
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
        while retry_count < max_rounds:
            retry_count += 1
            print(f"[验证码识别] 等待图片弹窗加载... ({retry_count}/{max_rounds})")
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
                        error_text = resp.text
                        print(f"[验证码识别] 模型调用失败: {error_text}")
                        if is_invalid_xai_key_error(error_text):
                            print(
                                "[验证码识别] xAI API Key 无效或未配置。"
                                " 请在 .env 设置 XAI_API_KEY（从 https://console.x.ai 获取），"
                                " 或设置 CAPTCHA_AUTO_SOLVE=0 改为纯手动验证。"
                            )
                            return False
                        return False

                except Exception as e:
                    if is_browser_closed_error(e):
                        raise BrowserRestartRequired("浏览器已关闭，重新启动") from e
                    print(f"[验证码识别] 截图/交互失败: {e}")
                    return False
            else:
                print("[验证码识别] 没有看到验证码图片弹窗，可能验证已通过或遇到不同形式验证码。")
                return True

    return True


async def handle_captcha(
    page: Page,
    captcha_state: dict[str, bool],
    product_url: str | None = None,
) -> bool:
    if await is_product_page_ready(page):
        return False
    if not await is_captcha_page_visible(page):
        return False

    target_url = product_url or page.url
    print(f"\n检测到验证/风控页面：{page.url}")
    if captcha_state.get("solved_once"):
        captcha_state["session_captcha_count"] = captcha_state.get("session_captcha_count", 1) + 1
        print(
            f"[验证码识别] 会话中再次出现验证码（第 {captcha_state['session_captcha_count']} 次），"
            "继续尝试自动处理..."
        )

    for attempt in range(1, CAPTCHA_RECOVERY_ROUNDS + 1):
        if await captcha_cleared_after_refresh(page):
            return False

        print(f"[验证码识别] 第 {attempt}/{CAPTCHA_RECOVERY_ROUNDS} 轮：尝试通过验证码...")

        if XAI_API_KEY and CAPTCHA_AUTO_SOLVE:
            print("[验证码识别] 自动识别验证码（单轮）。")
            await solve_captcha(page, max_rounds=1)
        else:
            if not XAI_API_KEY:
                print(
                    "[验证码识别] 未配置 XAI_API_KEY，"
                    f"等待 {CAPTCHA_MANUAL_PAUSE_SECONDS}s 以便手动处理..."
                )
            else:
                print(
                    f"[验证码识别] CAPTCHA_AUTO_SOLVE=0，"
                    f"等待 {CAPTCHA_MANUAL_PAUSE_SECONDS}s 以便手动处理..."
                )
            await asyncio.sleep(CAPTCHA_MANUAL_PAUSE_SECONDS)

        await close_captcha_popup(page)
        await refresh_after_captcha(page, target_url)
        await sleep(short=True)

        if await captcha_cleared_after_refresh(page):
            print("[验证码识别] 关闭弹窗并刷新后，商品页已可继续抓取。")
            captcha_state["solved_once"] = True
            captcha_state.setdefault("session_captcha_count", 1)
            return True

        if await is_captcha_page_visible(page):
            print("[验证码识别] 刷新后仍有验证码，继续下一轮...")
        else:
            print("[验证码识别] 刷新后页面状态未完全确认，继续下一轮...")

    raise BrowserRestartRequired(
        f"验证码处理 {CAPTCHA_RECOVERY_ROUNDS} 轮后仍未进入商品页，清空浏览器状态并重启"
    )

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
          const skuOptions = [];
          document.querySelectorAll('[data-sku-row]').forEach((rowEl) => {
            const rowId = rowEl.getAttribute('data-sku-row');
            if (!rowId) return;
            const box = rowEl.closest('[class*="sku-item--box"]') || rowEl.parentElement;
            let propName = '';
            if (box) {
              let prev = box.previousElementSibling;
              while (prev && !propName) {
                const text = (prev.innerText || '').replace(/\\s+/g, ' ').trim();
                if (text) propName = text.split(':')[0].trim();
                prev = prev.previousElementSibling;
              }
            }
            const values = [];
            rowEl.querySelectorAll('[data-sku-col]').forEach((colEl) => {
              const col = colEl.getAttribute('data-sku-col') || '';
              const title = colEl.getAttribute('title') || (colEl.innerText || '').trim();
              const dashIdx = col.indexOf('-');
              const valueId = dashIdx >= 0 ? col.slice(dashIdx + 1) : col;
              if (valueId) values.push({ id: valueId, name: title, presentation: title });
            });
            if (propName && values.length) skuOptions.push({ id: rowId, name: propName, values });
          });
          return { title, priceText, categories: breadItems, rating, reviews, soldCount, skuOptions };
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


async def fetch_product(page: Page, url: str, captcha_state: dict[str, bool]) -> list[dict[str, Any]]:
    """打开产品页，拦截 MTOP API 获取数据，映射为标准 30 字段 Schema。"""
    full_url = url if "gatewayAdapt" in url else f"{url}?gatewayAdapt=glo2usa"
    api_data: dict[str, Any] | None = None

    async def navigate_product() -> None:
        await page.goto(full_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        await handle_captcha(page, captcha_state, product_url=full_url)
        await sleep(short=True)

    try:
        api_data = await capture_pdp_api_data(page, navigate_product)
        if api_data:
            print("  [API] 拦截到 mtop.aliexpress.pdp.pc.query 响应")
        else:
            print("  [API] 未拦截到有效 pdp 接口，降级使用 LD+JSON")
    except BrowserRestartRequired:
        raise
    except Exception as exc:
        print(f"  [API] 页面加载失败，降级使用 LD+JSON: {exc}")

    # ---- 第四步：提取 LD+JSON + DOM 数据 ----
    ld_json_list = await extract_ld_json(page)
    dom_data = await extract_dom_data(page)
    description = await extract_product_description(page)
    if description:
        dom_data["description"] = description

    run_params = await page.evaluate(
        "() => (window.runParams && window.runParams.data) ? window.runParams.data : null"
    )
    if run_params:
        api_data = api_data or {"data": {"result": {}}}
        api_result = _get_api_result(api_data)
        if not isinstance(api_result, dict):
            api_result = {}
            if isinstance(api_data.get("data"), dict):
                api_data["data"]["result"] = api_result
        sku_module = run_params.get("skuModule") or run_params.get("skuComponent") or {}
        if sku_module and not api_result.get("SKU"):
            api_result["skuModule"] = sku_module
        price_component = run_params.get("priceComponent") or {}
        if price_component.get("skuPriceList"):
            api_result.setdefault("skuModule", {})
            if isinstance(api_result["skuModule"], dict):
                api_result["skuModule"]["skuPriceList"] = price_component["skuPriceList"]

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
    final_url = page.url
    redirect_info = build_redirect_info(url, final_url)
    save_url = redirect_info["final_url"] if redirect_info else url
    if redirect_info:
        if redirect_info["original_product_id"] != redirect_info["redirect_product_id"]:
            print(
                f"  [重定向] product_id 变化: {redirect_info['original_product_id']} "
                f"-> {redirect_info['redirect_product_id']}"
            )
        if redirect_info["original_source"] != redirect_info["final_source"]:
            print(
                f"  [重定向] 站点变化: {redirect_info['original_source']} "
                f"-> {redirect_info['final_source']}"
            )
        print(f"  [重定向] 类型: {redirect_info['reason']}")
        print(f"  [重定向] 最终 URL: {redirect_info['final_url'][:160]}")
        print("  [重定向] 将按最终 URL 的商品信息保存")

    page_text = await page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 8000) : ''")
    await raise_if_risk_control_page(page)

    record = build_standard_record(api_data, ld_json_list, dom_data, save_url)

    if is_unavailable_product_page(
        api_data=api_data,
        record=record,
        dom_data=dom_data,
        page_text=str(page_text or ""),
    ):
        await raise_if_risk_control_page(page)
        print(f"  重定向目标页不可用（下架/404）: {save_url}")
        return finalize_fetch_records(make_empty_record(save_url, redirect_info=redirect_info), redirect_info)

    if not record.get("title") or record["title"] == "ERROR":
        await raise_if_risk_control_page(page)
        print(f"  未提取到有效标题：{save_url}")
        return finalize_fetch_records(make_empty_record(save_url, redirect_info=redirect_info), redirect_info)

    validated, error = validate_product_record(record)
    if not validated:
        await raise_if_risk_control_page(page)
        print(f"  格式校验失败：{error}")
        return finalize_fetch_records(make_empty_record(save_url, redirect_info=redirect_info), redirect_info)

    validated = apply_redirect_metadata(validated, redirect_info)
    if redirect_info:
        print(
            f"  已按重定向商品保存: [{validated.get('source')}] {validated.get('product_id')} "
            f"(原请求 [{redirect_info['original_source']}] {redirect_info['original_product_id']})"
        )
        print(
            f"  已标记原 URL 不存在: [{redirect_info['original_source']}] "
            f"{redirect_info['original_product_id']}"
        )
    return finalize_fetch_records(validated, redirect_info)


class CrawlState:
    def __init__(self, processed_urls: set[str]):
        self.processed_urls = processed_urls
        self.in_progress: set[str] = set()
        self.in_progress_product_ids: set[str] = set()
        self.lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self.stats_lock = asyncio.Lock()
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.completed = 0

    async def is_processed(self, url: str) -> bool:
        async with self.lock:
            return url in self.processed_urls

    async def claim_url(self, url: str) -> tuple[bool, str]:
        """Reserve a URL for one worker.

        Returns (claimed, reason) where reason is:
        - ok: URL claimed successfully
        - duplicate: URL already processed or claimed by another worker
        - product_busy: another worker is already scraping this product_id
        """
        product_id = product_id_from_url(url)
        async with self.lock:
            if url in self.processed_urls or url in self.in_progress:
                return False, "duplicate"
            if product_id and product_id in self.in_progress_product_ids:
                return False, "product_busy"
            self.in_progress.add(url)
            if product_id:
                self.in_progress_product_ids.add(product_id)
            return True, "ok"

    async def release_url(self, url: str) -> None:
        product_id = product_id_from_url(url)
        async with self.lock:
            self.in_progress.discard(url)
            if product_id:
                self.in_progress_product_ids.discard(product_id)

    async def finish_url(self, url: str) -> None:
        product_id = product_id_from_url(url)
        async with self.lock:
            self.in_progress.discard(url)
            if product_id:
                self.in_progress_product_ids.discard(product_id)
            self.processed_urls.add(url)
            save_progress(self.processed_urls)

    async def mark_skipped(self) -> None:
        async with self.stats_lock:
            self.skipped += 1

    async def mark_url_done(self, *, success: bool = False, failed: bool = False) -> None:
        async with self.stats_lock:
            self.completed += 1
            if success:
                self.success += 1
            if failed:
                self.failed += 1

    async def should_stop(self) -> bool:
        if MAX_PRODUCTS <= 0:
            return False
        async with self.stats_lock:
            return self.completed >= MAX_PRODUCTS

    async def append_invalid(self, invalid_path: Path, payload: dict[str, Any]) -> None:
        async with self.file_lock:
            with invalid_path.open("a", encoding="utf-8") as invalid_fh:
                invalid_fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                invalid_fh.flush()

    async def save_product_records(
        self,
        products_path: Path,
        invalid_path: Path,
        url: str,
        product_records: list[dict[str, Any]],
    ) -> None:
        saved_primary = False
        primary_success = False
        primary_failed = False
        async with self.file_lock:
            with products_path.open("a", encoding="utf-8") as products_fh, invalid_path.open(
                "a", encoding="utf-8"
            ) as invalid_fh:
                for record_idx, product in enumerate(product_records):
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
                        if record_idx == 0:
                            primary_failed = True
                        print(f"  失败: {validation_error}")
                        continue

                    products_fh.write(json.dumps(validated, ensure_ascii=False) + "\n")
                    products_fh.flush()
                    upload_product(validated)
                    if record_idx == 0:
                        saved_primary = True
                        primary_success = bool(validated.get("existence"))
                        if primary_success:
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
                    else:
                        print(
                            f"  已标记原 URL 不存在: [{validated.get('source')}] "
                            f"{validated.get('product_id')} | existence=False"
                        )

        failed = primary_failed or (not saved_primary and bool(product_records))
        await self.mark_url_done(success=primary_success, failed=failed)


UrlTask = tuple[str, int, int, int, int]


async def close_browser_session(
    browser,
    context,
    worker_id: int,
) -> None:
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
    clear_browser_user_data(worker_id)


async def browser_worker(
    worker_id: int,
    state: CrawlState,
    task_queue: asyncio.Queue[UrlTask | None],
    products_path: Path,
    invalid_path: Path,
    total_links: int,
    playwright,
) -> None:
    worker_label = f"Worker-{worker_id}"
    captcha_restart_counts: dict[str, int] = {}
    pending_task: UrlTask | None = None
    print(f"[{worker_label}] 启动")

    while True:
        if await state.should_stop():
            break

        browser = None
        context = None
        reset_after_product = False
        try:
            browser, context, page = await launch_browser_context(playwright, worker_id=worker_id)
            print(f"[{worker_label}] 浏览器已打开（代理: {webshare_proxy_label()}）")
            captcha_state = {"solved_once": False}

            while True:
                if await state.should_stop():
                    break

                if pending_task is None:
                    item = await task_queue.get()
                    if item is None:
                        task_queue.task_done()
                        return
                    pending_task = item

                url, index, batch_no, batch_pos, batch_size = pending_task
                claimed, claim_reason = await state.claim_url(url)
                if not claimed:
                    if claim_reason == "product_busy":
                        await task_queue.put(pending_task)
                        task_queue.task_done()
                        pending_task = None
                        await asyncio.sleep(0.3)
                        continue
                    await state.mark_skipped()
                    task_queue.task_done()
                    pending_task = None
                    continue

                print(
                    f"[{worker_label}] [{index}/{total_links}] "
                    f"第 {batch_no} 批 {batch_pos}/{batch_size} 抓取详情: {url}"
                )
                try:
                    product_records = await fetch_product(page, url, captcha_state)
                    if not product_records:
                        raise BrowserRestartRequired("未获取到商品数据，不保存")

                    await state.save_product_records(products_path, invalid_path, url, product_records)
                    await state.finish_url(url)
                    task_queue.task_done()
                    pending_task = None
                    reset_after_product = True
                    await sleep()
                    break
                except BrowserRestartRequired:
                    raise
                except Exception as exc:
                    if is_browser_closed_error(exc):
                        raise BrowserRestartRequired("浏览器已关闭，重新启动") from exc
                    await state.append_invalid(
                        invalid_path,
                        {
                            "url": url,
                            "product_id": product_id_from_url(url),
                            "error": str(exc),
                            "date": datetime.now().replace(microsecond=0).isoformat(),
                        },
                    )
                    await state.mark_url_done(failed=True)
                    await state.finish_url(url)
                    task_queue.task_done()
                    pending_task = None
                    reset_after_product = True
                    print(f"  失败: {exc}")
                    await sleep()
                    break
        except BrowserRestartRequired as exc:
            if pending_task is None:
                continue

            url = pending_task[0]
            captcha_restart_counts[url] = captcha_restart_counts.get(url, 0) + 1
            retry_count = captcha_restart_counts[url]
            print(
                f"[{worker_label}] {exc}，当前商品浏览器重启 {retry_count}/"
                f"{MAX_CAPTCHA_RESTARTS_PER_URL}。"
            )
            print(
                f"[{worker_label}] 已清空浏览器 profile，{BROWSER_RESTART_DELAY_SECONDS}s 后重新启动浏览器"
                + (
                    "（Webshare rotate 代理将分配新 IP）"
                    if WEBSHARE_ROTATE or WEBSHARE_USER.endswith("-rotate")
                    else ""
                )
                + "。"
            )
            if retry_count >= MAX_CAPTCHA_RESTARTS_PER_URL:
                await state.append_invalid(
                    invalid_path,
                    {
                        "url": url,
                        "product_id": product_id_from_url(url),
                        "error": f"连续 {retry_count} 次遇到验证码，跳过当前商品",
                        "date": datetime.now().replace(microsecond=0).isoformat(),
                    },
                )
                await state.mark_url_done(failed=True)
                await state.finish_url(url)
                task_queue.task_done()
                pending_task = None
                print(f"[{worker_label}] 当前商品连续遇到验证码，已记录失败并跳到下一条。")
        finally:
            await close_browser_session(browser, context, worker_id)

        if await state.should_stop():
            break

        if pending_task is not None:
            print(f"[{worker_label}] 等待 {BROWSER_RESTART_DELAY_SECONDS} 秒后重新启动浏览器。")
            await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
        elif reset_after_product:
            rotate_note = (
                "（Webshare rotate 代理将分配新 IP）"
                if WEBSHARE_ROTATE or WEBSHARE_USER.endswith("-rotate")
                else ""
            )
            print(
                f"[{worker_label}] 当前商品已处理完毕，已重置浏览器，"
                f"{BROWSER_RESTART_DELAY_SECONDS}s 后启动新会话{rotate_note}。"
            )
            await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)

    print(f"[{worker_label}] 结束")


async def run_worker_batch(
    state: CrawlState,
    links: list[str],
    batch_no: int,
    batch_start: int,
    total_links: int,
    products_path: Path,
    invalid_path: Path,
) -> None:
    task_queue: asyncio.Queue[UrlTask | None] = asyncio.Queue()
    deferred_tasks: list[UrlTask] = []
    queued_product_ids: set[str] = set()

    for current_index, url in enumerate(links):
        if await state.should_stop():
            break
        if await state.is_processed(url):
            await state.mark_skipped()
            continue
        global_index = batch_start + current_index + 1
        task = (url, global_index, batch_no, current_index + 1, len(links))
        product_id = product_id_from_url(url)
        if product_id and product_id in queued_product_ids:
            deferred_tasks.append(task)
            continue
        if product_id:
            queued_product_ids.add(product_id)
        await task_queue.put(task)

    for task in deferred_tasks:
        if await state.should_stop():
            break
        if await state.is_processed(task[0]):
            await state.mark_skipped()
            continue
        await task_queue.put(task)

    if task_queue.empty():
        return

    pending_count = task_queue.qsize()
    print(f"[批次 {batch_no}] 待抓取 {pending_count} 条，启动 {WORKER_COUNT} 个 Worker")

    async with async_playwright() as playwright:
        workers = [
            asyncio.create_task(
                browser_worker(
                    worker_id,
                    state,
                    task_queue,
                    products_path,
                    invalid_path,
                    total_links,
                    playwright,
                )
            )
            for worker_id in range(WORKER_COUNT)
        ]
        await task_queue.join()
        for _ in range(WORKER_COUNT):
            await task_queue.put(None)
        await asyncio.gather(*workers)


async def main_async() -> None:
    print("=" * 60)
    print("AliExpress 商品详情抓取")
    print("=" * 60)
    print(f"链接来源: http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}")
    print(f"上传索引: {PRODUCT_INDEX_NAME}")
    print(f"ES 用户: {ES_USER or '(未设置)'}")
    if not ES_USER or not ES_PASSWORD:
        print()
        print("错误: Elasticsearch 认证未配置。")
        print("请在 .env 中设置以下任一方式：")
        print("  ES_USER=emuser1")
        print("  ES_PASSWORD=your_password")
        print("或一行 URL：")
        print("  ELASTICSEARCH_URL=http://emuser1:your_password@34.16.105.219:9200")
        raise SystemExit(1)
    print(f"详情输出目录: {OUTPUT_DIR}")
    print(f"并发 Worker 数: {WORKER_COUNT}")
    if WORKER_COUNT > 1:
        print(f"浏览器 profile 目录: {USER_DATA_DIR}/worker_0 .. worker_{WORKER_COUNT - 1}")
    else:
        print("浏览器模式: 无痕模式，不保存用户目录")
    print(f"浏览器代理: {webshare_proxy_label()}")
    print(f"xAI 验证码: {xai_config_label()}")
    print(f"验证码重试: {captcha_restart_label()}")
    print(f"浏览器重启等待: {BROWSER_RESTART_DELAY_SECONDS}s（重启前会清空 profile）")
    if MAX_PRODUCTS > 0:
        print(f"本地试跑: 最多处理 {MAX_PRODUCTS} 条商品")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clear_browser_user_data()
    state = CrawlState(load_progress())
    products_path = PRODUCTS_FILE
    invalid_path = INVALID_FILE
    total_links = get_total_link_count()
    print(f"[ES链接] 索引 {URLS_INDEX_NAME} 总数: {total_links}")

    batch_no = 0
    fetched_count = 0
    search_after: list[Any] | None = None

    while True:
        if await state.should_stop():
            break
        links, search_after = load_link_batch(search_after)
        if not links:
            if batch_no == 0:
                print("ES 链接索引中没有找到商品链接。")
            break
        batch_no += 1
        batch_start = fetched_count
        fetched_count += len(links)
        print(f"[ES链接] 第 {batch_no} 批读取 {len(links)} 条，累计读取 {fetched_count}/{total_links}")
        await run_worker_batch(
            state,
            links,
            batch_no,
            batch_start,
            total_links,
            products_path,
            invalid_path,
        )
        if await state.should_stop():
            break
        print(f"[ES链接] 第 {batch_no} 批处理完成，准备读取下一批。")

    print("\n完成。")
    print(f"成功: {state.success}")
    print(f"失败: {state.failed}")
    print(f"已完成: {state.completed}")
    print(f"跳过已处理: {state.skipped}")
    print(f"详情文件: {products_path}")
    print(f"失败文件: {invalid_path}")
    print(f"进度文件: {PROGRESS_FILE}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
