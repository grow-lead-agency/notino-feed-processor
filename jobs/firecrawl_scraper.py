"""
Job 6: Firecrawl-based Product Scraper for HARD shops
Cíl: shopy, které jsonld_scraper.py neumí (Cloudflare challenge, DataDome, JS-render).
Firecrawl managed scraping API projde anti-bot ochrany (stealth proxy).

Workflow:
  1. Načti shopy s difficulty IN ('hard', 'unknown') OR protection IN (...)
  2. Stáhni sitemap URLs (curl — sitemapy obvykle nejsou blokované)
  3. Pro každý product URL → POST https://api.firecrawl.dev/v1/scrape
  4. Parsuj JSON-LD z returned HTML
  5. Upsert do DB jako source='firecrawl_jsonld'

Cron: 1× denně v noci (0 3 * * *)
Budget: max 500 requests per run, 10 concurrent workers.
"""
import os
import sys
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

sys.path.insert(0, "/app")
from jobs.db import upsert_product_with_offer, get_conn

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

USER_AGENT_BOT = "Mozilla/5.0 (compatible; Googlebot/2.1)"

MAX_PRODUCTS_PER_SHOP = 500         # Firecrawl costs — controlled per shop
MAX_REQUESTS_PER_RUN = 2000         # Hard budget cap (safety) — ~$20 per run
MAX_CONCURRENT_WORKERS = 10         # Firecrawl API rate limit
FIRECRAWL_TIMEOUT_SECONDS = 60      # stealth scrapes are slow
FIRECRAWL_MAX_RETRIES = 2           # retry on 429 / transient 5xx

# Difficulty / protection filters — which shops should go through Firecrawl
HARD_DIFFICULTIES = ("hard", "unknown")
HARD_PROTECTIONS = ("cloudflare_challenge", "datadome", "cloudflare_passive")

# ----------------------------------------------------------------------------
# Shared state (thread-safe budget + cost tracking)
# ----------------------------------------------------------------------------
_budget_lock = Lock()
_budget_state = {
    "requests_made": 0,
    "credits_used": 0,
}


def _budget_consume() -> bool:
    """Atomically reserve one request slot. Returns False if budget exhausted."""
    with _budget_lock:
        if _budget_state["requests_made"] >= MAX_REQUESTS_PER_RUN:
            return False
        _budget_state["requests_made"] += 1
        return True


def _budget_add_credits(credits: int) -> None:
    with _budget_lock:
        _budget_state["credits_used"] += credits


# ----------------------------------------------------------------------------
# Sitemap fetch (plain HTTP — sitemapy nejsou běžně chráněné)
# ----------------------------------------------------------------------------
def fetch_sitemap(url: str, timeout: int = 30) -> str | None:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT_BOT},
            timeout=timeout,
            allow_redirects=True,
        )
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def get_product_urls_from_sitemap(feed_url: str, limit: int = MAX_PRODUCTS_PER_SHOP) -> list[str]:
    feed_url = feed_url.replace("&amp;", "&")
    xml = fetch_sitemap(feed_url)
    if not xml:
        return []
    urls = re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", xml)
    return urls[:limit]


# ----------------------------------------------------------------------------
# Firecrawl scrape call
# ----------------------------------------------------------------------------
def firecrawl_scrape(url: str) -> dict | None:
    """
    POST to Firecrawl /v1/scrape with stealth proxy.
    Returns response `data` dict or None on failure.
    """
    if not FIRECRAWL_API_KEY:
        print("[firecrawl] FATAL: FIRECRAWL_API_KEY env var missing", flush=True)
        return None

    # Primary: AI extraction via jsonOptions (works on JS-rendered SPAs)
    # Fallback: HTML for JSON-LD parsing
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
        "timeout": FIRECRAWL_TIMEOUT_SECONDS * 1000,
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                FIRECRAWL_API_URL,
                headers=headers,
                json=payload,
                timeout=FIRECRAWL_TIMEOUT_SECONDS + 10,
            )
        except requests.RequestException as e:
            if attempt < FIRECRAWL_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            print(f"[firecrawl] Network error for {url}: {e}", flush=True)
            return None

        # Retry on rate limit
        if resp.status_code == 429:
            if attempt < FIRECRAWL_MAX_RETRIES:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                time.sleep(min(wait, 30))
                continue
            print(f"[firecrawl] Rate limited (429) — giving up on {url}", flush=True)
            return None

        # Skip on client errors (403 / 404 — page gone or uncrackable)
        if resp.status_code in (403, 404):
            return None

        # Retry transient 5xx
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

        data = body.get("data") or {}
        # Track cost
        credits = body.get("creditsUsed") or data.get("metadata", {}).get("creditsUsed") or 1
        try:
            _budget_add_credits(int(credits))
        except (TypeError, ValueError):
            _budget_add_credits(1)

        return data

    return None


