"""
Redis Queue Engine — BullMQ-like features in Python.

Features:
  - Priority queue (sorted set, lower score = higher priority)
  - Job states: waiting → active → completed/failed/stalled
  - Retry with exponential backoff (delayed queue, no blocking sleep)
  - Stalled job detection (lock-based, configurable TTL)
  - Job deduplication (atomic Lua script — no race conditions)
  - Per-shop result history
  - Queue stats (counts per state)
  - Dead letter queue (exceeded max retries)
  - Multi-type scheduling from shop_scrape_config DB table

Redis key layout:
  {prefix}:waiting          — sorted set (score=priority)
  {prefix}:delayed          — sorted set (score=retry_after timestamp)
  {prefix}:active           — hash (job_id → lock_expires_at)
  {prefix}:completed        — list (last N completed job summaries)
  {prefix}:failed           — list (last N failed job summaries)
  {prefix}:dead             — list (exceeded max retries)
  {prefix}:job:{id}         — hash (full job data)
  {prefix}:dedup            — set (domain keys for dedup)
  {prefix}:stats            — hash (counters)
  {prefix}:results:{domain} — hash (last result per shop)
"""
import os
import sys
import json
import time
import uuid
from datetime import datetime, timezone, timedelta

# Allow `python /app/jobs/job_queue.py schedule` (cron entry point) to import
# sibling modules via `from jobs.db import ...` — same pattern as other jobs.
sys.path.insert(0, "/app")

import redis


REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PREFIX = os.environ.get("QUEUE_PREFIX", "notino:scrape")

# Defaults
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_MS = 5000
DEFAULT_LOCK_TTL_S = 3600  # 60 min — safe for large shop scrapes (50K products)
DEFAULT_STALE_CHECK_S = 60
MAX_COMPLETED_KEEP = 500
MAX_FAILED_KEEP = 200
MAX_DEAD_KEEP = 1000  # increased from 100 — need visibility during batch failures

# Lua script for atomic dedup + enqueue
LUA_DEDUP_ENQUEUE = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then
    return 0
