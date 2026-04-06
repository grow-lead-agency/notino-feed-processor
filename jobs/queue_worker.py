"""
Queue Worker — picks jobs from Redis, scrapes, handles failures.

Runs continuously. Picks highest-priority job, routes to correct scraper
based on scrape_type, handles retry/failure, reports results.

Features:
  - Graceful shutdown on SIGTERM/SIGINT (finishes current job)
  - Lock renewal for long-running scrapes
  - Delayed job promotion
  - Multiple workers can run in parallel (dedup prevents double-processing)
  - Multi-type routing via scrape_type (from shop_scrape_config)
  - Post-job callback updates shop_scrape_config status
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
    update_scrape_config_after_job,
)

WORKER_ID = os.environ.get("WORKER_ID", f"w-{os.getpid()}")
IDLE_SLEEP = 15          # Check queue every 15s (was 30s)
MAX_IDLE_CYCLES = 240    # 1h idle → exit (was 120 × 30s, now 240 × 15s)
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


def _check_firecrawl_budget() -> bool:
    """Check if daily Firecrawl budget is exceeded. Returns True if OK to proceed."""
    from jobs.db import get_conn, put_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT firecrawl_daily_budget_usd, firecrawl_daily_spent_usd,
                       firecrawl_daily_spent_reset_at
                FROM pipeline_config WHERE id = 1
            """)
            row = cur.fetchone()
            if not row:
                return True
            budget, spent, reset_at = row
            # Reset daily counter if new day
            from datetime import datetime, timezone
            if reset_at.date() < datetime.now(timezone.utc).date():
                cur.execute("""
                    UPDATE pipeline_config
                    SET firecrawl_daily_spent_usd = 0, firecrawl_daily_spent_reset_at = now()
                    WHERE id = 1
                """)
                conn.commit()
                spent = 0
            return spent < budget
    except Exception as e:
        print(f"[budget-check] Error: {e}, allowing job", flush=True)
        return True
    finally:
        put_conn(conn)


