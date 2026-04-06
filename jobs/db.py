"""
Database helper — notino-feed-processor
Neon PostgreSQL via psycopg2 with SSL required.
Updated 2026-04-05: Uses NEW GMC 3-table schema (products/offers/history).
Updated 2026-04-06: ThreadedConnectionPool for multi-worker scraping.
"""
import os
import re
import json
import unicodedata
from psycopg2.pool import ThreadedConnectionPool


# Thread-safe connection pool (lazy init)
_pool = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        conn_str = os.environ["DATABASE_URL"]
        _pool = ThreadedConnectionPool(minconn=2, maxconn=20, dsn=conn_str)
    return _pool


def get_conn():
    """Return a psycopg2 connection from the pool."""
    return _get_pool().getconn()


def put_conn(conn):
    """Return a connection to the pool."""
    _get_pool().putconn(conn)


def normalize_title(title: str) -> str:
    """Lowercase + remove accents + collapse whitespace for matching."""
    if not title:
        return ""
    nfkd = unicodedata.normalize("NFKD", title)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents.lower()).strip()


def extract_volume(title: str) -> tuple:
    """Extract volume from title (ml/g/oz) → (volume_str, volume_ml)."""
    if not title:
        return None, None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|g|oz|kg)", title, re.I)
    if not m:
        return None, None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit == "ml":
        volume_ml = num
    elif unit == "g":
        volume_ml = num  # approximation for cosmetics
    elif unit == "kg":
        volume_ml = num * 1000
    elif unit == "oz":
        volume_ml = num * 29.5735
    else:
        volume_ml = None
    return m.group(0), volume_ml


def get_or_create_brand(cur, brand_name: str) -> int | None:
    """Find or create brand by name. Returns brand_id or None."""
    if not brand_name or not brand_name.strip():
        return None
    brand_name = brand_name.strip()
    brand_normalized = normalize_title(brand_name)
    slug = re.sub(r"[^a-z0-9]+", "-", brand_normalized).strip("-")

    cur.execute("SELECT id FROM brands WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "INSERT INTO brands (name, name_normalized, slug) VALUES (%s, %s, %s) "
        "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (brand_name, brand_normalized, slug)
    )
    return cur.fetchone()[0]


