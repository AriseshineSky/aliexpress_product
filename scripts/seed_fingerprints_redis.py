#!/usr/bin/env python3
"""Generate browser fingerprints and push them to the Redis fingerprint queue.

Run on any machine that can reach Redis (does not need a browser).

Usage:
  .venv/bin/python scripts/seed_fingerprints_redis.py --count 100
  .venv/bin/python scripts/seed_fingerprints_redis.py --count 50 --diverse
  .venv/bin/python scripts/seed_fingerprints_redis.py --status
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed Redis fingerprint queue (alixq3:fps)")
    parser.add_argument(
        "--count",
        type=int,
        default=50,
        help="How many fingerprints to generate and push (default 50)",
    )
    parser.add_argument(
        "--diverse",
        action="store_true",
        help="Reject near-duplicate signatures while minting",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only print queue length and exit",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="America/New_York",
        help="timezone_id stored on each fingerprint",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass

    import alixq3
    from stealth_fp import (
        fingerprint_signature,
        fingerprint_to_dict,
        mint_random_fingerprint,
    )

    if not alixq3.REDIS_ENABLED:
        print("错误: 未配置 REDIS_URL / redis")
        raise SystemExit(1)

    rq = alixq3.get_redis_queue()
    print(f"指纹队列: {alixq3.REDIS_FP_QUEUE_KEY}")
    print(f"当前长度: {rq.fingerprint_queue_length()}")
    if args.status:
        return

    if args.count <= 0:
        print("--count 必须 > 0")
        raise SystemExit(1)

    items = []
    used: set[tuple] = set()
    attempts = 0
    max_attempts = max(args.count * 8, args.count + 20)
    while len(items) < args.count and attempts < max_attempts:
        attempts += 1
        fp = mint_random_fingerprint(timezone_id=args.timezone)
        sig = fingerprint_signature(fp)
        if args.diverse and sig in used:
            continue
        used.add(sig)
        items.append(fingerprint_to_dict(fp))

    length = rq.push_fingerprints(items)
    print(f"已推入 {len(items)} 条指纹（尝试 {attempts} 次），队列长度={length}")
    if items:
        sample = items[0]
        print(
            f"示例: ua={sample.get('user_agent', '')[:60]}… "
            f"vp={sample.get('viewport_width')}x{sample.get('viewport_height')} "
            f"gpu={str(sample.get('webgl_renderer', ''))[:40]}"
        )


if __name__ == "__main__":
    main()
