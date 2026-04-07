"""
Brand Page Scraper — extracts brand landing pages from url_discovery_queue.

Dequeues URLs with url_type='brand' from url_discovery_queue, scrapes each
page via Crawl4AI (LLM extraction), and populates shop_brand_pages.
Auto-matches to brands master catalog via brand_aliases + slug matching.

Pipeline: url_discovery_queue (brand) → Crawl4AI → shop_brand_pages → brands FK

Cron: every 2h, batch 100 URLs per run.
Engine: Crawl4AI primary, plain HTTP fallback for JSON-LD/meta extraction.

Usage:
    python jobs/brand_page_scraper.py               # batch 100
    python jobs/brand_page_scraper.py --limit 500   # bigger batch
    python jobs/brand_page_scraper.py --dry-run      # no DB writes
"""
import argparse
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn, normalize_title
from jobs.scrape_chain import scrape_with_chain
from jobs.langfuse_wrapper import flush

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_BATCH = 100
RATE_LIMIT_DELAY = 0.5

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; FeedProcessor/2.0; +https://growlead.cz/bot)",
})
SESSION.timeout = 20

BRAND_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_name": {"type": "string", "description": "The brand name displayed on the page"},
        "description": {"type": "string", "description": "Brand description or bio text"},
        "product_count": {"type": "integer", "description": "Number of products listed for this brand, if visible"},
        "h1": {"type": "string", "description": "The main H1 heading"},
        "hero_image_url": {"type": "string", "description": "Main banner/hero image URL"},
        "logo_url": {"type": "string", "description": "Brand logo image URL if present"},
    },
    "required": ["brand_name"],
}

BRAND_PROMPT = """Extract brand information from this brand landing page on an e-commerce shop.
Focus on: the brand name as displayed, any description/bio text, the count of products
listed for this brand (if shown as a number), the H1 heading, hero/banner image URL, and logo URL.
Return JSON matching the schema. If a field is not visible, omit it."""


