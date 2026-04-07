"""
Product Matcher — populate product_matches via EAN + title similarity.

Two matching strategies:
1. EAN match (gtin13) — exact match, confidence 1.0
2. Title similarity (pg_trgm) — fuzzy match, confidence = similarity score

A "canonical product" is the one with the most offers (most observed across shops).
All other products with the same EAN/title cluster point to it.

Writes: product_matches (canonical_product_id, product_id, match_type, match_confidence)

Run: python jobs/product_matcher.py [--strategy ean|title|both] [--min-similarity 0.6] [--vertical beauty]
"""
import argparse
import logging
from db import get_conn, put_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def match_by_ean(vertical: str | None = None) -> int:
    """
    Find products sharing the same gtin13 across different shops.
    For each EAN cluster, pick the canonical (most offers) and link others.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            where_vertical = ""
            params = []
            if vertical:
                where_vertical = "AND p.vertical = %s::vertical_type"
                params.append(vertical)

            # Find EAN clusters with 2+ products (different products, same EAN)
            cur.execute(f"""
                WITH ean_clusters AS (
                    SELECT gtin13, array_agg(id ORDER BY id) AS product_ids, count(*) AS cnt
                    FROM products
                    WHERE gtin13 IS NOT NULL AND deleted_at IS NULL
                    {where_vertical.replace('p.', '')}
                    GROUP BY gtin13
                    HAVING count(*) > 1
                ),
                canonical AS (
                    SELECT ec.gtin13,
                        (SELECT p2.id FROM products p2
                         JOIN product_offers po ON po.product_id = p2.id AND po.deleted_at IS NULL
                         WHERE p2.gtin13 = ec.gtin13 AND p2.deleted_at IS NULL
                         GROUP BY p2.id
                         ORDER BY count(po.id) DESC, p2.id
                         LIMIT 1) AS canonical_id,
                        ec.product_ids
                    FROM ean_clusters ec
                )
                SELECT canonical_id, unnest(product_ids) AS product_id
                FROM canonical
                WHERE canonical_id IS NOT NULL
            """, params)

            rows = cur.fetchall()
            inserted = 0
            for canonical_id, product_id in rows:
                if canonical_id == product_id:
                    continue  # skip self-match
                cur.execute("""
                    INSERT INTO product_matches (canonical_product_id, product_id, match_type, match_confidence)
                    VALUES (%s, %s, 'ean_exact', 1.0)
                    ON CONFLICT DO NOTHING
                """, (canonical_id, product_id))
                inserted += cur.rowcount

            conn.commit()
            log.info(f"EAN matching: {inserted} new matches from {len(rows)} candidates.")
            return inserted

    finally:
        put_conn(conn)


def match_by_title_similarity(min_similarity: float = 0.6, batch_size: int = 500, vertical: str | None = None) -> int:
    """
    Find products with similar normalized titles using pg_trgm.
    Only matches products WITHOUT EAN (EAN matches are handled separately).
    Same brand required for a match.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Ensure pg_trgm threshold is set
            cur.execute(f"SET pg_trgm.similarity_threshold = {min_similarity}")

            # Find title-similar products without EAN, same brand
            vertical_filter = "AND p.vertical = %s::vertical_type" if vertical else ""
            vertical_filter2 = "AND p2.vertical = %s::vertical_type" if vertical else ""
            query_params = []
            if vertical:
                query_params.append(vertical)
            query_params.append(batch_size)
            if vertical:
                query_params.append(vertical)

            cur.execute(f"""
                WITH unmatched AS (
                    SELECT p.id, p.title_normalized, p.brand_id
                    FROM products p
                    LEFT JOIN product_matches pm ON pm.product_id = p.id
                    WHERE p.gtin13 IS NULL
                      AND p.deleted_at IS NULL
                      AND p.title_normalized IS NOT NULL
                      AND p.brand_id IS NOT NULL
                      AND pm.id IS NULL
                    {vertical_filter}
                    LIMIT %s
                )
                SELECT p1.id AS id_a, p2.id AS id_b,
                       similarity(p1.title_normalized, p2.title_normalized) AS sim
                FROM unmatched p1
                JOIN products p2 ON p2.brand_id = p1.brand_id
                    AND p2.id > p1.id
                    AND p2.gtin13 IS NULL
                    AND p2.deleted_at IS NULL
                    AND p2.title_normalized %% p1.title_normalized
                    {vertical_filter2}
                ORDER BY sim DESC
                LIMIT 10000
            """, query_params)

            pairs = cur.fetchall()
            inserted = 0
            for id_a, id_b, sim in pairs:
                # Pick canonical: lower id (first seen)
                canonical_id = min(id_a, id_b)
                other_id = max(id_a, id_b)
                cur.execute("""
                    INSERT INTO product_matches (canonical_product_id, product_id, match_type, match_confidence)
                    VALUES (%s, %s, 'title_similarity', %s)
                    ON CONFLICT DO NOTHING
                """, (canonical_id, other_id, round(sim, 4)))
                inserted += cur.rowcount

            conn.commit()
            log.info(f"Title similarity matching: {inserted} new matches from {len(pairs)} candidates (threshold={min_similarity}).")
            return inserted

    finally:
        put_conn(conn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match products across shops by EAN and title similarity")
    parser.add_argument("--strategy", choices=["ean", "title", "both"], default="both")
    parser.add_argument("--min-similarity", type=float, default=0.6)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--vertical", type=str, default=None)
    args = parser.parse_args()

    if args.strategy in ("ean", "both"):
        match_by_ean(vertical=args.vertical)

    if args.strategy in ("title", "both"):
        match_by_title_similarity(
            min_similarity=args.min_similarity,
            batch_size=args.batch_size,
            vertical=args.vertical
        )
