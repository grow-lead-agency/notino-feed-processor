"""
Sitemap Discovery — scan shops without sitemap_product_feed and discover product sitemaps.

Plain HTTP only (no Firecrawl). Checks robots.txt, common sitemap paths, and sitemap
indexes for product-related child sitemaps. Saves results to website_checks and auto-
creates shop_scrape_config rows for newly discovered feeds.

Usage:
    python jobs/sitemap_discovery.py              # batch 50, easy/medium only
    python jobs/sitemap_discovery.py --all        # include hard/blocked
    python jobs/sitemap_discovery.py --limit 200  # custom batch size
"""
import argparse
import re
import sys
import time

import requests
from lxml import etree

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = 10
RATE_LIMIT_DELAY = 0.34  # ~3 req/s
DEFAULT_BATCH_SIZE = 50

USER_AGENT = "Mozilla/5.0 (compatible; FeedProcessor/1.0; +https://growlead.cz/bot)"

# Namespace for sitemap XML
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Standard paths to probe (in order)
SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-products.xml",
    "/product-sitemap.xml",
    "/sitemap/sitemap-products-1.xml",
    "/sitemap_products_sections.xml",
]

# Keywords that signal a product-related sitemap (matched case-insensitively)
PRODUCT_KEYWORDS = ("product", "produkt", "produit", "articl", "item", "tovar", "zbozi")


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/xml, text/xml, */*",
})


def _get(url: str) -> requests.Response | None:
    """GET with timeout. Returns None on any error."""
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------
def get_shops_without_feed(limit: int, include_hard: bool = False) -> list[dict]:
    """Shops that have no sitemap_product_feed yet."""
    difficulty_filter = (
        "AND wc.difficulty NOT IN ('dead', 'unreachable')"
        if include_hard
        else "AND wc.difficulty NOT IN ('dead', 'unreachable', 'hard', 'blocked')"
    )
    sql = f"""
        SELECT s.id, s.name, s.url, wc.id AS wc_id, wc.difficulty
        FROM shops s
        JOIN website_checks wc ON wc.shop_id = s.id
        WHERE wc.sitemap_product_feed IS NULL
          AND s.deleted_at IS NULL
          {difficulty_filter}
        ORDER BY
          CASE wc.difficulty WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
          s.category
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def save_feed_and_create_config(
    shop_id: int,
    wc_id: int,
    feed_url: str,
    difficulty: str | None,
    url_count: int | None,
) -> None:
    """
    1. UPDATE website_checks.sitemap_product_feed
    2. INSERT shop_scrape_config for catalog_sync + price_check (idempotent)
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Save discovered feed URL + count
            cur.execute(
                """
                UPDATE website_checks
                SET sitemap_product_feed = %s,
                    sitemap_urls_count = %s,
                    checked_at = NOW()
                WHERE id = %s
                """,
                (feed_url, url_count, wc_id),
            )

            # Determine scraper engine
            engine = "jsonld"
            if difficulty in ("hard", "blocked"):
                engine = "firecrawl"

            # catalog_sync — interval by shop class (we don't have category here,
            # so use a sensible default of 72h = 3 days)
            cur.execute(
                """
                INSERT INTO shop_scrape_config
                    (shop_id, scrape_type, interval_hours, priority, scraper_engine)
                VALUES (%s, 'catalog_sync'::scrape_type, 72, 5, %s)
                ON CONFLICT (shop_id, scrape_type) DO NOTHING
                """,
                (shop_id, engine),
            )

            # price_check — 6h default
            cur.execute(
                """
                INSERT INTO shop_scrape_config
                    (shop_id, scrape_type, interval_hours, priority, scraper_engine)
                VALUES (%s, 'price_check'::scrape_type, 6, 5, %s)
                ON CONFLICT (shop_id, scrape_type) DO NOTHING
                """,
                (shop_id, engine),
            )

            conn.commit()
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Sitemap parsing (reuses lxml pattern from sitemap_crawler.py)
# ---------------------------------------------------------------------------
def _parse_sitemap_xml(content: bytes) -> etree._Element | None:
    """Parse XML content, return root element or None."""
    try:
        return etree.fromstring(content)
    except Exception:
        return None


def _is_sitemap_index(root: etree._Element) -> bool:
    """Check if root element is a sitemapindex."""
    tag = root.tag.lower() if root.tag else ""
    return "sitemapindex" in tag


def _extract_locs(root: etree._Element) -> list[str]:
    """Extract all <loc> text from sitemap XML (handles both urlset and sitemapindex)."""
    locs = root.findall(".//sm:loc", SITEMAP_NS)
    if not locs:
        # Try without namespace (some sitemaps omit it)
        locs = root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    if not locs:
        # Last resort — plain tag match
        locs = root.iter()
        return [el.text.strip() for el in locs if el.tag.endswith("loc") and el.text]
    return [loc.text.strip() for loc in locs if loc.text]


def _is_product_sitemap_url(url: str) -> bool:
    """Does the URL path suggest a product sitemap?"""
    lower = url.lower()
    return any(kw in lower for kw in PRODUCT_KEYWORDS)


# ---------------------------------------------------------------------------
# Core discovery logic
# ---------------------------------------------------------------------------
def check_robots_txt(shop_url: str) -> list[str]:
    """Parse robots.txt for Sitemap: directives. Returns list of sitemap URLs."""
    url = shop_url.rstrip("/") + "/robots.txt"
    resp = _get(url)
    if not resp:
        return []
    time.sleep(RATE_LIMIT_DELAY)

    sitemaps = []
    for line in resp.text.splitlines():
        line = line.strip()
        m = re.match(r"^Sitemap:\s*(.+)", line, re.IGNORECASE)
        if m:
            sitemap_url = m.group(1).strip()
            if sitemap_url:
                sitemaps.append(sitemap_url)
    return sitemaps


def find_product_sitemap(sitemap_url: str, depth: int = 0) -> str | None:
    """
    If the URL is a sitemap index, look for a child whose URL contains
    product-related keywords. Returns the first matching child URL or None.
    Max recursion depth = 2 to avoid infinite loops.
    """
    if depth > 2:
        return None

    resp = _get(sitemap_url)
    if not resp:
        return None
    time.sleep(RATE_LIMIT_DELAY)

    root = _parse_sitemap_xml(resp.content)
    if root is None:
        return None

    if _is_sitemap_index(root):
        child_urls = _extract_locs(root)
        # First pass: look for product-related child sitemaps
        for child_url in child_urls:
            if _is_product_sitemap_url(child_url):
                return child_url
        # Second pass: recurse into generic-named children (e.g. sitemap-1.xml)
        for child_url in child_urls[:5]:  # limit to avoid excessive crawling
            result = find_product_sitemap(child_url, depth + 1)
            if result:
                return result
        return None

    # It's a regular urlset — check if it contains product-like URLs
    locs = _extract_locs(root)
    product_count = sum(1 for u in locs[:100] if _is_product_sitemap_url(u))
    if product_count >= 3:
        # This sitemap itself is a product sitemap
        return sitemap_url

    return None


def count_sitemap_urls(sitemap_url: str) -> int | None:
    """Quick count of <loc> entries in a sitemap."""
    resp = _get(sitemap_url)
    if not resp:
        return None
    time.sleep(RATE_LIMIT_DELAY)

    root = _parse_sitemap_xml(resp.content)
    if root is None:
        return None

    return len(_extract_locs(root))


def discover_sitemap(shop_url: str) -> tuple[str | None, int | None]:
    """
    Try all common sitemap URLs + robots.txt for a shop.
    Returns (product_sitemap_url, url_count) or (None, None).
    """
    base = shop_url.rstrip("/")

    # 1. robots.txt — may reveal non-standard sitemap locations
    robots_sitemaps = check_robots_txt(base)
    for sm_url in robots_sitemaps:
        product_sm = find_product_sitemap(sm_url)
        if product_sm:
            count = count_sitemap_urls(product_sm)
            return product_sm, count

    # 2. Standard candidate paths
    for path in SITEMAP_CANDIDATES:
        candidate_url = base + path
        resp = _get(candidate_url)
        if not resp:
            time.sleep(RATE_LIMIT_DELAY)
            continue
        time.sleep(RATE_LIMIT_DELAY)

        root = _parse_sitemap_xml(resp.content)
        if root is None:
            continue

        if _is_sitemap_index(root):
            # Look inside the index for product sitemaps
            product_sm = find_product_sitemap(candidate_url)
            if product_sm:
                count = count_sitemap_urls(product_sm)
                return product_sm, count
        else:
            # Regular urlset — check if product-oriented
            locs = _extract_locs(root)
            product_count = sum(1 for u in locs[:100] if _is_product_sitemap_url(u))
            if product_count >= 3:
                return candidate_url, len(locs)

    # 3. If robots.txt had sitemaps but none were product-specific,
    #    still try the first one as a generic sitemap
    if robots_sitemaps:
        first_sm = robots_sitemaps[0]
        resp = _get(first_sm)
        if resp:
            time.sleep(RATE_LIMIT_DELAY)
            root = _parse_sitemap_xml(resp.content)
            if root and not _is_sitemap_index(root):
                locs = _extract_locs(root)
                if len(locs) > 0:
                    # No product keywords but has URLs — could be a flat sitemap
                    # Only save if >50 URLs (likely a real sitemap, not just pages)
                    if len(locs) >= 50:
                        return first_sm, len(locs)

    return None, None


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
def run_sitemap_discovery(batch_size: int = DEFAULT_BATCH_SIZE, include_hard: bool = False) -> dict:
    """
    Main entry point. Discovers sitemaps for shops without feeds.
    Returns {checked, found, not_found, errors}.
    """
    start = time.time()
    print(f"=== Sitemap Discovery starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    shops = get_shops_without_feed(batch_size, include_hard=include_hard)
    print(f"Loaded {len(shops)} shops without sitemap_product_feed "
          f"(batch={batch_size}, include_hard={include_hard})", flush=True)

    stats = {"checked": 0, "found": 0, "not_found": 0, "errors": 0}

    for shop in shops:
        shop_id = shop["id"]
        name = shop["name"]
        url = shop["url"]
        wc_id = shop["wc_id"]
        difficulty = shop["difficulty"]

        if not url:
            print(f"  [{name}] SKIP — no URL", flush=True)
            stats["not_found"] += 1
            stats["checked"] += 1
            continue

        try:
            print(f"  [{name}] Checking {url} ...", flush=True)
            feed_url, url_count = discover_sitemap(url)

            if feed_url:
                print(f"  [{name}] FOUND: {feed_url} ({url_count or '?'} URLs)", flush=True)
                save_feed_and_create_config(shop_id, wc_id, feed_url, difficulty, url_count)
                stats["found"] += 1
            else:
                print(f"  [{name}] not found", flush=True)
                stats["not_found"] += 1

        except Exception as e:
            print(f"  [{name}] ERROR: {e}", flush=True)
            stats["errors"] += 1

        stats["checked"] += 1

    elapsed = time.time() - start
    print(
        f"=== Done: {stats['found']} found, {stats['not_found']} not found, "
        f"{stats['errors']} errors | {stats['checked']}/{len(shops)} checked | "
        f"{elapsed:.1f}s ===",
        flush=True,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover product sitemaps for shops without feeds")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE, help="Max shops to check (default 50)")
    parser.add_argument("--all", action="store_true", help="Include hard/blocked shops (slower)")
    args = parser.parse_args()

    run_sitemap_discovery(batch_size=args.limit, include_hard=args.all)
