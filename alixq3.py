

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
from stealth_fp import (
    FINGERPRINT_ENABLED,
    HUMAN_MOUSE_ENABLED,
    STEALTH_ENABLED,
    apply_stealth_and_fingerprint,
    ensure_diverse_fingerprints,
    fingerprint_from_dict,
    fingerprint_key_for_worker,
    fingerprint_to_dict,
    human_click_locator,
    human_idle,
    human_scroll,
    get_stored_fingerprint,
    load_or_create_fingerprint,
    mint_random_fingerprint,
    rebind_and_save_fingerprint,
    regenerate_fingerprint,
)


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

# 代理模式：
#   rotate — 现有 Webshare 网关 + rotate（硬失败可换 IP）
#   static — 使用 data/ 里固定住宅代理列表（同一会话保持同一 IP / cookies）
#   pool   — .env POOL_PROXIES 小代理池；验证码 1 轮失败即换代理+指纹
#   direct — 不走代理，用本机出口 IP（本地指纹/联调）
PROXY_MODE = (os.environ.get("PROXY_MODE", "rotate") or "rotate").strip().lower()
if PROXY_MODE in ("none", "off", "local", "no_proxy", "noproxy"):
    PROXY_MODE = "direct"
if PROXY_MODE not in ("rotate", "static", "direct", "pool"):
    PROXY_MODE = "rotate"


def _parse_pool_proxies_env() -> tuple[str, ...]:
    """Load pool proxies from ``POOL_PROXIES`` in .env.

    Accepts newline / ``|`` / ``;`` separated ``host:port:user:pass`` entries.
    """
    raw = os.environ.get("POOL_PROXIES", "").strip()
    if not raw:
        return ()
    # Strip wrapping quotes that some .env editors add.
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]
    lines: list[str] = []
    for part in re.split(r"[\n|;]+", raw):
        line = part.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return tuple(lines)


# pool 模式代理列表（来自 .env POOL_PROXIES，勿把真实账号写进代码）
FIXED_PROXY_POOL: tuple[str, ...] = _parse_pool_proxies_env()

