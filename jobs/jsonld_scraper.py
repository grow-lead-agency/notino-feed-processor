"""
Job: JSON-LD Product Page Scraper
Stáhne sitemap product feedy, naparsuje JSON-LD Product schema, upsertne do DB.
Interval: denně (cron: 30 6,18 * * *)
"""
import sys
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, "/app")
from jobs.db import upsert_product_with_offer, get_conn

USER_AGENT_PRODUCT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
USER_AGENT_BOT = "Mozilla/5.0 (compatible; Googlebot/2.1)"
MAX_PRODUCTS_PER_SHOP = 50000  # Full download — Neon Launch plan (10 GB) handles it


def fetch(url, ua=USER_AGENT_PRODUCT, timeout=12):
    """Simple curl-like fetch with UA."""
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=timeout, allow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def get_product_urls_from_sitemap(feed_url, limit=MAX_PRODUCTS_PER_SHOP):
    """Download sitemap XML, extract product URLs."""
    feed_url = feed_url.replace("&amp;", "&")
    html = fetch(feed_url, ua=USER_AGENT_BOT, timeout=30)
    if not html:
        return []
    urls = re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", html)
    return urls[:limit]


def extract_product(url):
    """Fetch product page, extract JSON-LD Product."""
    html = fetch(url, timeout=10)
    if not html:
        return None

    # JSON-LD
    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL | re.I
    )

    for block in blocks:
        try:
            data = json.loads(block)
            items = data if isinstance(data, list) else [data]
            for item in items:
                # Check direct Product
                for candidate in [item] + (item.get("@graph", []) or []):
                    t = candidate.get("@type", "")
                    if "Product" not in str(t):
                        continue

                    offers = candidate.get("offers", {}) or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}

                    price = offers.get("price") or offers.get("lowPrice")

                    brand = candidate.get("brand", "")
                    if isinstance(brand, dict):
                        brand = brand.get("name", "")

                    image = candidate.get("image", "")
                    if isinstance(image, list):
                        image = image[0] if image else ""

                    return {
                        "name": candidate.get("name", ""),
                        "brand": str(brand),
                        "sku": candidate.get("sku", ""),
                        "ean": (
                            candidate.get("gtin13", "") or
                            candidate.get("gtin12", "") or
                            candidate.get("gtin", "") or
                            candidate.get("gtin14", "") or
                            candidate.get("gtin8", "") or
                            candidate.get("mpn", "")
                        ),
                        "price": float(price) if price else None,
                        "currency": offers.get("priceCurrency", "EUR"),
                        "availability": str(offers.get("availability", "")),
                        "image_url": str(image) if image else "",
                        "description": (candidate.get("description", "") or "")[:500],
                        "url": url,
                        "source": "jsonld_scrape",
                        "raw": candidate if len(json.dumps(candidate)) < 5000 else None,
                    }
        except (json.JSONDecodeError, ValueError):
            continue

    return None


def get_shops_to_scrape():
    """Load shops with JSON-LD + product feed from DB."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name, wc.sitemap_product_feed, c.country_id
        FROM website_checks wc
        JOIN competitors c ON c.id = wc.competitor_id
        WHERE wc.sitemap_product_feed IS NOT NULL
          AND wc.difficulty IN ('easy', 'medium')
          AND c.country_id IS NOT NULL
        ORDER BY c.category, c.name
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def volume_ml_from_title(title):
    """Extract volume in ml from product title."""
    if not title:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|g|oz|kg)", title, re.I)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit == "ml" or unit == "g":
        return num
    if unit == "kg":
        return num * 1000
    if unit == "oz":
        return num * 29.5735
    return None


def scrape_shop(shop_id, domain, feed_url, country_id):
    """Scrape all products from a shop. Returns count saved."""
    print(f"[{domain}] Starting scrape...", flush=True)

    urls = get_product_urls_from_sitemap(feed_url)
    if not urls:
        print(f"[{domain}] No product URLs found in sitemap", flush=True)
        return 0

    print(f"[{domain}] Found {len(urls)} product URLs", flush=True)

    saved = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(extract_product, url): url for url in urls}
        for future in as_completed(futures):
            try:
                data = future.result()
                if not data or not data.get("name") or data.get("price") is None:
                    continue

                # Enrich volume
                data["volume_ml"] = volume_ml_from_title(data["name"])

                upsert_product_with_offer(
                    shop_id=shop_id,
                    country_id=country_id,
                    product_data=data,
                )
                saved += 1
            except Exception as e:
                errors += 1
                if errors < 5:
                    print(f"[{domain}] Error: {e}", flush=True)

    print(f"[{domain}] Saved {saved}/{len(urls)} (errors: {errors})", flush=True)
    return saved


def main():
    start_ts = time.time()
    print(f"=== JSONLD Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    shops = get_shops_to_scrape()
    print(f"Found {len(shops)} shops to scrape", flush=True)

    total_saved = 0
    for shop_id, domain, feed_url, country_id in shops:
        try:
            saved = scrape_shop(shop_id, domain, feed_url, country_id)
            total_saved += saved
        except Exception as e:
            print(f"[{domain}] Shop-level error: {e}", flush=True)

    elapsed = time.time() - start_ts
    print(f"=== Done: {total_saved} products saved in {elapsed:.1f}s ===", flush=True)


if __name__ == "__main__":
    main()