def upsert_product(*, gtin13=None, gtin12=None, mpn=None, title,
                   description=None, image_url=None, first_seen_shop_id=None,
                   brand_name=None, raw_data=None):
    """UPSERT product into catalog. Auto-creates brand. Returns product_id."""
    title_normalized = normalize_title(title)
    volume_str, volume_ml = extract_volume(title)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Auto-create brand
            brand_id = get_or_create_brand(cur, brand_name)

            # Match by GTIN first
            if gtin13:
                cur.execute("SELECT id FROM products WHERE gtin13 = %s", (gtin13,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """UPDATE products SET last_updated_at = NOW(),
                           brand_id = COALESCE(brand_id, %s),
                           canonical_image_url = COALESCE(canonical_image_url, %s)
                           WHERE id = %s""",
                        (brand_id, image_url, row[0])
                    )
                    conn.commit()
                    return row[0]

            if gtin12:
                cur.execute("SELECT id FROM products WHERE gtin12 = %s", (gtin12,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """UPDATE products SET last_updated_at = NOW(),
                           brand_id = COALESCE(brand_id, %s),
                           canonical_image_url = COALESCE(canonical_image_url, %s)
                           WHERE id = %s""",
                        (brand_id, image_url, row[0])
                    )
                    conn.commit()
                    return row[0]

            if mpn and first_seen_shop_id:
                cur.execute(
                    "SELECT id FROM products WHERE mpn = %s AND first_seen_shop_id = %s",
                    (mpn, first_seen_shop_id)
                )
                row = cur.fetchone()
                if row:
                    return row[0]

            # Insert new product with brand
            cur.execute(
                """
                INSERT INTO products (
                    gtin13, gtin12, mpn, title, title_normalized, description,
                    volume_ml, volume_unit_raw, canonical_image_url,
                    brand_id, first_seen_shop_id, raw_feed_data
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (gtin13, gtin12, mpn, title, title_normalized, description,
                 volume_ml, volume_str, image_url,
                 brand_id, first_seen_shop_id,
                 json.dumps(raw_data) if raw_data else None)
            )
            product_id = cur.fetchone()[0]
            conn.commit()
            return product_id
    finally:
        put_conn(conn)


def upsert_offer(*, product_id, shop_id, country_id, external_sku=None,
                 offer_title=None, product_url, image_url=None,
                 price=None, sale_price=None, price_currency="EUR",
                 availability=None, volume_ml_snapshot=None,
                 affiliate_url=None, source="jsonld_scrape", raw_data=None):
    """UPSERT offer. Returns offer_id. Tracks price changes in history."""
    unit_price_per_ml = None
    if volume_ml_snapshot and price and volume_ml_snapshot > 0:
        unit_price_per_ml = round(price / volume_ml_snapshot, 4)

    availability_enum = None
    if availability is not None:
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
            # Try match by SKU first, then by URL (fallback for NULL SKU)
            cur.execute(
                "SELECT id, price, availability FROM product_offers "
                "WHERE shop_id=%s AND (external_sku=%s OR product_url=%s) AND country_id=%s "
                "LIMIT 1",
                (shop_id, external_sku, product_url, country_id)
            )
            existing = cur.fetchone()

            if existing:
                offer_id, old_price, old_availability = existing
                price_changed = (old_price != price or old_availability != availability_enum)

                cur.execute(
                    """UPDATE product_offers SET
                        offer_title=%s, product_url=%s, image_url=%s,
                        price=%s, sale_price=%s, price_currency=%s,
                        availability=%s::availability_status,
                        volume_ml_snapshot=%s, unit_price_per_ml=%s,
                        affiliate_url=%s, source=%s::feed_source,
                        raw_offer_data=%s, last_seen_at=NOW(), updated_at=NOW(), is_active=TRUE
                       WHERE id=%s""",
                    (offer_title, product_url, image_url, price, sale_price, price_currency,
                     availability_enum, volume_ml_snapshot, unit_price_per_ml,
                     affiliate_url, source, json.dumps(raw_data) if raw_data else None, offer_id)
                )

                if price_changed and price is not None:
                    cur.execute(
                        """INSERT INTO offer_price_history
                           (offer_id, product_id, shop_id, country_id,
                            price, sale_price, price_currency, availability, source)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::availability_status,%s::feed_source)""",
                        (offer_id, product_id, shop_id, country_id, price, sale_price,
                         price_currency, availability_enum, source)
                    )
            else:
                cur.execute(
                    """INSERT INTO product_offers (
                        product_id, shop_id, country_id, external_sku,
                        offer_title, product_url, image_url,
                        price, sale_price, price_currency,
                        availability, volume_ml_snapshot, unit_price_per_ml,
                        affiliate_url, source, raw_offer_data
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::availability_status,%s,%s,%s,%s::feed_source,%s)
                    ON CONFLICT (shop_id, product_url) DO UPDATE SET
                        price = EXCLUDED.price,
                        sale_price = EXCLUDED.sale_price,
                        availability = EXCLUDED.availability,
                        last_seen_at = NOW(),
                        updated_at = NOW(),
                        is_active = TRUE
                    RETURNING id""",
                    (product_id, shop_id, country_id, external_sku, offer_title, product_url, image_url,
                     price, sale_price, price_currency, availability_enum,
                     volume_ml_snapshot, unit_price_per_ml, affiliate_url, source,
                     json.dumps(raw_data) if raw_data else None)
                )
                offer_id = cur.fetchone()[0]

                if price is not None:
                    cur.execute(
                        """INSERT INTO offer_price_history
                           (offer_id, product_id, shop_id, country_id,
                            price, sale_price, price_currency, availability, source)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::availability_status,%s::feed_source)""",
                        (offer_id, product_id, shop_id, country_id, price, sale_price,
                         price_currency, availability_enum, source)
                    )

            conn.commit()
            return offer_id
    finally:
        put_conn(conn)


def upsert_product_with_offer(*, shop_id, country_id, product_data):
    """
    High-level: UPSERT product + offer together.

    product_data keys: ean, sku, name, price, currency, url, image_url,
                       description, availability, volume_ml, raw
    Returns (product_id, offer_id).
    """
    ean = (product_data.get("ean") or "").strip() or None
    sku = (product_data.get("sku") or "").strip() or None

    gtin13 = ean if ean and len(ean) == 13 else None
    gtin12 = ean if ean and len(ean) == 12 else None
    if ean and not gtin13 and not gtin12:
        gtin13 = ean  # fallback

    product_id = upsert_product(
        gtin13=gtin13, gtin12=gtin12, mpn=sku,
        title=product_data["name"],
        description=product_data.get("description"),
        image_url=product_data.get("image_url"),
        brand_name=product_data.get("brand"),
        first_seen_shop_id=shop_id,
        raw_data=product_data.get("raw"),
    )

    offer_id = upsert_offer(
        product_id=product_id, shop_id=shop_id, country_id=country_id,
        external_sku=sku, offer_title=product_data.get("name"),
        product_url=product_data["url"], image_url=product_data.get("image_url"),
        price=product_data.get("price"), sale_price=product_data.get("sale_price"),
        price_currency=product_data.get("currency", "EUR"),
        availability=product_data.get("availability"),
        volume_ml_snapshot=product_data.get("volume_ml"),
        affiliate_url=product_data.get("affiliate_url"),
        source=product_data.get("source", "jsonld_scrape"),
        raw_data=product_data.get("raw"),
    )

    return product_id, offer_id
