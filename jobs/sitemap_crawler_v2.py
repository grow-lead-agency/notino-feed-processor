"""
Sitemap Crawler v2 — Universal URL Discovery

Replaces sitemap_crawler.py (v1). Crawls ALL sitemap feeds per shop,
classifies each sub-sitemap (product/category/brand/blog/page/image),
and enqueues ALL URLs into url_discovery_queue.

Pipeline:
  1. Load shops with known sitemaps from website_checks
  2. For each shop: fetch sitemap index → shop_sitemaps
  3. For each sub-sitemap: classify type → shop_sitemap_feeds
  4. For each URL: hash + enqueue → url_discovery_queue
  5. Backward compat: product URLs also go to product_url_queue (v1)

Classification layers:
  Layer 1: classify_feed_url() — PG regex on sub-sitemap URL (free, ~85%)
  Layer 2: content_sample — look at first 10 URLs (free, ~95%)
  Layer 3: LLM — send 10 sample URLs to gpt-4o-mini (cheap, ~99%) [TBD]

Usage:
    python jobs/sitemap_crawler_v2.py               # all shops with feeds
    python jobs/sitemap_crawler_v2.py --shop 123     # single shop
    python jobs/sitemap_crawler_v2.py --dry-run      # no DB writes
    python jobs/sitemap_crawler_v2.py --limit 50     # limit shops

Interval: every 2h via cron (same as v1).
"""
import argparse
import hashlib
import re
import sys
import time

import requests
from lxml import etree
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 0.3  # ~3 req/s
MAX_URLS_PER_FEED = 100_000  # safety limit
FETCH_WORKERS = 5  # parallel sub-sitemap fetches
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; FeedProcessor/2.0; +https://growlead.cz/bot)",
    "Accept": "application/xml, text/xml, */*",
})

# URL classification keywords (Layer 2: content sample)
PRODUCT_PATTERNS = (
    "/product", "/produkt", "/produit", "/producto", "/tovar",
    "/zbozi", "/p/", "/dp/", "/sku/", "/parfem", "/parfum",
    "/cosmetic", "/kosmetik", "/beauty", "/krasa",
)
CATEGORY_PATTERNS = (
    "/category", "/kategor", "/c/", "/rubri", "/rayon",
    "/oddelen", "/afdeling",
)
BRAND_PATTERNS = (
    "/brand", "/znacka", "/znack", "/marqu", "/marca",
    "/hersteller", "/marke", "/merk",
)
BLOG_PATTERNS = (
    "/blog", "/article", "/clanek", "/clanky", "/magazin",
    "/journal", "/news", "/ratgeber", "/conseil", "/poradna",
)
PAGE_PATTERNS = (
    "/about", "/contact", "/kontakt", "/shipping", "/doprav",
    "/return", "/vracen", "/faq", "/help", "/pomoc",
    "/impressum", "/datenschutz", "/agb", "/legal",
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response | None:
    """GET with timeout. Returns None on error."""
    try:
        resp = SESSION.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.debug("fetch_failed", url=url[:120], error=str(e)[:100])
        return None


def parse_sitemap_xml(content: bytes) -> etree._Element | None:
    """Parse sitemap XML, handle encoding issues."""
    try:
        return etree.fromstring(content)
    except etree.XMLSyntaxError:
        try:
            # Some sitemaps have BOM or encoding issues
            content = content.lstrip(b"\xef\xbb\xbf")
            return etree.fromstring(content)
        except Exception:
            return None


def extract_locs(root: etree._Element) -> list[dict]:
    """Extract <loc> + optional <lastmod> + <changefreq> from sitemap."""
    entries = []
    ns = SITEMAP_NS

    # Try namespaced first, then bare
    if root.tag.endswith("sitemapindex"):
        items = root.findall("sm:sitemap", ns) or root.findall(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap"
        )
    else:
        items = root.findall("sm:url", ns) or root.findall(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}url"
        )

    for item in items:
        loc = item.findtext("sm:loc", namespaces=ns)
        if not loc:
            loc = item.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if not loc:
            continue

        lastmod = item.findtext("sm:lastmod", namespaces=ns)
        if not lastmod:
            lastmod = item.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")

        changefreq = item.findtext("sm:changefreq", namespaces=ns)
        if not changefreq:
            changefreq = item.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}changefreq")

        entries.append({
            "loc": loc.strip(),
            "lastmod": lastmod.strip() if lastmod else None,
            "changefreq": changefreq.strip() if changefreq else None,
        })

    return entries


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_by_content_sample(urls: list[str]) -> tuple[str, float]:
    """
    Layer 2: classify a feed by sampling first N URLs.
    Returns (url_type, confidence).
    """
    if not urls:
        return "unknown", 0.0

    sample = urls[:20]
    counters = {"product": 0, "category": 0, "brand": 0, "blog": 0, "page": 0}

    for url in sample:
        lower = url.lower()
        if any(p in lower for p in PRODUCT_PATTERNS):
            counters["product"] += 1
        elif any(p in lower for p in CATEGORY_PATTERNS):
            counters["category"] += 1
        elif any(p in lower for p in BRAND_PATTERNS):
            counters["brand"] += 1
        elif any(p in lower for p in BLOG_PATTERNS):
            counters["blog"] += 1
        elif any(p in lower for p in PAGE_PATTERNS):
            counters["page"] += 1

    if not any(counters.values()):
        return "unknown", 0.0

    best_type = max(counters, key=counters.get)
    confidence = round(counters[best_type] / len(sample), 2)

    if confidence < 0.3:
        return "unknown", confidence

    return best_type, min(confidence + 0.1, 0.99)  # boost a bit for sample evidence


