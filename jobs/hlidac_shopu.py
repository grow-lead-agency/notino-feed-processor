"""
Job 4: Hlídač Shopů — Price History Enrichment + Price Sync
Volá api.hlidacshopu.cz/v2/detail?url= pro CZ/SK produkty.
Interval: každých 4 hodin (crontab line 26)

Two flows:
  1. sync_prices_from_hlidac() — price sync for CZ/SK hard/blocked shops
     Updates product_offers + offer_price_history from Hlídač Shopů API.
     Free alternative to Firecrawl price_check for shops like Sephora.cz,
     Flaconi.cz, makeup.cz, Notino.cz, DrMax, Mall, Alza, etc.

  2. run() — original enrichment flow (hlidac_shopu_data standalone table)
     Loads URLs from product_url_queue and saves raw API responses.
"""
import sys
import json
import time
from decimal import Decimal

import requests
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn

logger = structlog.get_logger()

HLIDAC_API = "https://api.hlidacshopu.cz/v2/detail"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "FeedProcessor/1.0 (notino-datamining; petr@growlead.cz)",
})

# Rate limiting: max 5 req/s → 200ms between requests
RATE_LIMIT_DELAY = 0.2
# Max URLs per single sync run
MAX_URLS_PER_RUN = 500
# How stale an offer must be before re-syncing (hours)
STALE_THRESHOLD_HOURS = 24
# Request timeout (seconds)
REQUEST_TIMEOUT = 15


def fetch_price_history(product_url: str) -> dict | None:
    """Fetch price history from Hlídač Shopů API."""
    try:
        resp = SESSION.get(HLIDAC_API, params={"url": product_url}, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("hlidac_api_failed", url=product_url, error=str(e))
        return None


def save_price_history(product_url: str, data: dict):
    """Save price history record to hlidac_shopu_data table (historical log)."""
    sql = """
        INSERT INTO hlidac_shopu_data
            (product_url, fetched_at, min_price, max_price, current_price, currency, raw_json)
        VALUES (%s, NOW(), %s, %s, %s, %s, %s)
    """
    min_p = data.get("metadata", {}).get("minPrice")
    max_p = data.get("metadata", {}).get("maxPrice")
    curr_p = data.get("currentPrice")
    currency = "CZK"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (product_url, min_p, max_p, curr_p, currency, json.dumps(data)))
        conn.commit()
    finally:
        put_conn(conn)


def was_fetched_today(product_url: str) -> bool:
    """Check if this URL was already fetched today."""
    sql = """
        SELECT 1 FROM hlidac_shopu_data
        WHERE product_url = %s
          AND fetched_at > NOW() - INTERVAL '6 hours'
        LIMIT 1
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (product_url,))
            return cur.fetchone() is not None
    finally:
        put_conn(conn)


def load_urls_for_enrichment(limit: int = 200) -> list[str]:
    """Load product URLs from queue that need Hlídač Shopů enrichment."""
    sql = """
        SELECT DISTINCT q.url
        FROM product_url_queue q
        LEFT JOIN hlidac_shopu_data h ON h.product_url = q.url
            AND h.fetched_at > NOW() - INTERVAL '6 hours'
        WHERE q.status = 'done'
          AND h.product_url IS NULL
          AND (q.url LIKE '%%notino%%'
            OR q.url LIKE '%%parfum%%'
            OR q.url LIKE '%%kosmetika%%'
            OR q.url LIKE '%%dm.cz%%'
            OR q.url LIKE '%%rossmann%%'
            OR q.url LIKE '%%fann%%')
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [row[0] for row in cur.fetchall()]
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# FLOW 1: Price Sync — update product_offers from Hlídač Shopů
# ---------------------------------------------------------------------------

def _parse_availability(data: dict) -> str | None:
    """Parse availability from Hlídač Shopů API response.

    API returns 'currentPrice' (null = out of stock in most cases).
    Some shops also have explicit availability in metadata.
    """
    meta = data.get("metadata", {})

    # Explicit availability in metadata (rare but some shops provide it)
    avail = meta.get("availability")
    if avail:
        avail_lower = str(avail).lower()
        if "instock" in avail_lower or "in_stock" in avail_lower:
            return "in_stock"
        if "outofstock" in avail_lower or "out_of_stock" in avail_lower:
            return "out_of_stock"
        if "preorder" in avail_lower:
            return "preorder"

    # Fallback: if currentPrice exists → in_stock, else → out_of_stock
    if data.get("currentPrice") is not None:
        return "in_stock"
    return "out_of_stock"


def _parse_sale_price(data: dict) -> Decimal | None:
    """Detect sale price from Hlídač Shopů data.

    Hlídač Shopů tracks originalPrice vs currentPrice.
    If originalPrice > currentPrice → it's a sale.
    """
    original = data.get("originalPrice")
    current = data.get("currentPrice")
    if original and current and float(original) > float(current):
        # currentPrice IS the sale price, originalPrice is the regular price
        return Decimal(str(current))
    return None


