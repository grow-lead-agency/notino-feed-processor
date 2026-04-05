"""
Job 2: JSON-LD Product Page Scraper
Parsuje structured data z 30+ shopů (Schema.org Product).
Interval: 2x denně (6:30, 18:30)
"""
import os
import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import structlog

sys.path.insert(0, "/app")
from jobs.db import upsert_products

logger = structlog.get_logger()

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
})
SESSION.timeout = 20


def extract_jsonld(html: str) -> list[dict]:
    """Extract all JSON-LD blocks from HTML."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
        except json.JSONDecodeError:
            pass
    return results


def find_product(jsonld_list: list[dict]) -> dict | None:
    """Find Schema.org Product in JSON-LD list."""
    for item in jsonld_list:
        t = item.get("@type", "")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            return item
    return None


def parse_price(product: dict) -> tuple[float | None, str | None]:
    """Extract price and currency from Schema.org Product."""
    offers = product.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    price_str = offers.get("price") or offers.get("lowPrice")
    currency = offers.get("priceCurrency", "CZK")

    try:
        price = float(str(price_str).replace(",", ".").replace(" ", ""))
        return price, currency
    except (TypeError, ValueError):
        return None, currency


def scrape_url(url: str, shop: str) -> dict | None:
    """Scrape a single product URL and return structured record."""
    try:
        resp = SESSION.get(url)
        resp.raise_for_status()

        jsonld_list = extract_jsonld(resp.text)
        product = find_product(jsonld_list)
        if not product:
            logger.debug("no_product_jsonld", url=url)
            return None

        price, currency = parse_price(product)

        return {
            "url": url,
            "shop": shop,
            "title": product.get("name", ""),
            "price": price,
            "currency": currency,
            "image_url": product.get("image", [None])[0] if isinstance(product.get("image"), list)
                         else product.get("image"),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "raw_json": json.dumps(product),
        }

    except Exception as e:
        logger.error("scrape_failed", url=url, shop=shop, error=str(e))
        return None


def load_pending_urls(shop: str, limit: int = 500) -> list[str]:
    """Load pending URLs from queue for a given shop."""
    from jobs.db import get_conn
    sql = """
        SELECT url FROM product_url_queue
        WHERE shop = %s AND status = 'pending'
        ORDER BY discovered_at ASC
        LIMIT %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (shop, limit))
            return [row[0] for row in cur.fetchall()]


def mark_done(urls: list[str]):
    from jobs.db import get_conn
    sql = "UPDATE product_url_queue SET status = 'done', processed_at = NOW() WHERE url = ANY(%s)"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (urls,))
        conn.commit()


SHOPS = [
    "notino", "parfums", "kosmetika", "bodylab", "rossmann",
    "dm", "makeup", "fann", "parfemy", "elnino",
    "beautycosi", "elnino_sk", "notino_sk", "dm_sk", "rossmann_sk",
    # Add more as configured
]


def run():
    logger.info("starting_jsonld_scraper")
    for shop in SHOPS:
        urls = load_pending_urls(shop)
        if not urls:
            continue

        records = []
        for url in urls:
            record = scrape_url(url, shop)
            if record:
                records.append(record)

        upsert_products(records)
        mark_done([r["url"] for r in records])
        logger.info("shop_done", shop=shop, scraped=len(records), skipped=len(urls) - len(records))


if __name__ == "__main__":
    run()