_DEFAULT_PROXY_FILE = BASE_DIR / "data" / "Webshare 1000 proxies.txt"
_DEFAULT_POOL_FILE = BASE_DIR / "data" / "fixed_proxy_pool.txt"
PROXY_FILE = Path(
    os.environ.get(
        "PROXY_FILE",
        str(_DEFAULT_POOL_FILE if PROXY_MODE == "pool" else _DEFAULT_PROXY_FILE),
    ).strip()
    or str(_DEFAULT_PROXY_FILE)
)
PROXY_INDEX = max(0, int(os.environ.get("PROXY_INDEX", "0") or "0"))
# static 模式下启动后先逛首页/分类/商品页做 cookies 预热
SESSION_WARMUP = os.environ.get(
    "SESSION_WARMUP",
    "1" if PROXY_MODE == "static" else "0",
).strip().lower() in ("1", "true", "yes", "on")
# 验证码 LLM 尝试失败后：是否保留浏览器 session/cookies/代理，跳过当前 URL 继续下一个
# pool 模式默认关闭：验证码失败视为代理被屏蔽，需换 IP+指纹
CAPTCHA_KEEP_SESSION = os.environ.get(
    "CAPTCHA_KEEP_SESSION",
    "1" if PROXY_MODE == "static" else "0",
).strip().lower() in ("1", "true", "yes", "on")
# static 模式下连续验证码未能通过多少次后认为代理“烧掉”，停止该 Worker
PROXY_MAX_CONSECUTIVE_CAPTCHA = max(
    1, int(os.environ.get("PROXY_MAX_CONSECUTIVE_CAPTCHA", "3") or "3")
)
HEADLESS = os.environ.get("HEADLESS", "0").strip().lower() in ("1", "true", "yes", "on")
CAPTCHA_WAIT_SECONDS = 120
CAPTCHA_MAX_ROUNDS = 30
CAPTCHA_RECOVERY_ROUNDS = int(os.environ.get("CAPTCHA_RECOVERY_ROUNDS", "2") or "2")
CAPTCHA_MANUAL_PAUSE_SECONDS = int(os.environ.get("CAPTCHA_MANUAL_PAUSE_SECONDS", "8") or "8")
MAX_CAPTCHA_RESTARTS_PER_URL = int(os.environ.get("MAX_CAPTCHA_RESTARTS_PER_URL", "2") or "2")
MAX_NETWORK_RESTARTS_PER_URL = int(os.environ.get("MAX_NETWORK_RESTARTS_PER_URL", "3") or "3")
BROWSER_RESTART_DELAY_SECONDS = int(os.environ.get("BROWSER_RESTART_DELAY_SECONDS", "5") or "5")
# 仅在确认无法获取商品信息（硬失败）时清空 persistent profile
CLEAR_PROFILE_ON_HARD_FAIL = os.environ.get("CLEAR_PROFILE_ON_HARD_FAIL", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
WORKER_COUNT = max(1, int(os.environ.get("WORKER_COUNT", "1") or "1"))
PRODUCT_PACE_SECONDS = float(os.environ.get("PRODUCT_PACE_SECONDS", "0") or "0")

# pool：验证码 1 轮、失败换代理+指纹、约 30s/商品；并发读 WORKER_COUNT（不超过代理数）
if PROXY_MODE == "pool":
    CAPTCHA_KEEP_SESSION = False
    CAPTCHA_RECOVERY_ROUNDS = int(os.environ.get("POOL_CAPTCHA_ROUNDS", "1") or "1")
    MAX_CAPTCHA_RESTARTS_PER_URL = int(os.environ.get("POOL_CAPTCHA_RESTARTS", "0") or "0")
    MAX_NETWORK_RESTARTS_PER_URL = int(os.environ.get("POOL_NETWORK_RESTARTS", "1") or "1")
    if "PRODUCT_PACE_SECONDS" not in os.environ:
        PRODUCT_PACE_SECONDS = 30.0
    # 默认先逛首页预热；验证码一律先走 Grok（CAPTCHA_AUTO_SOLVE）
    SESSION_WARMUP = os.environ.get("SESSION_WARMUP", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    _pool_proxy_n = len(FIXED_PROXY_POOL)
    if _pool_proxy_n <= 0:
        print(
            "[pool] 警告: .env 未配置 POOL_PROXIES（host:port:user:pass，多条用换行或 | 分隔）"
        )
    elif WORKER_COUNT > _pool_proxy_n:
        print(
            f"[pool] WORKER_COUNT={WORKER_COUNT} 超过代理数 {_pool_proxy_n}，"
            f"并发限制为 {_pool_proxy_n}"
        )
    if _pool_proxy_n > 0:
        WORKER_COUNT = max(1, min(WORKER_COUNT, _pool_proxy_n))

# 0 表示不限制；本地试跑可设 MAX_PRODUCTS=1
MAX_PRODUCTS = int(os.environ.get("MAX_PRODUCTS", "0") or "0")
REQUEST_DELAY_MS = (2000, 4000)

# Redis 分布式队列（多机共享任务；单机多窗口也可启用）
# 兼容 .env 里的 REDIS_URL= 或 redis=
REDIS_URL = (os.environ.get("REDIS_URL") or os.environ.get("redis") or "").strip().strip('"').strip("'")
REDIS_ENABLED = bool(REDIS_URL)
REDIS_QUEUE_KEY = os.environ.get("REDIS_QUEUE_KEY", "alixq3:urls").strip() or "alixq3:urls"
REDIS_FP_QUEUE_KEY = os.environ.get("REDIS_FP_QUEUE_KEY", "alixq3:fps").strip() or "alixq3:fps"
# pool 默认优先从 Redis 指纹队列取指纹；无 Redis 或队列空时本地 regenerate
REDIS_FP_ENABLED = os.environ.get(
    "REDIS_FP_ENABLED",
    "1" if PROXY_MODE == "pool" else "0",
).strip().lower() in ("1", "true", "yes", "on")
REDIS_SEEN_KEY = os.environ.get("REDIS_SEEN_KEY", "alixq3:seen").strip() or "alixq3:seen"
REDIS_CLAIM_PREFIX = os.environ.get("REDIS_CLAIM_PREFIX", "alixq3:claim:").strip() or "alixq3:claim:"
REDIS_SEED_LOCK_KEY = os.environ.get("REDIS_SEED_LOCK_KEY", "alixq3:seed_lock").strip() or "alixq3:seed_lock"
REDIS_CLAIM_TTL = max(60, int(os.environ.get("REDIS_CLAIM_TTL", "900") or "900"))
REDIS_SEED_LOCK_TTL = max(60, int(os.environ.get("REDIS_SEED_LOCK_TTL", "7200") or "7200"))
REDIS_BRPOP_TIMEOUT = max(1, int(os.environ.get("REDIS_BRPOP_TIMEOUT", "5") or "5"))
REDIS_IDLE_EXIT_ROUNDS = max(1, int(os.environ.get("REDIS_IDLE_EXIT_ROUNDS", "6") or "6"))
# Redis 模式下队列空时是否一直等待新任务（默认开启；设 0 则灌队结束后空闲退出）
REDIS_WAIT_FOREVER = os.environ.get("REDIS_WAIT_FOREVER", "1").strip().lower() in ("1", "true", "yes", "on")
# 队列空闲后隔多久重新从 ES 灌入「产品索引尚无 doc」的 URL
REDIS_RESEED_IDLE_SECONDS = max(
    10, int(os.environ.get("REDIS_RESEED_IDLE_SECONDS", "60") or "60")
)
REDIS_ROLE = (os.environ.get("REDIS_ROLE", "both") or "both").strip().lower()
if REDIS_ROLE not in ("producer", "consumer", "both"):
    REDIS_ROLE = "both"

# 优先抓取筛选（URL 索引里的列表页指标）
PRIORITY_FIRST = os.environ.get("PRIORITY_FIRST", "1").strip().lower() in ("1", "true", "yes", "on")
PRIORITY_ONLY = os.environ.get("PRIORITY_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
PRIORITY_MAX_PRICE = float(os.environ.get("PRIORITY_MAX_PRICE", "100") or "100")
PRIORITY_MIN_RATING = float(os.environ.get("PRIORITY_MIN_RATING", "4.4") or "4.4")
PRIORITY_MIN_REVIEWS = int(os.environ.get("PRIORITY_MIN_REVIEWS", "1000") or "1000")
PRIORITY_MIN_SOLD = int(os.environ.get("PRIORITY_MIN_SOLD", "1000") or "1000")

# 只抓取 aliexpress.us 链接（跳过 .com）
CRAWL_US_ONLY = os.environ.get("CRAWL_US_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
# 跳过产品索引里已有 doc 的 URL（默认开启，避免重复刷新、_count 只随新 doc 增长）
SKIP_EXISTING_PRODUCTS = os.environ.get("SKIP_EXISTING_PRODUCTS", "1").strip().lower() in ("1", "true", "yes", "on")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {}, app: { isInstalled: false }, csi: () => {}, loadTimes: () => {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
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

ALIEXPRESS_HOME_URL = "https://www.aliexpress.us/"


class BrowserRestartRequired(RuntimeError):
    pass


class CaptchaKeepSessionError(RuntimeError):
    """Captcha unresolved after LLM attempts; keep proxy/session and try next URL."""


class NetworkPageError(RuntimeError):
    """Chrome network error page; do not save product data."""


class StaticProxy:
    """Single endpoint from Webshare-style host:port:user:pass lines."""

    __slots__ = ("host", "port", "username", "password", "index")

    def __init__(
        self,
        host: str,
        port: str,
        username: str,
        password: str,
        index: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.index = index

    def to_playwright(self) -> dict[str, str]:
        return {
            "server": f"http://{self.host}:{self.port}",
            "username": self.username,
            "password": self.password,
        }

    def label(self) -> str:
        return f"#{self.index} {self.host}:{self.port}"


_static_proxies_cache: list[StaticProxy] | None = None
_worker_static_proxies: dict[int, StaticProxy] = {}
_pool_burned: set[str] = set()
_pool_cursor: int = 0
_pool_in_use: dict[int, str] = {}  # worker_id -> host:port (exclusive while active)


def _parse_proxy_line(line: str, index: int) -> StaticProxy | None:
    parts = line.split(":")
    if len(parts) < 4:
        return None
    host, port, username, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
    if not host or not port:
        return None
    return StaticProxy(
        host=host.strip(),
        port=port.strip(),
        username=username.strip(),
        password=password.strip(),
        index=index,
    )


def write_fixed_proxy_pool_file(path: Path | None = None) -> Path:
    """Persist FIXED_PROXY_POOL to disk (under data/, gitignored)."""
    out = path or _DEFAULT_POOL_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(FIXED_PROXY_POOL) + "\n", encoding="utf-8")
    return out


def load_fixed_proxy_pool() -> list[StaticProxy]:
    """Load the pool proxies from ``.env`` ``POOL_PROXIES`` (optionally mirrored to PROXY_FILE)."""
    lines = FIXED_PROXY_POOL or _parse_pool_proxies_env()
    proxies: list[StaticProxy] = []
    for raw in lines:
        proxy = _parse_proxy_line(raw.strip(), len(proxies))
        if proxy is not None:
            proxies.append(proxy)
    if not proxies:
        raise RuntimeError(
            "POOL_PROXIES 为空或格式无效。请在 .env 配置，例如：\n"
            "POOL_PROXIES=\"1.2.3.4:8080:user:pass|5.6.7.8:9090:user:pass\""
        )
    return proxies


def load_static_proxies(path: Path | None = None) -> list[StaticProxy]:
    """Load proxies from ``host:port:user:pass`` text file (one per line)."""
    global _static_proxies_cache
    if PROXY_MODE == "pool" and path is None:
        if _static_proxies_cache is not None:
            return _static_proxies_cache
        proxies = load_fixed_proxy_pool()
        try:
            write_fixed_proxy_pool_file(PROXY_FILE if PROXY_FILE.name else _DEFAULT_POOL_FILE)
        except OSError:
            pass
        _static_proxies_cache = proxies
        return proxies

    proxy_path = path or PROXY_FILE
    if _static_proxies_cache is not None and path is None:
        return _static_proxies_cache

    if not proxy_path.exists():
        raise FileNotFoundError(f"代理文件不存在: {proxy_path}")

    proxies = []
    for line_no, raw in enumerate(
        proxy_path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        proxy = _parse_proxy_line(line, len(proxies))
        if proxy is None:
            print(f"[代理] 跳过格式异常行 {line_no}: {line[:40]}")
            continue
        proxies.append(proxy)

    if not proxies:
        raise RuntimeError(f"代理文件为空或格式无效: {proxy_path}")
    if path is None:
        _static_proxies_cache = proxies
    return proxies


def _proxy_endpoint(proxy: StaticProxy) -> str:
    return f"{proxy.host}:{proxy.port}"


def _pool_endpoints_in_use_by_others(worker_id: int) -> set[str]:
    return {ep for wid, ep in _pool_in_use.items() if wid != worker_id}


def assign_pool_proxy(worker_id: int = 0) -> StaticProxy:
    """Give worker an exclusive non-burned pool proxy (multi-worker safe)."""
    global _pool_cursor
    proxies = load_static_proxies()
    others = _pool_endpoints_in_use_by_others(worker_id)

    def pick_from(candidates: list[StaticProxy]) -> StaticProxy | None:
        if not candidates:
            return None
        start = _pool_cursor % len(proxies)
        ordered = sorted(candidates, key=lambda p: (p.index - start) % len(proxies))
        return ordered[0]

    free = [
        p
        for p in proxies
        if _proxy_endpoint(p) not in _pool_burned and _proxy_endpoint(p) not in others
    ]
    proxy = pick_from(free)
    if proxy is None:
        # Prefer sharing a burned-reset over taking a peer's live proxy.
        if len(_pool_burned) >= len(proxies):
            print("[代理池] 全部代理都曾被屏蔽，重置屏蔽名单后继续")
            _pool_burned.clear()
        free = [
            p
            for p in proxies
            if _proxy_endpoint(p) not in _pool_burned and _proxy_endpoint(p) not in others
        ]
        proxy = pick_from(free) or pick_from(
            [p for p in proxies if _proxy_endpoint(p) not in others]
        ) or proxies[worker_id % len(proxies)]

    _pool_cursor = proxy.index
    _worker_static_proxies[worker_id] = proxy
    _pool_in_use[worker_id] = _proxy_endpoint(proxy)
    return proxy


def get_static_proxy_for_worker(worker_id: int = 0) -> StaticProxy:
    """Assign a fixed proxy to a worker (PROXY_INDEX + worker_id), or exclusive pool proxy."""
    if worker_id in _worker_static_proxies:
        return _worker_static_proxies[worker_id]
    proxies = load_static_proxies()
    if PROXY_MODE == "pool":
        return assign_pool_proxy(worker_id)
    idx = (PROXY_INDEX + worker_id) % len(proxies)
    proxy = proxies[idx]
    _worker_static_proxies[worker_id] = proxy
    return proxy


def prepare_pool_fingerprints(*, force_regenerate: bool = False) -> None:
    """Ensure each pool proxy has a distinct persisted fingerprint (local fallback)."""
    if PROXY_MODE != "pool" or not FINGERPRINT_ENABLED:
        return
    keys = [f"proxy:{_proxy_endpoint(p)}" for p in load_fixed_proxy_pool()]
    fps = ensure_diverse_fingerprints(
        keys, timezone_id="America/New_York", force_regenerate=force_regenerate
    )
    print(f"[指纹池] 已为 {len(fps)} 个代理准备差异化指纹：")
    for fp in fps:
        print(f"  - {fp.label()}")


def bind_fingerprint_for_proxy(proxy: StaticProxy, *, prefer_redis: bool = True):
    """Bind a fingerprint to ``proxy`` — Redis queue first, else local regenerate/create."""
    key = f"proxy:{_proxy_endpoint(proxy)}"
    if prefer_redis and REDIS_ENABLED and REDIS_FP_ENABLED:
        try:
            rq = get_redis_queue()
            raw = rq.pop_fingerprint(timeout=0)
            if raw is not None:
                fp = rebind_and_save_fingerprint(
                    fingerprint_from_dict(raw, key=key, timezone_id="America/New_York"),
                    key,
                )
                print(
                    f"[指纹队列] 已领取 Redis 指纹 → {fp.label()} "
                    f"(剩余 {rq.fingerprint_queue_length()})"
                )
                return fp
            print("[指纹队列] 为空，回退本地生成指纹")
        except Exception as exc:
            print(f"[指纹队列] 领取失败，回退本地: {exc}")
    if FINGERPRINT_ENABLED:
        return regenerate_fingerprint(key, timezone_id="America/New_York")
    return None


def rotate_pool_proxy(worker_id: int = 0, *, reason: str = "") -> StaticProxy | None:
    """Mark current pool proxy burned, assign next exclusive proxy + Redis/local fingerprint."""
    if PROXY_MODE != "pool":
        return None

    proxies = load_static_proxies()
    current = _worker_static_proxies.get(worker_id)
    if current is not None:
        burned_key = _proxy_endpoint(current)
        _pool_burned.add(burned_key)
        _pool_in_use.pop(worker_id, None)
        _worker_static_proxies.pop(worker_id, None)
        print(
            f"[代理池] 标记屏蔽: {current.label()}"
            + (f" | 原因: {reason}" if reason else "")
            + f" | 已屏蔽 {len(_pool_burned)}/{len(proxies)}"
        )

    next_proxy = assign_pool_proxy(worker_id)
    if FINGERPRINT_ENABLED:
        fp = bind_fingerprint_for_proxy(next_proxy, prefer_redis=True)
        print(
            f"[代理池] 切换到 {next_proxy.label()}"
            + (f" | 新指纹 {fp.label()}" if fp is not None else "")
        )
    else:
        print(f"[代理池] 切换到 {next_proxy.label()}")
    return next_proxy


class RedisUrlQueue:
    """Shared URL queue + product_id claim locks for multi-machine crawls."""

    def __init__(self, url: str = REDIS_URL) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise SystemExit(
                "已配置 REDIS_URL，但未安装 redis 包。请运行: pip install redis"
            ) from exc
        # Old Redis servers may not support RESP3 HELLO; force protocol 2.
        self.client = redis_lib.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=30,
            protocol=2,
        )
        self.client.ping()

    def queue_length(self) -> int:
        return int(self.client.llen(REDIS_QUEUE_KEY))

    def seen_count(self) -> int:
        return int(self.client.scard(REDIS_SEEN_KEY))

    def try_acquire_seed_lock(self) -> bool:
        return bool(
            self.client.set(REDIS_SEED_LOCK_KEY, "1", nx=True, ex=REDIS_SEED_LOCK_TTL)
        )

    def release_seed_lock(self) -> None:
        try:
            self.client.delete(REDIS_SEED_LOCK_KEY)
        except Exception:
            pass

    def enqueue(self, url: str) -> bool:
        """Add URL once. Returns True if newly queued."""
        if not url:
            return False
        if self.client.sadd(REDIS_SEEN_KEY, url) != 1:
            return False
        self.client.lpush(REDIS_QUEUE_KEY, url)
        return True

    def force_enqueue(self, url: str) -> bool:
        """Ensure URL is on the queue even if it was seen before.

        Used for not-yet-crawled URLs that were marked seen after a pop/skip
        but never landed in the products index. Dedupes existing list entries.
        """
        if not url:
            return False
        pipe = self.client.pipeline(transaction=True)
        pipe.sadd(REDIS_SEEN_KEY, url)
        pipe.lrem(REDIS_QUEUE_KEY, 0, url)
        pipe.lpush(REDIS_QUEUE_KEY, url)
        pipe.execute()
        return True

    def is_seen(self, url: str) -> bool:
        if not url:
            return False
        return bool(self.client.sismember(REDIS_SEEN_KEY, url))

    def mark_seen(self, url: str) -> None:
        if url:
            self.client.sadd(REDIS_SEEN_KEY, url)

    def blocking_pop(self, timeout: int = REDIS_BRPOP_TIMEOUT) -> str | None:
        result = self.client.brpop(REDIS_QUEUE_KEY, timeout=timeout)
        if not result:
            return None
        return str(result[1])

    def requeue(self, url: str) -> None:
        if url:
            self.client.lpush(REDIS_QUEUE_KEY, url)

    def claim_product(self, product_id: str) -> bool:
        if not product_id:
            return True
        key = f"{REDIS_CLAIM_PREFIX}{product_id}"
        return bool(self.client.set(key, "1", nx=True, ex=REDIS_CLAIM_TTL))

    def release_product(self, product_id: str) -> None:
        if not product_id:
            return
        try:
            self.client.delete(f"{REDIS_CLAIM_PREFIX}{product_id}")
        except Exception:
            pass

    def fingerprint_queue_length(self) -> int:
        return int(self.client.llen(REDIS_FP_QUEUE_KEY))

    def push_fingerprint(self, fp_dict: dict[str, Any]) -> int:
        """LPUSH one fingerprint JSON; returns new queue length."""
        payload = json.dumps(fp_dict, ensure_ascii=False)
        return int(self.client.lpush(REDIS_FP_QUEUE_KEY, payload))

    def push_fingerprints(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return self.fingerprint_queue_length()
        pipe = self.client.pipeline(transaction=False)
        for item in items:
            pipe.lpush(REDIS_FP_QUEUE_KEY, json.dumps(item, ensure_ascii=False))
        pipe.execute()
        return self.fingerprint_queue_length()

    def pop_fingerprint(self, timeout: int = 0) -> dict[str, Any] | None:
        """Pop one fingerprint. ``timeout=0`` is non-blocking RPOP; >0 uses BRPOP."""
        if timeout and timeout > 0:
            result = self.client.brpop(REDIS_FP_QUEUE_KEY, timeout=timeout)
            if not result:
                return None
            raw = result[1]
        else:
            raw = self.client.rpop(REDIS_FP_QUEUE_KEY)
            if not raw:
                return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None


_redis_queue: RedisUrlQueue | None = None


def get_redis_queue() -> RedisUrlQueue:
    global _redis_queue
    if _redis_queue is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL 未配置")
        print(f"[Redis] 连接中...")
        _redis_queue = RedisUrlQueue(REDIS_URL)
        print(
            f"[Redis] 已连接 queue={REDIS_QUEUE_KEY} "
            f"len={_redis_queue.queue_length()} seen={_redis_queue.seen_count()} "
            f"fps={_redis_queue.fingerprint_queue_length()}({REDIS_FP_QUEUE_KEY}) "
            f"role={REDIS_ROLE}"
        )
    return _redis_queue


class MissingPriceError(RuntimeError):
    """Available product fetched without a usable price; do not save product data."""


class IncompleteFetchError(RuntimeError):
    """Product page response is incomplete or invalid; retry later."""


def is_browser_closed_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "connection closed" in message
    )


def should_clear_profile_for_error(exc: BaseException | None) -> bool:
    """Only wipe profile when we cannot obtain product info in this session."""
    if not CLEAR_PROFILE_ON_HARD_FAIL or exc is None:
        return False
    return isinstance(
        exc,
        (BrowserRestartRequired, NetworkPageError, MissingPriceError, IncompleteFetchError),
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


def build_browser_proxy(worker_id: int = 0) -> dict[str, str] | None:
    """按 PROXY_MODE 选择 rotate / static / pool / direct。"""
    if PROXY_MODE == "direct":
        return None
    if PROXY_MODE in ("static", "pool"):
        return get_static_proxy_for_worker(worker_id).to_playwright()
    return build_webshare_proxy()


def webshare_proxy_label() -> str:
    proxy = build_webshare_proxy()
    if not proxy:
        return "未启用"
    username = proxy["username"]
    rotate_note = (
        "，硬失败清空 profile 并重启浏览器时会建立新连接并轮换 IP"
        if username.endswith("-rotate")
        else ""
    )
    return f"Webshare {WEBSHARE_HOST}:{WEBSHARE_PORT} (user={username}){rotate_note}"


def browser_proxy_label(worker_id: int = 0) -> str:
    if PROXY_MODE == "direct":
        return "direct 本机出口 IP（无代理）"
    if PROXY_MODE in ("static", "pool"):
        try:
            proxy = get_static_proxy_for_worker(worker_id)
        except Exception as exc:
            return f"{PROXY_MODE} 模式错误: {exc}"
        prefix = "pool" if PROXY_MODE == "pool" else "static"
        return f"{prefix} {proxy.label()} (burned={len(_pool_burned) if PROXY_MODE == 'pool' else 0})"
    return webshare_proxy_label()


def proxy_mode_label() -> str:
    if PROXY_MODE == "direct":
        return "direct 本机出口 IP（无代理）"
    if PROXY_MODE == "pool":
        fp_src = "Redis指纹队列" if (REDIS_ENABLED and REDIS_FP_ENABLED) else "本地指纹"
        return (
            f"pool .env 代理 {len(FIXED_PROXY_POOL)} 条 | workers={WORKER_COUNT} | "
            f"captcha_rounds={CAPTCHA_RECOVERY_ROUNDS} | "
            f"pace≈{PRODUCT_PACE_SECONDS:.0f}s/商品 | "
            f"屏蔽后换代理+{fp_src}"
        )
    if PROXY_MODE == "static":
        return (
            f"static 固定代理 | file={PROXY_FILE} | index={PROXY_INDEX} | "
            f"warmup={'on' if SESSION_WARMUP else 'off'} | "
            f"captcha_keep_session={'on' if CAPTCHA_KEEP_SESSION else 'off'} | "
            f"burn_after={PROXY_MAX_CONSECUTIVE_CAPTCHA} 次连续验证码失败"
        )
    return "rotate (Webshare 网关)"


def captcha_restart_label() -> str:
    if PROXY_MODE == "pool":
        return (
            f"验证码最多 {CAPTCHA_RECOVERY_ROUNDS} 轮；"
            "未通过即判定屏蔽，清空 profile 并切换代理+指纹"
        )
    total = CAPTCHA_RECOVERY_ROUNDS * MAX_CAPTCHA_RESTARTS_PER_URL
    return (
        f"单轮 {CAPTCHA_RECOVERY_ROUNDS} 次验证码尝试，"
        f"单商品最多硬重启浏览器 {MAX_CAPTCHA_RESTARTS_PER_URL} 次"
        f"（合计最多 {total} 次；仅硬失败时清空 profile）"
    )


def profile_policy_label() -> str:
    if CLEAR_PROFILE_ON_HARD_FAIL:
        return "会话复用 persistent profile；仅硬失败（验证码/网络/不完整）时清空"
    return "会话复用 persistent profile；硬失败也不清空 profile"


def resolve_fingerprint_for_worker(worker_id: int = 0):
    """Bind fingerprint to static/pool proxy identity (or worker id for rotate/direct)."""
    if not FINGERPRINT_ENABLED:
        return None
    proxy = None
    proxy_label: str | None = None
    if PROXY_MODE in ("static", "pool"):
        try:
            proxy = get_static_proxy_for_worker(worker_id)
            proxy_label = f"{proxy.host}:{proxy.port}"
        except Exception:
            proxy_label = None
    key = fingerprint_key_for_worker(worker_id, proxy_label)
    # direct: use local US Midwest default (host IP is Spectrum/MO); override via FP store.
    if PROXY_MODE == "direct":
        timezone_id = os.environ.get("FINGERPRINT_TIMEZONE", "America/Chicago").strip() or "America/Chicago"
    elif PROXY_MODE in ("static", "pool") or WEBSHARE_COUNTRY == "us":
        timezone_id = "America/New_York"
    else:
        timezone_id = "UTC"

    # pool: reuse bound FP if present; otherwise claim from Redis fingerprint queue.
    if PROXY_MODE == "pool" and proxy is not None:
        existing = get_stored_fingerprint(key, timezone_id=timezone_id)
        if existing is not None:
            return existing
        claimed = bind_fingerprint_for_proxy(proxy, prefer_redis=True)
        if claimed is not None:
            return claimed
    return load_or_create_fingerprint(key, timezone_id=timezone_id)


def build_chromium_args(worker_id: int = 0, *, viewport: tuple[int, int] | None = None) -> list[str]:
    args = list(CHROMIUM_ARGS)
    # Prefer window size matching fingerprint over --start-maximized (more consistent FP).
    if viewport is not None:
        args = [arg for arg in args if arg != "--start-maximized"]
        args.append(f"--window-size={viewport[0]},{viewport[1]}")
    elif WORKER_COUNT > 1:
        args = [arg for arg in args if arg != "--start-maximized"]
    if not HEADLESS and WORKER_COUNT > 1:
        cols = min(WORKER_COUNT, 3)
        row = worker_id // cols
        col = worker_id % cols
        args.extend(
            [
                f"--window-position={col * 680},{row * 420}",
            ]
        )
        if viewport is None:
            args.append("--window-size=640,400")
    return args


async def launch_browser_context(
    playwright,
    worker_id: int = 0,
    retries: int = 3,
    *,
    clear_profile: bool = False,
):
    """Launch a persistent Chromium context. Profile is wiped only when clear_profile=True."""
    last_error: Exception | None = None
    user_data_dir = worker_user_data_dir(worker_id)
    fingerprint = resolve_fingerprint_for_worker(worker_id)
    for attempt in range(1, retries + 1):
        wipe = clear_profile or attempt > 1
        if wipe:
            clear_browser_user_data(worker_id)
            if attempt > 1:
                print(f"浏览器启动失败后清空 profile 再试 ({attempt}/{retries})")
        else:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            cleanup_profile_locks(user_data_dir)
        try:
            proxy = build_browser_proxy(worker_id)
            use_fp_viewport = fingerprint is not None and FINGERPRINT_ENABLED
            context_kwargs: dict[str, Any] = {
                "headless": HEADLESS,
                "args": build_chromium_args(
                    worker_id,
                    viewport=(
                        (fingerprint.viewport_width, fingerprint.viewport_height)
                        if use_fp_viewport
                        else None
                    ),
                ),
                "ignore_default_args": ["--enable-automation"],
                "user_agent": fingerprint.user_agent if fingerprint else USER_AGENT,
                "locale": fingerprint.locale if fingerprint else "en-US",
            }
            if use_fp_viewport:
                context_kwargs["viewport"] = {
                    "width": fingerprint.viewport_width,
                    "height": fingerprint.viewport_height,
                }
            else:
                context_kwargs["viewport"] = None
                context_kwargs["no_viewport"] = True
            if proxy:
                context_kwargs["proxy"] = proxy
            if fingerprint:
                tz = fingerprint.timezone_id
            elif PROXY_MODE == "direct":
                tz = (
                    os.environ.get("FINGERPRINT_TIMEZONE", "America/Chicago").strip()
                    or "America/Chicago"
                )
            elif PROXY_MODE in ("static", "pool") or WEBSHARE_COUNTRY == "us":
                tz = "America/New_York"
            else:
                tz = None
            # Apply timezone for proxy and direct local runs (skip only when unknown).
            if tz and (proxy or PROXY_MODE == "direct"):
                context_kwargs["timezone_id"] = tz
            context = await playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                **context_kwargs,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            stealth_label = await apply_stealth_and_fingerprint(context, page, fingerprint)
            # Keep legacy init script as last-resort baseline when packages disabled.
            if not STEALTH_ENABLED:
                await context.add_init_script(STEALTH_SCRIPT)
            if fingerprint:
                print(f"[指纹] {fingerprint.label()} | stealth={stealth_label}")
            else:
                print(f"[指纹] 未启用 | stealth={stealth_label}")
            browser = context.browser
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


def build_url_query(*, priority: bool = False, exclude_priority: bool = False) -> dict[str, Any]:
    """Build ES query for the URL index.

    priority=True: price < max, rating/reviews/sold_count >= mins
    exclude_priority=True: everything that does NOT match priority (phase 2)
    """
    base_must: list[dict[str, Any]] = [{"exists": {"field": "url"}}]
    if CRAWL_US_ONLY:
        base_must.append(
            {
                "bool": {
                    "should": [
                        {"term": {"source": "aliexpress.us"}},
                        {"wildcard": {"url": "*aliexpress.us/*"}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    priority_must: list[dict[str, Any]] = [
        {"range": {"price": {"lt": PRIORITY_MAX_PRICE}}},
        {"range": {"rating": {"gte": PRIORITY_MIN_RATING}}},
        {"range": {"reviews": {"gte": PRIORITY_MIN_REVIEWS}}},
        {"range": {"sold_count": {"gte": PRIORITY_MIN_SOLD}}},
    ]
    if priority:
        return {"bool": {"must": base_must + priority_must}}
    if exclude_priority:
        return {
            "bool": {
                "must": base_must,
                "must_not": [{"bool": {"must": priority_must}}],
            }
        }
    return {"bool": {"must": base_must}}


def priority_filter_label() -> str:
    return (
        f"price<{PRIORITY_MAX_PRICE}, rating>={PRIORITY_MIN_RATING}, "
        f"reviews>={PRIORITY_MIN_REVIEWS}, sold_count>={PRIORITY_MIN_SOLD}"
    )


def crawl_scope_label() -> str:
    return "仅 aliexpress.us" if CRAWL_US_ONLY else "aliexpress.com + aliexpress.us"


def is_us_product_url(url: str) -> bool:
    host = urlparse(str(url or "")).netloc.lower()
    return host.endswith("aliexpress.us")


def normalize_crawl_url(url: str) -> str:
    """Ensure US-only crawls stay on aliexpress.us URLs."""
    link = normalize_https_url(str(url or "").strip())
    if not link:
        return link
    if CRAWL_US_ONLY and not is_us_product_url(link):
        parsed = urlparse(link)
        product_id = product_id_from_url(link)
        if product_id:
            return f"https://www.aliexpress.us/item/{product_id}.html"
        return link.replace("aliexpress.com", "aliexpress.us")
    return link


def get_total_link_count(*, priority: bool = False, exclude_priority: bool = False) -> int:
    """获取 Elasticsearch 产品链接索引总数。"""
    base_url = f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}"
    body = {"query": build_url_query(priority=priority, exclude_priority=exclude_priority)}
    count_resp = requests.post(
        f"{base_url}/_count",
        auth=(ES_USER, ES_PASSWORD),
        json=body,
        timeout=30,
    )
    count_resp.raise_for_status()
    return int(count_resp.json().get("count", 0))


def load_link_batch(
    search_after: list[Any] | None = None,
    *,
    priority: bool = False,
    exclude_priority: bool = False,
) -> tuple[list[str], list[Any] | None]:
    """从 Elasticsearch 产品链接索引读取一批 URL。"""
    base_url = f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}"
    auth = (ES_USER, ES_PASSWORD)

    search_body = {
        "_source": ["url", "product_id"],
        "size": URLS_BATCH_SIZE,
        "query": build_url_query(priority=priority, exclude_priority=exclude_priority),
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
        link = normalize_crawl_url(str(source.get("url") or "").strip())
        if not link or link in seen:
            continue
        if CRAWL_US_ONLY and not is_us_product_url(link):
            continue
        seen.add(link)
        links.append(link)

    next_search_after = hits[-1].get("sort") if hits else None
    return links, next_search_after


async def crawl_link_phases(
    state: "CrawlState",
    products_path: Path,
    invalid_path: Path,
) -> None:
    """Crawl priority URLs first, then remaining URLs when configured."""
    phases: list[tuple[str, bool, bool]] = []
    if PRIORITY_FIRST or PRIORITY_ONLY:
        phases.append(("优先筛选", True, False))
        if not PRIORITY_ONLY:
            phases.append(("其余商品", False, True))
    else:
        phases.append(("全部链接", False, False))

    batch_no = 0
    for phase_name, priority, exclude_priority in phases:
        if await state.should_stop():
            break
        total_links = get_total_link_count(priority=priority, exclude_priority=exclude_priority)
        print(f"[ES链接] 阶段「{phase_name}」: {total_links} 条")
        if priority or (PRIORITY_FIRST and not exclude_priority):
            print(f"[ES链接] 筛选条件: {priority_filter_label()}")
        if total_links <= 0:
            print(f"[ES链接] 阶段「{phase_name}」无待抓链接，跳过。")
            continue

        fetched_count = 0
        search_after: list[Any] | None = None
        phase_batches = 0
        while True:
            if await state.should_stop():
                break
            links, search_after = load_link_batch(
                search_after,
                priority=priority,
                exclude_priority=exclude_priority,
            )
            if not links:
                if phase_batches == 0:
                    print(f"[ES链接] 阶段「{phase_name}」没有找到商品链接。")
                break
            batch_no += 1
            phase_batches += 1
            batch_start = fetched_count
            fetched_count += len(links)
            print(
                f"[ES链接] [{phase_name}] 第 {batch_no} 批读取 {len(links)} 条，"
                f"本阶段累计 {fetched_count}/{total_links}"
            )
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
            print(f"[ES链接] [{phase_name}] 第 {batch_no} 批处理完成，准备读取下一批。")

        if PRIORITY_ONLY:
            break
        if await state.should_stop():
            break
        if priority and not PRIORITY_ONLY:
            print("[ES链接] 优先筛选阶段结束，开始抓取其余商品。")


def product_exists_in_es(url: str) -> bool:
    """Check whether the product doc already exists in the product index."""
    product_id = product_id_from_url(url)
    if not product_id:
        return False
    source = source_from_url(normalize_crawl_url(url))
    doc_id = product_doc_id(source, product_id)
    check_url = f"http://{ES_HOST}:{ES_PORT}/{PRODUCT_INDEX_NAME}/_doc/{doc_id}"
    try:
        resp = requests.head(check_url, auth=(ES_USER, ES_PASSWORD), timeout=15)
        return resp.status_code == 200
    except Exception:
        return False


def products_exist_in_es_batch(urls: list[str], *, chunk_size: int = 500) -> dict[str, bool]:
    """Batch-check product docs via ES _mget. Missing/invalid URLs map to False."""
    result = {u: False for u in urls}
    docs: list[tuple[str, str]] = []
    for url in urls:
        product_id = product_id_from_url(url)
        if not product_id:
            continue
        source = source_from_url(normalize_crawl_url(url))
        docs.append((url, product_doc_id(source, product_id)))
    if not docs:
        return result

    mget_url = f"http://{ES_HOST}:{ES_PORT}/{PRODUCT_INDEX_NAME}/_mget"
    auth = (ES_USER, ES_PASSWORD)
    for i in range(0, len(docs), chunk_size):
        chunk = docs[i : i + chunk_size]
        try:
            resp = requests.post(
                mget_url,
                auth=auth,
                json={"ids": [doc_id for _, doc_id in chunk]},
                timeout=120,
            )
            resp.raise_for_status()
            for (url, _), doc in zip(chunk, resp.json().get("docs") or []):
                result[url] = bool(doc.get("found"))
        except Exception as exc:
            print(f"[ES] _mget 批量检查失败，本批按不存在处理: {exc}")
    return result


def upload_product(product: dict[str, Any]) -> str:
    """Upload product to ES. Returns: created | updated | skipped | failed."""
    if is_invalid_product_record(product):
        title = str(product.get("title") or "")[:80]
        print(f"  [上传跳过] 无效商品数据，不上传 ES: {title or product.get('product_id')}")
        return "skipped"

    product_id = str(product.get("product_id") or product_id_from_url(str(product.get("url") or ""))).strip()
    if not product_id:
        print("  [上传失败] 缺少 product_id")
        return "failed"

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
            result = str(resp.json().get("result") or ("created" if resp.status_code == 201 else "updated"))
            if result not in ("created", "updated"):
                result = "updated"
            note = "新建 doc，_count +1" if result == "created" else "更新已有 doc，_count 不变"
            print(f"  [上传成功] 索引={PRODUCT_INDEX_NAME} id={doc_id} ({result}，{note})")
            return result
        print(f"  [上传失败] id={doc_id} status={resp.status_code} body={resp.text[:300]}")
    except Exception as exc:
        print(f"  [上传失败] id={doc_id} error={exc}")
    return "failed"


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


def pick_price_from_body_text(body_text: str) -> float:
    """Fallback: parse the first USD price from visible page text."""
    text = str(body_text or "")
    if not text:
        return 0.0
    patterns = (
        r"\$\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)",
        r"US\s*\$\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)",
        r"USD\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = to_float(match.group(1))
            if value > 0:
                return value
    return 0.0


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
BROWSER_NETWORK_ERROR_MARKERS = (
    "this site can't be reached",
    "this page isn't working",
    "this webpage is not available",
    "this site is unreachable",
    "err_connection_refused",
    "err_connection_reset",
    "err_connection_timed_out",
    "err_connection_closed",
    "err_proxy_connection_failed",
    "err_name_not_resolved",
    "err_timed_out",
    "err_internet_disconnected",
    "err_address_unreachable",
    "err_network_changed",
    "err_ssl_protocol_error",
    "no internet",
    "dns_probe_finished",
)
BROWSER_NETWORK_ERROR_TITLE_RE = re.compile(
    r"this site can.?t be reached|this page isn.?t working|this webpage is not available",
    re.I,
)


def normalize_match_text(text: str) -> str:
    """Normalize text for marker matching (Chrome often uses curly apostrophes)."""
    normalized = str(text or "").lower()
    for ch in ("\u2019", "\u2018", "\u02bc", "\u0060"):
        normalized = normalized.replace(ch, "'")
    return re.sub(r"\s+", " ", normalized).strip()


def is_browser_network_error_page(*, title: str, page_text: str, page_url: str) -> bool:
    """Detect Chromium built-in network error pages (not AliExpress content)."""
    if str(page_url or "").lower().startswith("chrome-error://"):
        return True
    combined = normalize_match_text(f"{title}\n{page_text}")
    if any(marker in combined for marker in BROWSER_NETWORK_ERROR_MARKERS):
        return True
    return bool(BROWSER_NETWORK_ERROR_TITLE_RE.search(combined))


def is_missing_available_price(record: dict[str, Any]) -> bool:
    """True when a product looks available but no price was extracted."""
    return bool(record.get("existence")) and float(record.get("price") or 0) <= 0


def raise_if_missing_available_price(record: dict[str, Any], *, url: str) -> None:
    if is_missing_available_price(record):
        raise MissingPriceError(f"商品可售但价格为 0，不保存: {url}")


def is_invalid_product_record(record: dict[str, Any]) -> bool:
    """Product payload that must not be saved or uploaded (network errors, incomplete fetches)."""
    title = str(record.get("title") or "")
    description = str(record.get("description") or "")
    url = str(record.get("url") or "")
    if is_browser_network_error_page(title=title, page_text=description, page_url=url):
        return True
    if record.get("existence") and (not title or title == "ERROR" or is_generic_page_title(title)):
        return True
    return is_missing_available_price(record)


def is_successful_product_record(record: dict[str, Any]) -> bool:
    if is_invalid_product_record(record):
        return False
    if not record.get("existence"):
        return False
    if float(record.get("price") or 0) <= 0:
        return False
    title = str(record.get("title") or "")
    if not title or title == "ERROR" or is_generic_page_title(title):
        return False
    return True


def is_genuine_unavailable_record(record: dict[str, Any]) -> bool:
    if record.get("existence"):
        return False
    title = str(record.get("title") or "")
    return title.startswith(MISSING_PRODUCT_TITLE_PREFIX)


def should_save_superseded_redirect(primary: dict[str, Any]) -> bool:
    """Only mark the original .com URL superseded after a real fetch result."""
    return is_successful_product_record(primary) or is_genuine_unavailable_record(primary)


def raise_if_incomplete_fetch_record(
    record: dict[str, Any],
    *,
    url: str,
    page_text: str = "",
    page_url: str = "",
) -> None:
    title = str(record.get("title") or "")
    if is_browser_network_error_page(title=title, page_text=page_text, page_url=page_url):
        raise NetworkPageError(f"浏览器网络错误页，不保存商品: {title or url}")
    if is_missing_available_price(record):
        raise MissingPriceError(f"商品可售但价格为 0，不保存: {url}")
    if not title or title == "ERROR":
        raise IncompleteFetchError(f"未提取到有效标题，不保存: {url}")
    if record.get("existence") and is_generic_page_title(title):
        raise IncompleteFetchError(f"页面标题无效，不保存: {title}")


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
    if redirect_info and should_save_superseded_redirect(record):
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


def image_dedupe_key(url: str) -> str:
    """Normalize image URL for deduplication (ignore query params and size suffixes)."""
    text = normalize_image_url(str(url or "").strip())
    if not text:
        return ""
    text = text.split("?", 1)[0]
    text = re.sub(r"_\.avif$", "", text, flags=re.I)
    text = re.sub(r"_(?:80x80|120x120|220x220|960x960)q75\.jpg.*$", "", text, flags=re.I)
    return text.lower()


def collect_product_images(
    api_data: dict[str, Any] | None,
    ld_product: dict[str, Any],
    dom_data: dict[str, Any],
) -> str:
    """Pick product images with API > DOM > LD+JSON fallback and URL dedup."""
    sources: list[list[str]] = []

    if api_data:
        api_imgs = parse_images_from_api(api_data)
        if api_imgs != PLACEHOLDER_IMAGE:
            sources.append([part.strip() for part in api_imgs.split(";") if part.strip()])

    dom_imgs = [
        normalize_image_url(str(img or ""))
        for img in (dom_data.get("images") or [])
        if normalize_image_url(str(img or ""))
    ]
    if dom_imgs:
        sources.append(dom_imgs)

    if ld_product.get("image"):
        ld_imgs = ld_product["image"] if isinstance(ld_product["image"], list) else [ld_product["image"]]
        ld_normalized = [normalize_image_url(str(img or "")) for img in ld_imgs if normalize_image_url(str(img or ""))]
        if ld_normalized:
            sources.append(ld_normalized)

    for candidates in sources:
        unique: list[str] = []
        seen: set[str] = set()
        for img in candidates:
            key = image_dedupe_key(img)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(normalize_image_url(img))
        if unique:
            return ";".join(unique)

    return PLACEHOLDER_IMAGE


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
    if price <= 0:
        price = pick_price_from_body_text(str(dom_data.get("bodyText") or ""))

    # ---- currency: LD+JSON > API ----
    currency = "USD"
    if ld_product.get("offers"):
        currency = (ld_product["offers"].get("priceCurrency") or "USD").upper()
    if currency == "USD" and api_result:
        currency = get_currency_from_api(api_data) if api_data else "USD"

    # ---- images: API > DOM > LD+JSON (deduped) ----
    images = collect_product_images(api_data, ld_product, dom_data)

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

    page_ready = bool(title and title != "ERROR") and not is_generic_page_title(title)
    if page_ready and is_browser_network_error_page(title=title, page_text=description, page_url=url):
        page_ready = False

    return {
        "date": datetime.now().replace(microsecond=0).isoformat(),
        "url": normalize_https_url(url),
        "source": source,
        "product_id": pid,
        "existence": page_ready,
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


async def raise_if_network_error_page(page: Page) -> None:
    """Raise when the browser shows a network error page instead of the product."""
    try:
        page_url = page.url or ""
        title = await page.title()
        page_text = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 4000) : ''"
        )
        if is_browser_network_error_page(
            title=str(title or ""),
            page_text=str(page_text or ""),
            page_url=page_url,
        ):
            label = str(title or "").strip() or page_url[:120]
            raise NetworkPageError(f"浏览器网络错误页，不保存商品: {label}")
    except NetworkPageError:
        raise
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserRestartRequired("浏览器已关闭，重新启动") from exc


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


async def pace_after_product() -> None:
    """Slow crawl cadence after a successful product (~30s in pool mode)."""
    if PRODUCT_PACE_SECONDS <= 0:
        await sleep()
        return
    jitter = random.uniform(-3.0, 5.0)
    delay = max(15.0, PRODUCT_PACE_SECONDS + jitter)
    print(f"[节奏] 等待 {delay:.1f}s 后再抓下一个商品…")
    await asyncio.sleep(delay)

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
            print("[验证码识别] 先用 Grok 自动识别验证码（单轮）。")
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

    raise (
        CaptchaKeepSessionError(
            f"验证码处理 {CAPTCHA_RECOVERY_ROUNDS} 轮后仍未进入商品页；"
            "保持 session/cookies/代理，跳过当前 URL"
        )
        if CAPTCHA_KEEP_SESSION
        else BrowserRestartRequired(
            f"验证码处理 {CAPTCHA_RECOVERY_ROUNDS} 轮后仍未进入商品页，需要硬重启浏览器"
        )
    )


async def _dismiss_aliexpress_popups(page: Page, *, worker_id: int = 0) -> None:
    selectors = (
        "button:has-text('Accept')",
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "[class*='close--']",
        "[aria-label='Close']",
        ".pop-close-btn",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                if HUMAN_MOUSE_ENABLED:
                    await human_click_locator(page, loc, worker_id=worker_id, timeout=1500)
                else:
                    await loc.click(timeout=1500)
                await asyncio.sleep(0.4)
        except Exception:
            continue


async def warmup_aliexpress_session(
    page: Page,
    captcha_state: dict[str, bool],
    *,
    worker_id: int = 0,
) -> None:
    """Browse homepage → category → product to warm cookies/session under a fixed proxy."""
    print("[预热] 打开 AliExpress 首页…")
    try:
        await page.goto(ALIEXPRESS_HOME_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        await _dismiss_aliexpress_popups(page, worker_id=worker_id)
        if await is_captcha_page_visible(page):
            print("[预热] 首页出现验证码，先用 Grok 尝试通过…")
            await handle_captcha(page, captcha_state, product_url=ALIEXPRESS_HOME_URL)
        await human_scroll(page, 1200, worker_id=worker_id)
        await human_idle(page, worker_id=worker_id, seconds=random.uniform(0.8, 1.6))
    except BrowserRestartRequired:
        raise
    except CaptchaKeepSessionError:
        print("[预热] 首页验证码未能通过，仍继续后续预热步骤（保留 session）")
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserRestartRequired("预热首页时浏览器已关闭") from exc
        print(f"[预热] 首页访问异常: {exc}")

    # Category / channel links (never confuse with product /item/ pages)
    category_href: str | None = None
    try:
        category_href = await page.evaluate(
            """() => {
              const links = Array.from(document.querySelectorAll('a[href]'));
              const isCategory = (href) =>
                /aliexpress\\.us\\/(category\\/|c\\/|w\\/wholesale-|ssr\\/category)/i.test(href)
                && !/\\/item\\//i.test(href);
              const preferred = links.find((a) => isCategory(a.href || ''));
              if (preferred) return preferred.href;
              const any = links.find((a) => {
                const href = a.href || '';
                if (!isCategory(href) && !/aliexpress\\.us\\/(all-wholesale|store)/i.test(href)) return false;
                if (/\\/item\\//i.test(href)) return false;
                const t = (a.innerText || '').trim().toLowerCase();
                return t.length > 0;
              });
              return any ? any.href : null;
            }"""
        )
    except Exception:
        category_href = None

    if not category_href:
        # Stable fallback category so warmup still exercises navigation + cookies
        category_href = "https://www.aliexpress.us/w/wholesale-electronics.html"
        print(f"[预热] 未发现站内分类链接，使用备用分类: {category_href}")
    else:
        print(f"[预热] 打开分类页: {category_href[:120]}")

    try:
        await page.goto(category_href, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2000)
        await _dismiss_aliexpress_popups(page, worker_id=worker_id)
        if await is_captcha_page_visible(page):
            print("[预热] 分类页出现验证码，先用 Grok 尝试通过…")
            await handle_captcha(page, captcha_state, product_url=category_href)
        await human_scroll(page, 1600, worker_id=worker_id)
        await human_idle(page, worker_id=worker_id, seconds=random.uniform(1.0, 2.0))
    except BrowserRestartRequired:
        raise
    except CaptchaKeepSessionError:
        print("[预热] 分类页验证码未能通过，继续尝试商品页")
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserRestartRequired("预热分类页时浏览器已关闭") from exc
        print(f"[预热] 分类页异常: {exc}")

    product_href: str | None = None
    try:
        product_href = await page.evaluate(
            """() => {
              const a = Array.from(document.querySelectorAll('a[href*="/item/"]'))
                .find((el) => /\\/item\\/\\d+/i.test(el.href || ''));
              return a ? a.href : null;
            }"""
        )
    except Exception:
        product_href = None

    if product_href:
        print(f"[预热] 打开商品页: {product_href[:120]}")
        try:
            # Prefer human click if the product card is visible on the current page.
            clicked = False
            if HUMAN_MOUSE_ENABLED:
                try:
                    card = page.locator('a[href*="/item/"]').first
                    if await card.count() > 0 and await card.is_visible():
                        clicked = await human_click_locator(page, card, worker_id=worker_id)
                except Exception:
                    clicked = False
            if not clicked:
                await page.goto(product_href, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            await _dismiss_aliexpress_popups(page, worker_id=worker_id)
            if await is_captcha_page_visible(page):
                print("[预热] 商品页出现验证码，先用 Grok 尝试通过…")
                await handle_captcha(page, captcha_state, product_url=product_href)
            await human_scroll(page, 900, worker_id=worker_id)
            await human_idle(page, worker_id=worker_id, seconds=random.uniform(0.8, 1.5))
        except BrowserRestartRequired:
            raise
        except CaptchaKeepSessionError:
            print("[预热] 商品页验证码未能通过，预热结束（保留 session）")
        except Exception as exc:
            if is_browser_closed_error(exc):
                raise BrowserRestartRequired("预热商品页时浏览器已关闭") from exc
            print(f"[预热] 商品页异常: {exc}")
    else:
        print("[预热] 未找到商品链接，跳过商品页预热")

    print("[预热] cookies/session 预热完成，开始从队列抓取")


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
          const priceEl = document.querySelector(
            '[class*="current--"], [class*="current--"] span, [class*="price-default--"], [class*="price--"] span, [data-pl="product-price"]'
          );
          let priceText = priceEl ? priceEl.innerText.trim() : '';
          if (!priceText) {
            const priceMatch = bodyText.match(/(?:US\\s*)?\\$\\s*\\d+(?:\\.\\d{1,2})?/);
            if (priceMatch) priceText = priceMatch[0];
          }
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
          return { title, priceText, bodyText, categories: breadItems, rating, reviews, soldCount, skuOptions };
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
    except CaptchaKeepSessionError:
        raise
    except NetworkPageError:
        raise
    except MissingPriceError:
        raise
    except IncompleteFetchError:
        raise
    except Exception as exc:
        print(f"  [API] 页面加载失败，降级使用 LD+JSON: {exc}")

    await raise_if_network_error_page(page)

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
        if price_component:
            existing_price_component = api_result.get("priceComponent") or {}
            if isinstance(existing_price_component, dict):
                api_result["priceComponent"] = {**existing_price_component, **price_component}
            else:
                api_result["priceComponent"] = price_component
        if price_component.get("skuPriceList"):
            api_result.setdefault("skuModule", {})
            if isinstance(api_result["skuModule"], dict):
                api_result["skuModule"]["skuPriceList"] = price_component["skuPriceList"]
        price_module = run_params.get("priceModule") or {}
        if price_module and not api_result.get("priceModule"):
            api_result["priceModule"] = price_module

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
    await raise_if_network_error_page(page)
    await raise_if_risk_control_page(page)

    record = build_standard_record(api_data, ld_json_list, dom_data, save_url)
    raise_if_incomplete_fetch_record(
        record,
        url=save_url,
        page_text=str(page_text or ""),
        page_url=page.url or "",
    )

    if is_unavailable_product_page(
        api_data=api_data,
        record=record,
        dom_data=dom_data,
        page_text=str(page_text or ""),
    ):
        await raise_if_risk_control_page(page)
        print(f"  重定向目标页不可用（下架/404）: {save_url}")
        return finalize_fetch_records(make_empty_record(save_url, redirect_info=redirect_info), redirect_info)

    validated, error = validate_product_record(record)
    if not validated:
        await raise_if_risk_control_page(page)
        raise IncompleteFetchError(f"格式校验失败，不保存: {error}")

    raise_if_incomplete_fetch_record(
        validated,
        url=save_url,
        page_text=str(page_text or ""),
        page_url=page.url or "",
    )

    validated = apply_redirect_metadata(validated, redirect_info)
    if redirect_info:
        print(
            f"  已按重定向商品保存: [{validated.get('source')}] {validated.get('product_id')} "
            f"(原请求 [{redirect_info['original_source']}] {redirect_info['original_product_id']})"
        )
        if should_save_superseded_redirect(validated):
            print(
                f"  已标记原 URL 不存在: [{redirect_info['original_source']}] "
                f"{redirect_info['original_product_id']}"
            )
    return finalize_fetch_records(validated, redirect_info)


class CrawlState:
    def __init__(self, processed_urls: set[str], redis_q: RedisUrlQueue | None = None):
        self.processed_urls = processed_urls
        self.redis_q = redis_q
        self.in_progress: set[str] = set()
        self.in_progress_product_ids: set[str] = set()
        self.lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self.stats_lock = asyncio.Lock()
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.completed = 0
        self.es_created = 0
        self.es_updated = 0
        self.es_upload_failed = 0
        self.es_skipped_existing = 0
        self._force_stop = False

    def request_stop(self, reason: str = "") -> None:
        self._force_stop = True
        if reason:
            print(f"[停止] {reason}")

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
            if SKIP_EXISTING_PRODUCTS and product_exists_in_es(url):
                return False, "already_in_es"
            if self.redis_q is not None and product_id:
                claimed = await asyncio.to_thread(self.redis_q.claim_product, product_id)
                if not claimed:
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
        if self.redis_q is not None and product_id:
            await asyncio.to_thread(self.redis_q.release_product, product_id)

    async def finish_url(self, url: str) -> None:
        product_id = product_id_from_url(url)
        async with self.lock:
            self.in_progress.discard(url)
            if product_id:
                self.in_progress_product_ids.discard(product_id)
            self.processed_urls.add(url)
            save_progress(self.processed_urls)
        if self.redis_q is not None and product_id:
            await asyncio.to_thread(self.redis_q.release_product, product_id)

    async def mark_skipped(self, *, already_in_es: bool = False) -> None:
        async with self.stats_lock:
            self.skipped += 1
            if already_in_es:
                self.es_skipped_existing += 1

    async def mark_es_upload(self, result: str) -> None:
        async with self.stats_lock:
            if result == "created":
                self.es_created += 1
            elif result == "updated":
                self.es_updated += 1
            elif result == "failed":
                self.es_upload_failed += 1

    async def mark_url_done(self, *, success: bool = False, failed: bool = False) -> None:
        async with self.stats_lock:
            self.completed += 1
            if success:
                self.success += 1
            if failed:
                self.failed += 1

    async def should_stop(self) -> bool:
        if self._force_stop:
            return True
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
    ) -> dict[str, bool | str | None]:
        saved_primary = False
        primary_success = False
        primary_failed = False
        primary_uploaded = False
        primary_upload_result: str | None = None
        async with self.file_lock:
            with products_path.open("a", encoding="utf-8") as products_fh, invalid_path.open(
                "a", encoding="utf-8"
            ) as invalid_fh:
                for record_idx, product in enumerate(product_records):
                    validated, validation_error = validate_product_record(product)
                    if validated and is_invalid_product_record(validated):
                        invalid_fh.write(
                            json.dumps(
                                {
                                    "url": url,
                                    "product_id": validated.get("product_id") or product_id_from_url(url),
                                    "error": "无效商品数据（网络错误页或不完整抓取），未写入 ES",
                                    "title": validated.get("title"),
                                    "date": datetime.now().replace(microsecond=0).isoformat(),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        invalid_fh.flush()
                        if record_idx == 0:
                            primary_failed = True
                        print(f"  失败: 无效商品数据，未保存: {validated.get('title')}")
                        continue

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
                    upload_result = upload_product(validated)
                    await self.mark_es_upload(upload_result)
                    if record_idx == 0:
                        saved_primary = True
                        primary_success = bool(validated.get("existence"))
                        if upload_result in ("created", "updated"):
                            primary_uploaded = True
                            primary_upload_result = upload_result
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
        await self.mark_url_done(success=primary_success and primary_uploaded, failed=failed)
        return {
            "primary_uploaded": primary_uploaded,
            "primary_upload_result": primary_upload_result,
            "saved_primary": saved_primary,
            "primary_failed": primary_failed,
        }


UrlTask = tuple[str, int, int, int, int]


async def close_browser_session(
    browser,
    context,
    worker_id: int,
    *,
    clear_profile: bool = False,
) -> None:
    if context:
        try:
            await asyncio.wait_for(context.close(), timeout=8)
        except Exception:
            pass
    elif browser:
        try:
            await asyncio.wait_for(browser.close(), timeout=8)
        except Exception:
            pass
    if clear_profile:
        clear_browser_user_data(worker_id)
    else:
        cleanup_profile_locks(worker_user_data_dir(worker_id))


def _session_alive(context, page) -> bool:
    if context is None or page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


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
    network_restart_counts: dict[str, int] = {}
    pending_task: UrlTask | None = None
    browser = None
    context = None
    page = None
    captcha_state: dict[str, bool] = {"solved_once": False}
    session_warmed = False
    consecutive_captcha_fails = 0
    proxy_success_count = 0
    proxy_label = browser_proxy_label(worker_id)
    print(f"[{worker_label}] 启动 | 代理模式={PROXY_MODE} | {proxy_label}")

    async def shutdown_session(*, clear_profile: bool = False) -> None:
        nonlocal browser, context, page, captcha_state, session_warmed
        await close_browser_session(browser, context, worker_id, clear_profile=clear_profile)
        browser = None
        context = None
        page = None
        captcha_state = {"solved_once": False}
        session_warmed = False

    async def ensure_browser() -> None:
        nonlocal browser, context, page, captcha_state, session_warmed, consecutive_captcha_fails
        if _session_alive(context, page):
            return

        warmup_rotate_attempts = 0
        max_warmup_rotates = len(FIXED_PROXY_POOL) if PROXY_MODE == "pool" else 0
        while True:
            if context is not None or browser is not None:
                await shutdown_session(clear_profile=False)
            browser, context, page = await launch_browser_context(
                playwright,
                worker_id=worker_id,
                clear_profile=False,
            )
            captcha_state = {"solved_once": False}
            session_warmed = False
            print(f"[{worker_label}] 浏览器已打开（代理: {browser_proxy_label(worker_id)}）")
            if not SESSION_WARMUP:
                return
            assert page is not None
            try:
                await warmup_aliexpress_session(page, captcha_state, worker_id=worker_id)
                session_warmed = True
                return
            except BrowserRestartRequired as exc:
                if PROXY_MODE == "pool" and warmup_rotate_attempts < max_warmup_rotates:
                    warmup_rotate_attempts += 1
                    print(
                        f"[{worker_label}] 预热失败，换代理+指纹后重开 "
                        f"({warmup_rotate_attempts}/{max_warmup_rotates}): {exc}"
                    )
                    await shutdown_session(clear_profile=True)
                    rotate_pool_proxy(worker_id, reason=f"warmup:{exc}")
                    await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
                    continue
                raise
            except CaptchaKeepSessionError as exc:
                consecutive_captcha_fails += 1
                print(
                    f"[{worker_label}] 预热阶段验证码未通过，保留 session: {exc} "
                    f"(连续失败 {consecutive_captcha_fails}/{PROXY_MAX_CONSECUTIVE_CAPTCHA})"
                )
                session_warmed = True
                return

    try:
        while True:
            if await state.should_stop():
                break
            if (
                PROXY_MODE == "static"
                and consecutive_captcha_fails >= PROXY_MAX_CONSECUTIVE_CAPTCHA
            ):
                print(
                    f"[{worker_label}] 代理疑似烧掉：连续 {consecutive_captcha_fails} 次验证码失败。"
                    f"本代理成功抓取 {proxy_success_count} 个商品。停止 Worker。"
                )
                state.request_stop(
                    f"{worker_label} 代理烧掉，成功 {proxy_success_count} 个 | {proxy_label}"
                )
                break

            # Claim a URL before opening the browser so idle workers do not sit on about:blank.
            while pending_task is None:
                if await state.should_stop():
                    return
                if (
                    PROXY_MODE == "static"
                    and consecutive_captcha_fails >= PROXY_MAX_CONSECUTIVE_CAPTCHA
                ):
                    state.request_stop(
                        f"{worker_label} 代理烧掉，成功 {proxy_success_count} 个 | {proxy_label}"
                    )
                    return

                item = await task_queue.get()
                if item is None:
                    task_queue.task_done()
                    return

                claimed, claim_reason = await state.claim_url(item[0])
                if not claimed:
                    if claim_reason == "product_busy":
                        if state.redis_q is not None:
                            await asyncio.to_thread(state.redis_q.requeue, item[0])
                            task_queue.task_done()
                        else:
                            await task_queue.put(item)
                            task_queue.task_done()
                        await asyncio.sleep(0.3)
                        continue
                    if claim_reason == "already_in_es":
                        await state.mark_skipped(already_in_es=True)
                        task_queue.task_done()
                        continue
                    await state.mark_skipped()
                    task_queue.task_done()
                    continue
                pending_task = item

            url, index, batch_no, batch_pos, batch_size = pending_task
            try:
                await ensure_browser()
                assert page is not None
                print(
                    f"[{worker_label}] [{index}{'' if total_links <= 0 else f'/{total_links}'}] "
                    f"第 {batch_no} 批 {batch_pos}/{batch_size or '?'} 抓取详情: {url}"
                    + (
                        f" | 本代理已成功 {proxy_success_count}"
                        if PROXY_MODE in ("static", "pool")
                        else ""
                    )
                )
                try:
                    product_records = await fetch_product(page, url, captcha_state)
                    if not product_records:
                        if PROXY_MODE == "pool":
                            raise BrowserRestartRequired("未获取到商品数据，判定代理被屏蔽")
                        if CAPTCHA_KEEP_SESSION:
                            raise CaptchaKeepSessionError("未获取到商品数据，保留 session 换下一 URL")
                        raise BrowserRestartRequired("未获取到商品数据，不保存")

                    save_result = await state.save_product_records(
                        products_path, invalid_path, url, product_records
                    )
                    if save_result["primary_uploaded"]:
                        await state.finish_url(url)
                        consecutive_captcha_fails = 0
                        if save_result.get("primary_uploaded"):
                            proxy_success_count += 1
                            if PROXY_MODE in ("static", "pool"):
                                print(
                                    f"[{worker_label}] 本代理累计成功: {proxy_success_count} "
                                    f"({browser_proxy_label(worker_id)})"
                                )
                    else:
                        await state.append_invalid(
                            invalid_path,
                            {
                                "url": url,
                                "product_id": product_id_from_url(url),
                                "error": "页面已抓取但 ES 未写入（上传失败或记录无效），留待下次重试",
                                "date": datetime.now().replace(microsecond=0).isoformat(),
                            },
                        )
                        await state.release_url(url)
                        print(f"  未写入 ES，URL 未标记为已处理，下次运行可重试: {url}")
                    task_queue.task_done()
                    pending_task = None
                    if save_result.get("primary_uploaded"):
                        await pace_after_product()
                    else:
                        await sleep()
                except CaptchaKeepSessionError as exc:
                    if PROXY_MODE == "pool":
                        # Keep-session is off for pool by default; treat as block just in case.
                        print(f"[{worker_label}] 验证码未通过，切换代理+指纹: {exc}")
                        await state.append_invalid(
                            invalid_path,
                            {
                                "url": url,
                                "product_id": product_id_from_url(url),
                                "error": f"pool 验证码失败换代理: {exc}",
                                "date": datetime.now().replace(microsecond=0).isoformat(),
                                "proxy": browser_proxy_label(worker_id),
                            },
                        )
                        await state.mark_url_done(failed=True)
                        if state.redis_q is not None:
                            await asyncio.to_thread(state.redis_q.requeue, url)
                        await state.release_url(url)
                        task_queue.task_done()
                        pending_task = None
                        await shutdown_session(clear_profile=True)
                        rotate_pool_proxy(worker_id, reason=str(exc))
                        await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
                        continue

                    consecutive_captcha_fails += 1
                    print(
                        f"[{worker_label}] {exc} "
                        f"(连续验证码失败 {consecutive_captcha_fails}/{PROXY_MAX_CONSECUTIVE_CAPTCHA}，"
                        f"本代理已成功 {proxy_success_count})"
                    )
                    await state.append_invalid(
                        invalid_path,
                        {
                            "url": url,
                            "product_id": product_id_from_url(url),
                            "error": f"验证码未通过，保留 session 跳过: {exc}",
                            "date": datetime.now().replace(microsecond=0).isoformat(),
                            "proxy": browser_proxy_label(worker_id),
                            "proxy_success_before_skip": proxy_success_count,
                        },
                    )
                    await state.mark_url_done(failed=True)
                    if state.redis_q is not None:
                        await asyncio.to_thread(state.redis_q.requeue, url)
                    await state.release_url(url)
                    task_queue.task_done()
                    pending_task = None
                    await sleep(short=True)
                except BrowserRestartRequired:
                    raise
                except NetworkPageError:
                    raise
                except MissingPriceError:
                    raise
                except IncompleteFetchError:
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
                    print(f"  失败: {exc}")
                    await sleep()
            except BrowserRestartRequired as exc:
                if pending_task is None:
                    continue

                url = pending_task[0]
                if PROXY_MODE == "pool":
                    print(f"[{worker_label}] {exc} → 判定当前代理被屏蔽，切换代理+指纹")
                    await state.append_invalid(
                        invalid_path,
                        {
                            "url": url,
                            "product_id": product_id_from_url(url),
                            "error": f"pool 屏蔽换代理: {exc}",
                            "date": datetime.now().replace(microsecond=0).isoformat(),
                            "proxy": browser_proxy_label(worker_id),
                        },
                    )
                    await state.mark_url_done(failed=True)
                    if state.redis_q is not None:
                        await asyncio.to_thread(state.redis_q.requeue, url)
                    await state.release_url(url)
                    task_queue.task_done()
                    pending_task = None
                    await shutdown_session(clear_profile=True)
                    rotate_pool_proxy(worker_id, reason=str(exc))
                    await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
                    continue

                if CAPTCHA_KEEP_SESSION:
                    # Defensive: map unexpected hard-restart into keep-session path.
                    consecutive_captcha_fails += 1
                    print(
                        f"[{worker_label}] {exc} → 保持 session 模式，跳过当前 URL "
                        f"(连续失败 {consecutive_captcha_fails}/{PROXY_MAX_CONSECUTIVE_CAPTCHA})"
                    )
                    await state.append_invalid(
                        invalid_path,
                        {
                            "url": url,
                            "product_id": product_id_from_url(url),
                            "error": f"keep-session 跳过: {exc}",
                            "date": datetime.now().replace(microsecond=0).isoformat(),
                            "proxy": browser_proxy_label(worker_id),
                        },
                    )
                    await state.mark_url_done(failed=True)
                    if state.redis_q is not None:
                        await asyncio.to_thread(state.redis_q.requeue, url)
                    await state.release_url(url)
                    task_queue.task_done()
                    pending_task = None
                    continue

                captcha_restart_counts[url] = captcha_restart_counts.get(url, 0) + 1
                retry_count = captcha_restart_counts[url]
                print(
                    f"[{worker_label}] {exc}，当前商品浏览器重启 {retry_count}/"
                    f"{MAX_CAPTCHA_RESTARTS_PER_URL}。"
                )
                wipe = should_clear_profile_for_error(exc)
                rotate_note = (
                    "（Webshare rotate 代理将分配新 IP）"
                    if wipe and (WEBSHARE_ROTATE or WEBSHARE_USER.endswith("-rotate"))
                    else ""
                )
                print(
                    f"[{worker_label}] "
                    + (
                        f"已清空浏览器 profile，{BROWSER_RESTART_DELAY_SECONDS}s 后硬重启浏览器{rotate_note}。"
                        if wipe
                        else f"{BROWSER_RESTART_DELAY_SECONDS}s 后重启浏览器（保留 profile）。"
                    )
                )
                await shutdown_session(clear_profile=wipe)
                if retry_count >= MAX_CAPTCHA_RESTARTS_PER_URL:
                    await state.append_invalid(
                        invalid_path,
                        {
                            "url": url,
                            "product_id": product_id_from_url(url),
                            "error": f"连续 {retry_count} 次遇到验证码，本批次跳过（下次运行可重试）",
                            "date": datetime.now().replace(microsecond=0).isoformat(),
                        },
                    )
                    await state.mark_url_done(failed=True)
                    await state.release_url(url)
                    task_queue.task_done()
                    pending_task = None
                    print(f"[{worker_label}] 当前商品验证码重试已达上限，本批次跳过，留待下次运行重试。")
                else:
                    print(f"[{worker_label}] 等待 {BROWSER_RESTART_DELAY_SECONDS} 秒后重新启动浏览器。")
                    await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
            except (NetworkPageError, MissingPriceError, IncompleteFetchError) as exc:
                if pending_task is None:
                    continue

                url = pending_task[0]
                network_restart_counts[url] = network_restart_counts.get(url, 0) + 1
                retry_count = network_restart_counts[url]
                print(
                    f"[{worker_label}] {exc}，当前商品重试 {retry_count}/"
                    f"{MAX_NETWORK_RESTARTS_PER_URL}。"
                )
                wipe = should_clear_profile_for_error(exc)
                if PROXY_MODE == "pool" and retry_count >= MAX_NETWORK_RESTARTS_PER_URL:
                    print(f"[{worker_label}] 网络/不完整失败，切换代理+指纹: {exc}")
                    await state.append_invalid(
                        invalid_path,
                        {
                            "url": url,
                            "product_id": product_id_from_url(url),
                            "error": f"pool 网络失败换代理: {exc}",
                            "date": datetime.now().replace(microsecond=0).isoformat(),
                            "proxy": browser_proxy_label(worker_id),
                        },
                    )
                    await state.mark_url_done(failed=True)
                    if state.redis_q is not None:
                        await asyncio.to_thread(state.redis_q.requeue, url)
                    await state.release_url(url)
                    task_queue.task_done()
                    pending_task = None
                    await shutdown_session(clear_profile=True)
                    rotate_pool_proxy(worker_id, reason=str(exc))
                    await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
                    continue

                if CAPTCHA_KEEP_SESSION and PROXY_MODE == "static":
                    # static 模式：网络/不完整也不随便换代理，尽量保留 session 换下一 URL
                    if retry_count >= MAX_NETWORK_RESTARTS_PER_URL:
                        await state.append_invalid(
                            invalid_path,
                            {
                                "url": url,
                                "product_id": product_id_from_url(url),
                                "error": f"static 模式重试耗尽: {exc}",
                                "date": datetime.now().replace(microsecond=0).isoformat(),
                                "proxy": browser_proxy_label(worker_id),
                            },
                        )
                        await state.mark_url_done(failed=True)
                        if state.redis_q is not None:
                            await asyncio.to_thread(state.redis_q.requeue, url)
                        await state.release_url(url)
                        task_queue.task_done()
                        pending_task = None
                        print(f"[{worker_label}] 跳过当前 URL，保留 session 继续: {url}")
                    else:
                        print(f"[{worker_label}] 保留 session，短暂等待后重试当前商品。")
                        await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)
                    continue

                if retry_count >= MAX_NETWORK_RESTARTS_PER_URL:
                    await shutdown_session(clear_profile=wipe)
                    await state.release_url(url)
                    task_queue.task_done()
                    pending_task = None
                    print(
                        f"[{worker_label}] 本批次重试已达上限，未写入 ES，留待下次运行重试: {url}"
                    )
                else:
                    rotate_note = (
                        "（Webshare rotate 代理将分配新 IP）"
                        if wipe and (WEBSHARE_ROTATE or WEBSHARE_USER.endswith("-rotate"))
                        else ""
                    )
                    print(
                        f"[{worker_label}] "
                        + (
                            f"已清空浏览器 profile，{BROWSER_RESTART_DELAY_SECONDS}s 后重试当前商品{rotate_note}。"
                            if wipe
                            else f"{BROWSER_RESTART_DELAY_SECONDS}s 后重试当前商品（保留 profile）。"
                        )
                    )
                    await shutdown_session(clear_profile=wipe)
                    print(f"[{worker_label}] 等待 {BROWSER_RESTART_DELAY_SECONDS} 秒后重新启动浏览器。")
                    await asyncio.sleep(BROWSER_RESTART_DELAY_SECONDS)

            if await state.should_stop():
                break
    finally:
        if PROXY_MODE in ("static", "pool"):
            print(
                f"[{worker_label}] 代理容量统计: 成功={proxy_success_count} "
                f"连续验证码失败={consecutive_captcha_fails} | {browser_proxy_label(worker_id)}"
            )
        await shutdown_session(clear_profile=False)
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


async def seed_redis_from_es(redis_q: RedisUrlQueue, state: CrawlState) -> int:
    """Push ES URL batches into Redis. Returns newly enqueued count.

    URLs that still need crawling (not in products index) are force-enqueued
    even if they were previously marked seen — otherwise a drained queue can
    stay empty forever while pending URLs remain.
    """
    if not redis_q.try_acquire_seed_lock():
        print("[Redis] 其他机器正在灌队列，本机跳过 producer")
        return 0

    enqueued = 0
    requeued = 0
    skipped_existing = 0
    skipped_local = 0
    try:
        phases: list[tuple[str, bool, bool]] = []
        if PRIORITY_FIRST or PRIORITY_ONLY:
            phases.append(("优先筛选", True, False))
            if not PRIORITY_ONLY:
                phases.append(("其余商品", False, True))
        else:
            phases.append(("全部链接", False, False))

        for phase_name, priority, exclude_priority in phases:
            if await state.should_stop():
                break
            total_links = get_total_link_count(priority=priority, exclude_priority=exclude_priority)
            print(f"[Redis灌队] 阶段「{phase_name}」: ES {total_links} 条")
            if total_links <= 0:
                continue

            search_after: list[Any] | None = None
            fetched = 0
            while True:
                if await state.should_stop():
                    break
                links, search_after = load_link_batch(
                    search_after,
                    priority=priority,
                    exclude_priority=exclude_priority,
                )
                if not links:
                    break
                fetched += len(links)
                exists_map = (
                    products_exist_in_es_batch(links) if SKIP_EXISTING_PRODUCTS else {u: False for u in links}
                )
                for url in links:
                    if await state.is_processed(url):
                        redis_q.mark_seen(url)
                        skipped_local += 1
                        continue
                    if SKIP_EXISTING_PRODUCTS and exists_map.get(url):
                        redis_q.mark_seen(url)
                        skipped_existing += 1
                        continue
                    already_seen = redis_q.is_seen(url)
                    if already_seen:
                        # Previously seen (popped/skipped) but still missing in products.
                        redis_q.force_enqueue(url)
                        requeued += 1
                    elif redis_q.enqueue(url):
                        enqueued += 1
                print(
                    f"[Redis灌队] [{phase_name}] 已扫描 {fetched}/{total_links}，"
                    f"新入队累计 {enqueued}，seen 补入累计 {requeued}，"
                    f"队列长度 {redis_q.queue_length()}"
                )
                if search_after is None:
                    break
            if PRIORITY_ONLY:
                break
    finally:
        redis_q.release_seed_lock()

    print(
        f"[Redis灌队] 完成: 新入队={enqueued} seen补入={requeued} "
        f"已存在跳过={skipped_existing} 本机已处理跳过={skipped_local} "
        f"当前队列={redis_q.queue_length()}"
    )
    return enqueued + requeued


async def redis_queue_feeder(
    state: CrawlState,
    redis_q: RedisUrlQueue,
    task_queue: asyncio.Queue[UrlTask | None],
    producer_done: asyncio.Event,
) -> None:
    """Move URLs from Redis into the local asyncio worker queue.

    By default (REDIS_WAIT_FOREVER=1) keeps waiting when the queue is empty so
    start.bat does not exit after a batch is finished.
    """
    index = 0
    idle_rounds = 0
    wait_notice_every = max(12, 60 // max(REDIS_BRPOP_TIMEOUT, 1))  # ~60s

    async def enqueue_local(url: str, idx: int) -> bool:
        """Put URL into local queue; return False if stop requested (URL requeued to Redis)."""
        while True:
            if await state.should_stop():
                await asyncio.to_thread(redis_q.requeue, url)
                return False
            try:
                await asyncio.wait_for(
                    task_queue.put((url, idx, 1, idx, 0)),
                    timeout=1.0,
                )
                return True
            except asyncio.TimeoutError:
                continue

    while True:
        if await state.should_stop():
            break
        url = await asyncio.to_thread(redis_q.blocking_pop, REDIS_BRPOP_TIMEOUT)
        if await state.should_stop():
            if url:
                await asyncio.to_thread(redis_q.requeue, url)
            break
        if not url:
            idle_rounds += 1
            if REDIS_WAIT_FOREVER:
                if idle_rounds == 1 or idle_rounds % wait_notice_every == 0:
                    qlen = await asyncio.to_thread(redis_q.queue_length)
                    print(
                        f"[Redis] 队列空，继续等待新任务 "
                        f"(queue={qlen}, waited≈{idle_rounds * REDIS_BRPOP_TIMEOUT}s, "
                        f"本机已投递 {index})",
                        flush=True,
                    )
                continue
            if producer_done.is_set() and redis_q.queue_length() == 0:
                if idle_rounds >= REDIS_IDLE_EXIT_ROUNDS:
                    break
            elif producer_done.is_set() and idle_rounds >= REDIS_IDLE_EXIT_ROUNDS:
                # Consumer/test mode with WAIT_FOREVER=0: exit even if Redis still has URLs
                # when local workers already stopped (should_stop) — handled above —
                # or when nobody is draining (idle with producer done).
                break
            continue
        idle_rounds = 0
        index += 1
        if not await enqueue_local(url, index):
            break

    for _ in range(WORKER_COUNT):
        try:
            await asyncio.wait_for(task_queue.put(None), timeout=1.0)
        except asyncio.TimeoutError:
            break
    print(f"[Redis] feeder 结束，共投递 {index} 条到本机 Worker")


async def crawl_with_redis(
    state: CrawlState,
    redis_q: RedisUrlQueue,
    products_path: Path,
    invalid_path: Path,
) -> None:
    """Producer seeds Redis from ES; consumers pull via feeder + browser workers."""
    producer_done = asyncio.Event()
    task_queue: asyncio.Queue[UrlTask | None] = asyncio.Queue(maxsize=max(WORKER_COUNT * 2, 8))

    async def producer() -> None:
        try:
            if REDIS_ROLE not in ("producer", "both"):
                print("[Redis] REDIS_ROLE=consumer，跳过灌队")
                return

            while True:
                added = await seed_redis_from_es(redis_q, state)
                if not REDIS_WAIT_FOREVER:
                    break
                print(
                    f"[Redis] 灌队阶段结束（本次入队 {added}）；"
                    f"队列空闲 {REDIS_RESEED_IDLE_SECONDS}s 后将再次扫描未抓取 URL"
                )
                idle_seconds = 0
                while idle_seconds < REDIS_RESEED_IDLE_SECONDS:
                    if await state.should_stop():
                        return
                    qlen = await asyncio.to_thread(redis_q.queue_length)
                    if qlen > 0:
                        # Work remains; wait and re-check without reseeding yet.
                        await asyncio.sleep(min(5, REDIS_RESEED_IDLE_SECONDS))
                        idle_seconds = 0
                        continue
                    await asyncio.sleep(min(5, REDIS_RESEED_IDLE_SECONDS - idle_seconds))
                    idle_seconds += 5
                if await state.should_stop():
                    return
                qlen = await asyncio.to_thread(redis_q.queue_length)
                if qlen > 0:
                    continue
                print("[Redis] 队列仍空，重新从 ES 灌入尚未入库的 URL…")
        finally:
            producer_done.set()
            if REDIS_WAIT_FOREVER and REDIS_ROLE in ("producer", "both"):
                print("[Redis] producer 已停止；Worker 若仍在运行可继续消费残留队列")

    if REDIS_ROLE == "producer":
        await producer()
        print("[Redis] producer 模式完成，不启动浏览器 Worker")
        return

    wait_mode = "一直等待新任务" if REDIS_WAIT_FOREVER else f"空闲约 {REDIS_IDLE_EXIT_ROUNDS * REDIS_BRPOP_TIMEOUT}s 后退出"
    print(f"[Redis] 启动 {WORKER_COUNT} 个浏览器 Worker 消费队列（{wait_mode}）")
    async with async_playwright() as playwright:
        workers = [
            asyncio.create_task(
                browser_worker(
                    worker_id,
                    state,
                    task_queue,
                    products_path,
                    invalid_path,
                    0,
                    playwright,
                )
            )
            for worker_id in range(WORKER_COUNT)
        ]
        await asyncio.gather(
            producer(),
            redis_queue_feeder(state, redis_q, task_queue, producer_done),
            *workers,
        )


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
    if PROXY_MODE == "static":
        try:
            proxies = load_static_proxies()
            print(f"静态代理池: 已加载 {len(proxies)} 条 from {PROXY_FILE}")
        except Exception as exc:
            print(f"错误: static 代理模式无法加载代理文件: {exc}")
            raise SystemExit(1) from exc
    elif PROXY_MODE == "pool":
        try:
            proxies = load_static_proxies()
            print(f"固定代理池: 已加载 {len(proxies)} 条（来自 .env POOL_PROXIES）")
            for p in proxies:
                print(f"  - {p.label()}")
            prepare_pool_fingerprints(force_regenerate=False)
        except Exception as exc:
            print(f"错误: pool 代理模式初始化失败: {exc}")
            raise SystemExit(1) from exc
        if PRODUCT_PACE_SECONDS > 0:
            print(f"抓取节奏: 约 {PRODUCT_PACE_SECONDS:.0f}s/商品")
    print(f"详情输出目录: {OUTPUT_DIR}")
    print(f"并发 Worker 数: {WORKER_COUNT}")
    if WORKER_COUNT > 1:
        print(f"浏览器 profile 目录: {USER_DATA_DIR}/worker_0 .. worker_{WORKER_COUNT - 1}")
    else:
        print(f"浏览器 profile 目录: {USER_DATA_DIR}")
    print(f"浏览器策略: {profile_policy_label()}")
    print(f"代理模式: {proxy_mode_label()}")
    print(f"浏览器代理: {browser_proxy_label(0)}")
    print(
        f"反检测: stealth={'on' if STEALTH_ENABLED else 'off'} | "
        f"fingerprint={'on' if FINGERPRINT_ENABLED else 'off'} | "
        f"human_mouse={'on' if HUMAN_MOUSE_ENABLED else 'off'}"
    )
    print(f"xAI 验证码: {xai_config_label()}")
    print(f"验证码重试: {captcha_restart_label()}")
    print(f"浏览器硬重启等待: {BROWSER_RESTART_DELAY_SECONDS}s")
    if PRIORITY_FIRST or PRIORITY_ONLY:
        mode = "仅优先筛选" if PRIORITY_ONLY else "优先筛选后再抓其余"
        print(f"抓取策略: {mode}")
        print(f"优先条件: {priority_filter_label()}")
    else:
        print("抓取策略: 全部链接（未启用优先筛选）")
    print(f"抓取站点: {crawl_scope_label()}")
    if SKIP_EXISTING_PRODUCTS:
        print("跳过策略: 产品索引已有 doc 的 URL 不再抓取（默认开启，_count 只随新建增长）")
    else:
        print("跳过策略: SKIP_EXISTING_PRODUCTS=0，已存在 doc 会被更新（_count 可能不变）")
    if MAX_PRODUCTS > 0:
        print(f"本地试跑: 最多处理 {MAX_PRODUCTS} 条商品")
    if REDIS_ENABLED:
        wait = "一直等待" if REDIS_WAIT_FOREVER else "空闲退出"
        print(f"任务队列: Redis ({REDIS_ROLE}) key={REDIS_QUEUE_KEY} ({wait})")
    else:
        print("任务队列: 本机内存（未配置 REDIS_URL）")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Keep persistent profiles across runs; only clear stale Chromium lock files.
    if WORKER_COUNT <= 1:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        cleanup_profile_locks(USER_DATA_DIR)
    else:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        for wid in range(WORKER_COUNT):
            profile_dir = worker_user_data_dir(wid)
            profile_dir.mkdir(parents=True, exist_ok=True)
            cleanup_profile_locks(profile_dir)
    redis_q = get_redis_queue() if REDIS_ENABLED else None
    state = CrawlState(load_progress(), redis_q=redis_q)
    products_path = PRODUCTS_FILE
    invalid_path = INVALID_FILE
    total_all = get_total_link_count()
    print(f"[ES链接] 索引 {URLS_INDEX_NAME} 总数: {total_all}")

    if redis_q is not None:
        await crawl_with_redis(state, redis_q, products_path, invalid_path)
    else:
        await crawl_link_phases(state, products_path, invalid_path)

    print("\n完成。")
    print(f"成功: {state.success}")
    print(f"失败: {state.failed}")
    print(f"已完成: {state.completed}")
    print(f"跳过已处理: {state.skipped}")
    print(f"ES 新建 doc: {state.es_created}（_count 增加）")
    print(f"ES 更新 doc: {state.es_updated}（_count 不变）")
    print(f"ES 上传失败: {state.es_upload_failed}")
    if SKIP_EXISTING_PRODUCTS:
        print(f"ES 已有 doc 跳过: {state.es_skipped_existing}")
    print(f"详情文件: {products_path}")
    print(f"失败文件: {invalid_path}")
    print(f"进度文件: {PROGRESS_FILE}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
