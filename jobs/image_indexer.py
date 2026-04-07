"""
Image Indexer — populate images + product_images from existing product URLs.

Phase 1: Index only (source_url, status='indexed', no download).
Phase 2 (TODO): Download to R2, populate cdn_url, dimensions, hash.

Reads: products.canonical_image_url, products.additional_image_urls
Writes: images, product_images

Run: python jobs/image_indexer.py [--batch-size 1000] [--vertical beauty]
"""
import argparse
import logging
import time
from db import get_conn, put_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def index_images(batch_size: int = 1000, vertical: str | None = None):
    """Index product image URLs into images + product_images tables."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Find products with images that aren't yet in product_images
            where = "WHERE p.deleted_at IS NULL AND p.canonical_image_url IS NOT NULL"
            params = []
            if vertical:
                where += " AND p.vertical = %s::vertical_type"
                params.append(vertical)

            cur.execute(f"""
                SELECT p.id, p.canonical_image_url, p.additional_image_urls, p.first_seen_shop_id
                FROM products p
                LEFT JOIN product_images pi ON pi.product_id = p.id
                {where}
                AND pi.id IS NULL
                ORDER BY p.id
                LIMIT %s
            """, params + [batch_size])

            products = cur.fetchall()
            if not products:
                log.info("No unindexed products found.")
                return 0

            log.info(f"Indexing images for {len(products)} products...")
            total_images = 0
            total_links = 0

            for product_id, canonical_url, additional_urls, shop_id in products:
                # Collect all URLs for this product
                urls = []
                if canonical_url:
                    urls.append(("main", canonical_url))
                if additional_urls:
                    for i, url in enumerate(additional_urls):
                        if url:
                            urls.append(("additional", url))

                for sort_order, (role, url) in enumerate(urls):
                    # Upsert into images (dedup by source_url)
                    cur.execute("""
                        INSERT INTO images (source_url, source_shop_id, status)
                        VALUES (%s, %s, 'indexed')
                        ON CONFLICT DO NOTHING
                        RETURNING id
                    """, (url, shop_id))
                    row = cur.fetchone()

                    if row:
                        image_id = row[0]
                        total_images += 1
                    else:
                        # Already exists, get the id
                        cur.execute("SELECT id FROM images WHERE source_url = %s", (url,))
                        row = cur.fetchone()
                        if not row:
                            continue
                        image_id = row[0]

                    # Link product ↔ image
                    cur.execute("""
                        INSERT INTO product_images (product_id, image_id, role, sort_order)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (product_id, image_id, role, sort_order))
                    total_links += 1

            conn.commit()
            log.info(f"Done: {total_images} new images, {total_links} product-image links for {len(products)} products.")
            return len(products)

    finally:
        put_conn(conn)


def run_full(batch_size: int = 1000, vertical: str | None = None):
    """Run indexer in batches until all products are processed."""
    total = 0
    while True:
        processed = index_images(batch_size=batch_size, vertical=vertical)
        total += processed
        if processed < batch_size:
            break
        log.info(f"Batch done ({total} total), continuing...")
        time.sleep(0.1)

    log.info(f"Full indexing complete: {total} products processed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index product images into images + product_images tables")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--vertical", type=str, default=None, help="Filter by vertical (beauty/furniture)")
    args = parser.parse_args()

    run_full(batch_size=args.batch_size, vertical=args.vertical)
