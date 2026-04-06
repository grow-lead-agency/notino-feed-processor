"""
Smart Price Checker for hard/blocked non-CZ/SK shops.

2-layer strategy:
  1. Sitemap diff (free) — detect new/removed products, no prices
  2. Firecrawl top-N (cheap) — price check only top 20 products per shop

Replaces disabled Firecrawl price_check for hard shops.
Cost: ~$0.20/shop/run instead of $5.00/shop/run (96% cheaper).

Usage:
  python jobs/smart_price_checker.py           # batch 10 shops, top 20 per shop
  python jobs/smart_price_checker.py --top 50  # top 50 per shop (for important shops)
"""
import os
import sys
import re
import json
import time
import argparse
from urllib.parse import urlparse

import requests
from lxml import etree

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn, upsert_product_with_offer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

SITEMAP_TIMEOUT = 30
FIRECRAWL_TIMEOUT = 60
FIRECRAWL_RATE_LIMIT_DELAY = 1.0  # 1 req/s for Firecrawl
FIRECRAWL_MAX_RETRIES = 2
MAX_SHOPS_PER_RUN = 10
DEFAULT_TOP_N = 20
MAX_NEW_PRODUCTS = 50  # max new URLs to scrape per shop via Firecrawl

SITEMAP_USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1)"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ---------------------------------------------------------------------------
# Sitemap fetching (plain HTTP — FREE)
# ---------------------------------------------------------------------------
def _fetch_sitemap_xml(url: str) -> bytes | None:
    """Fetch sitemap XML via plain HTTP. Returns raw bytes or None."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": SITEMAP_USER_AGENT},
            timeout=SITEMAP_TIMEOUT,
            allow_redirects=True,
        )
        if r.status_code == 200:
            return r.content
        print(f"  [sitemap] HTTP {r.status_code} for {url}", flush=True)
        return None
    except Exception as e:
        print(f"  [sitemap] Fetch error: {e}", flush=True)
        return None


def _extract_urls_from_sitemap(content: bytes) -> list[str]:
    """Parse sitemap XML, handle sitemap index (recursion)."""
    try:
        root = etree.fromstring(content)
    except etree.XMLSyntaxError:
        # Fallback: regex extraction for malformed XML
        text = content.decode("utf-8", errors="replace")
        return re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", text)

    # Sitemap index → recursively fetch child sitemaps
    if root.tag.endswith("sitemapindex"):
        urls = []
        for sitemap_el in root.findall("sm:sitemap", SITEMAP_NS):
            loc = sitemap_el.findtext("sm:loc", namespaces=SITEMAP_NS)
            if loc:
                child_content = _fetch_sitemap_xml(loc.strip())
                if child_content:
                    urls.extend(_extract_urls_from_sitemap(child_content))
        return urls

    # Regular sitemap
    return [
        loc.text.strip()
        for loc in root.findall(".//sm:loc", SITEMAP_NS)
        if loc.text
    ]


def _is_product_url(url: str) -> bool:
    """Heuristic: filter for product-like URLs."""
    path = urlparse(url).path.lower()
    # Common product URL patterns across EU e-shops
    product_indicators = [
        "/product", "/produkt", "/produit", "/prodotto", "/producto",
        "/p/", "/pd/", "/dp/", "-p-", "/item/", "/artikel/",
        "/sku/", "/detail/",
    ]
    # Exclude non-product pages
    exclude_indicators = [
        "/category", "/kategorie", "/search", "/cart", "/checkout",
        "/login", "/account", "/blog", "/article", "/page/",
        "/faq", "/kontakt", "/contact", "/about", "/impressum",
        "/sitemap", ".xml", "/cdn-cgi/",
    ]
    for ex in exclude_indicators:
        if ex in path:
            return False
    for ind in product_indicators:
        if ind in path:
            return True
    # Fallback: URLs with long paths are often products (e.g. /brand-name-product-100ml-12345)
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and len(parts[-1]) > 10:
        return True
    return False


# ---------------------------------------------------------------------------
# Firecrawl scrape (PAID — rate limited)
# ---------------------------------------------------------------------------
def _firecrawl_scrape_page(url: str) -> dict | None:
    """Scrape a single product page via Firecrawl API. Returns data dict or None."""
    if not FIRECRAWL_API_KEY:
        print("  [firecrawl] FATAL: FIRECRAWL_API_KEY not set", flush=True)
        return None

    product_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Product name/title"},
            "brand": {"type": "string", "description": "Brand name"},
            "price": {"type": "number", "description": "Current price as number"},
            "sale_price": {"type": "number", "description": "Sale price if discounted"},
            "currency": {"type": "string", "description": "Currency code (EUR, CZK, PLN...)"},
            "availability": {"type": "string", "description": "in_stock, out_of_stock, or preorder"},
            "ean": {"type": "string", "description": "EAN/GTIN barcode (13, 12, or 8 digits)"},
            "sku": {"type": "string", "description": "Product SKU/model number"},
            "image_url": {"type": "string", "description": "Main product image URL"},
            "description": {"type": "string", "description": "Short product description"},
            "volume_ml": {"type": "number", "description": "Volume in ml if applicable"},
        },
        "required": ["name", "price"],
    }

    payload = {
        "url": url,
        "formats": ["json", "html"],
        "jsonOptions": {
            "schema": product_schema,
            "prompt": "Extract product information from this e-commerce page.",
        },
        "proxy": "stealth",
        "onlyMainContent": False,
        "waitFor": 2000,
        "timeout": FIRECRAWL_TIMEOUT * 1000,
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
        try:
            time.sleep(FIRECRAWL_RATE_LIMIT_DELAY)
            resp = requests.post(
                FIRECRAWL_API_URL,
                headers=headers,
                json=payload,
                timeout=FIRECRAWL_TIMEOUT + 10,
            )
        except requests.RequestException as e:
            if attempt < FIRECRAWL_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            print(f"  [firecrawl] Network error for {url}: {e}", flush=True)
            return None

        if resp.status_code == 429:
            if attempt < FIRECRAWL_MAX_RETRIES:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                time.sleep(min(wait, 30))
                continue
            print(f"  [firecrawl] Rate limited — giving up on {url}", flush=True)
            return None

        if resp.status_code in (403, 404):
            return None

        if resp.status_code >= 500:
            if attempt < FIRECRAWL_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None

        if resp.status_code != 200:
            return None

        try:
            body = resp.json()
        except ValueError:
            return None

        if not body.get("success"):
            return None

        return body.get("data") or {}

    return None


# ---------------------------------------------------------------------------
# Product data extraction (from Firecrawl response)
# ---------------------------------------------------------------------------
def _parse_ai_extraction(json_data: dict, url: str) -> dict | None:
    """Parse Firecrawl AI-extracted JSON into product data dict."""
    if not json_data or not isinstance(json_data, dict):
        return None

    name = (json_data.get("name") or "").strip()
    price = json_data.get("price")
    if not name or price is None:
        return None

    try:
        price_val = float(price)
    except (TypeError, ValueError):
        return None

    sale_price = json_data.get("sale_price")
    try:
        sale_price_val = float(sale_price) if sale_price else None
    except (TypeError, ValueError):
        sale_price_val = None

    availability_raw = str(json_data.get("availability") or "").lower()
    if "in_stock" in availability_raw or "instock" in availability_raw or "available" in availability_raw:
        availability = "InStock"
    elif "out" in availability_raw or "sold" in availability_raw:
        availability = "OutOfStock"
    elif "preorder" in availability_raw:
        availability = "PreOrder"
    else:
        availability = availability_raw or "InStock"

    return {
        "name": name,
        "brand": str(json_data.get("brand") or ""),
        "sku": str(json_data.get("sku") or ""),
        "ean": str(json_data.get("ean") or ""),
        "price": price_val,
        "sale_price": sale_price_val,
        "currency": (json_data.get("currency") or "EUR").upper(),
        "availability": availability,
        "image_url": str(json_data.get("image_url") or ""),
        "description": (str(json_data.get("description") or ""))[:500],
        "volume_ml": json_data.get("volume_ml"),
        "url": url,
        "source": "firecrawl_jsonld",
        "raw": json_data if len(json.dumps(json_data)) < 5000 else None,
    }


def _parse_jsonld_product(html: str, url: str) -> dict | None:
    """Extract JSON-LD Product from HTML (fallback if AI extraction fails)."""
    if not html:
        return None

    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL | re.I,
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            candidates = [item] + (item.get("@graph", []) or [])
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                t = candidate.get("@type", "")
                if "Product" not in str(t):
                    continue

                offers = candidate.get("offers", {}) or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if not isinstance(offers, dict):
                    offers = {}

                price = offers.get("price") or offers.get("lowPrice")
                brand = candidate.get("brand", "")
                if isinstance(brand, dict):
                    brand = brand.get("name", "")

                image = candidate.get("image", "")
                if isinstance(image, list):
                    image = image[0] if image else ""

                try:
                    price_val = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price_val = None

                return {
                    "name": candidate.get("name", ""),
                    "brand": str(brand),
                    "sku": candidate.get("sku", ""),
                    "ean": (
                        candidate.get("gtin13", "")
                        or candidate.get("gtin12", "")
                        or candidate.get("gtin", "")
                        or candidate.get("gtin14", "")
                        or candidate.get("gtin8", "")
                        or candidate.get("mpn", "")
                    ),
                    "price": price_val,
                    "currency": offers.get("priceCurrency", "EUR"),
                    "availability": str(offers.get("availability", "")),
                    "image_url": str(image) if image else "",
                    "description": (candidate.get("description", "") or "")[:500],
                    "url": url,
                    "source": "firecrawl_jsonld",
                    "raw": candidate if len(json.dumps(candidate)) < 5000 else None,
                }
    return None


def _extract_product_from_firecrawl(url: str) -> dict | None:
    """Scrape product page via Firecrawl. Try AI extraction, fallback to JSON-LD."""
    data = _firecrawl_scrape_page(url)
    if not data:
        return None

    # Try AI extraction first
    json_extracted = data.get("json")
    if json_extracted:
        result = _parse_ai_extraction(json_extracted, url)
        if result and result.get("name") and result.get("price") is not None:
            return result

    # Fallback: JSON-LD from HTML
    html = data.get("html") or data.get("rawHtml") or ""
    return _parse_jsonld_product(html, url)


def _volume_ml_from_title(title: str) -> float | None:
    """Extract volume in ml from product title."""
    if not title:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|g|oz|kg)", title, re.I)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit in ("ml", "g"):
        return num
    if unit == "kg":
        return num * 1000
    if unit == "oz":
        return num * 29.5735
    return None


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------
def get_hard_shops_for_check() -> list[dict]:
    """
    Get hard/blocked non-CZ/SK shops that haven't been price-checked recently.
    Uses offers_checked_at (existing column on shops table).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.name, s.url, wc.sitemap_product_feed, wc.difficulty,
                       s.country_id
                FROM shops s
                JOIN website_checks wc ON wc.shop_id = s.id
                JOIN countries c ON c.id = s.country_id
                WHERE wc.difficulty IN ('hard', 'blocked')
                  AND c.iso2 NOT IN ('CZ', 'SK')
                  AND s.deleted_at IS NULL
                  AND wc.sitemap_product_feed IS NOT NULL
                  AND (s.offers_checked_at IS NULL
                       OR s.offers_checked_at < NOW() - INTERVAL '7 days')
                ORDER BY s.offers_checked_at ASC NULLS FIRST
                LIMIT %s
            """, (MAX_SHOPS_PER_RUN,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def _get_existing_offer_urls(shop_id: int) -> set[str]:
    """Get all known product_offer URLs for a shop."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_url FROM product_offers WHERE shop_id = %s AND is_active = TRUE",
                (shop_id,)
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        put_conn(conn)


