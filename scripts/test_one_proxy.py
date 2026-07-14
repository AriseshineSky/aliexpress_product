#!/usr/bin/env python3
"""Test how many products one static residential proxy can scrape.

Flow:
  1. Apply one proxy from data/Webshare 1000 proxies.txt
  2. Open Chromium + stealth
  3. Warm up homepage → category → product (cookies/session)
  4. Pop URLs from Redis queue and scrape
  5. On captcha: LLM solve up to CAPTCHA_RECOVERY_ROUNDS (default 2),
     keep the same proxy/session/cookies, then try the next URL
  6. Stop when the proxy is burned (consecutive captcha fails) or MAX_PRODUCTS

Usage:
  .venv/bin/python scripts/test_one_proxy.py
  .venv/bin/python scripts/test_one_proxy.py --proxy-index 0 --max-products 30
  .venv/bin/python scripts/test_one_proxy.py --proxy-index 5 --headless 0
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
    parser = argparse.ArgumentParser(description="Measure capacity of one static proxy")
    parser.add_argument("--proxy-index", type=int, default=0, help="0-based index in PROXY_FILE")
    parser.add_argument(
        "--proxy-file",
        type=str,
        default=str(BASE_DIR / "data" / "Webshare 1000 proxies.txt"),
        help="Path to host:port:user:pass list",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=0,
        help="Stop after N completed products (0 = until proxy burns)",
    )
    parser.add_argument(
        "--burn-after",
        type=int,
        default=3,
        help="Consecutive captcha failures before treating proxy as burned",
    )
    parser.add_argument("--headless", type=int, choices=(0, 1), default=0)
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Skip homepage/category/product warmup",
    )
    parser.add_argument(
        "--captcha-rounds",
        type=int,
        default=2,
        help="LLM captcha recovery rounds per URL (default 2)",
    )
    return parser.parse_args()


def _apply_env(args: argparse.Namespace) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass

    # Force static-proxy capacity-test knobs (after dotenv so they win).
    os.environ["PROXY_MODE"] = "static"
    os.environ["PROXY_FILE"] = args.proxy_file
    os.environ["PROXY_INDEX"] = str(args.proxy_index)
    os.environ["SESSION_WARMUP"] = "0" if args.no_warmup else "1"
    os.environ["CAPTCHA_KEEP_SESSION"] = "1"
    os.environ["CAPTCHA_RECOVERY_ROUNDS"] = str(args.captcha_rounds)
    os.environ["PROXY_MAX_CONSECUTIVE_CAPTCHA"] = str(args.burn_after)
    os.environ["WORKER_COUNT"] = "1"
    os.environ["HEADLESS"] = str(args.headless)
    os.environ["MAX_PRODUCTS"] = str(args.max_products)
    os.environ["REDIS_ROLE"] = "consumer"
    os.environ["REDIS_WAIT_FOREVER"] = "0"
    os.environ["CLEAR_PROFILE_ON_HARD_FAIL"] = "0"


def main() -> None:
    args = _parse_args()
    _apply_env(args)

    import alixq3

    print("=" * 60)
    print("单代理容量测试 (PROXY_MODE=static)")
    print("=" * 60)
    proxies = alixq3.load_static_proxies()
    proxy = alixq3.get_static_proxy_for_worker(0)
    print(f"代理文件: {alixq3.PROXY_FILE} ({len(proxies)} 条)")
    print(f"选用代理: {proxy.label()}")
    print(f"预热: {'off' if args.no_warmup else 'on'}")
    print(f"验证码 LLM 轮数: {args.captcha_rounds}")
    print(f"连续失败烧穿阈值: {args.burn_after}")
    print(f"MAX_PRODUCTS: {args.max_products or '不限（直到代理烧掉）'}")
    print(f"Redis: consumer，空闲后退出（不灌队）")
    print()

    if not alixq3.REDIS_ENABLED:
        print("错误: 需要配置 REDIS_URL / redis，以便从 queue 读取 link")
        raise SystemExit(1)

    asyncio.run(alixq3.main_async())


if __name__ == "__main__":
    main()
