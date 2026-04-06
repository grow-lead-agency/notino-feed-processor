"""
Queue Worker — picks jobs from Redis, scrapes, handles failures.

Runs continuously. Picks highest-priority job, routes to correct scraper,
handles retry/failure, reports results.

Multiple workers can run in parallel (dedup prevents double-processing).
"""
import os
import sys
import time

sys.path.insert(0, "/app")
from jobs.queue import get_redis, dequeue, complete, fail, recover_stalled, get_stats

WORKER_ID = os.environ.get("WORKER_ID", f"w-{os.getpid()}")
IDLE_SLEEP = 30
MAX_IDLE_CYCLES = 120  # 1h idle → exit (cron restarts)
STALE_CHECK_INTERVAL = 300  # check stalled every 5 min


def process_job(job: dict) -> dict:
    """Route job to correct scraper, return result dict."""
    shop_id = job["shop_id"]
    domain = job["domain"]
    country_id = job["country_id"]
    feed_url = job["feed_url"]
    difficulty = job.get("difficulty", "easy")

    if difficulty in ("hard", "blocked"):
        from jobs.firecrawl_scraper import scrape_shop as fc_scrape
        saved = fc_scrape(shop_id, domain, feed_url, country_id)
    else:
        from jobs.jsonld_scraper import scrape_shop
        saved = scrape_shop(shop_id, domain, feed_url, country_id)

    return {"saved": saved, "domain": domain, "difficulty": difficulty}


def main():
    print(f"=== Worker {WORKER_ID} starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    r = get_redis()
    idle_cycles = 0
    total_jobs = 0
    total_products = 0
    last_stale_check = time.time()

    while True:
        # Periodic stalled job recovery
        if time.time() - last_stale_check > STALE_CHECK_INTERVAL:
            recovered = recover_stalled(r)
            if recovered:
                print(f"[{WORKER_ID}] Recovered {recovered} stalled jobs", flush=True)
            last_stale_check = time.time()

        # Pick job
        job = dequeue(r, WORKER_ID)

        if job is None:
            idle_cycles += 1
            if idle_cycles >= MAX_IDLE_CYCLES:
                print(f"[{WORKER_ID}] Idle {IDLE_SLEEP * MAX_IDLE_CYCLES}s, exiting", flush=True)
                break
            if idle_cycles % 10 == 1:
                stats = get_stats(r)
                print(f"[{WORKER_ID}] Queue empty, sleeping... {stats}", flush=True)
            time.sleep(IDLE_SLEEP)
            continue

        idle_cycles = 0
        domain = job.get("domain", "?")
        attempt = job.get("attempts", 1)
        max_retries = job.get("max_retries", 3)

        print(f"\n[{WORKER_ID}] Processing: {domain} ({job.get('category','?')}, "
              f"priority={job.get('priority','?')}, attempt {attempt}/{max_retries})", flush=True)

        start = time.time()
        try:
            result = process_job(job)
            elapsed = time.time() - start
            result["duration_s"] = round(elapsed, 1)

            complete(r, job, result)
            total_jobs += 1
            total_products += result.get("saved", 0)

            print(f"[{WORKER_ID}] Done: {domain} → {result.get('saved', 0)} products in {elapsed:.1f}s", flush=True)

        except Exception as e:
            elapsed = time.time() - start
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[{WORKER_ID}] FAIL: {domain} after {elapsed:.1f}s — {error_msg}", flush=True)
            fail(r, job, error_msg, retry=True)

    stats = get_stats(r)
    print(f"\n[{WORKER_ID}] Exiting. Jobs: {total_jobs}, Products: {total_products}, Queue: {stats}", flush=True)


if __name__ == "__main__":
    main()
