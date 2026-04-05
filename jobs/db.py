"""
Database connection helper — notino-feed-processor
Neon PostgreSQL via psycopg2 with SSL required.
"""
import os
import psycopg2
from psycopg2.extras import execute_values
import structlog

logger = structlog.get_logger()


def get_conn():
    """Return a psycopg2 connection to Neon DB."""
    conn_str = os.environ["DATABASE_URL"]
    return psycopg2.connect(conn_str)


def upsert_products(records: list[dict], table: str = "products"):
    """
    Bulk upsert product records.
    Each record: {url, shop, title, price, currency, image_url, scraped_at, raw_json}
    """
    if not records:
        return 0

    cols = ["url", "shop", "title", "price", "currency", "image_url", "scraped_at", "raw_json"]
    values = [[r.get(c) for c in cols] for r in records]

    sql = f"""
        INSERT INTO {table} ({', '.join(cols)})
        VALUES %s
        ON CONFLICT (url) DO UPDATE SET
            title = EXCLUDED.title,
            price = EXCLUDED.price,
            currency = EXCLUDED.currency,
            image_url = EXCLUDED.image_url,
            scraped_at = EXCLUDED.scraped_at,
            raw_json = EXCLUDED.raw_json
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()

    logger.info("upserted", table=table, count=len(records))
    return len(records)