def _parse_regular_price(data: dict) -> Decimal | None:
    """Get the regular (non-sale) price.

    If originalPrice exists and > currentPrice → originalPrice is regular.
    Otherwise currentPrice is the regular price.
    """
    original = data.get("originalPrice")
    current = data.get("currentPrice")

    if current is None:
        return None

    if original and float(original) > float(current):
        return Decimal(str(original))
    return Decimal(str(current))


def _detect_currency(product_url: str) -> str:
    """Detect currency from URL domain.
    CZ shops → CZK, SK shops → EUR (Slovakia uses EUR since 2009).
    """
    url_lower = product_url.lower()
    if ".sk" in url_lower or "/sk/" in url_lower:
        return "EUR"
    # Default for .cz domains
    return "CZK"


def load_stale_offers(limit: int = MAX_URLS_PER_RUN) -> list[tuple]:
    """Load product offers from CZ/SK hard/blocked shops that need price refresh.

    Returns list of tuples:
        (offer_id, product_url, shop_id, price, sale_price, product_id,
         country_id, price_currency, availability)
    """
    sql = """
        SELECT po.id, po.product_url, po.shop_id, po.price, po.sale_price,
               po.product_id, po.country_id, po.price_currency, po.availability::text
        FROM product_offers po
        JOIN shops s ON s.id = po.shop_id
        JOIN website_checks wc ON wc.shop_id = s.id
        JOIN countries c ON c.id = po.country_id
        WHERE wc.difficulty IN ('hard', 'blocked')
          AND c.iso2 IN ('CZ', 'SK')
          AND po.deleted_at IS NULL
          AND po.product_url IS NOT NULL
          AND po.product_url != ''
          AND po.updated_at < NOW() - INTERVAL '%s hours'
        ORDER BY po.updated_at ASC NULLS FIRST
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (STALE_THRESHOLD_HOURS, limit))
            return cur.fetchall()
    finally:
        put_conn(conn)


def sync_prices_from_hlidac():
    """Sync prices from Hlídač Shopů API for CZ/SK hard/blocked shops.

    Free alternative to Firecrawl price_check for shops like
    Sephora.cz, Flaconi.cz, makeup.cz, Notino.cz, DrMax, Mall, Alza, etc.

    Flow:
      1. Query product_offers for CZ/SK hard shops where updated_at > 24h
      2. For each offer URL, call Hlídač Shopů API
      3. If API returns current price → UPDATE product_offers
      4. Save to hlidac_shopu_data (historical log)
      5. Save to offer_price_history (price tracking, only on change)
    """
    offers = load_stale_offers()
    total = len(offers)
    logger.info("hlidac_sync_start", stale_offers=total)

    if total == 0:
        logger.info("hlidac_sync_skip", reason="no stale offers")
        return

    synced = 0
    price_changed = 0
    not_found = 0
    api_errors = 0

    # Batch: collect all updates, commit in chunks of 50
    BATCH_SIZE = 50
    conn = get_conn()
    try:
        batch_count = 0
        for i, row in enumerate(offers):
            (offer_id, product_url, shop_id, old_price, old_sale_price,
             product_id, country_id, price_currency, old_availability) = row

            # Rate limiting: 200ms between requests
            if i > 0:
                time.sleep(RATE_LIMIT_DELAY)

            data = fetch_price_history(product_url)

            if data is None:
                not_found += 1
                continue

            if not data.get("currentPrice") and not data.get("originalPrice"):
                # API returned data but no price info — skip
                not_found += 1
                continue

            new_price = _parse_regular_price(data)
            new_sale_price = _parse_sale_price(data)
            new_availability = _parse_availability(data)
            detected_currency = _detect_currency(product_url)

            # Use detected currency if offer has none, otherwise keep existing
            if not price_currency or price_currency.strip() == '':
                price_currency = detected_currency

            with conn.cursor() as cur:
                # 1. Update product_offers
                cur.execute("""
                    UPDATE product_offers
                    SET price = COALESCE(%s, price),
                        sale_price = %s,
                        availability = COALESCE(%s::availability_status, availability),
                        price_currency = %s,
                        source = 'hlidac_shopu'::feed_source,
                        updated_at = NOW(),
                        last_seen_at = NOW()
                    WHERE id = %s
                """, (new_price, new_sale_price, new_availability,
                      price_currency, offer_id))

                # 2. Save to hlidac_shopu_data (historical log)
                min_p = data.get("metadata", {}).get("minPrice")
                max_p = data.get("metadata", {}).get("maxPrice")
                curr_p = data.get("currentPrice")
                cur.execute("""
                    INSERT INTO hlidac_shopu_data
                        (product_url, fetched_at, min_price, max_price,
                         current_price, currency, raw_json)
                    VALUES (%s, NOW(), %s, %s, %s, %s, %s)
                """, (product_url, min_p, max_p, curr_p,
                      detected_currency, json.dumps(data)))

                # 3. Insert price history if price actually changed
                has_change = (
                    old_price != new_price
                    or old_sale_price != new_sale_price
                    or old_availability != new_availability
                )
                if has_change and new_price is not None:
                    cur.execute("""
                        INSERT INTO offer_price_history
                            (offer_id, product_id, shop_id, country_id,
                             price, sale_price, price_currency,
                             availability, source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s,
                                %s::availability_status, 'hlidac_shopu'::feed_source)
                    """, (offer_id, product_id, shop_id, country_id,
                          new_price, new_sale_price, price_currency,
                          new_availability))
                    price_changed += 1

            synced += 1
            batch_count += 1

            # Commit every BATCH_SIZE rows
            if batch_count >= BATCH_SIZE:
                conn.commit()
                batch_count = 0
                logger.info("hlidac_sync_batch", synced=synced, of=total)

        # Final commit for remaining rows
        if batch_count > 0:
            conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("hlidac_sync_error")
        raise
    finally:
        put_conn(conn)

    logger.info("hlidac_sync_done",
                synced=synced, price_changed=price_changed,
                not_found=not_found, api_errors=api_errors, total=total)


# ---------------------------------------------------------------------------
# FLOW 2: Check for new CZ/SK hard shops without Hlídač Shopů coverage
# ---------------------------------------------------------------------------

def check_new_czsk_hard_shops():
    """Find CZ/SK hard/blocked shops that have offers but no recent Hlídač Shopů data.

    Logs shops that are candidates for Hlídač Shopů price sync but haven't
    been covered yet. Useful for discovering new shops added to the DB.
    """
    sql = """
        SELECT s.id, s.name, s.url, c.iso2, wc.difficulty,
               COUNT(po.id) AS offer_count,
               MAX(po.updated_at) AS last_offer_update
        FROM shops s
        JOIN website_checks wc ON wc.shop_id = s.id
        JOIN product_offers po ON po.shop_id = s.id AND po.deleted_at IS NULL
        JOIN countries c ON c.id = po.country_id
        WHERE wc.difficulty IN ('hard', 'blocked')
          AND c.iso2 IN ('CZ', 'SK')
          AND po.product_url IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM hlidac_shopu_data h
              WHERE h.product_url = po.product_url
                AND h.fetched_at > NOW() - INTERVAL '7 days'
          )
        GROUP BY s.id, s.name, s.url, c.iso2, wc.difficulty
        HAVING COUNT(po.id) > 0
        ORDER BY COUNT(po.id) DESC
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    if not rows:
        logger.info("hlidac_new_shops", found=0,
                     msg="all CZ/SK hard shops have recent coverage")
        return

    logger.info("hlidac_new_shops", found=len(rows))
    for shop_id, name, url, country, difficulty, offers, last_update in rows:
        logger.info("hlidac_uncovered_shop",
                     shop_id=shop_id, name=name, url=url,
                     country=country, difficulty=difficulty,
                     offers=offers, last_update=str(last_update))