end
redis.call('SADD', KEYS[1], ARGV[1])
return 1
"""

# Redis singleton
_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


# ============================================================
# JOB CREATION
# ============================================================

def create_job(
    *,
    shop_id: int,
    domain: str,
    category: str = "",
    country_id: int = 0,
    feed_url: str = "",
    difficulty: str = "easy",
    max_products: int = 50000,
    priority: int = 5,
    max_retries: int = DEFAULT_MAX_RETRIES,
    scrape_type: str = "catalog_sync",
    scraper_engine: str = "jsonld",
    config_override: dict | None = None,
    scrape_config_id: int | None = None,
) -> dict:
    """Create a job dict (not yet enqueued).

    Args:
        scrape_type: Type of scrape — 'catalog_sync', 'price_check', etc.
        scraper_engine: Which engine to use — 'jsonld', 'firecrawl', etc.
        config_override: Per-shop config overrides from shop_scrape_config.
        scrape_config_id: ID from shop_scrape_config for post-job status update.
    """
    return {
        "id": f"scrape:{domain}:{scrape_type}:{uuid.uuid4().hex[:8]}",
        "shop_id": shop_id,
        "domain": domain,
        "category": category,
        "country_id": country_id,
        "feed_url": feed_url,
        "difficulty": difficulty,
        "max_products": max_products,
        "priority": priority,
        "max_retries": max_retries,
        "attempts": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "state": "waiting",
        "scrape_type": scrape_type,
        "scraper_engine": scraper_engine,
        "config_override": config_override or {},
        "scrape_config_id": scrape_config_id,
    }


# ============================================================
# ENQUEUE
# ============================================================

def enqueue(r, job: dict) -> bool:
    """
    Add job to waiting queue.
    Returns False if duplicate (same domain+scrape_type already in waiting/active).
    Uses Lua script for atomic dedup check.
    """
    domain = job["domain"]
    scrape_type = job.get("scrape_type", "catalog_sync")
    dedup_value = f"{domain}:{scrape_type}"
    dedup_key = f"{PREFIX}:dedup"

    # Atomic dedup check + add
    added = r.eval(LUA_DEDUP_ENQUEUE, 1, dedup_key, dedup_value)
    if not added:
        return False

    job_id = job["id"]
    job_key = f"{PREFIX}:job:{job_id}"

    pipe = r.pipeline()
    pipe.hset(job_key, mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in job.items()})
    pipe.expire(job_key, 86400)  # TTL 24h
    pipe.zadd(f"{PREFIX}:waiting", {job_id: job["priority"]})
    pipe.hincrby(f"{PREFIX}:stats", "total_enqueued", 1)
    pipe.execute()

    return True


def enqueue_bulk(r, jobs: list[dict]) -> int:
    """Enqueue multiple jobs. Returns count enqueued (skips duplicates)."""
    enqueued = 0
    for job in jobs:
        if enqueue(r, job):
            enqueued += 1
    return enqueued


def enqueue_delayed(r, job: dict, retry_after_ts: float):
    """Add job to delayed queue (sorted by retry_after timestamp)."""
    job_id = job["id"]
    job_key = f"{PREFIX}:job:{job_id}"

    pipe = r.pipeline()
    pipe.hset(job_key, mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in job.items()})
    pipe.expire(job_key, 86400)
    pipe.zadd(f"{PREFIX}:delayed", {job_id: retry_after_ts})
    pipe.execute()


def promote_delayed(r) -> int:
    """Move delayed jobs that are ready to the waiting queue. Returns count promoted."""
    now = time.time()
    # Get all delayed jobs with score <= now (ready to run)
    ready = r.zrangebyscore(f"{PREFIX}:delayed", "-inf", str(now))
    if not ready:
        return 0

    promoted = 0
    for job_id in ready:
        job_key = f"{PREFIX}:job:{job_id}"
        raw = r.hgetall(job_key)
        if not raw:
            r.zrem(f"{PREFIX}:delayed", job_id)
            continue

        priority = int(raw.get("priority", "5"))

        pipe = r.pipeline()
        pipe.zrem(f"{PREFIX}:delayed", job_id)
        pipe.zadd(f"{PREFIX}:waiting", {job_id: priority})
        pipe.hset(job_key, "state", "waiting")
        pipe.execute()
        promoted += 1

    return promoted


# ============================================================
# DEQUEUE (Worker picks job)
# ============================================================

def dequeue(r, worker_id: str = "worker") -> dict | None:
    """
    Pop highest-priority job from waiting queue.
    Moves to active with lock.
    Returns job dict or None.
    """
    # ZPOPMIN = lowest score = highest priority
    result = r.zpopmin(f"{PREFIX}:waiting", count=1)
    if not result:
        return None

    job_id, score = result[0]
    job_key = f"{PREFIX}:job:{job_id}"

    # Load job data
    raw = r.hgetall(job_key)
    if not raw:
        return None

    # Parse job
    job = {}
    for k, v in raw.items():
        try:
            job[k] = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            job[k] = v

    # Ensure numeric types
    for int_field in ("shop_id", "country_id", "max_products", "priority", "max_retries", "attempts", "scrape_config_id"):
        if int_field in job and job[int_field] is not None:
            try:
                job[int_field] = int(job[int_field])
            except (ValueError, TypeError):
                pass

    # Set active with lock
    lock_expires = time.time() + DEFAULT_LOCK_TTL_S
    pipe = r.pipeline()
    pipe.hset(f"{PREFIX}:active", job_id, str(lock_expires))
    pipe.hset(job_key, "state", "active")
    pipe.hset(job_key, "started_at", datetime.now(timezone.utc).isoformat())
    pipe.hset(job_key, "worker_id", worker_id)
    pipe.hset(job_key, "attempts", str(job.get("attempts", 0) + 1))
    pipe.execute()

    job["state"] = "active"
    job["attempts"] = job.get("attempts", 0) + 1

    return job


# ============================================================
# LOCK RENEWAL
# ============================================================

def renew_lock(r, job_id: str, ttl: int = DEFAULT_LOCK_TTL_S):
    """Extend the lock on an active job. Call periodically for long-running scrapes."""
    r.hset(f"{PREFIX}:active", job_id, str(time.time() + ttl))


# ============================================================
# COMPLETE / FAIL
# ============================================================

def complete(r, job: dict, result: dict = None):
    """Mark job as completed."""
    job_id = job["id"]
    domain = job.get("domain", "?")
    scrape_type = job.get("scrape_type", "catalog_sync")
    dedup_value = f"{domain}:{scrape_type}"

    pipe = r.pipeline()
    pipe.hdel(f"{PREFIX}:active", job_id)
    pipe.srem(f"{PREFIX}:dedup", dedup_value)
    pipe.hset(f"{PREFIX}:job:{job_id}", "state", "completed")
    pipe.hset(f"{PREFIX}:job:{job_id}", "completed_at", datetime.now(timezone.utc).isoformat())
    if result:
        pipe.hset(f"{PREFIX}:job:{job_id}", "result", json.dumps(result))

    # Stats
    pipe.hincrby(f"{PREFIX}:stats", "total_completed", 1)

    # Completed log (keep last N)
    summary = json.dumps({"id": job_id, "domain": domain, "result": result, "at": datetime.now(timezone.utc).isoformat()})
    pipe.lpush(f"{PREFIX}:completed", summary)
    pipe.ltrim(f"{PREFIX}:completed", 0, MAX_COMPLETED_KEEP - 1)

    # Per-shop result
    pipe.hset(f"{PREFIX}:results", domain, json.dumps({"saved": result.get("saved", 0) if result else 0, "at": datetime.now(timezone.utc).isoformat()}))

    pipe.execute()


def fail(r, job: dict, error: str, retry: bool = True):
    """
    Mark job as failed.
    If retry=True and attempts < max_retries, schedule delayed re-enqueue.
    Otherwise, move to dead letter queue.
    No blocking sleep — uses delayed queue for backoff.
    """
    job_id = job["id"]
    domain = job.get("domain", "?")
    scrape_type = job.get("scrape_type", "catalog_sync")
    dedup_value = f"{domain}:{scrape_type}"
    attempts = job.get("attempts", 1)
    max_retries = job.get("max_retries", DEFAULT_MAX_RETRIES)

    if retry and attempts < max_retries:
        # Exponential backoff with jitter (non-blocking)
        import random
        delay_ms = DEFAULT_BACKOFF_BASE_MS * (2 ** (attempts - 1))
        jitter = random.uniform(0.5, 1.5)
        delay_s = (delay_ms * jitter) / 1000
        retry_after_ts = time.time() + delay_s

        # Atomic: remove from active + dedup, update stats
        pipe = r.pipeline()
        pipe.hdel(f"{PREFIX}:active", job_id)
        pipe.srem(f"{PREFIX}:dedup", dedup_value)
        pipe.hincrby(f"{PREFIX}:stats", "total_failed", 1)
        pipe.execute()

        # Create retry job and schedule it
        retry_job = job.copy()
        retry_job["id"] = f"scrape:{domain}:{scrape_type}:{uuid.uuid4().hex[:8]}"
        retry_job["state"] = "delayed"
        retry_job["last_error"] = error
        retry_job["retry_after"] = datetime.fromtimestamp(retry_after_ts, tz=timezone.utc).isoformat()

        enqueue_delayed(r, retry_job, retry_after_ts)

        print(f"  [RETRY] {domain} attempt {attempts}/{max_retries}, delayed {delay_s:.1f}s: {error}", flush=True)
    else:
        # Dead letter queue — atomic pipeline
        pipe = r.pipeline()
        pipe.hdel(f"{PREFIX}:active", job_id)
        pipe.srem(f"{PREFIX}:dedup", dedup_value)
        pipe.hset(f"{PREFIX}:job:{job_id}", "state", "dead")
        pipe.hset(f"{PREFIX}:job:{job_id}", "error", error)
        pipe.hincrby(f"{PREFIX}:stats", "total_failed", 1)
        pipe.hincrby(f"{PREFIX}:stats", "total_dead", 1)

        summary = json.dumps({"id": job_id, "domain": domain, "error": error, "attempts": attempts, "at": datetime.now(timezone.utc).isoformat()})
        pipe.lpush(f"{PREFIX}:dead", summary)
        pipe.ltrim(f"{PREFIX}:dead", 0, MAX_DEAD_KEEP - 1)
        pipe.execute()

        print(f"  [DEAD] {domain} after {attempts} attempts: {error}", flush=True)


# ============================================================
# STALLED JOB DETECTION
# ============================================================

def recover_stalled(r) -> int:
    """
    Check active jobs for expired locks.
    Re-enqueue stalled jobs. Atomic pipeline per job.
    Returns count of recovered jobs.
    """
    active = r.hgetall(f"{PREFIX}:active")
    now = time.time()
    recovered = 0

    for job_id, lock_expires_str in active.items():
        try:
            lock_expires = float(lock_expires_str)
        except ValueError:
            continue

        if now > lock_expires:
            # Job is stalled — worker probably crashed
            job_key = f"{PREFIX}:job:{job_id}"
            raw = r.hgetall(job_key)
            if not raw:
                r.hdel(f"{PREFIX}:active", job_id)
                continue

            domain = raw.get("domain", "?")
            scrape_type_raw = raw.get("scrape_type", "catalog_sync")
            dedup_value = f"{domain}:{scrape_type_raw}"
            attempts = int(raw.get("attempts", "1"))
            max_retries = int(raw.get("max_retries", str(DEFAULT_MAX_RETRIES)))

            print(f"  [STALLED] {domain} [{scrape_type_raw}] (job {job_id}) — lock expired, recovering", flush=True)

            # Atomic: remove from active + dedup + update stats
            pipe = r.pipeline()
            pipe.hdel(f"{PREFIX}:active", job_id)
            pipe.srem(f"{PREFIX}:dedup", dedup_value)
            pipe.hincrby(f"{PREFIX}:stats", "total_stalled", 1)
            pipe.execute()

            if attempts < max_retries:
                # Re-enqueue
                job = {}
                for k, v in raw.items():
                    try:
                        job[k] = json.loads(v)
                    except Exception:
                        job[k] = v
                job["id"] = f"scrape:{domain}:{scrape_type_raw}:{uuid.uuid4().hex[:8]}"
                job["state"] = "waiting"
                job["last_error"] = "stalled"
                for int_field in ("shop_id", "country_id", "max_products", "priority", "max_retries", "attempts", "scrape_config_id"):
                    if int_field in job:
                        try:
                            job[int_field] = int(job[int_field])
                        except Exception:
                            pass
                enqueue(r, job)
                recovered += 1

    return recovered


# ============================================================
# QUEUE STATS
# ============================================================

def get_stats(r) -> dict:
    """Get queue statistics."""
    stats = r.hgetall(f"{PREFIX}:stats") or {}
    return {
        "waiting": r.zcard(f"{PREFIX}:waiting"),
        "delayed": r.zcard(f"{PREFIX}:delayed"),
        "active": r.hlen(f"{PREFIX}:active"),
        "completed_total": int(stats.get("total_completed", 0)),
        "failed_total": int(stats.get("total_failed", 0)),
        "dead_total": int(stats.get("total_dead", 0)),
        "stalled_total": int(stats.get("total_stalled", 0)),
        "enqueued_total": int(stats.get("total_enqueued", 0)),
    }


def get_waiting_jobs(r, count: int = 10) -> list[str]:
    """Get top N waiting job IDs (highest priority first)."""
    return r.zrange(f"{PREFIX}:waiting", 0, count - 1)


def get_dead_letter(r, count: int = 10) -> list[dict]:
    """Get last N dead letter entries."""
    raw = r.lrange(f"{PREFIX}:dead", 0, count - 1)
    return [json.loads(x) for x in raw]


def flush_queue(r):
    """Clear all queue data. DANGEROUS."""
    keys = r.keys(f"{PREFIX}:*")
    if keys:
        r.delete(*keys)


# ============================================================
# ENGINE CONCURRENCY HELPERS
# ============================================================

def _count_firecrawl_jobs(r) -> int:
    """Count Firecrawl jobs currently in waiting + active queues."""
    count = 0
    # Check waiting queue
    waiting_ids = r.zrange(f"{PREFIX}:waiting", 0, -1)
    for job_id in waiting_ids:
        job_data = r.hgetall(f"{PREFIX}:job:{job_id}")
        if job_data and job_data.get("scraper_engine") == "firecrawl":
            count += 1
    # Check active queue
    active_ids = r.hkeys(f"{PREFIX}:active")
    for job_id in active_ids:
        job_data = r.hgetall(f"{PREFIX}:job:{job_id}")
        if job_data and job_data.get("scraper_engine") == "firecrawl":
            count += 1
    return count


# ============================================================
# SCHEDULER (enqueue due jobs from shop_scrape_config)
# ============================================================

# DEPRECATED: Legacy tier config. Kept for backward compatibility with
# schedule_due_shops(). New code should use shop_scrape_config DB table
# via schedule_due_jobs().
TIER_CONFIG = {
    "class-1": {"interval_hours": 24, "priority": 1, "max_products": 50000},
    "class-2": {"interval_hours": 48, "priority": 5, "max_products": 50000},
    "class-3": {"interval_hours": 168, "priority": 10, "max_products": 50000},
}


def update_scrape_config_after_job(scrape_config_id: int, success: bool, error: str | None = None):
    """Update shop_scrape_config after job completion.

    Called by queue_worker after each job finishes. Updates:
      - last_run_at (always)
      - last_success_at + reset consecutive_failures (on success)
      - last_error + increment consecutive_failures (on failure)
    """
    from jobs.db import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if success:
                cur.execute("""
                    UPDATE shop_scrape_config
                    SET last_run_at = now(),
                        last_success_at = now(),
                        last_error = NULL,
                        consecutive_failures = 0,
                        updated_at = now()
                    WHERE id = %s
                """, (scrape_config_id,))
            else:
                cur.execute("""
                    UPDATE shop_scrape_config
                    SET last_run_at = now(),
                        last_error = %s,
                        consecutive_failures = consecutive_failures + 1,
                        updated_at = now()
                    WHERE id = %s
                """, (error, scrape_config_id))
        conn.commit()
    finally:
        put_conn(conn)


def schedule_due_jobs(r) -> int:
    """Check shop_scrape_config for due jobs across ALL scrape types.

    Reads enabled configs from DB where:
      - last_success_at is NULL (never ran), OR
      - last_success_at is older than interval_hours
      - consecutive_failures < 5 (circuit breaker)
      - shop is not soft-deleted

    Returns count of newly enqueued jobs.
    """
    from jobs.db import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ssc.id, ssc.shop_id, s.name, s.url, s.category, s.country_id,
                       ssc.scrape_type, ssc.priority, ssc.scraper_engine, ssc.max_items,
                       ssc.config_override, ssc.last_success_at, ssc.interval_hours,
                       wc.sitemap_product_feed, wc.difficulty
                FROM shop_scrape_config ssc
                JOIN shops s ON s.id = ssc.shop_id
                LEFT JOIN website_checks wc ON wc.shop_id = s.id
                WHERE ssc.enabled = true
                  AND s.deleted_at IS NULL
                  AND (ssc.last_success_at IS NULL
                       OR ssc.last_success_at < now() - (ssc.interval_hours || ' hours')::interval)
                  AND ssc.consecutive_failures < 5
                ORDER BY ssc.priority ASC, ssc.last_success_at ASC NULLS FIRST
            """)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    # Engine-aware concurrency: limit how many Firecrawl jobs can be enqueued
    # to prevent 429 rate limiting and budget blowout.
    MAX_FIRECRAWL_CONCURRENT = 3  # max Firecrawl jobs in waiting+active at once
    firecrawl_in_queue = _count_firecrawl_jobs(r)

    enqueued = 0
    firecrawl_enqueued = 0
    firecrawl_skipped = 0

    for row in rows:
        (config_id, shop_id, shop_name, shop_url, category, country_id,
         scrape_type, priority, scraper_engine, max_items,
         config_override, last_success_at, interval_hours,
         feed_url, difficulty) = row

        # Firecrawl concurrency gate
        if scraper_engine == "firecrawl":
            if firecrawl_in_queue + firecrawl_enqueued >= MAX_FIRECRAWL_CONCURRENT:
                firecrawl_skipped += 1
                continue  # skip — will be picked up next scheduler run

        # Determine feed_url: from config_override first, then website_checks
        effective_feed_url = ""
        if config_override and config_override.get("feed_url"):
            effective_feed_url = config_override["feed_url"]
        elif feed_url:
            effective_feed_url = feed_url.replace("&amp;", "&")

        # Determine difficulty: from config_override or website_checks
        effective_difficulty = difficulty or "easy"
        if config_override and config_override.get("difficulty"):
            effective_difficulty = config_override["difficulty"]

        job = create_job(
            shop_id=shop_id,
            domain=shop_name,
            category=category or "",
            country_id=country_id or 0,
            feed_url=effective_feed_url,
            difficulty=effective_difficulty,
            max_products=max_items or 50000,
            priority=priority,
            scrape_type=scrape_type,
            scraper_engine=scraper_engine,
            config_override=config_override or {},
            scrape_config_id=config_id,
        )

        if enqueue(r, job):
            enqueued += 1
            if scraper_engine == "firecrawl":
                firecrawl_enqueued += 1
            print(f"  [ENQUEUE] {shop_name} [{scrape_type}] priority={priority} engine={scraper_engine}", flush=True)

    if firecrawl_skipped:
        print(f"  [THROTTLE] Skipped {firecrawl_skipped} firecrawl jobs (max {MAX_FIRECRAWL_CONCURRENT} concurrent, {firecrawl_in_queue} already queued)", flush=True)

    return enqueued


