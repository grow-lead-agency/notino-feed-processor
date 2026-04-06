"""
Job: Offer Enrichment Pipeline
Batch enrichment product_offers z existujících dat v DB.
Cíl: zvýšit fill rate sloupců (shipping_cost, delivery_days, stock_level, ...) z 0% na 50-80%.

6 enrichment operací — každá = 1 SQL UPDATE (žádný row-by-row processing).
Bezpečné: přepisuje POUZE NULL hodnoty, respektuje deleted_at.

Standalone: python jobs/offer_enricher.py
"""
import sys
import time

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn


def enrich_shipping_cost() -> int:
    """
    Naplní shipping_cost z cheapest home_delivery shipping_methods.
    shipping_zones (destination_country_id) → shipping_methods (shipping_zone_id).
    """
    sql = """
        UPDATE product_offers po
        SET shipping_cost = sub.base_price,
            updated_at = NOW()
        FROM (
            SELECT DISTINCT ON (sz.shop_id, sz.destination_country_id)
                sz.shop_id,
                sz.destination_country_id,
                sm.base_price
            FROM shipping_zones sz
            JOIN shipping_methods sm ON sm.shipping_zone_id = sz.id
            WHERE sz.ships_to = true
              AND sm.delivery_category = 'home_delivery'
              AND sm.base_price IS NOT NULL
              AND sm.is_active = true
            ORDER BY sz.shop_id, sz.destination_country_id, sm.base_price ASC
        ) sub
        WHERE po.shop_id = sub.shop_id
          AND po.country_id = sub.destination_country_id
          AND po.shipping_cost IS NULL
          AND po.deleted_at IS NULL
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        put_conn(conn)


def enrich_delivery_days() -> int:
    """
    Naplní delivery_days_min/max z shipping_methods (home_delivery).
    Bere nejrychlejší home_delivery metodu (ORDER BY estimated_days_min ASC).
    """
    sql = """
        UPDATE product_offers po
        SET delivery_days_min = sub.delivery_days_min,
            delivery_days_max = sub.delivery_days_max,
            updated_at = NOW()
        FROM (
            SELECT DISTINCT ON (sz.shop_id, sz.destination_country_id)
                sz.shop_id,
                sz.destination_country_id,
                sm.delivery_days_min,
                sm.delivery_days_max
            FROM shipping_zones sz
            JOIN shipping_methods sm ON sm.shipping_zone_id = sz.id
            WHERE sz.ships_to = true
              AND sm.delivery_category = 'home_delivery'
              AND sm.delivery_days_min IS NOT NULL
              AND sm.is_active = true
            ORDER BY sz.shop_id, sz.destination_country_id, sm.delivery_days_min ASC
        ) sub
        WHERE po.shop_id = sub.shop_id
          AND po.country_id = sub.destination_country_id
          AND po.delivery_days_min IS NULL
          AND po.deleted_at IS NULL
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        put_conn(conn)


def enrich_free_shipping_threshold() -> int:
    """
    Naplní free_shipping_threshold z shipping_zones.free_shipping_threshold.
    """
    sql = """
        UPDATE product_offers po
        SET free_shipping_threshold = sz.free_shipping_threshold,
            updated_at = NOW()
        FROM shipping_zones sz
        WHERE sz.shop_id = po.shop_id
          AND sz.destination_country_id = po.country_id
          AND sz.ships_to = true
          AND sz.free_shipping_threshold IS NOT NULL
          AND po.free_shipping_threshold IS NULL
          AND po.deleted_at IS NULL
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        put_conn(conn)


def enrich_unit_price() -> int:
    """
    Vypočítá unit_price_per_ml = price / products.volume_ml.
    Pouze kde products.volume_ml > 0 a price > 0.
    """
    sql = """
        UPDATE product_offers po
        SET unit_price_per_ml = ROUND(po.price / p.volume_ml, 4),
            updated_at = NOW()
        FROM products p
        WHERE p.id = po.product_id
          AND p.volume_ml IS NOT NULL AND p.volume_ml > 0
          AND po.price IS NOT NULL AND po.price > 0
          AND po.unit_price_per_ml IS NULL
          AND po.deleted_at IS NULL
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        put_conn(conn)


