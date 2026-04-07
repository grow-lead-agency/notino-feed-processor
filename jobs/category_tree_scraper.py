"""
Job: Category Tree Scraper
Extracts navigation category tree from e-shops into shop_categories.
Scrape type: category_tree, interval: 30d

3 extraction strategies (cascading):
  1. JSON-LD BreadcrumbList from product pages (cheapest — uses existing DB data)
  2. Sitemap category parsing (medium — HTTP fetch only)
  3. Firecrawl map (fallback for JS-heavy shops — costs Firecrawl credits)

Usage:
  # Standalone
  python jobs/category_tree_scraper.py

  # From queue_worker (scrape_type=category_tree)
  scrape_category_tree(shop_id, domain, shop_url)
"""
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.langfuse_wrapper import traced_generation


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1)"
REQUEST_TIMEOUT = 15
MAX_PRODUCT_SAMPLE = 50       # How many product pages to sample for BreadcrumbList
MAX_SITEMAP_CATEGORIES = 5000  # Safety cap per shop
MAX_FIRECRAWL_URLS = 2000     # Firecrawl /map cap
REQUEST_DELAY = 0.5           # 2 req/s rate limit
MAX_SHOPS_PER_RUN = 30

# URL segments that indicate NON-category pages (skip them in sitemap/firecrawl)
SKIP_SEGMENTS = {
    "product", "produkt", "p", "detail", "item", "zbozi",
    "cart", "checkout", "kosik", "pokladna",
    "account", "login", "register", "ucet", "prihlaseni",
    "blog", "article", "clanek", "magazin", "journal",
    "contact", "kontakt", "about", "o-nas",
    "faq", "help", "pomoc", "podpora",
    "terms", "privacy", "gdpr", "cookies", "obchodni-podminky",
    "shipping", "doprava", "delivery", "versand",
    "search", "hledani", "vyhledavani",
    "sitemap", "feed", "rss", "xml", "api",
    "wishlist", "compare", "porovnani",
    "static", "assets", "images", "media", "cdn",
}

# Sitemap path candidates for category sitemaps
SITEMAP_CANDIDATES = [
    "sitemap-categories.xml",
    "sitemap_categories.xml",
    "sitemap-category.xml",
    "category-sitemap.xml",
    "sitemap_category.xml",
    "sitemap.xml",           # generic — will filter URLs
]

# XML namespace
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> str | None:
    """Simple GET with rate limiting and error handling."""
    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def _normalize_name(name: str) -> str:
    """Lowercase, remove accents, collapse whitespace."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents.lower()).strip()


def _slugify(text: str) -> str:
    """Generate URL-safe slug from text."""
    if not text:
        return ""
    norm = _normalize_name(text)
    return re.sub(r"\s+", "-", norm).strip("-")[:200]


def _is_category_url(url: str, base_domain: str) -> bool:
    """Heuristic: does this URL look like a category page (not product/static)?"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # Must be same domain
    if base_domain not in parsed.netloc:
        return False

    path = parsed.path.strip("/").lower()
    if not path:
        return False  # homepage

    segments = path.split("/")

    # Skip if any segment matches product/static patterns
    for seg in segments:
        if seg in SKIP_SEGMENTS:
            return False

    # Skip URLs with numeric-heavy segments (product IDs like /p/12345)
    for seg in segments:
        digits = sum(1 for c in seg if c.isdigit())
        if len(seg) > 3 and digits / max(len(seg), 1) > 0.6:
            return False

    # Skip file extensions
    if re.search(r"\.(jpg|png|gif|pdf|css|js|json|xml|ico|svg|webp)$", path, re.I):
        return False

    # Category URLs typically have 1-5 path segments
    if len(segments) > 6:
        return False

    return True


