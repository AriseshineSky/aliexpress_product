#!/usr/bin/env python3
"""Seed Redis crawl queue from ES URL index.

Phase 1 — strict priority (same as crawler defaults):
  price < PRIORITY_MAX_PRICE, rating/reviews/sold_count >= mins
  CRAWL_US_ONLY, skip URLs already in the products index

Phase 2 — when phase-1 pending is empty (or after it), enqueue remaining
  URLs that do NOT meet the strict filters, ordered by:
    1) rating DESC
    2) reviews DESC
    3) sold_count DESC

Rebuilds alixq3:urls by default (use --append to keep existing queue).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Iterator

import redis
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from alixq3 import (  # noqa: E402
    CRAWL_US_ONLY,
    ES_HOST,
    ES_PASSWORD,
    ES_PORT,
    ES_USER,
    PRODUCT_INDEX_NAME,
    REDIS_CLAIM_PREFIX,
    REDIS_QUEUE_KEY,
    REDIS_SEED_LOCK_KEY,
    REDIS_SEEN_KEY,
    REDIS_URL,
    URLS_BATCH_SIZE,
    URLS_INDEX_NAME,
    build_url_query,
    crawl_scope_label,
    is_us_product_url,
    normalize_crawl_url,
    priority_filter_label,
    product_doc_id,
    product_id_from_url,
    source_from_url,
)

MGET_CHUNK = 500

# search_after tie-breakers must be unique enough for stable pagination
FALLBACK_SORT = [
    {"rating": {"order": "desc", "missing": "_last"}},
    {"reviews": {"order": "desc", "missing": "_last"}},
    {"sold_count": {"order": "desc", "missing": "_last"}},
    {"product_id": {"order": "asc", "missing": "_last"}},
    {"url": {"order": "asc"}},
]

PRIORITY_SORT = [
    {"product_id": {"order": "asc", "missing": "_last"}},
    {"url": {"order": "asc"}},
]


def resolve_redis_url() -> str:
    url = (REDIS_URL or os.environ.get("redis") or "").strip()
    if not url:
        raise SystemExit("REDIS_URL / redis not configured in .env")
    return url


def es_count(query: dict[str, Any]) -> int:
    resp = requests.post(
        f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}/_count",
        auth=(ES_USER, ES_PASSWORD),
        json={"query": query},
        timeout=60,
    )
    resp.raise_for_status()
    return int(resp.json().get("count", 0))


def products_exist_batch(urls: list[str]) -> dict[str, bool]:
    result = {u: False for u in urls}
    docs: list[tuple[str, str]] = []
    for url in urls:
        pid = product_id_from_url(url)
        if not pid:
            continue
        docs.append((url, product_doc_id(source_from_url(url), pid)))

    auth = (ES_USER, ES_PASSWORD)
    mget_url = f"http://{ES_HOST}:{ES_PORT}/{PRODUCT_INDEX_NAME}/_mget"
    for i in range(0, len(docs), MGET_CHUNK):
        chunk = docs[i : i + MGET_CHUNK]
        body = {"ids": [doc_id for _, doc_id in chunk]}
        resp = requests.post(mget_url, auth=auth, json=body, timeout=120)
        resp.raise_for_status()
        for (url, _), doc in zip(chunk, resp.json().get("docs") or []):
            result[url] = bool(doc.get("found"))
    return result


def iter_url_batches(
    *,
    query: dict[str, Any],
    sort: list[dict[str, Any]],
    skip_existing: bool,
) -> Iterator[tuple[list[str], int, int]]:
    """Yield (urls_to_enqueue, already_in_products, hit_count)."""
    base = f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}/_search"
    auth = (ES_USER, ES_PASSWORD)
    search_after: list[Any] | None = None

    while True:
        body: dict[str, Any] = {
            "_source": ["url", "product_id", "price", "rating", "reviews", "sold_count"],
            "size": URLS_BATCH_SIZE,
            "query": query,
            "sort": sort,
        }
        if search_after is not None:
            body["search_after"] = search_after
        resp = requests.post(base, auth=auth, json=body, timeout=120)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            break

        urls: list[str] = []
        seen_local: set[str] = set()
        for hit in hits:
            source = hit.get("_source") or {}
            link = normalize_crawl_url(str(source.get("url") or "").strip())
            if not link or link in seen_local:
                continue
            if CRAWL_US_ONLY and not is_us_product_url(link):
                continue
            if not product_id_from_url(link):
                continue
            seen_local.add(link)
            urls.append(link)

        already = 0
        if skip_existing and urls:
            exists_map = products_exist_batch(urls)
            kept: list[str] = []
            for u in urls:
                if exists_map.get(u):
                    already += 1
                else:
                    kept.append(u)
            urls = kept

        yield urls, already, len(hits)
        search_after = hits[-1].get("sort")
        if search_after is None:
            break


def clear_claims(client: redis.Redis) -> int:
    cursor = 0
    deleted = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=f"{REDIS_CLAIM_PREFIX}*", count=500)
        if keys:
            deleted += int(client.delete(*keys))
        if cursor == 0:
            break
    return deleted


def enqueue_batch(
    client: redis.Redis,
    urls: list[str],
    *,
    append: bool,
    seen_local: set[str],
) -> int:
    """LPUSH a batch immediately. With BRPOP, earlier LPUSH'd URLs are consumed first."""
    to_push: list[str] = []
    for url in urls:
        if url in seen_local:
            continue
        if append and client.sismember(REDIS_SEEN_KEY, url):
            continue
        seen_local.add(url)
        to_push.append(url)
    if not to_push:
        return 0
    pipe = client.pipeline(transaction=False)
    for url in to_push:
        pipe.sadd(REDIS_SEEN_KEY, url)
        pipe.lpush(REDIS_QUEUE_KEY, url)
    pipe.execute()
    return len(to_push)


