"""
Queue Worker — picks jobs from Redis queue and runs scraper.

Runs continuously (not cron). Picks highest-priority shop from queue,
scrapes it, marks done, picks next. Sleeps when queue empty.

Multiple workers can run in parallel (each picks different job).
"""
import os
import sys
import time
import json
from datetime import datetime, timezone

sys.path.insert(0, "/app")
from jobs.queue_scheduler import get_redis, dequeue_shop, mark_active, mark_completed, mark_failed, get_queue_stats, QUEUE_PREFIX
from jobs.jsonld_scraper import scrape_shop, extract_product, get_product_urls_from_sitemap, volume_ml_from_title
from jobs.db import upsert_product_with_offer

WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")
IDLE_SLEEP = 30  # seconds to sleep when queue empty
MAX_IDLE_CYCLES = 120  # exit after 1h idle (cron will restart)


def process_job(job):
    """Process a single scrape job from queue."""
    shop_id = job["shop_id"]
    domain = job["domain"]
    country_id = job["country_id"]
    feed_url = job["feed_url"]
    difficulty = job.get("difficulty", "easy")
    max_products = job.get("max_products", 50000)

    print(f"[{WORKER_ID}] Processing: {domain} ({job['category']}, priority={job['priority']})", flush=True)

    start = time.time()

    # Use appropriate scraper based on difficulty
    if difficulty in ("hard", "blocked"):
        # Firecrawl for hard shops
        try:
            from jobs.firecrawl_scraper import scrape_shop as firecrawl_scrape_shop
            saved = firecrawl_scrape_shop(shop_id, domain, feed_url, country_id)
        except ImportError:
            print(f"[{WORKER_ID}] Firecrawl scraper not available, skipping {domain}", flush=True)
            return 0
    else:
        # Standard jsonld + microdata scraper
        saved = scrape_shop(shop_id, domain, feed_url, country_id)

    elapsed = time.time() - start
    print(f"[{WORKER_ID}] Done: {domain} → {saved} products in {elapsed:.1f}s", flush=True)
    return saved


def main():
    print(f"=== Queue Worker {WORKER_ID} starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    r = get_redis()
    idle_cycles = 0
    total_jobs = 0
    total_products = 0

    while True:
        job = dequeue_shop(r)

        if job is None:
            idle_cycles += 1
            if idle_cycles >= MAX_IDLE_CYCLES:
                print(f"[{WORKER_ID}] Idle for {IDLE_SLEEP * MAX_IDLE_CYCLES}s, exiting", flush=True)
                break
            stats = get_queue_stats(r)
            if idle_cycles % 10 == 1:  # log every 5 min
                print(f"[{WORKER_ID}] Queue empty, sleeping {IDLE_SLEEP}s... (stats: {stats})", flush=True)
            time.sleep(IDLE_SLEEP)
            continue

        idle_cycles = 0
        job_id = job.get("id", f"unknown-{time.time()}")

        try:
            mark_active(r, job_id)
            saved = process_job(job)
            mark_completed(r, job_id)
            total_jobs += 1
            total_products += saved

            # Store job result
            r.hset(f"{QUEUE_PREFIX}:scrape:results", job["domain"], json.dumps({
                "saved": saved,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "worker": WORKER_ID,
            }))

        except Exception as e:
            print(f"[{WORKER_ID}] ERROR on {job.get('domain', '?')}: {e}", flush=True)
            mark_failed(r, job_id, str(e))

    print(f"[{WORKER_ID}] Exiting. Processed {total_jobs} jobs, {total_products} products.", flush=True)


if __name__ == "__main__":
    main()