def _url_to_breadcrumb(url: str, base_url: str) -> list[str]:
    """Extract breadcrumb-like path from URL segments."""
    try:
        parsed = urlparse(url)
        base_parsed = urlparse(base_url)
    except Exception:
        return []

    path = parsed.path.strip("/")
    base_path = base_parsed.path.strip("/")

    # Remove base path prefix if present
    if base_path and path.startswith(base_path):
        path = path[len(base_path):].strip("/")

    if not path:
        return []

    segments = path.split("/")
    # Clean up segments: remove extensions, replace hyphens with spaces
    breadcrumb = []
    for seg in segments:
        clean = re.sub(r"\.(html?|php|aspx?)$", "", seg, flags=re.I)
        clean = clean.replace("-", " ").replace("_", " ").strip()
        if clean:
            breadcrumb.append(clean.title())

    return breadcrumb


# ---------------------------------------------------------------------------
# Strategy 1: JSON-LD BreadcrumbList from existing product pages
# ---------------------------------------------------------------------------
def extract_breadcrumbs_from_db(shop_id: int) -> list[dict]:
    """
    Query existing product_offers for this shop, fetch a sample of product pages,
    and parse JSON-LD BreadcrumbList to reconstruct the category tree.

    Returns list of category dicts: {name, url, level, breadcrumb, parent_breadcrumb}
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT product_url FROM (
                    SELECT DISTINCT product_url
                    FROM product_offers
                    WHERE shop_id = %s AND is_active = TRUE AND product_url IS NOT NULL
                ) sub
                ORDER BY random()
                LIMIT %s
                """,
                (shop_id, MAX_PRODUCT_SAMPLE),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    if not rows:
        return []

    urls = [r[0] for r in rows]
    print(f"  [breadcrumb] Sampling {len(urls)} product pages for BreadcrumbList...", flush=True)

    # Collect all unique category paths from breadcrumbs
    # key = (level, breadcrumb_tuple) -> category dict
    seen_categories: dict[tuple, dict] = {}

    for url in urls:
        html = _fetch(url, timeout=10)
        if not html:
            continue

        # Parse JSON-LD blocks
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
                # Check direct BreadcrumbList or inside @graph
                candidates = [item] + (item.get("@graph", []) or [])
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    t = candidate.get("@type", "")
                    if "BreadcrumbList" not in str(t):
                        continue

                    elements = candidate.get("itemListElement", [])
                    if not isinstance(elements, list) or not elements:
                        continue

                    # Sort by position
                    try:
                        elements = sorted(elements, key=lambda e: int(e.get("position", 0)))
                    except (TypeError, ValueError):
                        pass

                    # Build category entries from breadcrumb trail
                    # Skip first element (usually "Home") and last (usually the product)
                    trail = []
                    for elem in elements:
                        if not isinstance(elem, dict):
                            continue
                        name = ""
                        elem_url = ""
                        item_data = elem.get("item", {})
                        if isinstance(item_data, dict):
                            name = item_data.get("name", "") or elem.get("name", "")
                            elem_url = item_data.get("@id", "") or item_data.get("url", "")
                        elif isinstance(item_data, str):
                            elem_url = item_data
                            name = elem.get("name", "")
                        else:
                            name = elem.get("name", "")

                        name = str(name).strip()
                        elem_url = str(elem_url).strip()

                        if not name:
                            continue

                        trail.append({"name": name, "url": elem_url})

                    # Skip home (position 1) and product page (last)
                    if len(trail) >= 2:
                        trail = trail[:-1]  # remove product (last)
                        # Remove "Home"/"Domů" etc from first position
                        if trail and trail[0]["name"].lower() in (
                            "home", "domů", "domov", "hlavní strana",
                            "úvod", "startseite", "accueil", "inicio",
                        ):
                            trail = trail[1:]

                    # Register each level as a category
                    for i, entry in enumerate(trail):
                        bc = tuple(e["name"] for e in trail[: i + 1])
                        key = (i, bc)
                        if key not in seen_categories:
                            seen_categories[key] = {
                                "name": entry["name"],
                                "url": entry["url"],
                                "level": i,
                                "breadcrumb": list(bc),
                                "parent_breadcrumb": list(bc[:-1]) if i > 0 else None,
                            }

    categories = list(seen_categories.values())
    print(f"  [breadcrumb] Found {len(categories)} unique categories from BreadcrumbList", flush=True)
    return categories


