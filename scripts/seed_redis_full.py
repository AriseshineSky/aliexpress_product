#!/usr/bin/env python3
"""Full-seed Redis crawl queue from ES URL index.

For every URL in user1_aliexpress_us_product_urls:
  - skip if product already exists in user1_aliexpress_us_products
  - otherwise push into Redis queue alixq3:urls

Rebuilds the pending list (clears alixq3:urls first) so the queue is exactly
the set of not-yet-crawled URLs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    URLS_INDEX_NAME,
    is_us_product_url,
    normalize_crawl_url,
    product_doc_id,
    product_id_from_url,
    source_from_url,
)

BATCH_SIZE = 1000
MGET_CHUNK = 500


def resolve_redis_url() -> str:
    url = (REDIS_URL or os.environ.get("redis") or "").strip()
    if not url:
        raise SystemExit("REDIS_URL / redis not configured in .env")
    return url


def iter_url_batches() -> Any:
    base = f"http://{ES_HOST}:{ES_PORT}/{URLS_INDEX_NAME}/_search"
    auth = (ES_USER, ES_PASSWORD)
    search_after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "_source": ["url", "product_id"],
            "size": BATCH_SIZE,
            "query": {"match_all": {}},
            "sort": [
                {"product_id": {"order": "asc", "missing": "_last"}},
                {"url": {"order": "asc"}},
            ],
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
                # normalize_crawl_url may already rewrite to .us
                if not is_us_product_url(link):
                    continue
            seen_local.add(link)
            urls.append(link)
        yield urls
        search_after = hits[-1].get("sort")
        if search_after is None:
            break


def products_exist_batch(urls: list[str]) -> dict[str, bool]:
    """Return url -> exists_in_products via ES _mget."""
    result = {u: False for u in urls}
    docs: list[tuple[str, str]] = []
    for url in urls:
        pid = product_id_from_url(url)
        if not pid:
            continue
        source = source_from_url(url)
        docs.append((url, product_doc_id(source, pid)))

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


def main() -> None:
    redis_url = resolve_redis_url()
    client = redis.from_url(
        redis_url,
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=15,
        socket_timeout=60,
    )
    client.ping()

    print(f"ES URLs index: {URLS_INDEX_NAME}")
    print(f"ES products index: {PRODUCT_INDEX_NAME}")
    print(f"Redis queue: {REDIS_QUEUE_KEY}")
    print(f"Before: queue={client.llen(REDIS_QUEUE_KEY)} seen={client.scard(REDIS_SEEN_KEY)}")

    # Rebuild pending queue from scratch for not-yet-crawled URLs.
    pipe_urls: list[str] = []
    scanned = 0
    already_in_products = 0
    to_queue = 0
    invalid = 0

    for batch in iter_url_batches():
        scanned += len(batch)
        exists_map = products_exist_batch(batch)
        for url in batch:
            if not product_id_from_url(url):
                invalid += 1
                continue
            if exists_map.get(url):
                already_in_products += 1
                client.sadd(REDIS_SEEN_KEY, url)
                continue
            pipe_urls.append(url)
            to_queue += 1
        print(
            f"scanned={scanned} already_in_products={already_in_products} "
            f"pending={to_queue} invalid={invalid}",
            flush=True,
        )

    # Replace queue list with full pending set (dedupe while preserving order).
    unique_pending: list[str] = list(dict.fromkeys(pipe_urls))
    print(f"Unique pending to enqueue: {len(unique_pending)}")

    client.delete(REDIS_QUEUE_KEY)
    # Clear stale claims
    cursor = 0
    deleted_claims = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=f"{REDIS_CLAIM_PREFIX}*", count=500)
        if keys:
            deleted_claims += client.delete(*keys)
        if cursor == 0:
            break

    # Push in chunks (LPUSH each URL; list head = newest)
    chunk = 1000
    for i in range(0, len(unique_pending), chunk):
        part = unique_pending[i : i + chunk]
        if not part:
            continue
        pipe = client.pipeline(transaction=False)
        for url in part:
            pipe.sadd(REDIS_SEEN_KEY, url)
            pipe.lpush(REDIS_QUEUE_KEY, url)
        pipe.execute()
        print(f"enqueued {min(i + chunk, len(unique_pending))}/{len(unique_pending)}", flush=True)

    try:
        client.delete(REDIS_SEED_LOCK_KEY)
    except Exception:
        pass

    print("---")
    print(f"scanned_urls: {scanned}")
    print(f"already_in_products (skipped): {already_in_products}")
    print(f"invalid_urls: {invalid}")
    print(f"enqueued: {len(unique_pending)}")
    print(f"queue_len_now: {client.llen(REDIS_QUEUE_KEY)}")
    print(f"seen_now: {client.scard(REDIS_SEEN_KEY)}")
    print(f"claims_cleared: {deleted_claims}")
    print("DONE")


if __name__ == "__main__":
    main()