def _get_top_n_offers(shop_id: int, n: int) -> list[dict]:
    """
    Get top N product offers for a shop, ordered by most recently created.
    Returns list of {offer_id, product_id, product_url, price, country_id}.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, product_id, product_url, price, country_id
                FROM product_offers
                WHERE shop_id = %s AND is_active = TRUE AND deleted_at IS NULL
                ORDER BY created_at DESC
                LIMIT %s
            """, (shop_id, n))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def _update_offer_price(offer_id: int, product_id: int, shop_id: int,
                        country_id: int, new_price: float, sale_price: float | None,
                        currency: str, availability: str | None) -> bool:
    """Update price on an existing offer + insert price history. Returns True if changed."""
    # Normalize availability
    availability_enum = None
    if availability:
        avail = str(availability).lower()
        if "instock" in avail or "in_stock" in avail or avail == "true":
            availability_enum = "in_stock"
        elif "outofstock" in avail or "out_of_stock" in avail or avail == "false":
            availability_enum = "out_of_stock"
        elif "preorder" in avail:
            availability_enum = "preorder"
        elif "backorder" in avail:
            availability_enum = "backorder"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Get current price
            cur.execute("SELECT price, availability FROM product_offers WHERE id = %s", (offer_id,))
            row = cur.fetchone()
            if not row:
                return False

            old_price, old_availability = row
            price_changed = (old_price != new_price or old_availability != availability_enum)

            # Update offer
            cur.execute("""
                UPDATE product_offers
                SET price = %s, sale_price = %s, price_currency = %s,
                    availability = %s::availability_status,
                    last_seen_at = NOW(), updated_at = NOW()
                WHERE id = %s
            """, (new_price, sale_price, currency, availability_enum, offer_id))

            # Insert price history if changed
            if price_changed:
                cur.execute("""
                    INSERT INTO offer_price_history
                    (offer_id, product_id, shop_id, country_id,
                     price, sale_price, price_currency, availability, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s,
                            %s::availability_status, 'firecrawl_jsonld'::feed_source)
                """, (offer_id, product_id, shop_id, country_id,
                      new_price, sale_price, currency, availability_enum))

            conn.commit()
            return price_changed
    finally:
        put_conn(conn)


