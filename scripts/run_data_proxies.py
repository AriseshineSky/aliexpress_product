#!/usr/bin/env python3
"""Local crawl using proxies from data/*.txt (pool rotate on captcha).

Behavior (forced before importing alixq3):
  - PROXY_MODE=pool
  - Proxies from data/Webshare 1000 proxies.txt (or --proxy-file)
  - 1 worker by default
  - Captcha: do not solve; cycle proxy + new fingerprint (IPs stay reusable)
  - Homepage warmup on
  - Redis consumer (shared URL queue)

Usage:
  .venv/bin/python scripts/run_data_proxies.py
  .venv/bin/python scripts/run_data_proxies.py --workers 1 --pace 15 --headless 0
  .venv/bin/python scripts/run_data_proxies.py --proxy-index 10 --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

DEFAULT_PROXY_FILE = next(
    (
        p
        for p in (
            BASE_DIR / "data" / "Webshare 100 proxies.txt",
            BASE_DIR / "data" / "Webshare 1000 proxies.txt",
        )
        if p.exists()
    ),
    BASE_DIR / "data" / "Webshare 100 proxies.txt",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl with data/ proxy list; rotate proxy+fingerprint on captcha"
    )
    parser.add_argument(
        "--proxy-file",
        type=str,
        default=str(DEFAULT_PROXY_FILE),
        help="Path to host:port:user:pass list (default: data/Webshare 1000 proxies.txt)",
    )
    parser.add_argument(
        "--proxy-index",
        type=int,
        default=0,
        help="Start cycling from this 0-based index in the file",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Use only the first N proxies from the file (0 = all)",
    )
    parser.add_argument("--workers", type=int, default=1, help="Browser workers (default 1)")
    parser.add_argument("--headless", type=int, choices=(0, 1), default=0)
    parser.add_argument(
        "--pace",
        type=float,
        default=15.0,
        help="Seconds to wait after each successful product (default 15)",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=0,
        help="Stop after N successes (0 = unlimited)",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Skip homepage/category/product warmup",
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

    # Force data-file pool mode (ignore .env POOL_PROXIES / WORKER_COUNT).
    os.environ["PROXY_MODE"] = "pool"
    os.environ["POOL_PROXIES"] = ""
    os.environ["PROXY_FILE"] = str(Path(args.proxy_file).expanduser().resolve())
    os.environ["PROXY_INDEX"] = str(max(0, args.proxy_index))
    os.environ["POOL_PROXY_LIMIT"] = str(max(0, args.limit))
    os.environ["WORKER_COUNT"] = str(max(1, args.workers))
    os.environ["HEADLESS"] = str(args.headless)
    os.environ["PRODUCT_PACE_SECONDS"] = str(args.pace)
    os.environ["MAX_PRODUCTS"] = str(args.max_products)
    os.environ["POOL_CAPTCHA_ROUNDS"] = "0"
    os.environ["POOL_CAPTCHA_RESTARTS"] = "0"
    os.environ["POOL_NETWORK_RESTARTS"] = "1"
    os.environ["CAPTCHA_KEEP_SESSION"] = "0"
    os.environ["CAPTCHA_AUTO_SOLVE"] = "0"
    os.environ["SESSION_WARMUP"] = "0" if args.no_warmup else "1"
    os.environ["CLEAR_PROFILE_ON_HARD_FAIL"] = "1"
    os.environ["FINGERPRINT_ENABLED"] = "1"
    os.environ["STEALTH_ENABLED"] = "1"
    os.environ["REDIS_FP_ENABLED"] = "0" if args.no_redis_fp else "1"
    os.environ.setdefault("REDIS_ROLE", "consumer")
    os.environ.setdefault("REDIS_WAIT_FOREVER", "1")


def main() -> None:
    args = _parse_args()
    _apply_env(args)

    import alixq3

    print("=" * 60)
    print("data 代理文件抓取 (PROXY_MODE=pool)")
    print("=" * 60)
    proxies = alixq3.load_fixed_proxy_pool()
    print(f"代理文件: {alixq3.PROXY_FILE}")
    print(f"代理数: {len(proxies)}（start_index={args.proxy_index}）")
    if proxies:
        first = proxies[0]
        print(f"首条代理: {first.label()}")
    print(f"并发 Worker: {alixq3.WORKER_COUNT}")
    print(f"节奏: ~{alixq3.PRODUCT_PACE_SECONDS:.0f}s/商品/Worker")
    print(f"预热: {'on' if alixq3.SESSION_WARMUP else 'off'}")
    print("验证码: 不求解；出现则换指纹并循环 data 代理（IP 不屏蔽）")
    print(f"Redis URL 队列: {alixq3.REDIS_ROLE if alixq3.REDIS_ENABLED else '未配置'}")
    if alixq3.REDIS_ENABLED and alixq3.REDIS_FP_ENABLED:
        try:
            rq = alixq3.get_redis_queue()
            print(
                f"Redis 指纹队列: {alixq3.REDIS_FP_QUEUE_KEY} "
                f"len={rq.fingerprint_queue_length()}"
            )
        except Exception as exc:
            print(f"Redis 指纹队列: 连接失败 ({exc})")
    else:
        print("Redis 指纹队列: offline / disabled")
    print()

    if not proxies:
        raise SystemExit("代理列表为空")
    if not alixq3.REDIS_ENABLED:
        print("警告: 未配置 REDIS_URL，将使用本机内存队列")
        print()

    asyncio.run(alixq3.main_async())


if __name__ == "__main__":
    main()