# ---------------------------------------------------------------------------
# Strategy 2: Sitemap category parsing
# ---------------------------------------------------------------------------
def extract_categories_from_sitemap(shop_url: str) -> list[dict]:
    """
    Try to find a category sitemap (or the main sitemap) and extract category URLs.
    Returns list of category dicts.
    """
    base = shop_url.rstrip("/")
    parsed_base = urlparse(base)
    base_domain = parsed_base.netloc

    for sitemap_path in SITEMAP_CANDIDATES:
        sitemap_url = f"{base}/{sitemap_path}"
        xml_text = _fetch(sitemap_url, timeout=20)
        if not xml_text:
            continue

        # Check if it's a sitemap index (contains <sitemapindex>)
        if "<sitemapindex" in xml_text.lower():
            # Parse sitemap index and look for category sitemaps
            try:
                root = ElementTree.fromstring(xml_text)
            except ElementTree.ParseError:
                continue

            child_urls = []
            for sitemap_elem in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS):
                loc = (sitemap_elem.text or "").strip()
                if loc and any(kw in loc.lower() for kw in ("categor", "katalog", "rubrik")):
                    child_urls.append(loc)

            # If no category-specific sitemaps found, try the first few generic ones
            if not child_urls:
                for sitemap_elem in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS):
                    loc = (sitemap_elem.text or "").strip()
                    if loc and "product" not in loc.lower():
                        child_urls.append(loc)
                    if len(child_urls) >= 3:
                        break

            # Parse found child sitemaps
            all_urls = []
            for child_url in child_urls:
                child_xml = _fetch(child_url, timeout=20)
                if child_xml:
                    all_urls.extend(_parse_sitemap_urls(child_xml))

            if all_urls:
                return _urls_to_categories(all_urls, base, base_domain)
            continue

        # Regular sitemap
        urls = _parse_sitemap_urls(xml_text)
        if urls:
            categories = _urls_to_categories(urls, base, base_domain)
            if categories:
                print(f"  [sitemap] Found {len(categories)} categories from {sitemap_path}", flush=True)
                return categories

    return []


