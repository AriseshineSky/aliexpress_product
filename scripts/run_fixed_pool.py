#!/usr/bin/env python3
"""Run AliExpress crawl on the hardcoded 5-proxy pool.

Behavior (forced via env before importing alixq3):
  - PROXY_MODE=pool
  - Concurrent workers from .env WORKER_COUNT (or --workers); capped by proxy count
  - ~30 seconds pacing between successful products (per worker)
  - Captcha: 1 recovery round; failure → switch proxy + fingerprint (Redis queue preferred)
  - Same Redis URL queue as the main crawler

Fingerprint producer (another machine/process):
  .venv/bin/python scripts/seed_fingerprints_redis.py --count 100 --diverse

Usage:
  .venv/bin/python scripts/run_fixed_pool.py                 # uses WORKER_COUNT from .env
  .venv/bin/python scripts/run_fixed_pool.py --workers 3     # override .env
  .venv/bin/python scripts/run_fixed_pool.py --headless 1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl with hardcoded 5-proxy pool")
    parser.add_argument("--headless", type=int, choices=(0, 1), default=None)
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Override .env WORKER_COUNT (each worker needs its own proxy; capped by pool size)",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=30.0,
        help="Seconds to wait after each successful product (default 30)",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=0,
        help="Stop after N successes (0 = unlimited)",
    )
    parser.add_argument(
        "--regen-fingerprints",
        action="store_true",
        help="Force new local fingerprints for all pool proxies at startup",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Skip homepage/category/product warmup (default: warmup on)",
    )
    parser.add_argument(
        "--no-redis-fp",
        action="store_true",
        help="Do not pull fingerprints from Redis (local regenerate only)",
    )
    return parser.parse_args()


def _apply_env(args: argparse.Namespace) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass

    # Pool mode + captcha policy. WORKER_COUNT comes from .env unless --workers is set.
    os.environ["PROXY_MODE"] = "pool"
    if args.workers is not None:
        os.environ["WORKER_COUNT"] = str(max(1, args.workers))
    elif not (os.environ.get("WORKER_COUNT") or "").strip():
        os.environ["WORKER_COUNT"] = "1"
    if args.headless is not None:
        os.environ["HEADLESS"] = str(args.headless)
    os.environ["PRODUCT_PACE_SECONDS"] = str(args.pace)
    os.environ["MAX_PRODUCTS"] = str(args.max_products)
    os.environ["POOL_CAPTCHA_ROUNDS"] = "1"
    os.environ["POOL_CAPTCHA_RESTARTS"] = "0"
    os.environ["POOL_NETWORK_RESTARTS"] = "1"
    os.environ["CAPTCHA_KEEP_SESSION"] = "0"
    os.environ["SESSION_WARMUP"] = "0" if args.no_warmup else "1"
    os.environ["CAPTCHA_AUTO_SOLVE"] = "1"
    os.environ["CLEAR_PROFILE_ON_HARD_FAIL"] = "1"
    os.environ["FINGERPRINT_ENABLED"] = "1"
    os.environ["STEALTH_ENABLED"] = "1"
    os.environ["REDIS_FP_ENABLED"] = "0" if args.no_redis_fp else "1"


def main() -> None:
    args = _parse_args()
    _apply_env(args)

    import alixq3

    print("=" * 60)
    print("固定代理池抓取 (PROXY_MODE=pool)")
    print("=" * 60)
    proxies = alixq3.load_fixed_proxy_pool()
    print(f"代理数: {len(proxies)}")
    for p in proxies:
        print(f"  - {p.label()}")
    env_workers = (os.environ.get("WORKER_COUNT") or "").strip()
    print(
        f"并发 Worker: {alixq3.WORKER_COUNT}"
        f"（.env WORKER_COUNT={env_workers or '?'}，pool 上限 {len(proxies)}）"
    )
    if env_workers.isdigit() and int(env_workers) > len(proxies):
        print(
            f"提示: .env WORKER_COUNT={env_workers} 超过代理数 {len(proxies)}，"
            f"已自动限制为 {alixq3.WORKER_COUNT}"
        )
    print(f"节奏: ~{alixq3.PRODUCT_PACE_SECONDS:.0f}s/商品/Worker")
    print(f"预热: {'on' if alixq3.SESSION_WARMUP else 'off'}（首页→分类→商品）")
    print(f"验证码: Grok={alixq3.CAPTCHA_AUTO_SOLVE} 轮数={alixq3.CAPTCHA_RECOVERY_ROUNDS}")
    print(f"Redis URL 队列: {alixq3.REDIS_ROLE if alixq3.REDIS_ENABLED else '未配置'}")
    if alixq3.REDIS_ENABLED and alixq3.REDIS_FP_ENABLED:
        try:
            rq = alixq3.get_redis_queue()
            print(f"Redis 指纹队列: {alixq3.REDIS_FP_QUEUE_KEY} len={rq.fingerprint_queue_length()}")
        except Exception as exc:
            print(f"Redis 指纹队列: 连接失败 ({exc})")
    else:
        print("Redis 指纹队列: off（屏蔽后本地 regenerate）")
    if args.regen_fingerprints:
        alixq3.prepare_pool_fingerprints(force_regenerate=True)
    print()

    if not alixq3.REDIS_ENABLED:
        print("警告: 未配置 REDIS_URL，将使用本机内存队列（不会监听共享任务队列）")
        print()

    asyncio.run(alixq3.main_async())


if __name__ == "__main__":
    main()
