"""
Job 4: Hlídač Shopů API — Price History Enrichment
Volá api.hlidacshopu.cz/v2/detail?url= pro CZ/SK produkty.
Interval: každých 6 hodin (offset od sitemap crawleru)
"""
import os
import sys
import json
from datetime import datetime, timezone

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
    """Save price history record to DB."""
    sql = """
        INSERT INTO price_history (product_url, fetched_at, min_price, max_price,
                                    current_price, currency, raw_json)
        VALUES (%s, NOW(), %s, %s, %s, %s, %s)
        ON CONFLICT (product_url, fetched_at::date)
        DO UPDATE SET raw_json = EXCLUDED.raw_json,
                      current_price = EXCLUDED.current_price
    """
    min_p = data.get("metadata", {}).get("minPrice")
    max_p = data.get("metadata", {}).get("maxPrice")
    curr_p = data.get("currentPrice")
    currency = "CZK"  # API is CZ/SK only

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (product_url, min_p, max_p, curr_p, currency, json.dumps(data)))
        conn.commit()


def load_urls_for_enrichment(limit: int = 200) -> list[str]:
    """Load product URLs that need price history refresh (done today < 1)."""
    sql = """
        SELECT DISTINCT p.url
        FROM products p
        LEFT JOIN price_history ph ON ph.product_url = p.url
            AND ph.fetched_at > NOW() - INTERVAL '6 hours'
        WHERE ph.product_url IS NULL
          AND p.url LIKE '%notino%' OR p.url LIKE '%parfum%' OR p.url LIKE '%kosmetika%'
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
    for url in urls:
        data = fetch_price_history(url)
        if data:
            save_price_history(url, data)
            enriched += 1

    logger.info("hlidac_done", enriched=enriched, total=len(urls))


if __name__ == "__main__":
    run()