# ---------------------------------------------------------------------------
# Dequeue from url_discovery_queue
# ---------------------------------------------------------------------------
def dequeue_brand_urls(batch_size: int) -> list[dict]:
    """Fetch pending brand URLs from queue using atomic dequeue."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dequeue_discovery_urls('brand'::url_type, %s, 'brand_page_scraper')",
                (batch_size,)
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Brand matching
# ---------------------------------------------------------------------------
def match_brand(cur, brand_name_raw: str) -> int | None:
    """Try to match raw brand name to master brands table. Returns brand_id or None."""
    if not brand_name_raw:
        return None

    normalized = normalize_title(brand_name_raw)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")

    # Direct slug match
    cur.execute("SELECT id FROM brands WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if row:
        return row[0]

    # Alias match
    cur.execute("""
        SELECT b.id FROM brand_aliases ba
        JOIN brands b ON b.id = ba.brand_id
        WHERE ba.alias_normalized = %s
        LIMIT 1
    """, (normalized,))
    row = cur.fetchone()
    if row:
        return row[0]

    # Fuzzy match (trigram) — only if high similarity
    cur.execute("""
        SELECT id, similarity(name_normalized, %s) as sim
        FROM brands
        WHERE name_normalized %% %s
        ORDER BY sim DESC
        LIMIT 1
    """, (normalized, normalized))
    row = cur.fetchone()
    if row and row[1] >= 0.7:
        return row[0]

    return None


# ---------------------------------------------------------------------------
# Plain HTTP fallback (meta tags + JSON-LD)
# ---------------------------------------------------------------------------
def extract_meta_brand(url: str) -> dict | None:
    """Fallback: extract basic brand info from meta tags and HTML."""
    try:
        resp = SESSION.get(url)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        title = soup.find("title")
        h1 = soup.find("h1")
        meta_desc = soup.find("meta", attrs={"name": "description"})
        og_image = soup.find("meta", attrs={"property": "og:image"})

        # Try to extract brand name from H1 or title
        brand_name = None
        if h1 and h1.get_text(strip=True):
            brand_name = h1.get_text(strip=True)
        elif title and title.get_text(strip=True):
            brand_name = title.get_text(strip=True).split("|")[0].split("-")[0].strip()

        if not brand_name:
            return None

        return {
            "brand_name": brand_name,
            "h1": h1.get_text(strip=True) if h1 else None,
            "description": meta_desc["content"] if meta_desc and meta_desc.get("content") else None,
            "hero_image_url": og_image["content"] if og_image and og_image.get("content") else None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Save to DB
# ---------------------------------------------------------------------------
def save_brand_page(queue_row: dict, data: dict) -> int | None:
    """Insert/update shop_brand_pages and mark queue row as done."""
    shop_id = queue_row["shop_id"]
    url = queue_row["url"]
    queue_id = queue_row["id"]

    brand_name_raw = data.get("brand_name", "").strip()
    if not brand_name_raw:
        _mark_queue(queue_id, "skipped", "no_brand_name")
        return None

    brand_name_normalized = normalize_title(brand_name_raw)
    slug = re.sub(r"[^a-z0-9]+", "-", brand_name_normalized).strip("-")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            brand_id = match_brand(cur, brand_name_raw)

            cur.execute("""
                INSERT INTO shop_brand_pages (
                    shop_id, url, brand_name_raw, brand_name_normalized, slug,
                    brand_id, title, meta_description, h1, description,
                    product_count, product_count_source,
                    hero_image_url, logo_url,
                    content_extracted_at, raw_data
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    NOW(), %s
                )
                ON CONFLICT (shop_id, url) DO UPDATE SET
                    brand_name_raw = EXCLUDED.brand_name_raw,
                    brand_name_normalized = EXCLUDED.brand_name_normalized,
                    brand_id = COALESCE(EXCLUDED.brand_id, shop_brand_pages.brand_id),
                    h1 = EXCLUDED.h1,
                    description = EXCLUDED.description,
                    product_count = EXCLUDED.product_count,
                    hero_image_url = EXCLUDED.hero_image_url,
                    logo_url = EXCLUDED.logo_url,
                    content_extracted_at = NOW(),
                    raw_data = EXCLUDED.raw_data
                RETURNING id
            """, (
                shop_id, url, brand_name_raw, brand_name_normalized, slug,
                brand_id,
                data.get("title") or data.get("h1"),
                data.get("meta_description"),
                data.get("h1"),
                data.get("description"),
                data.get("product_count"),
                "page_text" if data.get("product_count") else None,
                data.get("hero_image_url"),
                data.get("logo_url"),
                None,  # raw_data — skip for now to save space
            ))
            entity_id = cur.fetchone()[0]

            # Mark queue row as done
            cur.execute("""
                UPDATE url_discovery_queue
                SET status = 'done', processed_at = NOW(),
                    result_entity_type = 'shop_brand_page', result_entity_id = %s
                WHERE id = %s
            """, (entity_id, queue_id))

            conn.commit()
            return entity_id
    finally:
        put_conn(conn)


def _mark_queue(queue_id: int, status: str, error: str | None = None):
    """Update queue row status."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE url_discovery_queue
                SET status = %s, processed_at = NOW(), last_error = %s
                WHERE id = %s
            """, (status, error, queue_id))
        conn.commit()
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(batch_size: int = DEFAULT_BATCH, dry_run: bool = False):
    start = time.time()
    logger.info("brand_page_scraper_start", batch_size=batch_size, dry_run=dry_run)

    urls = dequeue_brand_urls(batch_size)
    logger.info("dequeued", count=len(urls))

    stats = {"processed": 0, "saved": 0, "skipped": 0, "errors": 0}
    consecutive_skips = 0
    MAX_CONSECUTIVE_SKIPS = 15  # if 15 in a row fail, stop — probably all from same blocked shop

    for row in urls:
        url = row["url"]
        try:
            result = scrape_with_chain(
                url, schema=BRAND_SCHEMA, prompt=BRAND_PROMPT,
                priority=row.get("priority", 5),
                html_extractor=extract_meta_brand,
            )
            data = result["data"]
            engine = result["engine"]

            if not data or not data.get("brand_name"):
                stats["skipped"] += 1
                consecutive_skips += 1
                if not dry_run:
                    _mark_queue(row["id"], "skipped", f"no_data|engines_tried={engine or 'all'}")
                if consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
                    logger.warning("consecutive_skip_limit", skips=consecutive_skips,
                                  last_url=url[:80])
                    # Mark remaining as skipped to avoid burning through queue
                    if not dry_run:
                        remaining = urls[urls.index(row) + 1:]
                        for r in remaining:
                            _mark_queue(r["id"], "skipped", "batch_abort_consecutive_fails")
                        stats["skipped"] += len(remaining)
                    break
                continue

            consecutive_skips = 0  # reset on success

            if dry_run:
                logger.info("dry_run", url=url[:80], brand=data.get("brand_name"),
                           engine=engine, products=data.get("product_count"))
                stats["processed"] += 1
                continue

            entity_id = save_brand_page(row, data)
            if entity_id:
                stats["saved"] += 1
                logger.debug("saved", url=url[:80], brand=data.get("brand_name"),
                            engine=engine, entity_id=entity_id)
            else:
                stats["skipped"] += 1

            stats["processed"] += 1
            time.sleep(RATE_LIMIT_DELAY)

        except Exception as e:
            stats["errors"] += 1
            logger.error("error", url=url[:80], error=str(e)[:200])
            if not dry_run:
                _mark_queue(row["id"], "error", str(e)[:500])

    elapsed = time.time() - start
    logger.info("brand_page_scraper_done", duration_s=round(elapsed, 1), **stats)
    flush()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Brand Page Scraper")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(batch_size=args.limit, dry_run=args.dry_run)
