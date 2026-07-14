#!/usr/bin/env python3
"""Copy N proxies from data/*.txt into .env POOL_PROXIES (gitignored).

Usage:
  .venv/bin/python scripts/sync_env_pool_proxies.py
  .venv/bin/python scripts/sync_env_pool_proxies.py --count 100 --shuffle
"""

from __future__ import annotations

import argparse
import random
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SRC = BASE_DIR / "data" / "Webshare 100 proxies.txt"
DEFAULT_ENV = BASE_DIR / ".env"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync data proxies into .env POOL_PROXIES")
    p.add_argument("--source", type=str, default=str(DEFAULT_SRC))
    p.add_argument("--env", type=str, default=str(DEFAULT_ENV))
    p.add_argument("--count", type=int, default=100, help="How many proxies to write")
    p.add_argument(
        "--no-shuffle",
        action="store_true",
        help="Take the first N lines instead of random sample (default: random sample)",
    )
    p.add_argument("--seed", type=int, default=None, help="RNG seed for shuffle")
    return p.parse_args()


def _load_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        lines.append(line)
    return lines


def _replace_pool_proxies(env_text: str, proxies: list[str]) -> str:
    # Store as pipe-separated single line (quoted) for dotenv compatibility.
    value = "|".join(proxies)
    block = f'POOL_PROXIES="{value}"'

    pattern = re.compile(
        r'(?m)^[ \t]*POOL_PROXIES\s*=\s*(?:"(?:\\.|[^"])*"|\'(?:\\.|[^\'])*\'|[^\n]*)\n?'
    )
    if pattern.search(env_text):
        return pattern.sub(block + "\n", env_text, count=1)

    # Insert after PROXY_MODE / SESSION_WARMUP if present.
    insert_after = re.compile(r"(?m)^(PROXY_MODE\s*=.*\n(?:SESSION_WARMUP\s*=.*\n)?)")
    if insert_after.search(env_text):
        return insert_after.sub(r"\1" + block + "\n", env_text, count=1)
    return env_text.rstrip() + "\n\n" + block + "\n"


def _ensure_pool_knobs(env_text: str) -> str:
    """Ensure pool-related knobs exist without clobbering unrelated settings."""
    defaults = {
        "PROXY_MODE": "pool",
        "SESSION_WARMUP": "1",
        "POOL_PICK": "random",
        "PRODUCT_PACE_SECONDS": "15",
    }
    out = env_text
    for key, value in defaults.items():
        pat = re.compile(rf"(?m)^[ \t]*{key}\s*=.*$")
        if pat.search(out):
            if key in ("PROXY_MODE", "POOL_PICK"):
                out = pat.sub(f"{key}={value}", out, count=1)
        else:
            out = out.rstrip() + f"\n{key}={value}\n"
    return out


def main() -> None:
    args = _parse_args()
    src = Path(args.source).expanduser().resolve()
    env_path = Path(args.env).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"代理文件不存在: {src}")
    if not env_path.exists():
        raise SystemExit(f".env 不存在: {env_path}")

    all_lines = _load_lines(src)
    if not all_lines:
        raise SystemExit(f"代理文件为空: {src}")

    count = max(1, min(args.count, len(all_lines)))
    if args.no_shuffle:
        chosen = all_lines[:count]
    else:
        rng = random.Random(args.seed)
        chosen = rng.sample(all_lines, count)

    text = env_path.read_text(encoding="utf-8")
    text = _replace_pool_proxies(text, chosen)
    text = _ensure_pool_knobs(text)
    env_path.write_text(text, encoding="utf-8")

    hosts = [line.split(":")[0] for line in chosen[:3]]
    print(f"已写入 {len(chosen)} 条代理到 {env_path}")
    print(f"来源: {src} ({'shuffle' if not args.no_shuffle else 'head'})")
    print(f"示例 host: {', '.join(hosts)} …")
    print("已设置 PROXY_MODE=pool POOL_PICK=random")


if __name__ == "__main__":
    main()