# ----------------------------------------------------------------------------
# JSON-LD extraction (mirrors jsonld_scraper.py logic)
# ----------------------------------------------------------------------------
def parse_jsonld_product(html: str, url: str) -> dict | None:
    if not html:
        return None

    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.I,
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


# ----------------------------------------------------------------------------
# Per-URL worker
# ----------------------------------------------------------------------------
def parse_ai_extraction(json_data: dict, url: str) -> dict | None:
    """Parse Firecrawl's AI-extracted JSON into our product schema."""
    if not json_data or not isinstance(json_data, dict):
        return None

    name = (json_data.get("name") or "").strip()
    price = json_data.get("price")

    # Must have name + price
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

    # Availability normalization
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


def extract_product(url: str) -> dict | None:
    """
    Fetch product page via Firecrawl.
    Primary: AI extraction via jsonOptions (works on JS SPAs).
    Fallback: JSON-LD parsing from HTML.
    """
    if not _budget_consume():
        return None

    data = firecrawl_scrape(url)
    if not data:
        return None

    # Try AI extraction first (most reliable for JS-rendered pages)
    json_extracted = data.get("json")
    if json_extracted:
        result = parse_ai_extraction(json_extracted, url)
        if result and result.get("name") and result.get("price") is not None:
            return result

    # Fallback: JSON-LD from HTML
    html = data.get("html") or data.get("rawHtml") or ""
    return parse_jsonld_product(html, url)


def volume_ml_from_title(title: str) -> float | None:
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


# ----------------------------------------------------------------------------
# Shop-level logic
# ----------------------------------------------------------------------------
def get_shops_to_scrape() -> list[tuple]:
    """
    Load shops that JSON-LD scraper can't handle:
      - difficulty IN ('hard', 'unknown') OR
      - protection IN ('cloudflare_challenge', 'datadome', 'cloudflare_passive')
    Must have sitemap_product_feed + JSON-LD price source.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.name, wc.sitemap_product_feed, c.country_id
        FROM website_checks wc
        JOIN competitors c ON c.id = wc.competitor_id
        WHERE wc.sitemap_product_feed IS NOT NULL
          AND c.country_id IS NOT NULL
          AND (
              wc.difficulty = ANY(%s)
              OR wc.protection = ANY(%s)
          )
        ORDER BY c.category, c.name
        """,
        (list(HARD_DIFFICULTIES), list(HARD_PROTECTIONS)),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def scrape_shop(shop_id: int, domain: str, feed_url: str, country_id: int) -> int:
    """Scrape all products from a shop via Firecrawl. Returns count saved."""
    # Budget check before starting a new shop
    with _budget_lock:
        remaining = MAX_REQUESTS_PER_RUN - _budget_state["requests_made"]
    if remaining <= 0:
        print(f"[{domain}] Budget exhausted — skipping shop", flush=True)
        return 0

    print(f"[{domain}] Starting Firecrawl scrape (budget left: {remaining})...", flush=True)

    urls = get_product_urls_from_sitemap(feed_url)
    if not urls:
        print(f"[{domain}] No product URLs found in sitemap", flush=True)
        return 0

    # Cap per shop also by remaining budget
    urls = urls[: min(len(urls), remaining)]
    print(f"[{domain}] Processing {len(urls)} product URLs via Firecrawl", flush=True)

    saved = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        futures = {executor.submit(extract_product, url): url for url in urls}
        for future in as_completed(futures):
            try:
                data = future.result()
                if not data or not data.get("name") or data.get("price") is None:
                    continue

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


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(
        f"=== Firecrawl Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===",
        flush=True,
    )

    if not FIRECRAWL_API_KEY:
        print("[firecrawl] ERROR: FIRECRAWL_API_KEY env var is not set — aborting.", flush=True)
        sys.exit(1)

    shops = get_shops_to_scrape()
    print(
        f"Found {len(shops)} hard/protected shops | "
        f"budget: {MAX_REQUESTS_PER_RUN} req | "
        f"concurrency: {MAX_CONCURRENT_WORKERS}",
        flush=True,
    )

    total_saved = 0
    for shop_id, domain, feed_url, country_id in shops:
        try:
            saved = scrape_shop(shop_id, domain, feed_url, country_id)
            total_saved += saved
        except Exception as e:
            print(f"[{domain}] Shop-level error: {e}", flush=True)

        # Stop if budget exhausted
        with _budget_lock:
            if _budget_state["requests_made"] >= MAX_REQUESTS_PER_RUN:
                print("[firecrawl] Budget exhausted — stopping shop loop.", flush=True)
                break

    elapsed = time.time() - start_ts
    with _budget_lock:
        reqs = _budget_state["requests_made"]
        credits = _budget_state["credits_used"]

    print(
        f"=== Done: {total_saved} products saved | "
        f"{reqs} requests | {credits} credits used | "
        f"{elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
