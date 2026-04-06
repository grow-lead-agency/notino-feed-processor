"""
Queue Worker — picks jobs from Redis, scrapes, handles failures.

Runs continuously. Picks highest-priority job, routes to correct scraper,
handles retry/failure, reports results.

Features:
  - Graceful shutdown on SIGTERM/SIGINT (finishes current job)
  - Lock renewal for long-running scrapes
  - Delayed job promotion
  - Multiple workers can run in parallel (dedup prevents double-processing)
"""
import os
import sys
import time
import signal
import threading

sys.path.insert(0, "/app")
from jobs.job_queue import (
    get_redis, dequeue, complete, fail, recover_stalled,
    get_stats, renew_lock, promote_delayed, DEFAULT_LOCK_TTL_S,
)

WORKER_ID = os.environ.get("WORKER_ID", f"w-{os.getpid()}")
IDLE_SLEEP = 30
MAX_IDLE_CYCLES = 120  # 1h idle → exit (cron restarts)
STALE_CHECK_INTERVAL = 300  # check stalled every 5 min
LOCK_RENEW_INTERVAL = DEFAULT_LOCK_TTL_S // 3  # renew lock every 1/3 of TTL

# Graceful shutdown
_shutdown_requested = False


def _handle_signal(sig, _frame):
    global _shutdown_requested
    _shutdown_requested = True
    print(f"[{WORKER_ID}] Shutdown requested (signal {sig}), finishing current job...", flush=True)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


class LockRenewer:
    """Background thread that periodically renews the lock on the active job."""

    def __init__(self, r, job_id: str, interval: int = LOCK_RENEW_INTERVAL):
        self._r = r
        self._job_id = job_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self._interval):
            renew_lock(self._r, self._job_id)


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

    while not _shutdown_requested:
        # Periodic: promote delayed jobs + recover stalled
        if time.time() - last_stale_check > STALE_CHECK_INTERVAL:
            promoted = promote_delayed(r)
            if promoted:
                print(f"[{WORKER_ID}] Promoted {promoted} delayed jobs", flush=True)

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

        # Start lock renewal for long-running scrapes
        renewer = LockRenewer(r, job["id"])
        renewer.start()

        start = time.time()
        try:
            result = process_job(job)
            elapsed = time.time() - start
            result["duration_s"] = round(elapsed, 1)

            renewer.stop()
            complete(r, job, result)
            total_jobs += 1
            total_products += result.get("saved", 0)

            print(f"[{WORKER_ID}] Done: {domain} → {result.get('saved', 0)} products in {elapsed:.1f}s", flush=True)

        except Exception as e:
            elapsed = time.time() - start
            renewer.stop()
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[{WORKER_ID}] FAIL: {domain} after {elapsed:.1f}s — {error_msg}", flush=True)
            fail(r, job, error_msg, retry=True)

    stats = get_stats(r)
    print(f"\n[{WORKER_ID}] Exiting. Jobs: {total_jobs}, Products: {total_products}, Queue: {stats}", flush=True)


if __name__ == "__main__":
    main()