def _update_shop_checked(shop_id: int) -> None:
    """Update shops.offers_checked_at timestamp."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shops SET offers_checked_at = NOW() WHERE id = %s",
                (shop_id,)
            )
            conn.commit()
    finally:
        put_conn(conn)


def _log_firecrawl_usage(shop_name: str, credits: int, pages: int,
                         success: int) -> None:
    """Log Firecrawl usage for cost tracking."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO firecrawl_usage
                (shop_domain, credits_used, pages_scraped, pages_success, cost_usd)
                VALUES (%s, %s, %s, %s, %s)
            """, (shop_name, credits, pages, success, round(pages * 0.01, 4)))
            conn.commit()
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Layer 1: Sitemap Diff (FREE)
# ---------------------------------------------------------------------------
def sitemap_diff(shop_id: int, shop_name: str,
                 sitemap_url: str) -> dict:
    """
    Fetch sitemap XML (free), compare URLs with DB.
    Returns {new_urls, removed_urls, existing_count, total_sitemap_urls}.
    """
    print(f"  [sitemap-diff] Fetching {sitemap_url}", flush=True)

    content = _fetch_sitemap_xml(sitemap_url)
    if not content:
        print(f"  [sitemap-diff] Failed to fetch sitemap", flush=True)
        return {"new_urls": [], "removed_urls": [], "existing_count": 0,
                "total_sitemap_urls": 0}

    all_urls = _extract_urls_from_sitemap(content)
    product_urls = [u for u in all_urls if _is_product_url(u)]
    sitemap_set = set(product_urls)

    print(f"  [sitemap-diff] {len(all_urls)} total URLs, {len(product_urls)} product URLs", flush=True)

    # Compare with DB
    db_urls = _get_existing_offer_urls(shop_id)
    new_urls = list(sitemap_set - db_urls)
    removed_urls = list(db_urls - sitemap_set)
    existing_count = len(db_urls & sitemap_set)

    print(f"  [sitemap-diff] New: {len(new_urls)}, Removed: {len(removed_urls)}, "
          f"Existing: {existing_count}", flush=True)

    return {
        "new_urls": new_urls,
        "removed_urls": removed_urls,
        "existing_count": existing_count,
        "total_sitemap_urls": len(product_urls),
    }


# ---------------------------------------------------------------------------
# Layer 2: Firecrawl Top-N Price Check (CHEAP)
# ---------------------------------------------------------------------------
def price_check_top_n(shop_id: int, shop_name: str, country_id: int,
                      n: int = DEFAULT_TOP_N) -> list[dict]:
    """
    Scrape top N product offers for price updates.
    Returns list of {offer_id, old_price, new_price, changed}.
    """
    offers = _get_top_n_offers(shop_id, n)
    if not offers:
        print(f"  [price-check] No existing offers for shop {shop_name}", flush=True)
        return []

    print(f"  [price-check] Checking {len(offers)} offers via Firecrawl...", flush=True)

    results = []
    success_count = 0

    for i, offer in enumerate(offers, 1):
        url = offer["product_url"]
        old_price = float(offer["price"]) if offer["price"] is not None else None

        product_data = _extract_product_from_firecrawl(url)
        if not product_data or product_data.get("price") is None:
            results.append({
                "offer_id": offer["id"],
                "old_price": old_price,
                "new_price": None,
                "changed": False,
                "error": "extraction_failed",
            })
            continue

        new_price = product_data["price"]
        sale_price = product_data.get("sale_price")
        currency = product_data.get("currency", "EUR")
        availability = product_data.get("availability")

        changed = _update_offer_price(
            offer_id=offer["id"],
            product_id=offer["product_id"],
            shop_id=shop_id,
            country_id=offer["country_id"],
            new_price=new_price,
            sale_price=sale_price,
            currency=currency,
            availability=availability,
        )

        success_count += 1
        results.append({
            "offer_id": offer["id"],
            "old_price": old_price,
            "new_price": new_price,
            "changed": changed,
        })

        if i % 5 == 0:
            print(f"  [price-check] {i}/{len(offers)} done...", flush=True)

    print(f"  [price-check] {success_count}/{len(offers)} successfully checked, "
          f"{sum(1 for r in results if r.get('changed'))} prices changed", flush=True)

    return results


# ---------------------------------------------------------------------------
# Handle new products from sitemap diff
# ---------------------------------------------------------------------------
def handle_new_products(shop_id: int, shop_name: str, country_id: int,
                        new_urls: list[str]) -> int:
    """
    Scrape newly discovered product URLs via Firecrawl.
    Limited to MAX_NEW_PRODUCTS per shop.
    Returns number of products saved.
    """
    if not new_urls:
        return 0

    urls_to_scrape = new_urls[:MAX_NEW_PRODUCTS]
    print(f"  [new-products] Scraping {len(urls_to_scrape)} new URLs "
          f"(of {len(new_urls)} discovered)...", flush=True)

    saved = 0
    errors = 0

    for i, url in enumerate(urls_to_scrape, 1):
        product_data = _extract_product_from_firecrawl(url)
        if not product_data or not product_data.get("name") or product_data.get("price") is None:
            errors += 1
            continue

        # Enrich volume
        product_data["volume_ml"] = _volume_ml_from_title(product_data["name"])

        try:
            upsert_product_with_offer(
                shop_id=shop_id,
                country_id=country_id,
                product_data=product_data,
            )
            saved += 1
        except Exception as e:
            errors += 1
            if errors < 5:
                print(f"  [new-products] Upsert error: {e}", flush=True)

        if i % 10 == 0:
            print(f"  [new-products] {i}/{len(urls_to_scrape)} done...", flush=True)

    print(f"  [new-products] Saved {saved}/{len(urls_to_scrape)} (errors: {errors})", flush=True)
    return saved


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_smart_price_check(top_n: int = DEFAULT_TOP_N) -> dict:
    """
    Main entry. Processes hard/blocked non-CZ/SK shops with 2-layer strategy.
    Returns stats dict.
    """
    start_ts = time.time()
    print(f"=== Smart Price Checker starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===",
          flush=True)
    print(f"Config: top_n={top_n}, max_shops={MAX_SHOPS_PER_RUN}, "
          f"max_new={MAX_NEW_PRODUCTS}", flush=True)

    if not FIRECRAWL_API_KEY:
        print("[FATAL] FIRECRAWL_API_KEY not set — aborting.", flush=True)
        return {"error": "FIRECRAWL_API_KEY not set"}

    shops = get_hard_shops_for_check()
    print(f"Found {len(shops)} hard/blocked non-CZ/SK shops to check\n", flush=True)

    if not shops:
        print("No shops need checking — all up to date.", flush=True)
        return {
            "shops_checked": 0,
            "prices_checked": 0,
            "prices_changed": 0,
            "new_products_found": 0,
            "new_products_saved": 0,
            "firecrawl_requests": 0,
            "estimated_cost_usd": 0,
        }

    stats = {
        "shops_checked": 0,
        "prices_checked": 0,
        "prices_changed": 0,
        "new_products_found": 0,
        "new_products_saved": 0,
        "firecrawl_requests": 0,
        "shop_details": [],
    }

    for i, shop in enumerate(shops, 1):
        shop_id = shop["id"]
        shop_name = shop["name"]
        sitemap_url = shop["sitemap_product_feed"]
        country_id = shop["country_id"]
        difficulty = shop.get("difficulty", "hard")

        print(f"--- [{i}/{len(shops)}] {shop_name} (id={shop_id}, "
              f"difficulty={difficulty}) ---", flush=True)

        shop_stats = {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "difficulty": difficulty,
        }
        shop_fc_requests = 0

        # Layer 1: Sitemap diff (FREE)
        diff = sitemap_diff(shop_id, shop_name, sitemap_url)
        shop_stats["sitemap_total"] = diff["total_sitemap_urls"]
        shop_stats["new_urls"] = len(diff["new_urls"])
        shop_stats["removed_urls"] = len(diff["removed_urls"])
        shop_stats["existing"] = diff["existing_count"]

        stats["new_products_found"] += len(diff["new_urls"])

        # Layer 2a: Price check top-N existing products
        price_results = price_check_top_n(shop_id, shop_name, country_id, n=top_n)
        checked = len([r for r in price_results if r.get("new_price") is not None])
        changed = sum(1 for r in price_results if r.get("changed"))
        shop_fc_requests += len(price_results)

        shop_stats["prices_checked"] = checked
        shop_stats["prices_changed"] = changed
        stats["prices_checked"] += checked
        stats["prices_changed"] += changed

        # Layer 2b: Handle new products from sitemap diff
        new_saved = 0
        if diff["new_urls"]:
            new_saved = handle_new_products(shop_id, shop_name, country_id,
                                            diff["new_urls"])
            shop_fc_requests += min(len(diff["new_urls"]), MAX_NEW_PRODUCTS)

        shop_stats["new_products_saved"] = new_saved
        stats["new_products_saved"] += new_saved
        stats["firecrawl_requests"] += shop_fc_requests

        # Update shop checked timestamp
        _update_shop_checked(shop_id)
        stats["shops_checked"] += 1
        stats["shop_details"].append(shop_stats)

        # Log Firecrawl usage
        if shop_fc_requests > 0:
            _log_firecrawl_usage(shop_name, shop_fc_requests,
                                 shop_fc_requests, checked + new_saved)

        print(f"  [done] Prices: {checked} checked / {changed} changed, "
              f"New: {new_saved} saved, FC requests: {shop_fc_requests}\n", flush=True)

    elapsed = time.time() - start_ts
    cost = round(stats["firecrawl_requests"] * 0.01, 2)
    stats["estimated_cost_usd"] = cost
    stats["duration_s"] = round(elapsed, 1)

    print(f"{'=' * 60}", flush=True)
    print(f"DONE in {elapsed:.1f}s", flush=True)
    print(f"  Shops checked:      {stats['shops_checked']}", flush=True)
    print(f"  Prices checked:     {stats['prices_checked']}", flush=True)
    print(f"  Prices changed:     {stats['prices_changed']}", flush=True)
    print(f"  New products found: {stats['new_products_found']}", flush=True)
    print(f"  New products saved: {stats['new_products_saved']}", flush=True)
    print(f"  Firecrawl requests: {stats['firecrawl_requests']}", flush=True)
    print(f"  Estimated cost:     ${cost}", flush=True)
    print(f"{'=' * 60}", flush=True)

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart Price Checker for hard shops")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                        help=f"Top N products to price-check per shop (default: {DEFAULT_TOP_N})")
    args = parser.parse_args()

    # Load env from parent .env if running locally
    env_file = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if os.path.exists(env_file):
        print(f"Loading env from {env_file}", flush=True)
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

    result = run_smart_price_check(top_n=args.top)
    sys.exit(0 if "error" not in result else 1)