def schedule_due_shops(r) -> int:
    """DEPRECATED: Use schedule_due_jobs() instead.

    Kept for backward compatibility. Internally delegates to schedule_due_jobs()
    which reads from shop_scrape_config DB table.

    Falls back to legacy TIER_CONFIG logic if shop_scrape_config table
    does not exist (pre-migration 015).
    """
    try:
        return schedule_due_jobs(r)
    except Exception as e:
        # Fallback: shop_scrape_config table might not exist yet (pre-migration 015)
        error_msg = str(e)
        if "shop_scrape_config" in error_msg or "scrape_type" in error_msg:
            print("  [WARN] shop_scrape_config not found, falling back to legacy TIER_CONFIG", flush=True)
            return _schedule_due_shops_legacy(r)
        raise


def _schedule_due_shops_legacy(r) -> int:
    """Legacy scheduler using hardcoded TIER_CONFIG. Only used as fallback."""
    from jobs.db import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.name, s.category, s.country_id,
                       wc.sitemap_product_feed, wc.difficulty,
                       (SELECT max(last_seen_at) FROM product_offers WHERE shop_id = s.id) as last_scraped
                FROM website_checks wc
                JOIN shops s ON s.id = wc.shop_id
                WHERE wc.sitemap_product_feed IS NOT NULL
                  AND wc.difficulty NOT IN ('dead', 'unreachable')
                  AND s.country_id IS NOT NULL
                  AND s.deleted_at IS NULL
                ORDER BY s.category, s.name
            """)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    now = datetime.now(timezone.utc)
    enqueued = 0

    for shop_id, domain, category, country_id, feed_url, difficulty, last_scraped in rows:
        tier = TIER_CONFIG.get(category, TIER_CONFIG["class-3"])

        # Check if due
        if last_scraped is not None:
            if last_scraped.tzinfo is None:
                last_scraped = last_scraped.replace(tzinfo=timezone.utc)
            if (now - last_scraped) < timedelta(hours=tier["interval_hours"]):
                continue

        job = create_job(
            shop_id=shop_id,
            domain=domain,
            category=category,
            country_id=country_id,
            feed_url=feed_url.replace("&amp;", "&"),
            difficulty=difficulty,
            max_products=tier["max_products"],
            priority=tier["priority"],
        )

        if enqueue(r, job):
            enqueued += 1

    return enqueued


# ============================================================
# CLI
# ============================================================

def main():
    """CLI: schedule, stats, recover, flush."""
    import sys
    r = get_redis()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "schedule"

    if cmd == "schedule":
        print(f"=== Queue Scheduler at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
        stats_before = get_stats(r)
        print(f"Before: {stats_before}", flush=True)

        # Promote delayed jobs first
        promoted = promote_delayed(r)
        if promoted:
            print(f"Promoted {promoted} delayed jobs to waiting", flush=True)

        # Recover stalled
        recovered = recover_stalled(r)
        if recovered:
            print(f"Recovered {recovered} stalled jobs", flush=True)

        # schedule_due_shops() delegates to schedule_due_jobs() internally,
        # with fallback to legacy TIER_CONFIG if shop_scrape_config not yet created
        enqueued = schedule_due_shops(r)

        stats_after = get_stats(r)
        print(f"Enqueued: {enqueued}, After: {stats_after}", flush=True)

        # DLQ alerting
        dead_count = stats_after["dead_total"]
        dead_before = stats_before["dead_total"]
        if dead_count > dead_before:
            print(f"  [WARN] DLQ grew by {dead_count - dead_before} entries! Check dead letter queue.", flush=True)

    elif cmd == "stats":
        stats = get_stats(r)
        print(json.dumps(stats, indent=2))

    elif cmd == "waiting":
        jobs = get_waiting_jobs(r, 20)
        for j in jobs:
            print(f"  {j}")

    elif cmd == "dead":
        dead = get_dead_letter(r, 20)
        for d in dead:
            print(f"  {d['domain']}: {d.get('error','?')} ({d.get('attempts','?')} attempts)")

    elif cmd == "recover":
        recovered = recover_stalled(r)
        print(f"Recovered: {recovered}")

    elif cmd == "flush":
        flush_queue(r)
        print("Queue flushed")

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: job_queue.py [schedule|stats|waiting|dead|recover|flush]")


if __name__ == "__main__":
    main()