def _record_firecrawl_spend(credits: int):
    """Add credits to daily Firecrawl spend counter."""
    from jobs.db import get_conn, put_conn
    cost = credits * 0.01
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pipeline_config
                SET firecrawl_daily_spent_usd = firecrawl_daily_spent_usd + %s
                WHERE id = 1
            """, (cost,))
        conn.commit()
    except Exception:
        pass
    finally:
        put_conn(conn)


def process_job(job: dict) -> dict:
    """Route job to correct scraper based on scrape_type, return result dict.

    Firecrawl jobs check daily budget before proceeding.
    """
    shop_id = job["shop_id"]
    domain = job["domain"]
    country_id = job["country_id"]
    feed_url = job["feed_url"]
    difficulty = job.get("difficulty", "easy")
    scrape_type = job.get("scrape_type", "catalog_sync")
    scraper_engine = job.get("scraper_engine", "jsonld")

    # Budget gate for Firecrawl jobs
    if scraper_engine == "firecrawl" or difficulty in ("hard", "blocked"):
        if not _check_firecrawl_budget():
            print(f"[{WORKER_ID}] SKIP {domain} [{scrape_type}] — daily Firecrawl budget exceeded", flush=True)
            return {"saved": 0, "domain": domain, "scrape_type": scrape_type, "skipped": "budget"}

    if scrape_type == "catalog_sync":
        saved = _run_catalog_sync(shop_id, domain, feed_url, country_id, difficulty, scraper_engine)
        return {"saved": saved, "domain": domain, "scrape_type": scrape_type}

    elif scrape_type == "price_check":
        # Hard/blocked shops with Firecrawl engine → smart price checker (top-N, 96% cheaper)
        if scraper_engine == "firecrawl" or difficulty in ("hard", "blocked"):
            from jobs.smart_price_checker import price_check_top_n
            top_n = job.get("top_n", 20)
            results = price_check_top_n(shop_id, domain, country_id, n=top_n)
            checked = len([r for r in results if r.get("new_price") is not None])
            changed = sum(1 for r in results if r.get("changed"))
            return {"saved": checked, "changed": changed, "domain": domain,
                    "scrape_type": scrape_type}
        # Easy/medium shops → full catalog sync (cheap enough with plain HTTP)
        saved = _run_catalog_sync(shop_id, domain, feed_url, country_id, difficulty, scraper_engine)
        return {"saved": saved, "domain": domain, "scrape_type": scrape_type}

    elif scrape_type == "category_tree":
        from jobs.category_tree_scraper import scrape_category_tree
        shop_url = job.get("shop_url") or feed_url  # shop_url preferred, feed_url as fallback
        count = scrape_category_tree(shop_id, domain, shop_url)
        return {"saved": count, "domain": domain, "scrape_type": scrape_type}

    elif scrape_type == "voucher_scan":
        from jobs.voucher_scraper import scrape_vouchers
        shop_url = job.get("shop_url") or feed_url
        count = scrape_vouchers(shop_id, domain, shop_url, country_id)
        return {"saved": count, "domain": domain, "scrape_type": scrape_type}

    elif scrape_type == "shop_profile":
        from jobs.shop_profile_scraper import scrape_shop_profile
        shop_url = job.get("shop_url") or feed_url
        config_override = job.get("config_override")
        count = scrape_shop_profile(shop_id, domain, shop_url, country_id, config_override)
        return {"saved": count, "domain": domain, "scrape_type": scrape_type}

    elif scrape_type == "review_harvest":
        from jobs.review_harvester import run_review_harvest
        return run_review_harvest(job)

    elif scrape_type == "ingredient_extract":
        from jobs.ingredient_extractor import run_from_queue
        return run_from_queue(job)

    elif scrape_type in (
        "product_detail", "shipping_intel",
        "availability_pulse",
    ):
        raise NotImplementedError(
            f"Scrape type '{scrape_type}' is not yet implemented. "
            f"Shop: {domain}, engine: {scraper_engine}"
        )

    else:
        raise ValueError(f"Unknown scrape_type '{scrape_type}' for shop {domain}")


def _run_catalog_sync(
    shop_id: int, domain: str, feed_url: str, country_id: int,
    difficulty: str, scraper_engine: str,
) -> int:
    """Run catalog/price scraper. Returns number of saved products."""
    if scraper_engine == "firecrawl" or difficulty in ("hard", "blocked"):
        from jobs.firecrawl_scraper import scrape_shop as fc_scrape
        return fc_scrape(shop_id, domain, feed_url, country_id)
    else:
        from jobs.jsonld_scraper import scrape_shop
        return scrape_shop(shop_id, domain, feed_url, country_id)


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
        scrape_type = job.get("scrape_type", "catalog_sync")
        scrape_config_id = job.get("scrape_config_id")
        attempt = job.get("attempts", 1)
        max_retries = job.get("max_retries", 3)

        print(f"\n[{WORKER_ID}] Processing: {domain} [{scrape_type}] ({job.get('category','?')}, "
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

            # Update shop_scrape_config status
            if scrape_config_id:
                try:
                    update_scrape_config_after_job(scrape_config_id, success=True)
                except Exception as cb_err:
                    print(f"[{WORKER_ID}] WARN: Failed to update scrape_config {scrape_config_id}: {cb_err}", flush=True)

            print(f"[{WORKER_ID}] Done: {domain} [{scrape_type}] → {result.get('saved', 0)} products in {elapsed:.1f}s", flush=True)

        except Exception as e:
            elapsed = time.time() - start
            renewer.stop()
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[{WORKER_ID}] FAIL: {domain} [{scrape_type}] after {elapsed:.1f}s — {error_msg}", flush=True)

            # NotImplementedError = no point retrying
            should_retry = not isinstance(e, NotImplementedError)
            fail(r, job, error_msg, retry=should_retry)

            # Update shop_scrape_config status
            if scrape_config_id:
                try:
                    update_scrape_config_after_job(scrape_config_id, success=False, error=error_msg)
                except Exception as cb_err:
                    print(f"[{WORKER_ID}] WARN: Failed to update scrape_config {scrape_config_id}: {cb_err}", flush=True)

    stats = get_stats(r)
    print(f"\n[{WORKER_ID}] Exiting. Jobs: {total_jobs}, Products: {total_products}, Queue: {stats}", flush=True)


if __name__ == "__main__":
    main()