def classify_feed(feed_url: str, sample_urls: list[str] | None = None) -> dict:
    """
    Combined classification: Layer 1 (DB regex) → Layer 2 (content sample).
    Returns {feed_type, confidence, method}.
    """
    # Layer 1: classify via DB function (will be called in SQL, not here)
    # We replicate the logic in Python for speed (avoid DB roundtrip per feed)
    l1_type, l1_conf = _classify_url_pattern(feed_url)

    if l1_type != "unknown" and l1_conf >= 0.80:
        return {"feed_type": l1_type, "confidence": l1_conf, "method": "url_pattern"}

    # Layer 2: content sample
    if sample_urls:
        l2_type, l2_conf = classify_by_content_sample(sample_urls)
        if l2_type != "unknown" and l2_conf > l1_conf:
            return {"feed_type": l2_type, "confidence": l2_conf, "method": "content_sample"}

    # Best effort from Layer 1
    if l1_type != "unknown":
        return {"feed_type": l1_type, "confidence": l1_conf, "method": "url_pattern"}

    return {"feed_type": "unknown", "confidence": 0.0, "method": "url_pattern"}


def _classify_url_pattern(url: str) -> tuple[str, float]:
    """Python mirror of classify_feed_url() PG function."""
    lower = url.lower()
    # Order matches PG function
    if re.search(r'(categor|kategor|rubri|rayon|oddelen|sekcj|afdeling)', lower):
        return "category", 0.85
    if re.search(r'(brand|znack|znacka|marqu|marca|hersteller|marke|merk)', lower):
        return "brand", 0.85
    if re.search(r'(blog|articl|clanek|clanky|clanok|magazin|journal|news|aktuality|poradna|inspirac|ratgeber|conseil)', lower):
        return "blog", 0.80
    if re.search(r'([\-_/]image|[\-_/]img|[\-_/]bild|[\-_/]foto|[\-_/]photo|media[\-_]sitemap)', lower):
        return "image", 0.90
    if re.search(r'([\-_/]page|strank|seite|[\-_/]cms|about|kontakt|contact|shipping|doprav|vracen|return|reklamac|[\-_/]faq|pomoc|[\-_/]help|career|kariér|press|tisk|sustain|impressum|datenschutz|agb)', lower):
        return "page", 0.75
    if re.search(r'(product|produkt|produit|producto|tovar|[\-_/]sku|zbozi|produk)', lower):
        return "product", 0.85
    return "unknown", 0.0


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------
def load_shops_with_sitemaps(limit: int | None = None, shop_id: int | None = None) -> list[dict]:
    """Load shops that have a known sitemap."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if shop_id:
                cur.execute("""
                    SELECT s.id, s.name, s.url, wc.sitemap_product_feed, wc.domain
                    FROM shops s
                    JOIN website_checks wc ON wc.shop_id = s.id
                    WHERE s.id = %s AND s.deleted_at IS NULL
                """, (shop_id,))
            else:
                sql = """
                    SELECT s.id, s.name, s.url, wc.sitemap_product_feed, wc.domain
                    FROM shops s
                    JOIN website_checks wc ON wc.shop_id = s.id
                    WHERE s.deleted_at IS NULL
                      AND (wc.sitemap_product_feed IS NOT NULL OR wc.sitemap_urls_count > 0)
                    ORDER BY wc.sitemap_urls_count DESC NULLS LAST
                """
                if limit:
                    sql += f" LIMIT {int(limit)}"
                cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def upsert_shop_sitemap(shop_id: int, sitemap_url: str, http_status: int | None,
                        etag: str | None, last_modified: str | None,
                        total_feeds: int, total_urls: int, error: str | None) -> int:
    """Insert or update shop_sitemaps row. Returns id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shop_sitemaps (shop_id, sitemap_index_url, last_crawled_at,
                    last_http_status, etag, last_modified, total_feeds, total_urls, last_error,
                    last_success_at)
                VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s IS NULL THEN NOW() ELSE NULL END)
                ON CONFLICT (shop_id) DO UPDATE SET
                    sitemap_index_url = EXCLUDED.sitemap_index_url,
                    last_crawled_at = NOW(),
                    last_http_status = EXCLUDED.last_http_status,
                    etag = COALESCE(EXCLUDED.etag, shop_sitemaps.etag),
                    last_modified = COALESCE(EXCLUDED.last_modified, shop_sitemaps.last_modified),
                    total_feeds = EXCLUDED.total_feeds,
                    total_urls = EXCLUDED.total_urls,
                    last_error = EXCLUDED.last_error,
                    last_success_at = CASE WHEN EXCLUDED.last_error IS NULL THEN NOW()
                                      ELSE shop_sitemaps.last_success_at END
                RETURNING id
            """, (shop_id, sitemap_url, http_status, etag, last_modified,
                  total_feeds, total_urls, error, error))
            conn.commit()
            return cur.fetchone()[0]
    finally:
        put_conn(conn)


def upsert_sitemap_feed(shop_sitemap_id: int, shop_id: int, feed_url: str,
                        feed_type: str, urls_count: int, lastmod: str | None,
                        changefreq: str | None, classification: dict,
                        http_status: int | None) -> int:
    """Insert or update shop_sitemap_feeds row. Returns id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shop_sitemap_feeds (
                    shop_sitemap_id, shop_id, feed_url, feed_type,
                    urls_count, lastmod, changefreq,
                    classification_method, classification_confidence, classification_model,
                    last_crawled_at, last_http_status
                ) VALUES (%s, %s, %s, %s::url_type, %s, %s, %s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (shop_id, feed_url) DO UPDATE SET
                    feed_type = EXCLUDED.feed_type,
                    urls_count_prev = shop_sitemap_feeds.urls_count,
                    urls_count = EXCLUDED.urls_count,
                    lastmod = EXCLUDED.lastmod,
                    changefreq = EXCLUDED.changefreq,
                    classification_method = EXCLUDED.classification_method,
                    classification_confidence = EXCLUDED.classification_confidence,
                    classification_model = EXCLUDED.classification_model,
                    last_crawled_at = NOW(),
                    last_http_status = EXCLUDED.last_http_status
                RETURNING id
            """, (shop_sitemap_id, shop_id, feed_url, feed_type,
                  urls_count, lastmod, changefreq,
                  classification.get("method"), classification.get("confidence"),
                  classification.get("model"), http_status))
            conn.commit()
            return cur.fetchone()[0]
    finally:
        put_conn(conn)


def enqueue_urls(shop_id: int, feed_id: int, url_type: str,
                 urls: list[dict], priority: int = 5) -> int:
    """Bulk insert URLs into url_discovery_queue. Returns count inserted."""
    if not urls:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Batch insert with ON CONFLICT skip
            inserted = 0
            batch_size = 500
            for i in range(0, len(urls), batch_size):
                batch = urls[i:i + batch_size]
                values = []
                params = []
                for entry in batch:
                    url = entry["loc"]
                    url_hash = hashlib.sha256(url.encode("utf-8")).digest()
                    lastmod = entry.get("lastmod")
                    values.append("(%s, %s, %s, %s::url_type, %s, %s, %s)")
                    params.extend([url, url_hash, shop_id, url_type, feed_id, priority, lastmod])

                sql = f"""
                    INSERT INTO url_discovery_queue
                        (url, url_hash, shop_id, url_type, feed_id, priority, lastmod)
                    VALUES {', '.join(values)}
                    ON CONFLICT (url_hash) DO NOTHING
                """
                cur.execute(sql, params)
                inserted += cur.rowcount

            conn.commit()
            return inserted
    finally:
        put_conn(conn)


def enqueue_product_urls_v1(urls: list[dict], shop_name: str) -> int:
    """Backward compat: also write product URLs to old product_url_queue."""
    if not urls:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            inserted = 0
            for entry in urls:
                try:
                    cur.execute("""
                        INSERT INTO product_url_queue (url, shop, discovered_at, status)
                        VALUES (%s, %s, NOW(), 'pending')
                        ON CONFLICT (url) DO NOTHING
                    """, (entry["loc"], shop_name))
                    inserted += cur.rowcount
                except Exception:
                    conn.rollback()
                    continue
            conn.commit()
            return inserted
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Core crawl logic
# ---------------------------------------------------------------------------
def discover_sitemap_index(shop_url: str) -> str:
    """Find the sitemap index URL for a shop. Returns best URL found."""
    base = shop_url.rstrip("/")

    # Try robots.txt first
    resp = fetch_url(f"{base}/robots.txt", timeout=10)
    if resp:
        import re
        for line in resp.text.splitlines():
            m = re.match(r"^Sitemap:\s*(.+)", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()

    # Fallback to /sitemap.xml
    return f"{base}/sitemap.xml"


def crawl_shop_sitemap(shop: dict, dry_run: bool = False) -> dict:
    """
    Crawl one shop's entire sitemap structure.
    Returns stats dict.
    """
    shop_id = shop["id"]
    shop_name = shop["name"]
    shop_url = shop["url"] or ""

    stats = {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "feeds_found": 0,
        "urls_enqueued": 0,
        "by_type": {},
        "error": None,
    }

    # Always start from root sitemap index (not the product feed URL)
    # website_checks.sitemap_product_feed points to a sub-feed, we need the root
    sitemap_url = None
    if shop_url:
        sitemap_url = discover_sitemap_index(shop_url)
    if not sitemap_url:
        # Fallback: use the known product feed URL
        sitemap_url = shop.get("sitemap_product_feed")
    if not sitemap_url:
        stats["error"] = "no_sitemap_url"
        return stats

    # Fetch root sitemap
    resp = fetch_url(sitemap_url)
    if not resp:
        if not dry_run:
            upsert_shop_sitemap(shop_id, sitemap_url, None, None, None, 0, 0, "fetch_failed")
        stats["error"] = "fetch_failed"
        return stats

    etag = resp.headers.get("ETag")
    last_modified = resp.headers.get("Last-Modified")

    root = parse_sitemap_xml(resp.content)
    if root is None:
        if not dry_run:
            upsert_shop_sitemap(shop_id, sitemap_url, resp.status_code, etag, last_modified, 0, 0, "parse_failed")
        stats["error"] = "parse_failed"
        return stats

    # Determine if it's a sitemap index or a single sitemap
    is_index = root.tag.lower().endswith("sitemapindex") if root.tag else False

    if is_index:
        # It's a sitemap index — each child is a separate feed
        child_entries = extract_locs(root)
        feeds_to_process = []

        for entry in child_entries:
            feeds_to_process.append({
                "url": entry["loc"],
                "lastmod": entry.get("lastmod"),
                "changefreq": entry.get("changefreq"),
            })

        total_urls = 0

        # Save shop_sitemaps
        if not dry_run:
            sm_id = upsert_shop_sitemap(
                shop_id, sitemap_url, resp.status_code,
                etag, last_modified,
                len(feeds_to_process), 0, None
            )
        else:
            sm_id = 0

        # Process each sub-sitemap
        for feed_info in feeds_to_process:
            feed_url = feed_info["url"]
            time.sleep(RATE_LIMIT_DELAY)

            feed_resp = fetch_url(feed_url)
            if not feed_resp:
                logger.debug("feed_fetch_failed", shop=shop_name, feed=feed_url[:80])
                continue

            feed_root = parse_sitemap_xml(feed_resp.content)
            if feed_root is None:
                continue

            # Handle nested sitemap indexes (recursive)
            if feed_root.tag and "sitemapindex" in feed_root.tag.lower():
                # Skip nested indexes for now (rare, complex)
                logger.debug("nested_index_skip", shop=shop_name, feed=feed_url[:80])
                continue

            url_entries = extract_locs(feed_root)
            if not url_entries:
                continue

            # Classify this feed
            sample_urls = [e["loc"] for e in url_entries[:20]]
            classification = classify_feed(feed_url, sample_urls)
            feed_type = classification["feed_type"]

            stats["feeds_found"] += 1
            total_urls += len(url_entries)

            if feed_type not in stats["by_type"]:
                stats["by_type"][feed_type] = 0
            stats["by_type"][feed_type] += len(url_entries)

            if dry_run:
                logger.info("dry_run_feed", shop=shop_name, feed_type=feed_type,
                           urls=len(url_entries), confidence=classification["confidence"],
                           method=classification["method"], feed=feed_url[:80])
                continue

            # Save feed metadata
            feed_id = upsert_sitemap_feed(
                sm_id, shop_id, feed_url, feed_type,
                len(url_entries), feed_info.get("lastmod"),
                feed_info.get("changefreq"), classification,
                feed_resp.status_code
            )

            # Enqueue all URLs
            enqueued = enqueue_urls(shop_id, feed_id, feed_type,
                                   url_entries[:MAX_URLS_PER_FEED])
            stats["urls_enqueued"] += enqueued

            # Backward compat: product URLs also to v1 queue
            if feed_type == "product":
                enqueue_product_urls_v1(url_entries, shop_name)

        # Update total_urls on shop_sitemaps
        if not dry_run and total_urls > 0:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE shop_sitemaps SET total_urls = %s WHERE id = %s",
                                (total_urls, sm_id))
                conn.commit()
            finally:
                put_conn(conn)

    else:
        # Single sitemap (not an index) — treat as one feed
        url_entries = extract_locs(root)
        if not url_entries:
            stats["error"] = "empty_sitemap"
            return stats

        sample_urls = [e["loc"] for e in url_entries[:20]]
        classification = classify_feed(sitemap_url, sample_urls)
        feed_type = classification["feed_type"]

        stats["feeds_found"] = 1
        stats["by_type"][feed_type] = len(url_entries)

        if dry_run:
            logger.info("dry_run_single", shop=shop_name, feed_type=feed_type,
                       urls=len(url_entries), confidence=classification["confidence"])
            return stats

        sm_id = upsert_shop_sitemap(
            shop_id, sitemap_url, resp.status_code,
            etag, last_modified, 1, len(url_entries), None
        )

        feed_id = upsert_sitemap_feed(
            sm_id, shop_id, sitemap_url, feed_type,
            len(url_entries), None, None, classification,
            resp.status_code
        )

        enqueued = enqueue_urls(shop_id, feed_id, feed_type,
                               url_entries[:MAX_URLS_PER_FEED])
        stats["urls_enqueued"] = enqueued

        if feed_type == "product":
            enqueue_product_urls_v1(url_entries, shop_name)

    return stats


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
def run(limit: int | None = None, shop_id: int | None = None, dry_run: bool = False):
    """Main entry point."""
    start = time.time()
    logger.info("sitemap_crawler_v2_start", limit=limit, shop_id=shop_id, dry_run=dry_run)

    shops = load_shops_with_sitemaps(limit=limit, shop_id=shop_id)
    logger.info("shops_loaded", count=len(shops))

    totals = {
        "shops_crawled": 0,
        "shops_failed": 0,
        "feeds_found": 0,
        "urls_enqueued": 0,
        "by_type": {},
    }

    for shop in shops:
        try:
            stats = crawl_shop_sitemap(shop, dry_run=dry_run)

            if stats.get("error"):
                totals["shops_failed"] += 1
                logger.warning("shop_failed", shop=shop["name"], error=stats["error"])
            else:
                totals["shops_crawled"] += 1
                totals["feeds_found"] += stats["feeds_found"]
                totals["urls_enqueued"] += stats["urls_enqueued"]
                for url_type, count in stats.get("by_type", {}).items():
                    totals["by_type"][url_type] = totals["by_type"].get(url_type, 0) + count

                logger.info("shop_done", shop=shop["name"],
                           feeds=stats["feeds_found"],
                           urls=stats["urls_enqueued"],
                           types=stats["by_type"])

            time.sleep(RATE_LIMIT_DELAY)

        except Exception as e:
            totals["shops_failed"] += 1
            logger.error("shop_error", shop=shop["name"], error=str(e))

    elapsed = time.time() - start
    logger.info("sitemap_crawler_v2_done",
                duration_s=round(elapsed, 1),
                shops_crawled=totals["shops_crawled"],
                shops_failed=totals["shops_failed"],
                feeds_found=totals["feeds_found"],
                urls_enqueued=totals["urls_enqueued"],
                by_type=totals["by_type"])

    # Run queue cleanup (purge done > 3 days)
    if not dry_run:
        try:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT cleanup_discovery_queue(3)")
                    purged = cur.fetchone()[0]
                    if purged:
                        logger.info("queue_cleanup", purged=purged)
                conn.commit()
            finally:
                put_conn(conn)
        except Exception as e:
            logger.warning("cleanup_failed", error=str(e))

    return totals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sitemap Crawler v2 — Universal URL Discovery")
    parser.add_argument("--limit", type=int, default=None, help="Max shops to crawl")
    parser.add_argument("--shop", type=int, default=None, help="Single shop ID")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes, just classify and log")
    args = parser.parse_args()

    run(limit=args.limit, shop_id=args.shop, dry_run=args.dry_run)
