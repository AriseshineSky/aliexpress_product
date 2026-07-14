#!/usr/bin/env python3
"""Generate Windows Chrome fingerprints and push them to the Redis queue.

Profiles are Windows desktop only (platform=Win32, Windows NT 10 Chrome UA,
Direct3D11 ANGLE GPUs). Run on any machine that can reach Redis — typically
the Windows VPS producer host. No browser is required.

Usage:
  .venv\\Scripts\\python.exe scripts\\seed_fingerprints_redis.py --count 200 --diverse
  .venv\\Scripts\\python.exe scripts\\seed_fingerprints_redis.py --status
  seed-fingerprints.bat 200
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed Redis with Windows Chrome fingerprints (alixq3:fps)"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="How many Windows fingerprints to generate and push (default 100)",
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
    parser.add_argument(
        "--os",
        dest="host_os",
        choices=("windows",),
        default="windows",
        help="Fingerprint host OS family (only windows is supported)",
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
    print(f"目标主机指纹: {args.host_os} (Win32 / Chrome / Direct3D11)")
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
        # Guard: producer must only enqueue Windows desktop profiles.
        if fp.platform != "Win32" or "Windows NT" not in fp.user_agent:
            continue
        sig = fingerprint_signature(fp)
        if args.diverse and sig in used:
            continue
        used.add(sig)
        payload = fingerprint_to_dict(fp)
        payload["host_os"] = "windows"
        items.append(payload)

    if len(items) < args.count:
        print(
            f"警告: 只生成了 {len(items)}/{args.count} 条 "
            f"（尝试 {attempts} 次，可能--diverse 签名池不够大）"
        )

    length = rq.push_fingerprints(items)
    print(f"已推入 {len(items)} 条 Windows 指纹（尝试 {attempts} 次），队列长度={length}")
    if items:
        sample = items[0]
        print(
            f"示例: platform={sample.get('platform')} "
            f"ua={str(sample.get('user_agent', ''))[:70]}… "
            f"vp={sample.get('viewport_width')}x{sample.get('viewport_height')} "
            f"gpu={str(sample.get('webgl_renderer', ''))[:48]}"
        )


if __name__ == "__main__":
    main()
