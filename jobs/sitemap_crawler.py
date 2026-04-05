"""
Job 1: Sitemap XML Feed Crawler
Stahuje product sitemaps z 316 beauty e-shopů a ukládá URL seznam do DB.
Interval: každých 6 hodin
"""
import os
import sys
import json
import hashlib
import time
from datetime import datetime
from pathlib import Path

import requests
from lxml import etree
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn

logger = structlog.get_logger()

CACHE_DIR = Path("/app/cache/sitemaps")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; FeedProcessor/1.0; +https://growlead.cz/bot)",
    "Accept": "application/xml, text/xml, */*",
})
SESSION.timeout = 30

# Shopů seznam — načítá se z DB nebo ze souboru konfigurace
SHOPS_CONFIG = Path("/app/config/shops_sitemaps.json")


def load_shops() -> list[dict]:
    """Load shop sitemap URLs from config file."""
    if not SHOPS_CONFIG.exists():
        logger.warning("shops_config_missing", path=str(SHOPS_CONFIG))
        return []
    with open(SHOPS_CONFIG) as f:
        return json.load(f)


def fetch_sitemap(url: str, shop_id: str) -> list[str]:
    """Fetch sitemap XML and return list of product URLs."""
    cache_file = CACHE_DIR / f"{shop_id}.xml"

    try:
        resp = SESSION.get(url)
        resp.raise_for_status()
        content = resp.content

        # Cache to disk
        cache_file.write_bytes(content)

        # Parse XML
        root = etree.fromstring(content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        # Handle sitemap index (nested sitemaps)
        if root.tag.endswith("sitemapindex"):
            urls = []
            for sitemap in root.findall("sm:sitemap", ns):
                loc = sitemap.findtext("sm:loc", namespaces=ns)
                if loc:
                    urls.extend(fetch_sitemap(loc, f"{shop_id}_sub_{hashlib.md5(loc.encode()).hexdigest()[:6]}"))
            return urls

        # Regular sitemap
        return [
            loc.text for loc in root.findall(".//sm:loc", ns)
            if loc.text and "/product" in loc.text.lower()
        ]

    except Exception as e:
        logger.error("sitemap_fetch_failed", shop=shop_id, url=url, error=str(e))
        return []


def save_urls_to_db(urls: list[str], shop: str):
    """Save discovered product URLs to crawl queue."""
    if not urls:
        return
    sql = """
        INSERT INTO product_url_queue (url, shop, discovered_at, status)
        VALUES (%s, %s, NOW(), 'pending')
        ON CONFLICT (url) DO NOTHING
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, [(url, shop) for url in urls])
        conn.commit()
    logger.info("urls_queued", shop=shop, count=len(urls))


def run():
    shops = load_shops()
    logger.info("starting", total_shops=len(shops))

    total_urls = 0
    for shop in shops:
        urls = fetch_sitemap(shop["sitemap_url"], shop["id"])
        save_urls_to_db(urls, shop["id"])
        total_urls += len(urls)
        logger.info("shop_done", shop=shop["id"], urls=len(urls))
        time.sleep(0.5)  # polite crawling

    logger.info("completed", total_urls=total_urls)


if __name__ == "__main__":
    run()
