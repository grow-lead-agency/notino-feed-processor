"""
Job 4: Hlídač Shopů API — Price History Enrichment
Volá api.hlidacshopu.cz/v2/detail?url= pro CZ/SK produkty.
Interval: každých 6 hodin (offset od sitemap crawleru)

Výsledky ukládá do hlidac_shopu_data tabulky (standalone, bez FK na products).
"""
import os
import sys
import json
from datetime import datetime, timezone, timedelta

import requests
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn

logger = structlog.get_logger()

HLIDAC_API = "https://api.hlidacshopu.cz/v2/detail"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "FeedProcessor/1.0 (notino-datamining; petr@growlead.cz)",
})
SESSION.timeout = 15


def fetch_price_history(product_url: str) -> dict | None:
    """Fetch price history from Hlídač Shopů API."""
    try:
        resp = SESSION.get(HLIDAC_API, params={"url": product_url})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("hlidac_api_failed", url=product_url, error=str(e))
        return None


def save_price_history(product_url: str, data: dict):
    """Save price history record to hlidac_shopu_data table."""
    sql = """
        INSERT INTO hlidac_shopu_data 
            (product_url, fetched_at, min_price, max_price, current_price, currency, raw_json)
        VALUES (%s, NOW(), %s, %s, %s, %s, %s)
    """
    min_p = data.get("metadata", {}).get("minPrice")
    max_p = data.get("metadata", {}).get("maxPrice")
    curr_p = data.get("currentPrice")
    currency = "CZK"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (product_url, min_p, max_p, curr_p, currency, json.dumps(data)))
        conn.commit()


def was_fetched_today(product_url: str) -> bool:
    """Check if this URL was already fetched today."""
    sql = """
        SELECT 1 FROM hlidac_shopu_data
        WHERE product_url = %s
          AND fetched_at > NOW() - INTERVAL '6 hours'
        LIMIT 1
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (product_url,))
            return cur.fetchone() is not None


def load_urls_for_enrichment(limit: int = 200) -> list[str]:
    """Load product URLs from queue that need Hlídač Shopů enrichment."""
    sql = """
        SELECT DISTINCT q.url
        FROM product_url_queue q
        LEFT JOIN hlidac_shopu_data h ON h.product_url = q.url
            AND h.fetched_at > NOW() - INTERVAL '6 hours'
        WHERE q.status = 'done'
          AND h.product_url IS NULL
          AND (q.url LIKE '%%notino%%'
            OR q.url LIKE '%%parfum%%'
            OR q.url LIKE '%%kosmetika%%'
            OR q.url LIKE '%%dm.cz%%'
            OR q.url LIKE '%%rossmann%%'
            OR q.url LIKE '%%fann%%')
        LIMIT %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [row[0] for row in cur.fetchall()]


def run():
    urls = load_urls_for_enrichment()
    logger.info("starting_hlidac", urls=len(urls))

    enriched = 0
    skipped = 0
    failed = 0

    for url in urls:
        data = fetch_price_history(url)
        if data:
            save_price_history(url, data)
            enriched += 1
        elif data is None:
            skipped += 1  # 404 — not tracked
        else:
            failed += 1

    logger.info("hlidac_done", enriched=enriched, skipped=skipped, failed=failed, total=len(urls))


if __name__ == "__main__":
    run()
