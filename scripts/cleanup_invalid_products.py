#!/usr/bin/env python3
"""Remove invalid product documents from Elasticsearch (network error pages, incomplete fetches)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent


def load_alixq3():
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    spec = importlib.util.spec_from_file_location("alixq3", BASE_DIR / "alixq3.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iter_product_hits(mod, *, existence_only: bool = False):
    base = f"http://{mod.ES_HOST}:{mod.ES_PORT}/{mod.PRODUCT_INDEX_NAME}"
    auth = (mod.ES_USER, mod.ES_PASSWORD)
    query: dict = {"match_all": {}}
    if existence_only:
        query = {"term": {"existence": True}}

    resp = requests.post(
        f"{base}/_search?scroll=2m",
        auth=auth,
        json={"query": query, "size": 500, "_source": True},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    scroll_id = data.get("_scroll_id")
    hits = data.get("hits", {}).get("hits", [])

    while hits:
        for hit in hits:
            yield hit
        scroll_resp = requests.post(
            f"http://{mod.ES_HOST}:{mod.ES_PORT}/_search/scroll",
            auth=auth,
            json={"scroll": "2m", "scroll_id": scroll_id},
            timeout=60,
        )
        scroll_resp.raise_for_status()
        data = scroll_resp.json()
        scroll_id = data.get("_scroll_id")
        hits = data.get("hits", {}).get("hits", [])

    if scroll_id:
        requests.delete(
            f"http://{mod.ES_HOST}:{mod.ES_PORT}/_search/scroll",
            auth=auth,
            json={"scroll_id": scroll_id},
            timeout=30,
        )


def bulk_delete(mod, doc_ids: list[str]) -> int:
    if not doc_ids:
        return 0

    base = f"http://{mod.ES_HOST}:{mod.ES_PORT}/{mod.PRODUCT_INDEX_NAME}/_bulk"
    auth = (mod.ES_USER, mod.ES_PASSWORD)
    lines: list[str] = []
    for doc_id in doc_ids:
        lines.append(f'{{"delete":{{"_id":"{doc_id}"}}}}')
    body = "\n".join(lines) + "\n"
    resp = requests.post(base, auth=auth, data=body, headers={"Content-Type": "application/x-ndjson"}, timeout=120)
    resp.raise_for_status()
    result = resp.json()
    if result.get("errors"):
        failed = sum(1 for item in result.get("items", []) if item.get("delete", {}).get("error"))
        return len(doc_ids) - failed
    return len(doc_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete invalid AliExpress product records from Elasticsearch.")
    parser.add_argument("--dry-run", action="store_true", help="Only report invalid documents, do not delete.")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Only scan existence=true records (covers all known bad network-error pages).",
    )
    args = parser.parse_args()

    mod = load_alixq3()
    print(f"ES index: {mod.PRODUCT_INDEX_NAME} @ {mod.ES_HOST}:{mod.ES_PORT}")

    invalid_ids: list[str] = []
    invalid_samples: list[str] = []
    scanned = 0

    for hit in iter_product_hits(mod, existence_only=args.fast):
        scanned += 1
        source = hit.get("_source") or {}
        if not mod.is_invalid_product_record(source):
            continue
        doc_id = hit["_id"]
        invalid_ids.append(doc_id)
        if len(invalid_samples) < 10:
            invalid_samples.append(
                f"  {doc_id} | {str(source.get('title', ''))[:60]} | price={source.get('price')}"
            )

    print(f"Scanned: {scanned}")
    print(f"Invalid: {len(invalid_ids)}")
    for line in invalid_samples:
        print(line)
    if len(invalid_ids) > len(invalid_samples):
        print(f"  ... and {len(invalid_ids) - len(invalid_samples)} more")

    if args.dry_run:
        print("Dry run — no documents deleted.")
        return 0

    deleted = 0
    batch_size = 200
    for start in range(0, len(invalid_ids), batch_size):
        batch = invalid_ids[start : start + batch_size]
        deleted += bulk_delete(mod, batch)
        print(f"Deleted {deleted}/{len(invalid_ids)}")

    print(f"Done. Removed {deleted} invalid documents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