def enrich_stock_level() -> int:
    """
    Mapuje availability ENUM → stock_level text:
      in_stock   → 'available'
      limited    → 'low'
      out_of_stock → 'out'
      preorder   → 'preorder'
      backorder  → 'backorder'
    """
    sql = """
        UPDATE product_offers
        SET stock_level = CASE availability
            WHEN 'in_stock' THEN 'available'
            WHEN 'limited' THEN 'low'
            WHEN 'out_of_stock' THEN 'out'
            WHEN 'preorder' THEN 'preorder'
            WHEN 'backorder' THEN 'backorder'
            ELSE NULL
        END,
        updated_at = NOW()
        WHERE stock_level IS NULL
          AND availability IS NOT NULL
          AND deleted_at IS NULL
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        put_conn(conn)


def enrich_voucher_codes() -> int:
    """
    Přiřadí nejhodnotnější aktivní voucher_code z shop_voucher_codes.
    Respektuje: is_active, valid_to, min_order_value, country_id match (NULL = all).
    """
    sql = """
        UPDATE product_offers po
        SET voucher_code = sub.code,
            updated_at = NOW()
        FROM (
            SELECT DISTINCT ON (svc.shop_id, po_inner.id)
                po_inner.id AS offer_id,
                svc.code
            FROM product_offers po_inner
            JOIN shop_voucher_codes svc ON svc.shop_id = po_inner.shop_id
            WHERE po_inner.voucher_code IS NULL
              AND po_inner.deleted_at IS NULL
              AND svc.is_active = true
              AND (svc.valid_to IS NULL OR svc.valid_to > NOW())
              AND (svc.min_order_value IS NULL OR po_inner.price >= svc.min_order_value)
              AND (svc.country_id IS NULL OR svc.country_id = po_inner.country_id)
            ORDER BY svc.shop_id, po_inner.id, svc.discount_value DESC NULLS LAST
        ) sub
        WHERE po.id = sub.offer_id
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        put_conn(conn)


def run_offer_enrichment() -> dict:
    """
    Main entry. Runs all 6 enrichment operations sequentially.
    Returns stats dict: {operation_name: rows_updated, ...}.
    """
    print("=" * 60, flush=True)
    print("OFFER ENRICHMENT PIPELINE — START", flush=True)
    print("=" * 60, flush=True)

    start = time.time()
    stats = {}
    operations = [
        ("shipping_cost", enrich_shipping_cost),
        ("delivery_days", enrich_delivery_days),
        ("free_shipping_threshold", enrich_free_shipping_threshold),
        ("unit_price_per_ml", enrich_unit_price),
        ("stock_level", enrich_stock_level),
        ("voucher_codes", enrich_voucher_codes),
    ]

    for name, func in operations:
        op_start = time.time()
        try:
            count = func()
            op_duration = time.time() - op_start
            stats[name] = count
            print(f"  [{name}] {count:,} rows updated ({op_duration:.1f}s)", flush=True)
        except Exception as e:
            stats[name] = -1
            print(f"  [{name}] ERROR: {e}", flush=True)

    duration = time.time() - start
    total = sum(v for v in stats.values() if v > 0)
    stats["_total"] = total
    stats["_duration_s"] = round(duration, 1)

    print("-" * 60, flush=True)
    print(f"DONE — {total:,} total updates in {duration:.1f}s", flush=True)
    print(f"Stats: {stats}", flush=True)
    print("=" * 60, flush=True)

    return stats


if __name__ == "__main__":
    import os
    # Load .env from parent directory if running locally
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path) and "DATABASE_URL" not in os.environ:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    run_offer_enrichment()