def stream_phase(
    client: redis.Redis,
    *,
    label: str,
    query: dict[str, Any],
    sort: list[dict[str, Any]],
    total: int,
    skip_existing: bool,
    limit: int,
    append: bool,
    seen_local: set[str],
) -> tuple[int, int, int]:
    """Scan ES and enqueue each batch immediately. Returns (enqueued, scanned, already)."""
    enqueued = 0
    scanned = 0
    already_in_products = 0
    for batch, already, hit_count in iter_url_batches(
        query=query, sort=sort, skip_existing=skip_existing
    ):
        scanned += hit_count
        already_in_products += already
        if limit > 0:
            room = limit - enqueued
            if room <= 0:
                break
            batch = batch[:room]
        pushed = enqueue_batch(client, batch, append=append, seen_local=seen_local)
        enqueued += pushed
        qlen = client.llen(REDIS_QUEUE_KEY)
        print(
            f"[{label}] scanned={scanned}/{total} already_in_products={already_in_products} "
            f"enqueued={enqueued} queue={qlen}",
            flush=True,
        )
        if limit > 0 and enqueued >= limit:
            print(f"[{label}] hit --limit {limit}", flush=True)
            break
    return enqueued, scanned, already_in_products


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed Redis: strict priority first, then rating/reviews/sold fallback"
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to existing queue instead of rebuilding alixq3:urls",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Also enqueue URLs that already have a product doc (default: skip them)",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Clear alixq3:seen before seeding (only with rebuild, not --append)",
    )
    parser.add_argument(
        "--priority-only",
        action="store_true",
        help="Only seed strict-priority URLs; do not fall back to rating-sorted rest",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max URLs to enqueue in total (0 = no limit). Applies across both phases.",
    )
    args = parser.parse_args()
    skip_existing = not args.include_existing
    limit = max(0, int(args.limit or 0))

    if not ES_USER or not ES_PASSWORD:
        raise SystemExit("ES_USER / ES_PASSWORD not configured in .env")

    redis_url = resolve_redis_url()
    client = redis.from_url(
        redis_url,
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=15,
        socket_timeout=60,
    )
    client.ping()

    priority_query = build_url_query(priority=True, exclude_priority=False)
    fallback_query = build_url_query(priority=False, exclude_priority=True)
    priority_total = es_count(priority_query)
    fallback_total = es_count(fallback_query)

    print(f"ES URLs index: {URLS_INDEX_NAME}")
    print(f"ES products index: {PRODUCT_INDEX_NAME}")
    print(f"Redis queue: {REDIS_QUEUE_KEY}")
    print(f"优先条件: {priority_filter_label()}")
    print("回退排序: rating DESC → reviews DESC → sold_count DESC")
    print(f"抓取站点: {crawl_scope_label()}")
    print(
        "跳过策略: "
        + (
            "产品索引已有 doc 的 URL 不再入队"
            if skip_existing
            else "包含已有 doc（会重复刷新）"
        )
    )
    print(f"ES 优先 URL 总数: {priority_total}")
    print(f"ES 非优先 URL 总数: {fallback_total}")
    if limit > 0:
        print(f"入队上限: {limit}")
    print(f"Before: queue={client.llen(REDIS_QUEUE_KEY)} seen={client.scard(REDIS_SEEN_KEY)}")

    if not args.append:
        client.delete(REDIS_QUEUE_KEY)
        claims_cleared = clear_claims(client)
        if args.reset_seen:
            client.delete(REDIS_SEEN_KEY)
            print("cleared alixq3:seen")
        print(f"claims_cleared: {claims_cleared}")

    remaining = limit
    total_enqueued = 0
    scanned = 0
    already_in_products = 0
    seen_local: set[str] = set()

    # Phase 1: strict priority — enqueue each batch immediately
    phase_limit = remaining if limit > 0 else 0
    p_enqueued, p_scanned, p_already = stream_phase(
        client,
        label="优先筛选",
        query=priority_query,
        sort=PRIORITY_SORT,
        total=priority_total,
        skip_existing=skip_existing,
        limit=phase_limit,
        append=args.append,
        seen_local=seen_local,
    )
    scanned += p_scanned
    already_in_products += p_already
    total_enqueued += p_enqueued
    print(f"优先已入队: {p_enqueued}")
    if limit > 0:
        remaining = max(0, limit - total_enqueued)

    # Phase 2: non-priority, sorted by rating / reviews / sold — stream enqueue
    need_fallback = not args.priority_only and (limit == 0 or remaining > 0)
    if need_fallback:
        if p_enqueued == 0:
            print(
                "优先池无待抓 URL，按星级→评论→销量边扫边灌入非优先商品…",
                flush=True,
            )
        else:
            print(
                "优先池已入队，继续按星级→评论→销量边扫边灌入非优先商品…",
                flush=True,
            )
        f_enqueued, f_scanned, f_already = stream_phase(
            client,
            label="非优先回退",
            query=fallback_query,
            sort=FALLBACK_SORT,
            total=fallback_total,
            skip_existing=skip_existing,
            limit=remaining if limit > 0 else 0,
            append=args.append,
            seen_local=seen_local,
        )
        scanned += f_scanned
        already_in_products += f_already
        total_enqueued += f_enqueued
        print(f"非优先已入队: {f_enqueued}")
    elif args.priority_only and p_enqueued == 0:
        print("优先池无待抓 URL，且指定了 --priority-only，跳过回退灌队。")

    try:
        client.delete(REDIS_SEED_LOCK_KEY)
    except Exception:
        pass

    qlen = client.llen(REDIS_QUEUE_KEY)
    print("---")
    print(f"scanned_hits: {scanned}")
    print(f"already_in_products (skipped): {already_in_products}")
    print(f"enqueued: {total_enqueued}")
    print(f"queue_len_now: {qlen}")
    if total_enqueued > 0 and qlen == 0:
        print(
            "提示: 已入队但队列长度为 0 —— 通常是抓取 Worker 正在 BRPOP 即时消费。"
        )
    print(f"seen_now: {client.scard(REDIS_SEEN_KEY)}")
    print("DONE")


if __name__ == "__main__":
    main()