def _parse_sitemap_urls(xml_text: str) -> list[str]:
    """Parse <loc> elements from sitemap XML."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        # Fallback: regex parse
        return re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", xml_text)

    urls = []
    for loc in root.findall(".//sm:loc", SITEMAP_NS):
        url = (loc.text or "").strip()
        if url:
            urls.append(url)

    # Try without namespace (some sitemaps don't use it)
    if not urls:
        for loc in root.iter("loc"):
            url = (loc.text or "").strip()
            if url:
                urls.append(url)

    return urls[:MAX_SITEMAP_CATEGORIES]


def _urls_to_categories(urls: list[str], base_url: str, base_domain: str) -> list[dict]:
    """Convert a list of URLs to category dicts using URL path analysis."""
    # Filter to category-like URLs
    cat_urls = [u for u in urls if _is_category_url(u, base_domain)]

    if not cat_urls:
        return []

    # Deduplicate
    cat_urls = list(dict.fromkeys(cat_urls))

    # Build tree from URL paths
    seen: dict[tuple, dict] = {}
    for url in cat_urls:
        breadcrumb = _url_to_breadcrumb(url, base_url)
        if not breadcrumb:
            continue

        # Register each level
        for i in range(len(breadcrumb)):
            bc = tuple(breadcrumb[: i + 1])
            if bc not in seen:
                # Build URL for intermediate levels (if not the full URL)
                if i == len(breadcrumb) - 1:
                    cat_url = url
                else:
                    # Reconstruct intermediate URL
                    segments = url.split("/")
                    parsed = urlparse(url)
                    path_parts = parsed.path.strip("/").split("/")
                    intermediate_path = "/".join(path_parts[: i + 1])
                    cat_url = f"{parsed.scheme}://{parsed.netloc}/{intermediate_path}"

                seen[bc] = {
                    "name": breadcrumb[i],
                    "url": cat_url,
                    "level": i,
                    "breadcrumb": list(bc),
                    "parent_breadcrumb": list(bc[:-1]) if i > 0 else None,
                }

    return list(seen.values())


# ---------------------------------------------------------------------------
# Strategy 3: Firecrawl /map
# ---------------------------------------------------------------------------
def extract_categories_from_firecrawl(shop_url: str) -> list[dict]:
    """
    Use Firecrawl /v1/map to get all URLs from a shop,
    then filter for category-like pages. Traced via Langfuse.
    """
    if not FIRECRAWL_API_KEY:
        print("  [firecrawl] FIRECRAWL_API_KEY missing — skipping", flush=True)
        return []

    print(f"  [firecrawl] Calling /map for {shop_url}...", flush=True)

    with traced_generation(
        name="firecrawl-category-map",
        model="firecrawl/map",
        input_data={"url": shop_url, "limit": MAX_FIRECRAWL_URLS},
        metadata={"job": "category_tree_scraper"},
        tags=["feed-processor", "firecrawl", "category-map"],
    ) as gen:
        try:
            resp = requests.post(
                "https://api.firecrawl.dev/v1/map",
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "url": shop_url,
                    "limit": MAX_FIRECRAWL_URLS,
                },
                timeout=60,
            )
        except requests.RequestException as e:
            print(f"  [firecrawl] Network error: {e}", flush=True)
            gen.end(level="ERROR", status_message=f"Network error: {e}")
            return []

        if resp.status_code != 200:
            print(f"  [firecrawl] HTTP {resp.status_code}", flush=True)
            gen.end(level="WARNING", status_message=f"HTTP {resp.status_code}")
            return []

        try:
            body = resp.json()
        except ValueError:
            gen.end(level="ERROR", status_message="Invalid JSON response")
            return []

        links = body.get("links", [])
        if not isinstance(links, list):
            gen.end(level="WARNING", status_message="No links in response")
            return []

        print(f"  [firecrawl] Got {len(links)} URLs from /map", flush=True)

        parsed_base = urlparse(shop_url)
        base_domain = parsed_base.netloc

        categories = _urls_to_categories(links, shop_url, base_domain)
        print(f"  [firecrawl] Filtered to {len(categories)} category URLs", flush=True)

        gen.end(
            output={"total_urls": len(links), "category_urls": len(categories)},
            usage={"total": 1},
        )
        return categories


# ---------------------------------------------------------------------------
# Upsert into shop_categories (+ product_shop_categories bridge)
# ---------------------------------------------------------------------------
def upsert_categories(shop_id: int, categories: list[dict]) -> int:
    """
    Batch upsert categories into shop_categories.
    Resolves parent_id via breadcrumb matching.
    Returns count of upserted rows.

    UNIQUE constraint: (shop_id, url)
    """
    if not categories:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # First pass: upsert all categories (without parent_id)
            # Map breadcrumb_tuple -> db id for parent resolution
            bc_to_id: dict[tuple, int] = {}
            upserted = 0

            # Sort by level so parents are inserted before children
            categories_sorted = sorted(categories, key=lambda c: c.get("level", 0))

            for cat in categories_sorted:
                name = (cat.get("name") or "").strip()
                url = (cat.get("url") or "").strip()

                if not name or not url:
                    continue

                level = cat.get("level", 0)
                breadcrumb = cat.get("breadcrumb") or [name]
                bc_key = tuple(breadcrumb)
                slug = _slugify(name)
                name_normalized = _normalize_name(name)
                path = " > ".join(breadcrumb)

                # Resolve parent_id from previously inserted parents
                parent_id = None
                parent_bc = cat.get("parent_breadcrumb")
                if parent_bc:
                    parent_key = tuple(parent_bc)
                    parent_id = bc_to_id.get(parent_key)

                try:
                    cur.execute(
                        """
                        INSERT INTO shop_categories (
                            shop_id, url, slug, name, name_normalized,
                            level, path, breadcrumb, parent_id,
                            scraped_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (shop_id, url) DO UPDATE SET
                            name = EXCLUDED.name,
                            name_normalized = EXCLUDED.name_normalized,
                            slug = EXCLUDED.slug,
                            level = EXCLUDED.level,
                            path = EXCLUDED.path,
                            breadcrumb = EXCLUDED.breadcrumb,
                            parent_id = COALESCE(EXCLUDED.parent_id, shop_categories.parent_id),
                            scraped_at = NOW(),
                            updated_at = NOW()
                        RETURNING id
                        """,
                        (
                            shop_id, url, slug, name, name_normalized,
                            level, path, breadcrumb, parent_id,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        bc_to_id[bc_key] = row[0]
                        upserted += 1
                except Exception as e:
                    print(f"  [upsert] Error for '{name}': {e}", flush=True)
                    conn.rollback()
                    continue

            conn.commit()

        return upserted
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main entry point (called from queue_worker or standalone)
# ---------------------------------------------------------------------------
def scrape_category_tree(
    shop_id: int,
    domain: str,
    shop_url: str,
    config_override: dict | None = None,
) -> int:
    """
    Main entry point for category tree scraping.
    Tries 3 strategies in order: BreadcrumbList, sitemap, Firecrawl map.

    Returns count of upserted categories.
    """
    print(f"[{domain}] Starting category tree scrape...", flush=True)
    start = time.time()

    categories: list[dict] = []

    # Strategy 1: JSON-LD BreadcrumbList from existing product pages
    print(f"[{domain}] Strategy 1: JSON-LD BreadcrumbList...", flush=True)
    try:
        categories = extract_breadcrumbs_from_db(shop_id)
    except Exception as e:
        print(f"[{domain}] BreadcrumbList error: {e}", flush=True)

    # Strategy 2: Sitemap parsing
    if not categories:
        print(f"[{domain}] Strategy 2: Sitemap parsing...", flush=True)
        try:
            categories = extract_categories_from_sitemap(shop_url)
        except Exception as e:
            print(f"[{domain}] Sitemap error: {e}", flush=True)

    # Strategy 3: Firecrawl /map (fallback)
    if not categories:
        print(f"[{domain}] Strategy 3: Firecrawl /map...", flush=True)
        try:
            categories = extract_categories_from_firecrawl(shop_url)
        except Exception as e:
            print(f"[{domain}] Firecrawl error: {e}", flush=True)

    if not categories:
        elapsed = time.time() - start
        print(f"[{domain}] No categories found (tried all 3 strategies) | {elapsed:.1f}s", flush=True)
        return 0

    # Upsert
    count = upsert_categories(shop_id, categories)
    elapsed = time.time() - start

    print(
        f"[{domain}] Saved {count} categories "
        f"(from {len(categories)} candidates) | {elapsed:.1f}s",
        flush=True,
    )
    return count


# ---------------------------------------------------------------------------
# Standalone: batch scrape all shops
# ---------------------------------------------------------------------------
def get_shops_to_scrape(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """
    Load shops that need category tree scraping.
    Priority: shops with existing product offers (breadcrumb strategy likely works).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.name, s.url, s.country_id
                FROM shops s
                WHERE s.deleted_at IS NULL
                  AND s.url IS NOT NULL
                  AND s.id NOT IN (
                      SELECT DISTINCT shop_id FROM shop_categories
                      WHERE scraped_at > NOW() - INTERVAL '30 days'
                  )
                ORDER BY
                    (s.offers_checked_at IS NOT NULL) DESC,
                    s.id
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


def main() -> None:
    start_ts = time.time()
    print(f"=== Category Tree Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    shops = get_shops_to_scrape()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run)", flush=True)

    if not shops:
        print("No shops to scrape. Done.", flush=True)
        return

    total_categories = 0
    shops_success = 0
    shops_empty = 0

    for i, (shop_id, name, url, _country_id) in enumerate(shops, 1):
        print(f"\n--- [{i}/{len(shops)}] {name} ---", flush=True)
        try:
            count = scrape_category_tree(shop_id, name, url)
            total_categories += count
            if count > 0:
                shops_success += 1
            else:
                shops_empty += 1
        except Exception as e:
            print(f"[{name}] Shop-level error: {e}", flush=True)
            shops_empty += 1

    elapsed = time.time() - start_ts
    print(
        f"\n{'=' * 60}\n"
        f"DONE: {total_categories} categories saved | "
        f"{shops_success}/{len(shops)} shops with data | "
        f"{shops_empty} empty | {elapsed:.1f}s ({elapsed / 60:.1f}m)\n"
        f"{'=' * 60}",
        flush=True,
    )


if __name__ == "__main__":
    main()