# ---------------------------------------------------------------------------
# Original enrichment flow (standalone hlidac_shopu_data)
# ---------------------------------------------------------------------------

def run():
    """Original enrichment: fetch Hlídač Shopů data for URLs in product_url_queue."""
    urls = load_urls_for_enrichment()
    logger.info("starting_hlidac_enrichment", urls=len(urls))

    enriched = 0
    skipped = 0
    failed = 0

    for i, url in enumerate(urls):
        # Rate limiting
        if i > 0:
            time.sleep(RATE_LIMIT_DELAY)

        data = fetch_price_history(url)
        if data:
            save_price_history(url, data)
            enriched += 1
        elif data is None:
            skipped += 1  # 404 — not tracked
        else:
            failed += 1

    logger.info("hlidac_enrichment_done",
                enriched=enriched, skipped=skipped, failed=failed, total=len(urls))


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Hlídač Shopů price sync + enrichment")
    parser.add_argument("--sync-only", action="store_true",
                        help="Run only price sync (skip enrichment)")
    parser.add_argument("--enrich-only", action="store_true",
                        help="Run only enrichment (skip price sync)")
    parser.add_argument("--check-shops", action="store_true",
                        help="Only check for uncovered CZ/SK hard shops")
    parser.add_argument("--limit", type=int, default=MAX_URLS_PER_RUN,
                        help=f"Max URLs per sync run (default: {MAX_URLS_PER_RUN})")
    args = parser.parse_args()

    if args.limit != MAX_URLS_PER_RUN:
        MAX_URLS_PER_RUN = args.limit

    if args.check_shops:
        check_new_czsk_hard_shops()
    elif args.sync_only:
        sync_prices_from_hlidac()
    elif args.enrich_only:
        run()
    else:
        # Default: run both flows
        # 1. Price sync for CZ/SK hard shops (main value)
        sync_prices_from_hlidac()
        # 2. Check for newly added hard shops without coverage
        check_new_czsk_hard_shops()
        # 3. Original enrichment (if there are URLs in queue)
        run()
